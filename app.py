# -*- coding: utf-8 -*-
"""Веб-интерфейс сборщика лидов: API + отдача статики."""

import csv
import io
import json
from datetime import date
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Body, HTTPException, Query, Request, Response, UploadFile, File
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import activity
import auth
import config
import db
import history
import metrics
import utils
import pipeline
import refs
import scoring
from enrich import research_brief

app = FastAPI(title="Сборщик лидов — ниша бронирования", docs_url=None, redoc_url=None)
db.init()
auth.cleanup_sessions()

# Сюда пускаем без входа: сама форма логина, её стили и ответ поисковикам.
# Иконки и манифест тоже без входа: браузер тянет манифест без куки, а айфон
# берёт apple-touch-icon с корня ещё до того, как человек вошёл.
PUBLIC_PATHS = {"/login", "/robots.txt", "/favicon.ico", "/site.webmanifest",
                "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await call_next(request)

    login = auth.session_login(request.cookies.get(auth.COOKIE_NAME))
    if not login:
        # Фоновым запросам нужен код, а не HTML формы входа,
        # иначе интерфейс покажет разметку вместо данных.
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Требуется вход"}, status_code=401)
        return RedirectResponse(f"/login?next={quote(path)}", status_code=302)

    request.state.login = login
    return await call_next(request)


# ── вход и выход ─────────────────────────────────────────────────────────────

@app.get("/login")
def login_page(request: Request):
    # Уже вошедшего незачем держать на форме
    if auth.session_login(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=302)
    return FileResponse(config.BASE_DIR / "static" / "login.html")


@app.post("/login")
def login(request: Request, payload: dict = Body(...)):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    user, error = auth.authenticate(payload.get("login"), payload.get("password"), ip)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=401)

    token = auth.create_session(user, request.headers.get("user-agent", ""))
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,                       # из JavaScript куку не достать
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request):
    auth.drop_session(request.cookies.get(auth.COOKIE_NAME))
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return response


@app.get("/robots.txt")
def robots():
    """Полный запрет обхода: сервис не должен попадать в поисковую выдачу."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


ICONS = config.BASE_DIR / "static" / "icons"


@app.get("/favicon.ico")
def favicon():
    """Браузер спрашивает корневой /favicon.ico, даже когда в head есть ссылки."""
    return FileResponse(ICONS / "favicon-32.png", media_type="image/png")


@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    """Safari ищет иконку в корне, если не нашёл ссылку в head страницы."""
    return FileResponse(ICONS / "apple-touch-icon.png", media_type="image/png")


@app.get("/site.webmanifest")
def webmanifest():
    """Отдаём с корня: путь манифеста задаёт область видимости приложения."""
    return FileResponse(config.BASE_DIR / "static" / "site.webmanifest",
                        media_type="application/manifest+json")


# Порядок словаря задаёт порядок кнопок в карточке и порядок в фильтре,
# поэтому он повторяет реальный путь сделки, а не алфавит.
STATUSES = {
    "new": "Новый",
    "auto_checked": "Автопроверка",      # Claude поискал, человек ещё не смотрел
    "in_work": "В работе",
    "no_answer": "Не дозвонились",
    "contacted": "Связались",
    "audit": "Аудит",                    # согласились на бесплатный аудит сайта
    "proposal": "КП отправлено",         # аудит и смета у клиента, ждём решения
    "refused": "Отказ",
    "deal": "Сделка",
}

# Исход попытки дозвона и как он читается в ленте журнала.
CALL_OUTCOMES = {"no_answer": "не ответили", "answered": "дозвонились"}

# Статус, в который перевели лида, сам говорит об исходе звонка: нажимать
# ещё и «записать попытку» продажник не должен.
STATUS_CALL = {"no_answer": "no_answer", "contacted": "answered"}

SORTS = {
    "score": "score DESC, id DESC",
    # Неоценённые уходят в конец: COALESCE вместо NULL, иначе порядок неочевиден
    "priority": "COALESCE(priority, 0) DESC, score DESC, id DESC",
    "name": "name COLLATE NOCASE ASC",
    "new": "id DESC",
    "checked": "checked_at DESC",
}


@app.get("/api/config")
def get_config(request: Request):
    return {
        "city": config.CITY_NAME,
        "statuses": STATUSES,
        "reasons": {k: v[0] for k, v in scoring.REASONS.items()},
        "dadata_ready": bool(config.DADATA_TOKEN),
        "hot": scoring.HOT,
        "login": getattr(request.state, "login", ""),
        "is_admin": auth.is_admin(getattr(request.state, "login", "")),
        "editable": EDITABLE,
        "field_labels": FIELD_LABELS,

    }


# ── выборка лидов ────────────────────────────────────────────────────────────

def _where(reason, status, category, q, has, source, hidden="", org=""):
    sql, params = [], []

    # По умолчанию скрытые не показываем: их убрали именно чтобы не мешали.
    if hidden == "only":
        sql.append("hidden = 1")
    elif hidden != "all":
        sql.append("COALESCE(hidden, 0) = 0")

    if org == "active":
        sql.append("COALESCE(org_status,'') = 'ACTIVE'")
    elif org == "dead":
        sql.append("COALESCE(org_status,'') IN ('LIQUIDATED','BANKRUPT')")
    elif org == "liquidating":
        sql.append("COALESCE(org_status,'') = 'LIQUIDATING'")
    elif org == "unknown":
        sql.append("COALESCE(org_status,'') = ''")

    if reason:
        sql.append("reason_code = ?")
        params.append(reason)
    if status:
        sql.append("status = ?")
        params.append(status)
    if category:
        sql.append("category = ?")
        params.append(category)
    if source:
        sql.append("source = ?")
        params.append(source)
    if q:
        sql.append("(name LIKE ? OR address LIKE ? OR website LIKE ? OR inn LIKE ?)")
        params += [f"%{q}%"] * 4
    if has == "contact":
        sql.append("(COALESCE(phone,'') <> '' OR COALESCE(telegram,'') <> '' "
                   "OR COALESCE(vk,'') <> '' OR COALESCE(whatsapp,'') <> '')")
    elif has == "phone":
        sql.append("COALESCE(phone,'') <> ''")
    elif has == "telegram":
        sql.append("COALESCE(telegram,'') <> ''")
    elif has == "vk":
        sql.append("COALESCE(vk,'') <> ''")
    elif has == "none":
        sql.append("COALESCE(phone,'')='' AND COALESCE(telegram,'')='' "
                   "AND COALESCE(vk,'')='' AND COALESCE(whatsapp,'')=''")
    return ("WHERE " + " AND ".join(sql) if sql else ""), params


@app.get("/api/leads")
def get_leads(reason: str = "", status: str = "", category: str = "", q: str = "",
              has: str = "", source: str = "", hidden: str = "", org: str = "",
              sort: str = "score", limit: int = 100, offset: int = 0):
    where, params = _where(reason, status, category, q, has, source, hidden, org)
    order = SORTS.get(sort, SORTS["score"])
    c = db.conn()
    total = c.execute(f"SELECT COUNT(*) n FROM leads {where}", params).fetchone()["n"]
    rows = c.execute(
        f"SELECT * FROM leads {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [min(limit, 500), offset],
    ).fetchall()
    return {"total": total, "items": [_with_calls(db.row_to_dict(r)) for r in rows]}


@app.get("/api/stats")
def get_stats():
    c = db.conn()

    # Везде считаем только видимые: скрытые убраны намеренно
    # и не должны раздувать цифры на карточках.
    visible = "COALESCE(hidden, 0) = 0"

    def group(field):
        return {r[field] or "—": r["n"] for r in c.execute(
            f"SELECT {field}, COUNT(*) n FROM leads WHERE {visible} "
            f"GROUP BY {field} ORDER BY n DESC")}

    total = c.execute(f"SELECT COUNT(*) n FROM leads WHERE {visible}").fetchone()["n"]
    with_contact = c.execute(
        f"SELECT COUNT(*) n FROM leads WHERE {visible} AND (COALESCE(phone,'') <> '' "
        "OR COALESCE(telegram,'') <> '' OR COALESCE(vk,'') <> '')").fetchone()["n"]
    hot = c.execute(f"SELECT COUNT(*) n FROM leads WHERE {visible} AND score >= ?",
                    (scoring.HOT,)).fetchone()["n"]
    hidden_count = c.execute("SELECT COUNT(*) n FROM leads WHERE hidden = 1").fetchone()["n"]
    liquidated = c.execute(
        f"SELECT COUNT(*) n FROM leads WHERE {visible} AND "
        "COALESCE(org_status,'') IN ('LIQUIDATED','BANKRUPT')").fetchone()["n"]
    in_egrul = c.execute(
        f"SELECT COUNT(*) n FROM leads WHERE {visible} AND COALESCE(inn,'') <> ''").fetchone()["n"]

    return {
        "total": total,
        "with_contact": with_contact,
        "hot": hot,
        "hidden": hidden_count,
        "liquidated": liquidated,
        "in_egrul": in_egrul,
        "by_reason": group("reason_code"),
        "by_status": group("status"),
        "by_category": group("category"),
        "by_source": group("source"),
        "reason_titles": {k: v[0] for k, v in scoring.REASONS.items()},
        "last_run": db.last_run(),
    }


def _with_calls(d: dict) -> dict:
    """Дорисовывает время последнего звонка словами.

    В базе лежит UTC, а смотрит на него человек из города — перевод делаем
    на сервере, чтобы формат был один и в списке, и в карточке.
    """
    ts = db.call_ts(d.get("last_call_at"))
    d["call_count"] = d.get("call_count") or 0
    d["last_call_text"] = activity.stamp(ts)
    d["last_call_short"] = activity.day_text(ts)
    return d


@app.get("/api/lead/{lead_id}")
def get_lead(lead_id: int):
    r = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Лид не найден")
    d = _with_calls(db.row_to_dict(r))
    # Дата закрытия — не сумма: её видит и продажник, ему важно понимать,
    # что по лиду уже заплатили. Деньги остаются в разделе для владельца.
    d["deal_closed"] = _day_ru(db.first_payment_at(lead_id))
    return d


# Каким действием журнала записать правку каждого поля: «скрыт» и «статус»
# читаются в ленте куда быстрее, чем безликое «правка поля».
UPDATE_ACTIONS = {"status": "status", "priority": "priority",
                  "note": "note", "hidden_reason": "hide"}


def _record_call(login: str, lead, outcome: str) -> dict:
    """Попытка дозвона: в calls, в счётчики лида и в журнал.

    В UNDOABLE действие «звонок» не входит: откатывать факт звонка бессмысленно.
    """
    stats = db.add_call(lead["id"], login, outcome)
    history.log(login, "call", lead=lead, new=CALL_OUTCOMES[outcome])
    return _with_calls(stats)


def _note_view(n: dict) -> dict:
    """Заметка для интерфейса: время словами и пометка о переносе.

    Цвет автора считает фронтенд из логина — здесь он не нужен, а лишнее поле
    в ответе только сбивает с толку.
    """
    return {
        "id": n["id"],
        "login": n["login"],
        "text": n["text"],
        "ts": n["ts"],
        "when": activity.when_text(n["ts"]),
        # Автора у перенесённых заметок не было в журнале — врать не будем.
        "system": n["login"] == db.SYSTEM_LOGIN,
    }


def _add_note(login: str, lead, text: str) -> dict:
    n = db.add_note(lead["id"], login, text)
    # Без field: заметку нельзя откатить, её можно только удалить.
    history.log(login, "note", lead=lead, new=text)
    return _note_view(n)


@app.get("/api/lead/{lead_id}/notes")
def get_notes(lead_id: int):
    return {"items": [_note_view(n) for n in db.notes_for(lead_id)]}


@app.post("/api/lead/{lead_id}/notes")
def post_note(request: Request, lead_id: int, payload: dict = Body(...)):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "Пустую заметку сохранять незачем")
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(404, "Лид не найден")
    return _add_note(request.state.login, lead, text)


@app.delete("/api/note/{note_id}")
def delete_note(request: Request, note_id: int):
    """Свою заметку убирает автор, чужую — только владелец базы."""
    n = db.note_by_id(note_id)
    if not n:
        raise HTTPException(404, "Заметка не найдена")
    who = request.state.login
    if n["login"] != who and not auth.is_admin(who):
        raise HTTPException(403, "Чужую заметку может удалить только владелец базы")
    db.delete_note(n)
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (n["lead_id"],)).fetchone()
    history.log(who, "note_del", lead=lead, new=n["text"])
    return {"ok": True}


@app.post("/api/lead/{lead_id}")
def update_lead(request: Request, lead_id: int, payload: dict = Body(...)):
    # Заметка теперь живёт лентой: старые клиенты и форма «+ Объект» шлют её
    # тем же полем, поэтому здесь она уходит в ленту, а не переписывает поле.
    note_text = (payload.get("note") or "").strip() if "note" in payload else ""
    fields = {k: v for k, v in payload.items()
              if k in ("status", "hidden", "hidden_reason", "priority")}
    if "hidden" in fields:
        fields["hidden"] = 1 if fields["hidden"] else 0
    if "priority" in fields:
        value = fields["priority"]
        if value in ("", None):
            fields["priority"] = None            # оценку сняли
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise HTTPException(400, "Приоритет — число от 1 до 10")
            if not 1 <= value <= 10:
                raise HTTPException(400, "Приоритет — число от 1 до 10")
            fields["priority"] = value
    if not fields and not note_text:
        raise HTTPException(400, "Нечего обновлять")
    if "status" in fields and fields["status"] not in STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    c = db.conn()
    before = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not before:
        raise HTTPException(404, "Лид не найден")

    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
                  list(fields.values()) + [db.now(), lead_id])
        c.commit()

    who = request.state.login
    if note_text:
        _add_note(who, before, note_text)
    for field, value in fields.items():
        if history._text(before[field]) == history._text(value):
            continue
        action = ("hide" if value else "show") if field == "hidden"             else UPDATE_ACTIONS.get(field, "edit")
        history.log(who, action, lead=before, field=field,
                    old=before[field], new=value)
        # Статус меняется только если он действительно другой — значит и
        # попытка дозвона запишется ровно одна, а не на каждое нажатие.
        if field == "status" and value in STATUS_CALL:
            _record_call(who, before, STATUS_CALL[value])
    return {"ok": True}


@app.post("/api/lead/{lead_id}/call")
def add_call(request: Request, lead_id: int, payload: dict = Body(default={})):
    """Ещё одна попытка дозвона по лиду, у которого статус уже не меняется."""
    outcome = (payload or {}).get("outcome") or "no_answer"
    if outcome not in CALL_OUTCOMES:
        raise HTTPException(400, "Неизвестный исход звонка")
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(404, "Лид не найден")
    return _record_call(request.state.login, lead, outcome)


# ── сделки и платежи ─────────────────────────────────────────────────────────
#
# Весь раздел закрыт _admin_only: выручка и вознаграждение продажника — не та
# информация, которую сам продажник должен видеть в своей же карточке.

DEAL_KIND_LABELS = {"project": "проект", "retainer": "абонентка"}
PAYMENT_KIND_LABELS = {"first": "первая", "final": "финальная",
                       "monthly": "месячный", "other": "прочее"}

# Ниже этой доли первой оплаты сделка не идёт в зачёт премии продажнику —
# условие из договора, и о нём легко забыть при вводе.
BONUS_MIN_SHARE = 30

# Неразрывный пробел: сумма не должна разрываться переносом строки.
NBSP = " "


def _rub(n) -> str:
    """78500 -> «78 500 ₽». Пробел неразрывный: сумма не должна рваться переносом."""
    return f"{int(n or 0):,}".replace(",", NBSP) + NBSP + "₽"


def _money(value, what="Сумма") -> int:
    """Рубли целым числом: деньги через плавающую точку считать нельзя."""
    try:
        n = int(str(value).replace(" ", "").replace(NBSP, ""))
    except (TypeError, ValueError):
        raise HTTPException(400, f"{what} — целое число рублей")
    if n <= 0:
        raise HTTPException(400, f"{what} должна быть больше нуля")
    return n


def _iso_date(value, what="Дата") -> str:
    v = (value or "").strip()
    if not v:
        raise HTTPException(400, f"{what} обязательна")
    try:
        date.fromisoformat(v)
    except ValueError:
        raise HTTPException(400, f"{what} — в формате ГГГГ-ММ-ДД")
    return v


def _seller(value) -> str:
    """Продавец — из списка входящих в базу: отдельный справочник тут лишний."""
    v = (value or "").strip()
    if v not in auth.load_users():
        raise HTTPException(400, "Выберите продавца из списка")
    return v


def _deal_text(d) -> str:
    kind = DEAL_KIND_LABELS.get(d["kind"], d["kind"])
    tail = ", повторная" if d["is_repeat"] else ""
    return f"{kind}, {_rub(d['amount'])}{tail}"


def _day_ru(iso: str) -> str:
    """ISO-дату в привычный вид. Пустую строку не трогаем."""
    try:
        return date.fromisoformat(iso).strftime("%d.%m.%Y")
    except (ValueError, TypeError):
        return iso or ""


def _lead_or_404(lead_id: int):
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead:
        raise HTTPException(404, "Лид не найден")
    return lead


def _close_lead(login: str, lead_id: int) -> None:
    """Поступили деньги — лид закрыт.

    По договору сделка считается закрытой в дату фактического поступления
    первой оплаты, поэтому статус ставится сам. Обратно при удалении платежа
    не откатываем: вернуть лид в работу — решение человека.
    """
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not lead or lead["status"] == "deal":
        return
    db.conn().execute("UPDATE leads SET status='deal', updated_at=? WHERE id=?",
                      (db.now(), lead_id))
    db.conn().commit()
    history.log(login, "status", lead=lead, field="status",
                old=lead["status"], new="deal")


def _deal_view(d: dict) -> dict:
    """Сделка для интерфейса: подписи, даты и пометка про премию."""
    d["kind_text"] = DEAL_KIND_LABELS.get(d["kind"], d["kind"])
    d["contract_text"] = _day_ru(d.get("contract_at") or "")
    # Условие про 30 % — из договора на разовую разработку; к абонентке,
    # где платят помесячно, оно неприменимо.
    d["low_first"] = (d["kind"] == "project" and bool(d["payments"])
                      and d["first_share"] < BONUS_MIN_SHARE)
    for p in d["payments"]:
        p["kind_text"] = PAYMENT_KIND_LABELS.get(p["kind"], p["kind"])
        p["paid_text"] = _day_ru(p.get("paid_at") or "")
    return d


@app.get("/api/lead/{lead_id}/deals")
def get_deals(request: Request, lead_id: int):
    _admin_only(request)
    return {
        "items": [_deal_view(d) for d in db.deals_for(lead_id)],
        "sellers": sorted(auth.load_users()),
        "bonus_min_share": BONUS_MIN_SHARE,
    }


@app.post("/api/lead/{lead_id}/deals")
def create_deal(request: Request, lead_id: int, payload: dict = Body(...)):
    who = _admin_only(request)
    lead = _lead_or_404(lead_id)
    kind = (payload.get("kind") or "").strip()
    if kind not in db.DEAL_KINDS:
        raise HTTPException(400, "Тип сделки — проект или абонентка")
    deal_id = db.add_deal(
        lead_id, who,
        kind=kind,
        title=(payload.get("title") or "").strip(),
        amount=_money(payload.get("amount"), "Сумма договора"),
        contract_at=_iso_date(payload.get("contract_at"), "Дата договора"),
        owner_login=_seller(payload.get("owner_login")),
        is_repeat=payload.get("is_repeat"),
    )
    history.log(who, "deal", lead=lead,
                new="заведена: " + _deal_text(db.deal_by_id(deal_id)))
    return {"id": deal_id}


@app.post("/api/deal/{deal_id}")
def edit_deal(request: Request, deal_id: int, payload: dict = Body(...)):
    who = _admin_only(request)
    deal = db.deal_by_id(deal_id)
    if not deal:
        raise HTTPException(404, "Сделка не найдена")

    fields = {}
    if "kind" in payload:
        if payload["kind"] not in db.DEAL_KINDS:
            raise HTTPException(400, "Тип сделки — проект или абонентка")
        fields["kind"] = payload["kind"]
    if "amount" in payload:
        fields["amount"] = _money(payload["amount"], "Сумма договора")
    if "contract_at" in payload:
        fields["contract_at"] = _iso_date(payload["contract_at"], "Дата договора")
    if "owner_login" in payload:
        fields["owner_login"] = _seller(payload["owner_login"])
    if "is_repeat" in payload:
        fields["is_repeat"] = 1 if payload["is_repeat"] else 0
    if "title" in payload:
        fields["title"] = (payload["title"] or "").strip()
    if "state" in payload:
        if payload["state"] not in db.DEAL_STATES:
            raise HTTPException(400, "Неизвестное состояние сделки")
        fields["state"] = payload["state"]
    if not fields:
        raise HTTPException(400, "Нечего обновлять")

    db.update_deal(deal_id, fields)
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (deal["lead_id"],)).fetchone()
    history.log(who, "deal", lead=lead,
                new="изменена: " + _deal_text(db.deal_by_id(deal_id)))
    return {"ok": True}


@app.delete("/api/deal/{deal_id}")
def remove_deal(request: Request, deal_id: int):
    who = _admin_only(request)
    deal = db.deal_by_id(deal_id)
    if not deal:
        raise HTTPException(404, "Сделка не найдена")
    if db.payments_count(deal_id):
        raise HTTPException(400, "По сделке есть платежи — сначала удалите их")
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (deal["lead_id"],)).fetchone()
    db.delete_deal(deal_id)
    history.log(who, "deal", lead=lead, new="удалена: " + _deal_text(deal))
    return {"ok": True}


@app.post("/api/deal/{deal_id}/payments")
def create_payment(request: Request, deal_id: int, payload: dict = Body(...)):
    who = _admin_only(request)
    deal = db.deal_by_id(deal_id)
    if not deal:
        raise HTTPException(404, "Сделка не найдена")
    kind = (payload.get("kind") or "other").strip()
    if kind not in db.PAYMENT_KINDS:
        raise HTTPException(400, "Неизвестный вид платежа")
    amount = _money(payload.get("amount"), "Сумма платежа")
    paid_at = _iso_date(payload.get("paid_at"), "Дата поступления")

    pay_id = db.add_payment(deal_id, who, amount=amount, paid_at=paid_at, kind=kind,
                            note=(payload.get("note") or "").strip())
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?", (deal["lead_id"],)).fetchone()
    history.log(who, "payment", lead=lead, new=f"{_rub(amount)} от {_day_ru(paid_at)}")
    _close_lead(who, deal["lead_id"])
    return {"id": pay_id}


@app.delete("/api/payment/{payment_id}")
def remove_payment(request: Request, payment_id: int):
    who = _admin_only(request)
    pay = db.payment_by_id(payment_id)
    if not pay:
        raise HTTPException(404, "Платёж не найден")
    deal = db.deal_by_id(pay["deal_id"])
    lead = db.conn().execute("SELECT * FROM leads WHERE id=?",
                             (deal["lead_id"],)).fetchone() if deal else None
    db.delete_payment(payment_id)
    history.log(who, "payment", lead=lead,
                new=f"удалён платёж {_rub(pay['amount'])} от {_day_ru(pay['paid_at'])}")
    return {"ok": True}


def _find_duplicate(name, website):
    """Ищет уже заведённый объект — по домену или по названию."""
    c = db.conn()

    host = ""
    if website:
        host = (urlsplit(website if "//" in website else "http://" + website).hostname or "")
        host = host[4:] if host.startswith("www.") else host

    if host:
        row = c.execute(
            "SELECT id, name FROM leads WHERE website LIKE ? OR final_url LIKE ?",
            (f"%{host}%", f"%{host}%"),
        ).fetchone()
        if row:
            return row

    key = pipeline.norm_name(name)
    if key:
        for row in c.execute("SELECT id, name FROM leads"):
            if pipeline.norm_name(row["name"]) == key:
                return row
    return None


@app.post("/api/lead")
def create_lead(request: Request, payload: dict = Body(...)):
    """Добавляет объект руками: маркетолог нашёл его сам."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Нужно название объекта")

    website = utils.decode_idna((payload.get("website") or "").strip())
    if website and not utils.looks_like_url(website):
        raise HTTPException(400, "Не похоже на адрес сайта")

    dup = _find_duplicate(name, website)
    if dup:
        raise HTTPException(409, f"Такой объект уже есть: «{dup['name']}»")

    phones = utils.split_phones(payload.get("phone") or "")
    host = (urlsplit(website if "//" in website else "http://" + website).hostname
            if website else "") or ""

    lead = {
        "name": name,
        "category": (payload.get("category") or "").strip() or "Добавлен вручную",
        "address": (payload.get("address") or "").strip(),
        "lat": None, "lon": None,
        "source": "manual",
        "source_ref": (host or pipeline.norm_name(name))[:120],
        "source_detail": "Добавлен вручную",
        "website": website,
        # Адрес вписан руками — сбор не должен его переписывать
        "website_manual": 1 if website else 0,
        "phone": phones[0] if phones else "",
        "phones": phones,
        "telegram": (payload.get("telegram") or "").strip().lstrip("@"),
        "vk": (payload.get("vk") or "").strip(),
        "email": (payload.get("email") or "").strip(),
        "whatsapp": "",
        "contact_source": {k: "вручную" for k, v in (
            ("phone", phones), ("telegram", payload.get("telegram")),
            ("vk", payload.get("vk")), ("email", payload.get("email"))) if v},
    }

    processed = pipeline.process_lead(lead, do_dadata=bool(config.DADATA_TOKEN))

    c = db.conn()
    fields = [f for f in db.UPSERT_FIELDS + ["source", "source_ref", "website_manual",
                                             "created_at", "updated_at"]
              if f in processed or f in ("created_at", "updated_at")]
    processed["created_at"] = processed["updated_at"] = db.now()
    ph = ", ".join("?" * len(fields))
    cur = c.execute(f"INSERT INTO leads ({', '.join(fields)}) VALUES ({ph})",
                    [processed.get(f) for f in fields])
    c.commit()

    row = c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()
    history.log(request.state.login, "create", lead=row, new=row["name"])
    if (payload.get("note") or "").strip():
        _add_note(request.state.login, row, payload["note"].strip())
        row = c.execute("SELECT * FROM leads WHERE id=?", (cur.lastrowid,)).fetchone()
    return db.row_to_dict(row)


# Что можно править руками. Подпись — для формы в карточке.
EDITABLE = {
    "name": "Название",
    "category": "Категория",
    "address": "Адрес",
    "phone": "Телефон",
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "whatsapp": "WhatsApp",
    "email": "Почта",
    "org_name": "Название в реестре",
    "inn": "ИНН",
    "ogrn": "ОГРН",
    "director": "Руководитель",
    "director_post": "Должность",
    "okved": "ОКВЭД",
    "legal_address": "Юр. адрес",
}
CONTACT_FIELDS = ("phone", "telegram", "vk", "whatsapp", "email")

# Подписи полей для журнала изменений: в ленте должно быть написано
# «Телефон», а не phone. Правимые руками поля берём из EDITABLE, остальное —
# то, что вообще способно попасть в журнал.
FIELD_LABELS = dict(EDITABLE, **{
    "status": "Статус",
    "priority": "Приоритет",
    "hidden": "Скрыт из списка",
    "hidden_reason": "Причина скрытия",
    "note": "Заметка",
    "website": "Адрес сайта",
    "previous_website": "Прежний адрес",
    "final_url": "Конечный адрес",
    "site_status": "Состояние сайта",
    "reason_code": "Главный признак",
    "reason_text": "Главный признак",
    "score": "Балл",
    "phones": "Все телефоны",
    "contact_source": "Источник контактов",
    "manual_fields": "Защита ручных правок",
    "ai_summary": "Сводка автопроверки",
    "ai_problems": "Проблемы по автопроверке",
    "ai_sources": "Источники автопроверки",
    "ai_aggregators": "Агрегаторы",
    "ai_found_site": "Найденный сайт",
    "ai_director": "Руководитель по поиску",
    "ai_company": "Реквизиты по поиску",
    "ai_is_open": "Работает ли объект",
    "ai_checked_at": "Дата автопроверки",
    "ai_model": "Чем проверено",
    "ai_error": "Ошибка автопроверки",
})


@app.post("/api/lead/{lead_id}/edit")
def edit_lead(request: Request, lead_id: int, payload: dict = Body(...)):
    """Правит поля карточки руками и защищает их от следующего сбора."""
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Лид не найден")

    current = db.row_to_dict(row)
    changes, touched = {}, set(current.get("manual_fields") or [])

    for field, value in payload.items():
        if field not in EDITABLE:
            continue
        value = (value or "").strip()

        if field in ("phone", "whatsapp"):
            phones = utils.split_phones(value)
            value = phones[0] if phones else ""
            if value == "" and (payload.get(field) or "").strip():
                raise HTTPException(400, f"{EDITABLE[field]}: не похоже на российский номер")
        elif field == "telegram":
            value = value.lstrip("@").split("/")[-1]
        elif field == "vk":
            value = value.rstrip("/").split("/")[-1]

        if value != (current.get(field) or ""):
            changes[field] = value
            touched.add(field)

    if not changes:
        return current

    if "name" in changes and not changes["name"]:
        raise HTTPException(400, "Название не может быть пустым")

    # Телефоны в списке держим согласованными с основным номером
    if "phone" in changes:
        phones = [p for p in (current.get("phones") or []) if p != current.get("phone")]
        if changes["phone"]:
            phones.insert(0, changes["phone"])
        changes["phones"] = json.dumps(phones[:8], ensure_ascii=False)

    source = dict(current.get("contact_source") or {})
    for field in CONTACT_FIELDS:
        if field in changes:
            if changes[field]:
                source[field] = "вручную"
            else:
                source.pop(field, None)
    changes["contact_source"] = json.dumps(source, ensure_ascii=False)
    changes["manual_fields"] = json.dumps(sorted(touched), ensure_ascii=False)

    sets = ", ".join(f"{f}=?" for f in changes)
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              list(changes.values()) + [db.now(), lead_id])
    c.commit()

    # В журнал идут только те поля, которые человек и правда вписал. Служебная
    # пересборка phones, contact_source и manual_fields — не его правка.
    history.log_changes(request.state.login, row, changes, only=set(EDITABLE))

    # Контакты влияют на балл, поэтому пересчитываем
    return pipeline.rescore_one(lead_id)


@app.get("/api/lead/{lead_id}/brief")
def research_brief_text(lead_id: int):
    """Готовый запрос для чата с Claude по этому объекту."""
    row = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Лид не найден")
    return {"text": research_brief.build(db.row_to_dict(row), config.CITY_NAME)}


@app.post("/api/lead/{lead_id}/findings")
def save_findings(request: Request, lead_id: int, payload: dict = Body(...)):
    """Принимает JSON, который человек принёс из чата, и раскладывает по полям."""
    c = db.conn()
    before = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not before:
        raise HTTPException(404, "Лид не найден")

    found, error = research_brief.parse(payload.get("text"))
    if error:
        raise HTTPException(400, error)

    lead = pipeline.apply_research(lead_id, found)
    if not lead:
        raise HTTPException(404, "Лид не найден")

    # Автора ставим человека, а не «сбор»: он выбрал, какой ответ из чата
    # вставить, и отвечает за него. Каждое поле — своя строка, чтобы неудачную
    # вставку можно было вернуть по частям.
    after = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    history.log_changes(request.state.login, before,
                        {k: after[k] for k in after.keys()}, action="research")
    return lead


@app.post("/api/lead/{lead_id}/unlock")
def unlock_lead(request: Request, lead_id: int):
    """Снимает защиту ручных правок — сбор снова будет обновлять эти поля."""
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Лид не найден")
    c.execute("UPDATE leads SET manual_fields='', website_manual=0, updated_at=? WHERE id=?",
              (db.now(), lead_id))
    c.commit()
    history.log(request.state.login, "unlock", lead=row, old=row["manual_fields"] or "")
    return db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


@app.post("/api/lead/{lead_id}/website")
def change_website(request: Request, lead_id: int, payload: dict = Body(...)):
    """Ставит новый адрес сайта, помнит старый и сразу перепроверяет лид."""
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Лид не найден")

    new = utils.decode_idna((payload.get("website") or "").strip())
    old = (row["website"] or "").strip()

    if new == old:
        raise HTTPException(400, "Это тот же адрес")
    if new and not utils.looks_like_url(new):
        raise HTTPException(400, "Не похоже на адрес сайта")

    # Старый адрес не затираем пустым: если сайт убрали совсем,
    # прежняя ссылка всё равно пригодится для истории.
    previous = old or (row["previous_website"] or "")

    c.execute(
        "UPDATE leads SET website=?, previous_website=?, website_manual=?, updated_at=? "
        "WHERE id=?",
        (new, previous, 1, db.now(), lead_id),
    )
    c.commit()
    history.log(request.state.login, "website", lead=row,
                field="website", old=old, new=new)

    return pipeline.recheck_lead(lead_id, drop_site_contacts=True)


@app.post("/api/lead/{lead_id}/recheck")
def recheck(lead_id: int):
    """Перепроверить сайт лида, ничего не меняя в адресе."""
    lead = pipeline.recheck_lead(lead_id)
    if not lead:
        raise HTTPException(404, "Лид не найден")
    return lead


# ── запуск сбора ─────────────────────────────────────────────────────────────

@app.post("/api/run")
def start_run(request: Request, payload: dict = Body(default={})):
    if pipeline.state()["running"]:
        raise HTTPException(409, "Сбор уже идёт")
    history.log(request.state.login, "run")
    pipeline.run_async(
        use_osm=payload.get("use_osm", True),
        use_dadata=payload.get("use_dadata", True),
        do_whois=payload.get("do_whois", False),
        use_cache=payload.get("use_cache", True),
        dadata_discover=payload.get("dadata_discover", False),
    )
    return {"ok": True}


@app.get("/api/run/status")
def run_status():
    return pipeline.state()


@app.post("/api/rescore")
def rescore():
    """Пересчитать баллы после правки весов в scoring.py — без обхода сайтов."""
    if pipeline.state()["running"]:
        raise HTTPException(409, "Идёт сбор, дождитесь окончания")
    return {"ok": True, "updated": pipeline.rescore_all()}


# ── выгрузка и загрузка ──────────────────────────────────────────────────────

EXPORT_COLUMNS = [
    ("name", "Название"), ("category", "Категория"), ("reason_text", "Признак"),
    ("priority", "Мой приоритет"), ("score", "Балл"), ("phone", "Телефон"), ("telegram", "Telegram"),
    ("vk", "ВКонтакте"), ("whatsapp", "WhatsApp"), ("email", "Почта"),
    ("website", "Сайт"), ("address", "Адрес"), ("missing", "Чего не хватает"),
    ("pitch", "С чего начать разговор"), ("source_detail", "Откуда лид"),
    ("contact_source", "Откуда контакт"), ("org_name", "В реестре"),
    ("org_status_text", "Статус организации"), ("director", "Руководитель"),
    ("inn", "ИНН"), ("legal_address", "Юр. адрес"),
    ("dadata_confidence", "Точность совпадения"),
    ("status", "Статус"), ("note", "Заметка"),
]


@app.get("/api/export.csv")
def export_csv(reason: str = "", status: str = "", category: str = "",
               q: str = "", has: str = "", source: str = "", hidden: str = "",
               org: str = "", sort: str = "score"):
    where, params = _where(reason, status, category, q, has, source, hidden, org)
    # Тот же порядок, что и на экране: иначе выгрузка не совпадёт со списком
    order = SORTS.get(sort, SORTS["score"])
    rows = db.conn().execute(
        f"SELECT * FROM leads {where} ORDER BY {order}", params).fetchall()

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    w.writerow([title for _, title in EXPORT_COLUMNS])

    # Авторы последних заметок — одним запросом на всю выгрузку, а не по
    # запросу на строку: в выгрузку уходит вся база целиком.
    authors = db.last_note_authors()

    for r in rows:
        d = db.row_to_dict(r)
        line = []
        for key, _ in EXPORT_COLUMNS:
            v = d.get(key)
            if key == "missing":
                v = "; ".join(v or [])
            elif key == "contact_source":
                v = ", ".join(f"{k}: {s}" for k, s in (v or {}).items())
            elif key == "status":
                v = STATUSES.get(v, v)
            elif key == "note" and v:
                who = authors.get(d["id"])
                v = f"{who}: {v}" if who and who != db.SYSTEM_LOGIN else v
            line.append(v if v is not None else "")
        w.writerow(line)

    # utf-8-sig — чтобы Excel открыл кириллицу без плясок с кодировкой
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return StreamingResponse(
        data, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'})


@app.post("/api/import")
async def import_csv(request: Request, file: UploadFile = File(...)):
    """Ручной импорт: name;website;phone;address;category. Лиды сразу проверяются."""
    raw = (await file.read()).decode("utf-8-sig", errors="replace")
    dialect = csv.Sniffer().sniff(raw[:2000], delimiters=";,\t") \
        if raw.strip() else csv.excel
    reader = csv.DictReader(io.StringIO(raw), dialect=dialect)

    added = 0
    for i, row in enumerate(reader):
        row = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        name = row.get("name") or row.get("название")
        if not name:
            continue

        # Номера из чужого файла приводим к тому же виду, что и собранные сами,
        # иначе ссылки tel: в интерфейсе не работают.
        phones = utils.split_phones(row.get("phone") or row.get("телефон") or "")

        lead = {
            "name": name,
            "category": row.get("category") or row.get("категория") or "Импорт",
            "address": row.get("address") or row.get("адрес") or "",
            "website": utils.decode_idna(row.get("website") or row.get("сайт") or ""),
            "phone": phones[0] if phones else "",
            "phones": phones,
            "telegram": row.get("telegram") or "", "vk": row.get("vk") or "",
            "email": row.get("email") or "", "whatsapp": "",
            "lat": None, "lon": None,
            "source": "manual",
            "source_ref": f"{file.filename}:{i}:{name[:40]}",
            "source_detail": f"Ручной импорт из {file.filename}",
            "contact_source": {"phone": "импорт"} if phones else {},
        }
        db.upsert(pipeline.process_lead(lead))
        added += 1

    history.log(request.state.login, "import", new=f"{added}")
    return {"ok": True, "added": added}


# ── референсы: хорошие сайты по типам объектов ───────────────────────────────

@app.get("/refs")
def refs_page():
    return FileResponse(config.BASE_DIR / "static" / "refs.html")


@app.get("/api/refs")
def refs_list(category: str = ""):
    grouped = refs.listing(category)
    return {"groups": grouped,
            "total": sum(len(v) for v in grouped.values())}


@app.post("/api/refs")
def refs_add(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    url = utils.decode_idna((payload.get("url") or "").strip())
    category = (payload.get("category") or "").strip()

    if not name or not category:
        raise HTTPException(400, "Нужны название и категория")
    if not url or not utils.looks_like_url(url):
        raise HTTPException(400, "Не похоже на адрес сайта")

    strengths = payload.get("strengths") or []
    if isinstance(strengths, str):
        strengths = [s.strip() for s in strengths.splitlines() if s.strip()]

    return refs.save(category, name, url,
                     city=(payload.get("city") or "").strip(),
                     note=(payload.get("note") or "").strip(),
                     strengths=strengths)


@app.post("/api/refs/{ref_id}/recheck")
def refs_recheck(ref_id: int):
    ref = refs.recheck(ref_id)
    if not ref:
        raise HTTPException(404, "Референс не найден")
    return ref


@app.delete("/api/refs/{ref_id}")
def refs_delete(ref_id: int):
    if not refs.get(ref_id):
        raise HTTPException(404, "Референс не найден")
    refs.remove(ref_id)
    return {"ok": True}


# ── учёт рабочего времени ────────────────────────────────────────────────────

def _admin_only(request: Request):
    """Отчёт по сотрудникам видит только владелец базы."""
    login = getattr(request.state, "login", "")
    if not auth.is_admin(login):
        raise HTTPException(403, "Доступ только у владельца базы")
    return login


@app.post("/api/ping")
def ping(request: Request):
    """Сигнал «я сейчас работаю». Шлёт открытая вкладка раз в минуту."""
    activity.touch(getattr(request.state, "login", ""))
    return {"ok": True, "beat": activity.BEAT}


@app.get("/api/activity")
def activity_report(request: Request, login: str = "", days: int = 14):
    _admin_only(request)
    days = max(1, min(days, 180))
    since = activity.days_back(days)
    return {
        "since": since,
        "until": activity.today(),
        "days": days,
        "people": activity.people(),
        "report": activity.report(login.strip() or None, since=since),
        # Часы сами по себе ничего не говорят: рядом должно стоять, сколько
        # человек за это время реально сделал в базе.
        "edits": history.counts(since=since),
    }


@app.get("/activity")
def activity_page(request: Request):
    # Чужому логину страницы просто нет — возвращаем его к лидам.
    if not auth.is_admin(getattr(request.state, "login", "")):
        return RedirectResponse("/", status_code=302)
    return FileResponse(config.BASE_DIR / "static" / "activity.html")


# ── журнал изменений ─────────────────────────────────────────────────────────

# ── конверсии и показатели ───────────────────────────────────────────────────

@app.get("/metrics")
def metrics_page(request: Request):
    # Чужому логину раздела просто нет: по нему считают выручку и выплаты.
    if not auth.is_admin(getattr(request.state, "login", "")):
        return RedirectResponse("/", status_code=302)
    return FileResponse(config.BASE_DIR / "static" / "metrics.html")


@app.get("/api/metrics")
def metrics_report(request: Request,
                   day_from: str = Query("", alias="from"),
                   day_to: str = Query("", alias="to")):
    _admin_only(request)
    # Период по умолчанию — текущий месяц: именно им меряются планы и выплаты.
    preset = metrics.presets()["month"]
    day_from = _iso_date(day_from or preset[0], "Начало периода")
    day_to = _iso_date(day_to or preset[1], "Конец периода")
    if day_from > day_to:
        day_from, day_to = day_to, day_from
    return metrics.report(day_from, day_to)


@app.post("/api/settings")
def save_settings(request: Request, payload: dict = Body(...)):
    """Ставки, доли и пороги правит владелец: договор прямо предусматривает
    их пересмотр, и это не повод править код и катить деплой."""
    who = _admin_only(request)
    changed = []
    for key, value in (payload or {}).items():
        if key not in db.DEFAULT_SETTINGS:
            raise HTTPException(400, f"Неизвестная настройка: {key}")
        # Доли конвертов обязаны давать ровно сто: иначе раскладка поступлений
        # молча перестанет сходиться с выручкой.
        if key == "envelopes":
            if not isinstance(value, dict) or not value:
                raise HTTPException(400, "Конверты — список долей")
            total = sum(value.values())
            if round(total) != 100:
                raise HTTPException(400, f"Сумма долей конвертов должна быть 100, а не {total}")
        old = db.settings().get(key)
        if old == value:
            continue
        db.save_setting(key, value)
        changed.append(key)
        history.log(who, "settings",
                    new=f"{key}: {json.dumps(old, ensure_ascii=False)} → "
                        f"{json.dumps(value, ensure_ascii=False)}")
    return {"ok": True, "changed": changed}


@app.get("/api/lead/{lead_id}/history")
def lead_history(request: Request, lead_id: int, system: int = 0):
    """История одного объекта. Видна всем: знать, кто менял сайт, полезно обоим."""
    if not db.conn().execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise HTTPException(404, "Лид не найден")
    admin = auth.is_admin(request.state.login)
    return {"rows": history.for_lead(lead_id, with_system=bool(system), with_money=admin),
            "can_undo": admin}


@app.get("/api/history")
def history_feed(request: Request, login: str = "", action: str = "",
                 since: str = "", until: str = "", q: str = "",
                 system: int = 0, limit: int = 200, offset: int = 0):
    """Общая лента по всем объектам. Только владельцу базы."""
    _admin_only(request)
    data = history.feed(login=login.strip(), action=action.strip(),
                        since=since.strip(), until=until.strip(), q=q.strip(),
                        with_system=bool(system),
                        limit=max(1, min(limit, 500)), offset=max(0, offset))
    data["people"] = activity.people() + [history.SYSTEM_LABEL]
    data["actions"] = history.ACTIONS
    return data


@app.post("/api/history/{entry_id}/revert")
def history_revert(request: Request, entry_id: int):
    """Возвращает прежнее значение одной правки."""
    _admin_only(request)
    entry = db.conn().execute("SELECT field FROM history WHERE id=?",
                              (entry_id,)).fetchone()
    lead, error = history.revert(entry_id, request.state.login)
    if error:
        raise HTTPException(400, error)

    # Вернули адрес сайта — весь прежний аудит относится к чужому домену.
    # Перепроверяем сразу, иначе в карточке останется состояние не того сайта.
    if entry and entry["field"] == "website":
        return pipeline.recheck_lead(lead["id"])
    return db.row_to_dict(lead)


@app.get("/history")
def history_page(request: Request):
    if not auth.is_admin(getattr(request.state, "login", "")):
        return RedirectResponse("/", status_code=302)
    return FileResponse(config.BASE_DIR / "static" / "history.html")


# ── статика ──────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(config.BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
