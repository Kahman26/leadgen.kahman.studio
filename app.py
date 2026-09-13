# -*- coding: utf-8 -*-
"""Веб-интерфейс сборщика лидов: API + отдача статики."""

import csv
import io
import json
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Body, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

import auth
import config
import db
import utils
import pipeline
import scoring
from enrich import research_brief

app = FastAPI(title="Сборщик лидов — ниша бронирования", docs_url=None, redoc_url=None)
db.init()
auth.cleanup_sessions()

# Сюда пускаем без входа: сама форма логина, её стили и ответ поисковикам.
PUBLIC_PATHS = {"/login", "/robots.txt", "/favicon.ico"}


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

STATUSES = {
    "new": "Новый",
    "auto_checked": "Автопроверка",      # Claude поискал, человек ещё не смотрел
    "in_work": "В работе",
    "contacted": "Связались",
    "refused": "Отказ",
    "deal": "Сделка",
}

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
        "editable": EDITABLE,

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
    return {"total": total, "items": [db.row_to_dict(r) for r in rows]}


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


@app.get("/api/lead/{lead_id}")
def get_lead(lead_id: int):
    r = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Лид не найден")
    return db.row_to_dict(r)


@app.post("/api/lead/{lead_id}")
def update_lead(lead_id: int, payload: dict = Body(...)):
    fields = {k: v for k, v in payload.items()
              if k in ("status", "note", "hidden", "hidden_reason", "priority")}
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
    if not fields:
        raise HTTPException(400, "Нечего обновлять")
    if "status" in fields and fields["status"] not in STATUSES:
        raise HTTPException(400, "Неизвестный статус")
    sets = ", ".join(f"{k}=?" for k in fields)
    c = db.conn()
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              list(fields.values()) + [db.now(), lead_id])
    c.commit()
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
def create_lead(payload: dict = Body(...)):
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
    if (payload.get("note") or "").strip():
        c.execute("UPDATE leads SET note=? WHERE id=?",
                  (payload["note"].strip(), cur.lastrowid))
    c.commit()

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


@app.post("/api/lead/{lead_id}/edit")
def edit_lead(lead_id: int, payload: dict = Body(...)):
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
def save_findings(lead_id: int, payload: dict = Body(...)):
    """Принимает JSON, который человек принёс из чата, и раскладывает по полям."""
    if not db.conn().execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise HTTPException(404, "Лид не найден")

    found, error = research_brief.parse(payload.get("text"))
    if error:
        raise HTTPException(400, error)

    lead = pipeline.apply_research(lead_id, found)
    if not lead:
        raise HTTPException(404, "Лид не найден")
    return lead


@app.post("/api/lead/{lead_id}/unlock")
def unlock_lead(lead_id: int):
    """Снимает защиту ручных правок — сбор снова будет обновлять эти поля."""
    c = db.conn()
    if not c.execute("SELECT 1 FROM leads WHERE id=?", (lead_id,)).fetchone():
        raise HTTPException(404, "Лид не найден")
    c.execute("UPDATE leads SET manual_fields='', website_manual=0, updated_at=? WHERE id=?",
              (db.now(), lead_id))
    c.commit()
    return db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


@app.post("/api/lead/{lead_id}/website")
def change_website(lead_id: int, payload: dict = Body(...)):
    """Ставит новый адрес сайта, помнит старый и сразу перепроверяет лид."""
    c = db.conn()
    row = c.execute("SELECT website, previous_website FROM leads WHERE id=?",
                    (lead_id,)).fetchone()
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
def start_run(payload: dict = Body(default={})):
    if pipeline.state()["running"]:
        raise HTTPException(409, "Сбор уже идёт")
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
            line.append(v if v is not None else "")
        w.writerow(line)

    # utf-8-sig — чтобы Excel открыл кириллицу без плясок с кодировкой
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    return StreamingResponse(
        data, media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'})


@app.post("/api/import")
async def import_csv(file: UploadFile = File(...)):
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

    return {"ok": True, "added": added}


# ── статика ──────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(config.BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
