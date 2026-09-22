# -*- coding: utf-8 -*-
"""Журнал изменений: кто, когда и что поменял в базе.

Пишем на уровне приложения, а не триггерами SQLite: триггер не знает, какой
человек вошёл, а именно это и нужно, когда разбираешь чужую ошибку.

Одна правка — одна строка. Правку можно вернуть назад: отмена ставит прежнее
значение и сама попадает в журнал. Возвращается именно одна правка, а не
«состояние на вчера», — иначе откат затрёт то, что после ошибки поправили уже
верно.

Автор SYSTEM — это сбор и перепроверка. Такие строки пишутся только по важным
полям и в ленте по умолчанию скрыты: полный прогон трогает три сотни объектов,
и без этого действия людей в ленте утонут.
"""

import time

import activity
import db

SYSTEM = "@system"          # автор-автомат: сбор, перепроверка
SYSTEM_LABEL = "сбор"

# Что пишем от имени сбора. Весь остальной машинный шум (код ответа, скорость
# загрузки, дата проверки) в журнале не нужен — он меняется каждый прогон.
WATCHED = ("name", "website", "phone", "site_status", "reason_code")

ACTIONS = {
    "create":   "Объект заведён",
    "edit":     "Правка поля",
    "status":   "Статус",
    "call":     "звонок",
    "priority": "Приоритет",
    "hide":     "Скрыт",
    "show":     "Возвращён в работу",
    "note":     "Заметка",
    "note_del": "удалил заметку",
    "deal":     "сделка",
    "payment":  "оплата",
    "settings": "настройки",
    "website":  "Адрес сайта",
    "research": "Результат автопроверки",
    "unlock":   "Снята защита правок",
    "recheck":  "Перепроверка сайта",
    "import":   "Импорт CSV",
    "run":      "Запуск сбора",
    "revert":   "Отмена правки",
    "niches":   "Ниши и категории",
    "backup":   "Резервная копия",
}

# Отменить можно только то, что меняло одно поле и знает прежнее значение.
# Заметки в список не входят: они живут лентой в lead_notes, а leads.note —
# только зеркало последней. Откат зеркала ленту не изменит и разведёт их.
UNDOABLE = {"edit", "status", "priority", "hide", "show", "website",
            "research", "revert"}

# Денежные действия. В карточке их видит только владелец базы: суммы сделок
# и поступлений не та информация, которую продажник должен читать по своему же
# лиду — по ним считается его собственное вознаграждение.
MONEY_ACTIONS = {"deal", "payment"}

# Служебные колонки: их журнал не восстанавливает, даже если запись о них есть.
NEVER_WRITE = {"id", "source", "source_ref", "created_at", "updated_at"}


# ── запись ───────────────────────────────────────────────────────────────────

def _text(value):
    """Значение в текст: журнал хранит всё строкой, None — пустой строкой."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def log(login, action, lead=None, field="", old="", new=""):
    """Одна строка журнала. lead — строка таблицы, словарь или id."""
    lead_id, lead_name = None, ""
    if isinstance(lead, int):
        lead_id = lead
        row = db.conn().execute("SELECT name FROM leads WHERE id=?", (lead,)).fetchone()
        lead_name = row["name"] if row else ""
    elif lead is not None:
        lead_id = lead["id"]
        lead_name = lead["name"] or ""

    db.conn().execute(
        "INSERT INTO history (ts, login, lead_id, lead_name, action, field, "
        "old_value, new_value) VALUES (?,?,?,?,?,?,?,?)",
        (int(time.time()), login or "", lead_id, lead_name, action, field,
         _text(old), _text(new)),
    )
    db.conn().commit()


def log_changes(login, before, after, action="edit", only=None):
    """Пишет по строке на каждое реально изменившееся поле.

    before — строка лида до правки, after — словарь новых значений.
    only ограничивает набор полей (для сбора — WATCHED).
    """
    if not before:
        return 0
    written = 0
    for field, value in (after or {}).items():
        if only is not None and field not in only:
            continue
        if field in NEVER_WRITE:
            continue
        try:
            old = before[field]
        except (IndexError, KeyError):
            continue
        if _text(old) == _text(value):
            continue
        log(login, action, lead=before, field=field, old=old, new=value)
        written += 1
    return written


# ── чтение ───────────────────────────────────────────────────────────────────

def _columns():
    return {r["name"]: (r["type"] or "").upper()
            for r in db.conn().execute("PRAGMA table_info(leads)")}


def _row(r):
    when = activity.local(r["ts"])
    return {
        "id": r["id"],
        "ts": r["ts"],
        "when": when.strftime("%d.%m.%Y %H:%M"),
        "day": when.date().isoformat(),
        "login": SYSTEM_LABEL if r["login"] == SYSTEM else r["login"],
        "by_system": r["login"] == SYSTEM,
        "lead_id": r["lead_id"],
        "lead_name": r["lead_name"],
        "action": r["action"],
        "action_text": ACTIONS.get(r["action"], r["action"]),
        "field": r["field"],
        "old": r["old_value"],
        "new": r["new_value"],
        "reverted": bool(r["reverted"]),
        "undoable": r["action"] in UNDOABLE and bool(r["field"]) and bool(r["lead_id"])
                    and not r["reverted"],
    }


def for_lead(lead_id, with_system=False, limit=200, with_money=True):
    """История одного объекта. Машинные строки по умолчанию скрыты: иначе
    перепроверка сайта вытесняет из карточки то, что делал человек.

    with_money=False убирает строки про сделки и платежи — их показываем
    только владельцу базы."""
    sql = "SELECT * FROM history WHERE lead_id=?"
    params = [lead_id]
    if not with_system:
        sql += " AND login <> ?"
        params.append(SYSTEM)
    if not with_money:
        ph = ",".join("?" * len(MONEY_ACTIONS))
        sql += f" AND action NOT IN ({ph})"
        params.extend(sorted(MONEY_ACTIONS))
    rows = db.conn().execute(sql + " ORDER BY id DESC LIMIT ?",
                             params + [limit]).fetchall()
    return [_row(r) for r in rows]


def feed(login="", action="", since="", until="", q="",
         with_system=False, limit=200, offset=0):
    """Общая лента. Машинные записи по умолчанию скрыты."""
    where, params = [], []
    if login:
        where.append("login = ?")
        params.append(SYSTEM if login == SYSTEM_LABEL else login)
    elif not with_system:
        where.append("login <> ?")
        params.append(SYSTEM)
    if action:
        where.append("action = ?")
        params.append(action)
    if since:
        where.append("ts >= ?")
        params.append(activity._day_bounds(since)[0])
    if until:
        where.append("ts < ?")
        params.append(activity._day_bounds(until)[1])
    if q:
        where.append("lead_name LIKE ?")
        params.append(f"%{q}%")

    sql = "SELECT * FROM history"
    if where:
        sql += " WHERE " + " AND ".join(where)

    total = db.conn().execute(
        sql.replace("SELECT *", "SELECT COUNT(*) AS n", 1), params).fetchone()["n"]
    rows = db.conn().execute(sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                             params + [limit, offset]).fetchall()
    return {"total": total, "rows": [_row(r) for r in rows]}


def counts(since="", until=""):
    """Сколько правок сделал каждый человек за период — для отчёта о времени."""
    where, params = ["login <> ?"], [SYSTEM]
    if since:
        where.append("ts >= ?")
        params.append(activity._day_bounds(since)[0])
    if until:
        where.append("ts < ?")
        params.append(activity._day_bounds(until)[1])
    rows = db.conn().execute(
        "SELECT login, COUNT(*) AS edits, COUNT(DISTINCT lead_id) AS leads "
        "FROM history WHERE " + " AND ".join(where) + " GROUP BY login", params)
    return {r["login"]: {"edits": r["edits"], "leads": r["leads"]} for r in rows}


# ── отмена ───────────────────────────────────────────────────────────────────

def _typed(column_type, value):
    """Пустую строку в числовой колонке возвращаем как NULL, а не как 0."""
    if "INT" in column_type or "REAL" in column_type:
        if value in ("", None):
            return None
        try:
            return int(value) if "INT" in column_type else float(value)
        except ValueError:
            return None
    return value


def revert(entry_id, login):
    """Возвращает прежнее значение одной правки. Отмена тоже идёт в журнал.

    Возвращает (запись_лида, текст ошибки).
    """
    c = db.conn()
    e = c.execute("SELECT * FROM history WHERE id=?", (entry_id,)).fetchone()
    if not e:
        return None, "Запись журнала не найдена"
    if e["reverted"]:
        return None, "Эту правку уже вернули"
    if not e["lead_id"] or not e["field"] or e["action"] not in UNDOABLE:
        return None, "Это действие нельзя отменить: оно не меняло одно поле"

    # Ниша и категория в журнале записаны названиями, а в лиде лежат номерами:
    # возврат идёт через справочник, а не прямой записью в колонку.
    if e["field"] in ("niche", "category"):
        return _revert_niche(c, e, login)

    columns = _columns()
    if e["field"] not in columns or e["field"] in NEVER_WRITE:
        return None, f"Поле «{e['field']}» больше не редактируется"

    lead = c.execute("SELECT * FROM leads WHERE id=?", (e["lead_id"],)).fetchone()
    if not lead:
        return None, "Объект удалён из базы"

    field = e["field"]
    current = lead[field]
    c.execute(f"UPDATE leads SET {field}=?, updated_at=? WHERE id=?",
              (_typed(columns[field], e["old_value"]), db.now(), e["lead_id"]))
    c.execute("UPDATE history SET reverted=1 WHERE id=?", (entry_id,))
    c.commit()

    log(login, "revert", lead=lead, field=field, old=current, new=e["old_value"])
    return c.execute("SELECT * FROM leads WHERE id=?", (e["lead_id"],)).fetchone(), ""


def _revert_niche(c, e, login):
    import niches
    lead = c.execute("SELECT * FROM leads WHERE id=?", (e["lead_id"],)).fetchone()
    if not lead:
        return None, "Объект удалён из базы"
    before = niches.titles(c, lead)
    error = niches.revert(c, lead, e["field"], e["old_value"])
    if error:
        return None, error
    c.execute("UPDATE history SET reverted=1 WHERE id=?", (e["id"],))
    c.commit()

    after = c.execute("SELECT * FROM leads WHERE id=?", (e["lead_id"],)).fetchone()
    now = niches.titles(c, after)
    # Возврат ниши может заодно снять категорию из чужой ниши — пишем обе
    for i, field in enumerate(("niche", "category")):
        if before[i] != now[i]:
            log(login, "revert", lead=lead, field=field, old=before[i], new=now[i])
    return after, ""
