# -*- coding: utf-8 -*-
"""Конверсии и показатели: воронка, деньги, вознаграждение, конверты, найм.

Всё считается здесь, а не во фронтенде: цифры из этого раздела идут в планы,
в выплаты продажнику и в решение о найме, и дублировать формулы в двух местах
нельзя — они разойдутся.

Про время. В базе три разных представления дат, и путать их нельзя:
  * calls.ts и history.ts — unix-время UTC, переводится границами местных суток
    (иначе звонок в 23:30 уедет в следующий день);
  * payments.paid_at, deals.contract_at — ISO-дата, которую ввёл человек, она
    уже местная, сравнивается строкой;
  * leads.created_at — ISO-дата-время от сервера.
"""

import statistics
from datetime import date, timedelta

import activity
import db

# Действия журнала, по которым восстанавливается движение по воронке.
# Момент перехода больше нигде не хранится: в leads лежит только текущий статус.
STATUS_ACTION = "status"


# ── периоды ──────────────────────────────────────────────────────────────────

def _bounds(day_from: str, day_to: str):
    """Границы периода в unix-времени: [начало первых суток, конец последних)."""
    return activity._day_bounds(day_from)[0], activity._day_bounds(day_to)[1]


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def prev_range(day_from: str, day_to: str):
    """Предыдущий период такой же длины. Без сравнения число ничего не значит."""
    length = (date.fromisoformat(day_to) - date.fromisoformat(day_from)).days + 1
    return _shift(day_from, -length), _shift(day_from, -1)


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _month_shift(d: date, months: int) -> date:
    """Сдвиг на месяцы по первым числам — без арифметики с 31-м числом."""
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def presets() -> dict:
    """Готовые периоды считаем на сервере: у него часовой пояс города,
    а браузер владельца может стоять где угодно."""
    today = date.fromisoformat(activity.today())
    monday = today - timedelta(days=today.weekday())
    m0 = _month_start(today)
    prev_m = _month_shift(m0, -1)
    q_start = date(today.year, (today.month - 1) // 3 * 3 + 1, 1)
    return {
        "week": [monday.isoformat(), today.isoformat()],
        "month": [m0.isoformat(), today.isoformat()],
        "prev_month": [prev_m.isoformat(), (m0 - timedelta(days=1)).isoformat()],
        "quarter": [q_start.isoformat(), today.isoformat()],
        "today": today.isoformat(),
    }


def _delta(cur, prev):
    """Изменение в процентах. None — когда сравнивать не с чем: рост «с нуля»
    в процентах не выражается, и рисовать там +100 % было бы враньём."""
    if not prev:
        return None
    return round((cur - prev) * 100 / prev)


def _pct(part, whole):
    return round(part * 100 / whole) if whole else 0


def _median(values):
    return int(statistics.median(values)) if values else 0


# ── сырые выборки ────────────────────────────────────────────────────────────

def _status_moves(day_from: str, day_to: str) -> dict:
    """{статус: сколько лидов перешло в него за период}. Считаем лиды, а не
    записи: вернуть лид в статус и снова из него — не два новых лида."""
    lo, hi = _bounds(day_from, day_to)
    rows = db.conn().execute(
        "SELECT new_value AS st, COUNT(DISTINCT lead_id) n FROM history "
        "WHERE action=? AND ts >= ? AND ts < ? GROUP BY new_value",
        (STATUS_ACTION, lo, hi))
    return {r["st"]: r["n"] for r in rows}


def _first_payments() -> dict:
    """{id сделки: (дата, сумма) самого раннего поступления}.

    Самое раннее по дате, а не по порядку ввода: платежи заводят задним числом.
    """
    out = {}
    for r in db.conn().execute(
            "SELECT deal_id, paid_at, amount, id FROM payments "
            "ORDER BY COALESCE(paid_at,''), id"):
        out.setdefault(r["deal_id"], (r["paid_at"] or "", r["amount"]))
    return out


def _deals_index() -> dict:
    return {r["id"]: dict(r) for r in db.conn().execute("SELECT * FROM deals")}


def _closed_in(day_from: str, day_to: str, deals: dict, firsts: dict) -> list:
    """Сделки, закрытые за период. Закрытие — дата первого поступления:
    так это определено в договоре с продажником."""
    out = []
    for deal_id, (paid_at, first_amount) in firsts.items():
        if not (day_from <= paid_at <= day_to):
            continue
        d = deals.get(deal_id)
        if d:
            out.append(dict(d, first_amount=first_amount, first_at=paid_at))
    return out


def _bonus_ok(deal: dict, min_share: int) -> bool:
    """Идёт ли сделка в зачёт премии.

    Порог доли первой оплаты — условие из договора на разовую разработку.
    К абонентке оно неприменимо: там первый платёж это целиком первый месяц,
    и сравнивать его с «ценой договора» не с чем.
    """
    if deal["kind"] != "project":
        return True
    amount = deal["amount"] or 0
    return bool(amount) and deal["first_amount"] * 100 / amount >= min_share


# ── воронка ──────────────────────────────────────────────────────────────────

def funnel(day_from: str, day_to: str, deals: dict, firsts: dict) -> dict:
    c = db.conn()
    lo, hi = _bounds(day_from, day_to)
    moves = _status_moves(day_from, day_to)

    total = c.execute(
        "SELECT COUNT(*) n FROM leads WHERE COALESCE(hidden,0)=0 "
        "AND COALESCE(created_at,'') <= ?", (day_to + "T23:59:59",)).fetchone()["n"]
    added = c.execute(
        "SELECT COUNT(*) n FROM leads WHERE COALESCE(hidden,0)=0 "
        "AND COALESCE(created_at,'') >= ? AND COALESCE(created_at,'') <= ?",
        (day_from, day_to + "T23:59:59")).fetchone()["n"]

    calls = c.execute("SELECT COUNT(*) n FROM calls WHERE ts >= ? AND ts < ?",
                      (lo, hi)).fetchone()["n"]
    # Процент дозвона считаем по лидам, а не по звонкам: десять попыток до
    # одного человека — это один дозвон, а не десять шансов.
    dialed = c.execute("SELECT COUNT(DISTINCT lead_id) n FROM calls "
                       "WHERE ts >= ? AND ts < ?", (lo, hi)).fetchone()["n"]
    answered = c.execute("SELECT COUNT(DISTINCT lead_id) n FROM calls "
                         "WHERE outcome='answered' AND ts >= ? AND ts < ?",
                         (lo, hi)).fetchone()["n"]

    in_work = moves.get("in_work", 0)
    audit = moves.get("audit", 0)
    proposal = moves.get("proposal", 0)
    refused = moves.get("refused", 0)
    closed = len({d["lead_id"] for d in _closed_in(day_from, day_to, deals, firsts)})

    steps = [
        {"key": "base", "title": "Лидов в базе", "value": total,
         "note": f"добавлено за период: {added}"},
        {"key": "in_work", "title": "Ручная проверка", "value": in_work,
         "of": total},
        {"key": "calls", "title": "Попыток дозвона", "value": calls,
         "note": f"лидов обзвонено: {dialed}"},
        {"key": "answered", "title": "Дозвонились", "value": answered, "of": dialed},
        {"key": "audit", "title": "Согласились на аудит", "value": audit, "of": answered},
        {"key": "proposal", "title": "Отправлено КП", "value": proposal, "of": audit},
        {"key": "deal", "title": "Сделок закрыто", "value": closed, "of": proposal},
        {"key": "refused", "title": "Отказов", "value": refused, "of": in_work},
    ]
    for st in steps:
        if "of" in st:
            st["conv"] = _pct(st["value"], st["of"])

    return {
        "steps": steps,
        "added": added,
        "dial_rate": _pct(answered, dialed),
        "overall": _pct(closed, in_work),
        "closed": closed,
    }


# ── деньги ───────────────────────────────────────────────────────────────────

def money(day_from: str, day_to: str, deals: dict, firsts: dict, st: dict) -> dict:
    c = db.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT p.amount, p.deal_id FROM payments p "
        "WHERE COALESCE(p.paid_at,'') >= ? AND COALESCE(p.paid_at,'') <= ?",
        (day_from, day_to))]

    total = sum(r["amount"] for r in rows)
    repeat = sum(r["amount"] for r in rows
                 if deals.get(r["deal_id"], {}).get("is_repeat"))

    closed = _closed_in(day_from, day_to, deals, firsts)
    amounts = [d["amount"] for d in closed if d["amount"]]

    # Среднюю долю первой оплаты считаем только по проектам: у абонентки
    # amount — месячный платёж, и деление даёт бессмыслицу.
    shares = [round(d["first_amount"] * 100 / d["amount"])
              for d in closed if d["kind"] == "project" and d["amount"]]

    # Цикл сделки: от первого касания до денег. Медиана, а не среднее —
    # одна сделка, которую тянули полгода, перекосила бы среднее.
    cycles = []
    for d in closed:
        r = c.execute("SELECT MIN(ts) t FROM calls WHERE lead_id=?",
                      (d["lead_id"],)).fetchone()
        if not r or not r["t"]:
            continue
        started = activity.local(r["t"]).date()
        try:
            paid = date.fromisoformat(d["first_at"])
        except (ValueError, TypeError):
            continue
        days = (paid - started).days
        # Платёж раньше первого звонка — значит сделку завели задним числом,
        # без истории обзвона. Цикл по такой не известен, и ноль здесь был бы
        # не «закрыли за день», а «мы не знаем».
        if days >= 0:
            cycles.append(days)

    mrr = c.execute("SELECT COALESCE(SUM(amount),0) s FROM deals "
                    "WHERE kind='retainer' AND state='active'").fetchone()["s"]
    with_deal = c.execute("SELECT COUNT(DISTINCT lead_id) n FROM deals").fetchone()["n"]
    with_ret = c.execute("SELECT COUNT(DISTINCT lead_id) n FROM deals "
                         "WHERE kind='retainer'").fetchone()["n"]

    avg_share = round(sum(shares) / len(shares)) if shares else 0
    return {
        "total": total,
        "new": total - repeat,
        "repeat": repeat,
        "avg_check": round(sum(amounts) / len(amounts)) if amounts else 0,
        "avg_first_share": avg_share,
        "low_first_share": bool(shares) and avg_share < st["first_share_min"],
        "cycle_days": _median(cycles),
        "cycle_n": len(cycles),
        "mrr": mrr,
        "retainer_share": _pct(with_ret, with_deal),
        "closed": len(closed),
    }


# ── вознаграждение продажника ────────────────────────────────────────────────

def reward(day_from: str, day_to: str, deals: dict, firsts: dict, st: dict) -> dict:
    """По договору: процент от фактически поступивших сумм плюс премия
    за число закрытых сделок. Ставки и пороги — из настроек: договор прямо
    предусматривает их пересмотр."""
    c = db.conn()
    rate_new, rate_rep = st["rate_new"], st["rate_repeat"]
    levels = sorted(st["bonus_levels"], key=lambda x: x[0])
    min_share = st["first_share_min"]

    by_owner = {}

    def slot(login):
        return by_owner.setdefault(login or "—", {
            "login": login or "—", "paid_new": 0, "paid_repeat": 0,
            "deals": 0, "deals_bonus": 0})

    for r in c.execute("SELECT p.amount, p.deal_id FROM payments p "
                       "WHERE COALESCE(p.paid_at,'') >= ? AND COALESCE(p.paid_at,'') <= ?",
                       (day_from, day_to)):
        d = deals.get(r["deal_id"])
        if not d:
            continue
        s = slot(d["owner_login"])
        s["paid_repeat" if d["is_repeat"] else "paid_new"] += r["amount"]

    for d in _closed_in(day_from, day_to, deals, firsts):
        s = slot(d["owner_login"])
        s["deals"] += 1
        if _bonus_ok(d, min_share):
            s["deals_bonus"] += 1

    rows = []
    for s in by_owner.values():
        s["reward_new"] = round(s["paid_new"] * rate_new / 100)
        s["reward_repeat"] = round(s["paid_repeat"] * rate_rep / 100)
        # Премии не суммируются: берётся одна, наибольшая из достигнутых.
        s["bonus"] = max((amount for need, amount in levels
                          if s["deals_bonus"] >= need), default=0)
        s["bonus_next"] = next(((need, amount) for need, amount in levels
                                if s["deals_bonus"] < need), None)
        s["total"] = s["reward_new"] + s["reward_repeat"] + s["bonus"]
        rows.append(s)
    rows.sort(key=lambda x: -x["total"])

    # Срок выплаты — до 10 числа месяца, следующего за отчётным.
    today = date.fromisoformat(activity.today())
    due = _month_shift(_month_start(date.fromisoformat(day_to)), 1).replace(day=10)
    left = (due - today).days
    return {
        "rows": rows,
        "rate_new": rate_new,
        "rate_repeat": rate_rep,
        "min_share": min_share,
        "levels": levels,
        "due": due.isoformat(),
        "days_left": left,
        "due_soon": 0 <= left < 5,
        "overdue": left < 0,
    }


# ── конверты ─────────────────────────────────────────────────────────────────

def _split(total: int, shares: dict) -> dict:
    """Раскладка суммы по долям без потери копеек.

    Обычное округление каждой доли даёт расхождение с исходной суммой, а
    конверты должны сходиться до рубля. Поэтому берём целые части, а остаток
    отдаём тем, у кого дробный хвост был больше.
    """
    if total <= 0 or not shares:
        return {k: 0 for k in shares}
    raw = {k: total * v / 100 for k, v in shares.items()}
    out = {k: int(v) for k, v in raw.items()}
    rest = total - sum(out.values())
    for k in sorted(raw, key=lambda k: -(raw[k] - int(raw[k])))[:rest]:
        out[k] += 1
    return out


def envelopes(day_from: str, day_to: str, st: dict) -> dict:
    c = db.conn()
    shares = st["envelopes"]
    total_share = sum(shares.values())

    period = c.execute("SELECT COALESCE(SUM(amount),0) s FROM payments "
                       "WHERE COALESCE(paid_at,'') >= ? AND COALESCE(paid_at,'') <= ?",
                       (day_from, day_to)).fetchone()["s"]
    ever = c.execute("SELECT COALESCE(SUM(amount),0) s FROM payments").fetchone()["s"]

    got = _split(period, shares)
    saved = _split(ever, shares)

    costs = st["role_costs"]
    rows = []
    for name, pct in shares.items():
        # Фонд найма: пока человека на роль не взяли, конверт копится, и
        # важно не «сколько там лежит», а «на сколько месяцев этого хватит».
        cost = costs.get(name, 0)
        rows.append({
            "name": name, "pct": pct,
            "period": got.get(name, 0), "saved": saved.get(name, 0),
            "role_cost": cost,
            "months": round(saved.get(name, 0) / cost, 1) if cost else None,
            "ready": bool(cost) and saved.get(name, 0) >= cost * 3,
            "need": max(cost * 3 - saved.get(name, 0), 0) if cost else 0,
        })
    return {
        "rows": rows,
        "period_total": period,
        "ever_total": ever,
        "share_sum": total_share,
        "share_ok": total_share == 100,
        "check_ok": sum(got.values()) == period,
    }


# ── найм ─────────────────────────────────────────────────────────────────────

def _deals_by_month(deals: dict, firsts: dict, months: int = 6) -> list:
    """Сколько сделок и проектов закрыто в каждом из последних месяцев."""
    today = date.fromisoformat(activity.today())
    out = []
    for i in range(months):
        m = _month_shift(_month_start(today), -i)
        lo, hi = m.isoformat(), (_month_shift(m, 1) - timedelta(days=1)).isoformat()
        closed = _closed_in(lo, hi, deals, firsts)
        out.append({
            "month": m.isoformat(),
            "deals": len(closed),
            "projects": len([d for d in closed if d["kind"] == "project"]),
            # «Сдан» считаем по факту полной оплаты: отдельной даты сдачи
            # в базе нет, а полностью оплаченный проект почти всегда сдан.
            "done": len([d for d in closed
                         if d["kind"] == "project" and d["state"] == "done"]),
        })
    return out


def hiring(deals: dict, firsts: dict, mrr: int, env_rows: list, st: dict) -> list:
    h = st["hiring"]
    by_month = _deals_by_month(deals, firsts)
    fund = {r["name"]: r for r in env_rows}

    def streak(key, need, months):
        """Держится ли порог нужное число месяцев подряд. Текущий месяц
        не считаем: он ещё не кончился, и судить по нему рано."""
        window = by_month[1:1 + months]
        return len(window) == months and all(m[key] >= need for m in window), window

    active_ret = db.conn().execute(
        "SELECT COUNT(*) n FROM deals WHERE kind='retainer' AND state='active'"
    ).fetchone()["n"]

    presale_ok, presale_w = streak("deals", h["presale_deals"], h["presale_months"])
    dev_ok, dev_w = streak("projects", h["dev_projects"], h["dev_months"])

    last = by_month[1] if len(by_month) > 1 else {"done": 0, "deals": 0}
    seller_ok = last["done"] >= last["deals"] * 2 and last["deals"] > 0

    return [
        {"role": "Пресейл-аккаунт", "envelope": "Производство", "ok": presale_ok,
         "rule": f"не меньше {h['presale_deals']} сделок в месяц "
                 f"{h['presale_months']} месяца подряд",
         "fact": ", ".join(f"{m['month'][:7]}: {m['deals']}" for m in presale_w) or "нет данных",
         "fund": fund.get("Производство")},
        {"role": "Контекстолог", "envelope": "Сопровождение",
         "ok": active_ret >= h["context_retainers"],
         "rule": f"не меньше {h['context_retainers']} активных абонентских сделок",
         "fact": f"сейчас активных: {active_ret}",
         "fund": fund.get("Сопровождение")},
        {"role": "Разработчик", "envelope": "Производство", "ok": dev_ok,
         "rule": f"{h['dev_projects']}–{h['dev_projects'] + 1} проектов в месяц "
                 f"{h['dev_months']} месяца подряд",
         "fact": ", ".join(f"{m['month'][:7]}: {m['projects']}" for m in dev_w) or "нет данных",
         "fund": fund.get("Производство")},
        {"role": "Второй продажник", "envelope": "Продажи", "ok": seller_ok,
         "rule": "производство держит двойной поток: сдано не меньше, "
                 "чем удвоенное число новых сделок",
         "fact": f"за прошлый месяц сдано {last['done']} при {last['deals']} сделках",
         "fund": fund.get("Продажи")},
        {"role": "Маркетолог", "envelope": "Сопровождение",
         "ok": mrr >= h["marketer_mrr"],
         "rule": f"MRR не ниже {h['marketer_mrr']} ₽",
         "fact": f"сейчас MRR: {mrr} ₽",
         "fund": fund.get("Сопровождение")},
    ]


# ── лимит НПД ────────────────────────────────────────────────────────────────

def npd(st: dict) -> dict:
    """Лимит считается за скользящие 12 месяцев: так видно приближение
    заранее, а не 31 декабря."""
    c = db.conn()
    today = date.fromisoformat(activity.today())
    since = _month_shift(_month_start(today), -11).isoformat()
    got = c.execute("SELECT COALESCE(SUM(amount),0) s FROM payments "
                    "WHERE COALESCE(paid_at,'') >= ?", (since,)).fetchone()["s"]

    limit = st["npd_limit"]
    months = (today.year * 12 + today.month) - (
        date.fromisoformat(since).year * 12 + date.fromisoformat(since).month) + 1
    rate = round(got / months) if months else 0
    left = max(limit - got, 0)
    # Прогноз только при живом темпе: делить на ноль и обещать «никогда»
    # одинаково бесполезно.
    forecast = ""
    if rate > 0 and left > 0:
        forecast = _month_shift(_month_start(today), int(left / rate) + 1).isoformat()
    return {
        "got": got, "limit": limit, "left": left,
        "pct": _pct(got, limit),
        "rate": rate,
        "since": since,
        "forecast": forecast,
        "warn": _pct(got, limit) >= st["npd_warn"],
        "warn_at": st["npd_warn"],
    }


# ── активность команды ───────────────────────────────────────────────────────

def team(day_from: str, day_to: str) -> list:
    c = db.conn()
    lo, hi = _bounds(day_from, day_to)
    rows = {}

    def slot(login):
        return rows.setdefault(login, {
            "login": login, "leads": 0, "calls": 0, "answered": 0,
            "notes": 0, "statuses": 0})

    for r in c.execute("SELECT login, COUNT(*) n, "
                       "SUM(CASE WHEN outcome='answered' THEN 1 ELSE 0 END) a "
                       "FROM calls WHERE ts >= ? AND ts < ? GROUP BY login", (lo, hi)):
        s = slot(r["login"])
        s["calls"], s["answered"] = r["n"], r["a"] or 0

    # Автора-автомат в отчёт по людям не пускаем: перенесённые заметки
    # и записи сбора числились бы за несуществующим сотрудником.
    for r in c.execute("SELECT login, COUNT(*) n FROM lead_notes "
                       "WHERE deleted=0 AND login <> ? AND ts >= ? AND ts < ? "
                       "GROUP BY login", (db.SYSTEM_LOGIN, lo, hi)):
        slot(r["login"])["notes"] = r["n"]

    for r in c.execute("SELECT login, action, COUNT(*) n FROM history "
                       "WHERE login <> ? AND ts >= ? AND ts < ? "
                       "AND action IN ('create','status') GROUP BY login, action",
                       (db.SYSTEM_LOGIN, lo, hi)):
        s = slot(r["login"])
        s["leads" if r["action"] == "create" else "statuses"] = r["n"]

    out = sorted(rows.values(), key=lambda x: -(x["calls"] + x["statuses"]))
    for s in out:
        s["dial_rate"] = _pct(s["answered"], s["calls"])
    return out


# ── сборка ───────────────────────────────────────────────────────────────────

def report(day_from: str, day_to: str) -> dict:
    st = db.settings()
    deals, firsts = _deals_index(), _first_payments()

    pf, pt = prev_range(day_from, day_to)
    cur_funnel = funnel(day_from, day_to, deals, firsts)
    cur_money = money(day_from, day_to, deals, firsts, st)
    old_funnel = funnel(pf, pt, deals, firsts)
    old_money = money(pf, pt, deals, firsts, st)

    # Сравнение с прошлым периодом пришиваем к тем числам, по которым
    # принимают решения: остальные и так читаются в контексте.
    for step, was in zip(cur_funnel["steps"], old_funnel["steps"]):
        step["prev"] = was["value"]
        step["delta"] = _delta(step["value"], was["value"])
    for key in ("total", "new", "repeat", "avg_check", "cycle_days", "mrr", "closed"):
        cur_money[key + "_prev"] = old_money[key]
        cur_money[key + "_delta"] = _delta(cur_money[key], old_money[key])

    env = envelopes(day_from, day_to, st)
    return {
        "range": {"from": day_from, "to": day_to, "prev_from": pf, "prev_to": pt},
        "presets": presets(),
        "funnel": cur_funnel,
        "money": cur_money,
        "reward": reward(day_from, day_to, deals, firsts, st),
        "envelopes": env,
        "hiring": hiring(deals, firsts, cur_money["mrr"], env["rows"], st),
        "npd": npd(st),
        "team": team(day_from, day_to),
        "settings": st,
    }
