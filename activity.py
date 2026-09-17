# -*- coding: utf-8 -*-
"""Учёт рабочего времени: когда сотрудник сидел в базе и сколько это вышло.

Браузер раз в минуту шлёт сигнал — но только пока вкладка открыта И человек
что-то делал последние несколько минут. Забытая на ночь вкладка рабочих часов
не намотает.

Сервер не хранит каждый сигнал. Пока сигналы идут подряд, у последнего отрезка
сдвигается конец; перерыв длиннее PAUSE начинает новый отрезок. За смену
остаётся несколько строк вместо нескольких сотен, а сам отрезок отвечает на
вопрос «в какие промежутки человек работал».

Сотрудники ничем не выделены: считается любой логин из LEADGEN_USERS, так что
нового человека достаточно завести в .env — учёт по нему пойдёт сам.
"""

import time
from datetime import date, datetime, timedelta

import auth
import config
import db

EPOCH = datetime(1970, 1, 1)

# Раз в столько секунд браузер шлёт сигнал.
BEAT = 60
# Пауза, после которой считаем, что человек отошёл, и начинаем новый отрезок.
# Пять минут выбраны так, чтобы короткий перерыв на звонок остался внутри
# рабочего отрезка, а закрытая вкладка добавила к отчёту не больше минуты.
PAUSE = 5 * 60
# Одиночный сигнал — это не ноль времени, а минута работы: человек открыл
# базу и что-то в ней сделал. Поэтому у каждого отрезка есть хвост.
TAIL = BEAT


# ── местное время ────────────────────────────────────────────────────────────

def local(ts):
    """unix-время → datetime в часовом поясе города. Нужен и журналу."""
    return EPOCH + timedelta(seconds=ts + config.TZ_OFFSET_HOURS * 3600)


MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")


def stamp(ts):
    """unix → «18.09.2026, 14:32» в часовом поясе города."""
    return local(ts).strftime("%d.%m.%Y, %H:%M") if ts else ""


def day_text(ts):
    """unix → «14 сентября»: короткая форма, когда точное время не важно."""
    if not ts:
        return ""
    d = local(ts)
    return f"{d.day} {MONTHS[d.month - 1]}"


def _day_of(ts):
    return local(ts).date().isoformat()


def _day_bounds(day):
    """Границы местных суток в unix-времени: [начало, конец)."""
    d = date.fromisoformat(day)
    start_local = datetime(d.year, d.month, d.day)
    start = int((start_local - EPOCH).total_seconds()) - config.TZ_OFFSET_HOURS * 3600
    return start, start + 86400


def today():
    return _day_of(int(time.time()))


def days_back(n):
    """День, с которого начинать отчёт за последние n суток включительно."""
    d = date.fromisoformat(today()) - timedelta(days=max(0, n - 1))
    return d.isoformat()


# ── запись ───────────────────────────────────────────────────────────────────

def touch(login, now=None):
    """Отмечает, что сотрудник работает прямо сейчас."""
    login = (login or "").strip()
    if not login:
        return
    now = int(now if now is not None else time.time())
    c = db.conn()
    row = c.execute(
        "SELECT id, ended FROM activity WHERE login=? ORDER BY started DESC LIMIT 1",
        (login,)).fetchone()

    if row and 0 <= now - row["ended"] <= PAUSE:
        # Часы на машине сотрудника могут отставать. Конец отрезка двигаем
        # только вперёд, иначе чужие часы урежут уже засчитанное время.
        if now > row["ended"]:
            c.execute("UPDATE activity SET ended=? WHERE id=?", (now, row["id"]))
    else:
        c.execute("INSERT INTO activity (login, started, ended) VALUES (?,?,?)",
                  (login, now, now))
    c.commit()


# ── отчёт ────────────────────────────────────────────────────────────────────

def _split_by_day(started, ended):
    """Режет отрезок по местной полуночи: [(день, начало, конец), ...].

    Вечерний отрезок, перешедший за полночь, иначе целиком упал бы в один
    день и час ночи посчитался бы вчерашним.
    """
    stop = max(ended, started) + TAIL
    out, cur = [], started
    while cur < stop:
        day = _day_of(cur)
        edge = min(stop, _day_bounds(day)[1])
        out.append((day, cur, edge))
        cur = edge
    return out


def _fetch(login, since, until):
    sql = "SELECT login, started, ended FROM activity"
    where, params = [], []
    if login:
        where.append("login = ?")
        params.append(login)
    if since:
        where.append("ended >= ?")
        params.append(_day_bounds(since)[0] - TAIL)
    if until:
        where.append("started < ?")
        params.append(_day_bounds(until)[1])
    if where:
        sql += " WHERE " + " AND ".join(where)
    return db.conn().execute(sql + " ORDER BY started", params).fetchall()


def _hhmm(ts):
    return local(ts).strftime("%H:%M")


def report(login=None, since=None, until=None):
    """Рабочее время по сотрудникам: сумма по дням и промежутки внутри дня.

    Возвращает список людей, у каждого — дни от свежих к старым.
    """
    collected = {}
    for r in _fetch(login, since, until):
        for day, a, b in _split_by_day(r["started"], r["ended"]):
            if (since and day < since) or (until and day > until):
                continue
            collected.setdefault(r["login"], {}).setdefault(day, []).append((a, b))

    now = int(time.time())
    people_out = []
    for person in sorted(collected):
        days = []
        for day in sorted(collected[person], reverse=True):
            spans = sorted(collected[person][day])
            days.append({
                "day": day,
                "seconds": sum(b - a for a, b in spans),
                "intervals": [{"from": _hhmm(a), "to": _hhmm(b), "seconds": b - a}
                              for a, b in spans],
            })
        last = db.conn().execute(
            "SELECT MAX(ended) AS e FROM activity WHERE login=?", (person,)).fetchone()
        people_out.append({
            "login": person,
            "total": sum(d["seconds"] for d in days),
            "days": days,
            # Отрезок из будущего (часы сотрудника ушли вперёд) не должен
            # навсегда подсвечивать человека как сидящего в базе.
            "online": bool(last and last["e"] and 0 <= now - last["e"] <= PAUSE),
            "last_seen": local(last["e"]).strftime("%d.%m %H:%M")
                         if last and last["e"] else "",
        })
    return people_out


def people():
    """Все, по кому имеет смысл смотреть отчёт: заведённые и когда-либо видённые."""
    out = []
    seen = [r["login"] for r in
            db.conn().execute("SELECT DISTINCT login FROM activity ORDER BY login")]
    for name in sorted(auth.load_users()) + seen:
        if name not in out:
            out.append(name)
    return out
