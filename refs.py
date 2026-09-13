# -*- coding: utf-8 -*-
"""Референсы — хорошие сайты по каждому типу объектов.

Нужны для двух вещей: понимать, как должен выглядеть сайт в этой нише,
и показывать клиенту живой пример вместо описания словами.

«Хороший» здесь не на слово: каждый референс проходит тот же аудит, что
и лиды, и рядом с оценкой лежат объективные факты — адаптив, движок
бронирования, скорость. Если сайт перестанет им соответствовать, это
будет видно после перепроверки.
"""

import json
import re

import requests
from bs4 import BeautifulSoup

import config
import db
from enrich import site_audit

# Некоторые сайты отвечают дольше, чем терпит обход лидов: там короткий
# таймаут нужен ради скорости на трёхстах объектах, здесь спешить незачем.
REF_TIMEOUT = 40


def page_title(url):
    try:
        r = requests.get(url, timeout=REF_TIMEOUT,
                         headers={"User-Agent": config.USER_AGENT})
        tag = BeautifulSoup(site_audit.decode_body(r), "lxml").find("title")
        return re.sub(r"\s+", " ", tag.get_text()).strip()[:120] if tag else ""
    except Exception:
        return ""


def audit(url):
    """Проверяет референс с увеличенным таймаутом."""
    original = config.HTTP_TIMEOUT
    config.HTTP_TIMEOUT = REF_TIMEOUT
    try:
        return site_audit.audit(url)
    finally:
        config.HTTP_TIMEOUT = original


def save(category, name, url, city="", note="", strengths=(), check=True):
    """Добавляет или обновляет референс. Возвращает строку из базы."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Нужен адрес сайта")

    data = {
        "category": category, "name": name, "url": url, "city": city,
        "note": note,
        "strengths": json.dumps([s for s in strengths if s], ensure_ascii=False),
    }

    if check:
        a = audit(url)
        data.update({
            "site_status": a["site_status"], "http_code": a["http_code"],
            "https": a["https"], "mobile_ready": a["mobile_ready"],
            "booking_type": a["booking_type"], "booking_engine": a["booking_engine"],
            "cms": a["cms"], "load_ms": a["load_ms"],
            "page_title": page_title(url) if a["site_status"] == "ok" else "",
            "checked_at": db.now(),
        })

    c = db.conn()
    row = c.execute("SELECT id FROM refs WHERE url=?", (url,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in data)
        c.execute(f"UPDATE refs SET {sets} WHERE id=?",
                  list(data.values()) + [row["id"]])
        ref_id = row["id"]
    else:
        data["created_at"] = db.now()
        ph = ", ".join("?" * len(data))
        cur = c.execute(f"INSERT INTO refs ({', '.join(data)}) VALUES ({ph})",
                        list(data.values()))
        ref_id = cur.lastrowid
    c.commit()
    return get(ref_id)


def recheck(ref_id):
    row = db.conn().execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
    if not row:
        return None
    a = audit(row["url"])
    c = db.conn()
    c.execute(
        "UPDATE refs SET site_status=?, http_code=?, https=?, mobile_ready=?, "
        "booking_type=?, booking_engine=?, cms=?, load_ms=?, page_title=?, "
        "checked_at=? WHERE id=?",
        (a["site_status"], a["http_code"], a["https"], a["mobile_ready"],
         a["booking_type"], a["booking_engine"], a["cms"], a["load_ms"],
         page_title(row["url"]) if a["site_status"] == "ok" else row["page_title"],
         db.now(), ref_id),
    )
    c.commit()
    return get(ref_id)


def _to_dict(row):
    d = dict(row)
    d["strengths"] = db.json_list(d.get("strengths"))
    return d


def get(ref_id):
    row = db.conn().execute("SELECT * FROM refs WHERE id=?", (ref_id,)).fetchone()
    return _to_dict(row) if row else None


def listing(category=""):
    """Все референсы, сгруппированные по категории."""
    where, params = ("WHERE category = ?", [category]) if category else ("", [])
    rows = db.conn().execute(
        f"SELECT * FROM refs {where} ORDER BY category, name COLLATE NOCASE",
        params).fetchall()

    grouped = {}
    for row in rows:
        grouped.setdefault(row["category"], []).append(_to_dict(row))
    return grouped


def remove(ref_id):
    c = db.conn()
    c.execute("DELETE FROM refs WHERE id=?", (ref_id,))
    c.commit()
