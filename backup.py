# -*- coding: utf-8 -*-
"""Резервные копии базы. Раз в день, вечером, делаются две вещи:

1. Снимок файла базы рядом с ней, в backups/. Из него база восстанавливается
   целиком: остановить сервис, подложить файл, запустить. Храним 14 дней.
2. Выгрузка всех таблиц в Google Таблицу, по листу на таблицу. Это копия
   вне сервера, которую можно открыть и прочитать глазами. Восстановить
   из неё базу напрямую нельзя — типы и связи в таблице теряются.

Сессии входа в таблицу не выгружаются никогда: токен сессии — это готовый
вход в базу под чужим именем.

Выгрузка работает от сервисного аккаунта Google. Его ключ и номер таблицы
лежат на сервере в /etc/leadgen.env, а не в коде: репозиторий публичный.

Расписание — поток внутри сервиса, а не cron: так не нужно ничего
настраивать на сервере, а если в назначенный час сервис лежал, копия
сделается, как только он поднимется.
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
import db

SHEET_ID = os.getenv("LEADGEN_SHEET_ID", "").strip()
GOOGLE_KEY = os.getenv("LEADGEN_GOOGLE_KEY", "").strip()   # путь к JSON-ключу
HOUR = int(os.getenv("LEADGEN_BACKUP_HOUR", "21"))           # местное время города
KEEP = 14                                                    # сколько снимков хранить
CHECK_EVERY = 300                                            # как часто смотреть на часы, сек
RETRY_AFTER = 3600                                           # повтор после ошибки, сек

API = "https://sheets.googleapis.com/v4/spreadsheets/"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Таблица базы → лист. Порядок — порядок листов. sessions нет намеренно.
TABLES = [
    ("leads", "Лиды"),
    ("lead_notes", "Заметки"),
    ("calls", "Звонки"),
    ("deals", "Сделки"),
    ("payments", "Платежи"),
    ("history", "Журнал"),
    ("activity", "Рабочее время"),
    ("niches", "Ниши"),
    ("categories", "Категории"),
    ("category_aliases", "Синонимы категорий"),
    ("refs", "Референсы"),
    ("settings", "Настройки"),
    ("runs", "Запуски сбора"),
]
SUMMARY = "Сводка"

CELL_LIMIT = 50000          # больше символов в ячейку Google не пускает
ROWS_PER_REQUEST = 5000     # крупные листы пишем частями

_lock = threading.Lock()
_state = {"running": False}


def sheet_ready():
    return bool(SHEET_ID and GOOGLE_KEY)


def backup_dir():
    return Path(os.getenv("LEADGEN_BACKUP_DIR") or Path(config.DB_PATH).parent / "backups")


def local_now():
    return datetime.now(timezone.utc) + timedelta(hours=config.TZ_OFFSET_HOURS)


# ── снимок файла ─────────────────────────────────────────────────────────────

def snapshot():
    """Согласованная копия базы через backup API SQLite.

    Просто скопировать файл нельзя: при включённом WAL часть свежих записей
    лежит в соседнем -wal, и копия без него окажется битой или старой.
    """
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"leads-{local_now().date().isoformat()}.db"
    tmp = path.with_suffix(".db.tmp")

    src = sqlite3.connect(config.DB_PATH, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, path)

    old = sorted(folder.glob("leads-*.db"))[:-KEEP]
    for f in old:
        f.unlink(missing_ok=True)
    return path


# ── выгрузка в Google Таблицу ────────────────────────────────────────────────

def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    return text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT - 20] + " …[обрезано]"


def collect():
    """[(название листа, строки с заголовком первой)] по всем таблицам."""
    c = db.conn()
    out = []
    for table, title in TABLES:
        cur = c.execute(f"SELECT * FROM {table} ORDER BY rowid")
        header = [d[0] for d in cur.description]
        rows = [[_cell(v) for v in r] for r in cur.fetchall()]
        out.append((title, [header] + rows))
    return out


def _session():
    # Импорт внутри: без настроенной выгрузки библиотека не нужна вовсе
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_file(GOOGLE_KEY, scopes=SCOPES)
    return AuthorizedSession(creds)


def _call(s, method, url, **kw):
    r = s.request(method, url, timeout=120, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = r.text[:300]
        if r.status_code == 403:
            detail += (" — откройте таблицу и дайте доступ «Редактор» адресу "
                       "сервисного аккаунта (client_email в JSON-ключе)")
        raise RuntimeError(f"Google Sheets {r.status_code}: {detail}")
    return r.json()


def export_sheet():
    """Переписывает листы таблицы текущими данными. Возвращает число строк."""
    s = _session()
    sheets = collect()
    total = sum(len(rows) - 1 for _, rows in sheets)
    stamp = local_now().strftime("%d.%m.%Y %H:%M")
    summary = [["Выгружено", stamp], ["Часовой пояс", f"UTC+{config.TZ_OFFSET_HOURS}"],
               [], ["Лист", "Строк"]]
    summary += [[title, len(rows) - 1] for title, rows in sheets]
    summary += [[], ["Сессии входа не выгружаются намеренно."],
                ["Время в колонках ts, started, ended — unix-время UTC."]]
    sheets.insert(0, (SUMMARY, summary))

    meta = _call(s, "GET", API + SHEET_ID,
                 params={"fields": "sheets.properties(sheetId,title)"})
    ids = {p["properties"]["title"]: p["properties"]["sheetId"] for p in meta.get("sheets", [])}

    missing = [title for title, _ in sheets if title not in ids]
    if missing:
        reply = _call(s, "POST", API + SHEET_ID + ":batchUpdate", json={"requests": [
            {"addSheet": {"properties": {"title": t}}} for t in missing]})
        for rep in reply.get("replies", []):
            p = rep["addSheet"]["properties"]
            ids[p["title"]] = p["sheetId"]

    # Размер листа ставим ровно под данные: так пропадают строки, оставшиеся
    # от прошлой выгрузки, и не упираемся в стандартные 1000×26. Строк минимум
    # две — Google не даёт закрепить заголовок на листе из одной строки.
    requests = []
    for title, rows in sheets:
        width = max(len(r) for r in rows) or 1
        grid = {"rowCount": max(len(rows), 2), "columnCount": width}
        fields = "gridProperties(rowCount,columnCount"
        if title != SUMMARY:
            grid["frozenRowCount"] = 1
            fields += ",frozenRowCount"
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": ids[title], "gridProperties": grid},
            "fields": fields + ")"}})
    for pos, (title, _) in enumerate(sheets):
        requests.append({"updateSheetProperties": {
            "properties": {"sheetId": ids[title], "index": pos}, "fields": "index"}})
    _call(s, "POST", API + SHEET_ID + ":batchUpdate", json={"requests": requests})

    # RAW: значения кладутся как есть. Текст, начинающийся с «=», не станет
    # формулой — а в заметках и названиях с сайтов бывает что угодно.
    _call(s, "POST", API + SHEET_ID + "/values:batchClear",
          json={"ranges": [f"'{title}'" for title, _ in sheets]})
    for title, rows in sheets:
        for start in range(0, len(rows), ROWS_PER_REQUEST):
            _call(s, "POST", API + SHEET_ID + "/values:batchUpdate", json={
                "valueInputOption": "RAW",
                "data": [{"range": f"'{title}'!A{start + 1}",
                          "values": rows[start:start + ROWS_PER_REQUEST]}]})
    return total


# ── запуск ───────────────────────────────────────────────────────────────────

def _meta(key, value=None):
    c = db.conn()
    if value is None:
        r = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else ""
    c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    c.commit()


def run(login="@system"):
    """Снимок и выгрузка. Ошибка одной части не отменяет другую."""
    if not _lock.acquire(blocking=False):
        return status()
    _state["running"] = True
    result = {"at": local_now().strftime("%d.%m.%Y %H:%M"), "by": login}
    try:
        try:
            path = snapshot()
            result["file"] = path.name
        except Exception as exc:                      # noqa: BLE001 — пишем в статус
            result["file_error"] = str(exc)

        if sheet_ready():
            try:
                result["rows"] = export_sheet()
            except Exception as exc:                  # noqa: BLE001
                result["sheet_error"] = str(exc)
        else:
            result["sheet_error"] = ("Выгрузка в Google Таблицу не настроена: нет "
                                     "LEADGEN_SHEET_ID или LEADGEN_GOOGLE_KEY в /etc/leadgen.env")

        ok = "file" in result and "rows" in result
        _meta("backup_last", json.dumps(result, ensure_ascii=False))
        if ok:
            _meta("backup_day", local_now().date().isoformat())

        import history
        history.log(login, "backup", new=_describe(result))
    finally:
        _state["running"] = False
        _lock.release()
    return status()


def _describe(r):
    parts = [f"снимок {r['file']}" if r.get("file") else f"снимок: ошибка — {r.get('file_error')}"]
    parts.append(f"в таблицу {r['rows']} строк" if "rows" in r
                 else f"таблица: {r.get('sheet_error')}")
    return "; ".join(parts)


def run_async(login):
    threading.Thread(target=run, args=(login,), daemon=True).start()


def status():
    last = _meta("backup_last")
    files = sorted(backup_dir().glob("leads-*.db")) if backup_dir().exists() else []
    return {
        "running": _state["running"],
        "sheet_ready": sheet_ready(),
        "hour": HOUR,
        "last": json.loads(last) if last else None,
        "last_ok_day": _meta("backup_day"),
        "files": [f.name for f in reversed(files)],
    }


def _due():
    now = local_now()
    if now.hour < HOUR or _meta("backup_day") == now.date().isoformat():
        return False
    # После ошибки не долбим Google каждые пять минут
    last = _meta("backup_last")
    if last:
        try:
            tried = datetime.strptime(json.loads(last)["at"], "%d.%m.%Y %H:%M")
            if (now.replace(tzinfo=None) - tried).total_seconds() < RETRY_AFTER:
                return False
        except (ValueError, KeyError, TypeError):
            pass
    return True


def _loop():
    while True:
        time.sleep(CHECK_EVERY)
        try:
            if _due():
                run()
        except Exception:                             # noqa: BLE001 — поток не должен умереть
            pass


def start():
    threading.Thread(target=_loop, name="backup", daemon=True).start()
