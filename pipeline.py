# -*- coding: utf-8 -*-
"""Оркестратор: собрать → склеить → проверить сайты → оценить → сохранить."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import config
import db
import scoring
from enrich import dadata_lookup, site_audit, whois_check
from sources import dadata, overpass

# ── состояние запуска для веб-интерфейса ─────────────────────────────────────
_state = {
    "running": False, "stage": "не запускался", "log": [],
    "done": 0, "total": 0, "run_id": None, "error": None,
}
_lock = threading.Lock()


def state():
    with _lock:
        return dict(_state, log=list(_state["log"][-60:]))


def _log(msg):
    with _lock:
        _state["stage"] = msg
        _state["log"].append(msg)
    print(msg, flush=True)


# ── склейка дублей между источниками ─────────────────────────────────────────
LEGAL_FORMS = re.compile(
    r"\b(ооо|оао|зао|пао|ао|ип|нко|тсж|ткс|гостиничный комплекс|отель|гостиница)\b", re.I)


def norm_name(name):
    s = (name or "").lower().replace("ё", "е")
    s = LEGAL_FORMS.sub(" ", s)
    s = re.sub(r"[^a-zа-я0-9]+", "", s)
    return s


def merge_sources(osm_leads, dadata_leads):
    """Реквизиты из ЕГРЮЛ подклеиваем к объекту с карты, а не плодим дубли."""
    by_name = {}
    for lead in osm_leads:
        key = norm_name(lead["name"])
        if key:
            by_name.setdefault(key, lead)

    result = list(osm_leads)
    merged = 0

    for lead in dadata_leads:
        key = norm_name(lead["name"])
        target = by_name.get(key) if key else None
        if target:
            for field in ("inn", "ogrn", "director", "okved", "registered_at"):
                if lead.get(field) and not target.get(field):
                    target[field] = lead[field]
            if lead.get("phone") and not target.get("phone"):
                target["phone"] = lead["phone"]
                target.setdefault("contact_source", {})["phone"] = "Dadata"
            target["source_detail"] += f" + ЕГРЮЛ (ИНН {lead.get('inn')})"
            merged += 1
        else:
            result.append(lead)

    return result, merged


# ── проверка одного лида ─────────────────────────────────────────────────────

EGRUL_FIELDS = ("inn", "ogrn", "org_name", "org_status", "org_status_text",
                "director", "director_post", "okved", "legal_address",
                "registered_at", "liquidated_at", "employee_count",
                "dadata_confidence", "dadata_match")


def enrich_from_egrul(lead):
    """Подтягивает данные ЕГРЮЛ. Молча пропускает, если совпадение неуверенное."""
    # По ИНН уточняем только тот, которому уже доверяли. Иначе получится
    # замкнутый круг: однажды ошибочно подобранный ИНН сам себя подтвердит.
    trusted_inn = lead.get("inn") if lead.get("dadata_confidence") == "high" else ""

    try:
        found = (dadata_lookup.by_inn(trusted_inn) if trusted_inn
                 else dadata_lookup.by_name(lead.get("name", ""), lead.get("address") or ""))
    except PermissionError:
        raise
    except Exception:
        return False                       # сеть или лимит — прежние данные не трогаем

    # Запрос прошёл. Если уверенного совпадения нет, старые реквизиты надо
    # убрать: иначе ошибка прошлого прогона останется в базе навсегда.
    for field in EGRUL_FIELDS:
        lead[field] = None if field == "employee_count" else ""

    if not found:
        return False
    lead.update(found)
    return True


def process_lead(lead, do_whois=False, do_dadata=False):
    """Аудит сайта + добор контактов со страницы + скоринг."""
    if do_dadata:
        enrich_from_egrul(lead)

    audit = site_audit.audit(lead.get("website", ""))

    contacts = audit.get("contacts") or {}
    src = dict(lead.get("contact_source") or {})
    for field in ("phone", "telegram", "vk", "whatsapp", "email"):
        if contacts.get(field) and not lead.get(field):
            lead[field] = contacts[field]
            src[field] = "сайт"
    lead["contact_source"] = src
    # Номера из OSM и найденные на сайте — в один список без дублей.
    phones = list(lead.get("phones") or [])
    for p in (contacts.get("phones") or []):
        if p not in phones:
            phones.append(p)
    if lead.get("phone") and lead["phone"] not in phones:
        phones.insert(0, lead["phone"])
    lead["phones"] = phones[:8]

    lead.update({
        "site_status": audit["site_status"],
        "http_code": audit["http_code"],
        "https": audit["https"],
        "mobile_ready": audit["mobile_ready"],
        "online_booking": audit["online_booking"],
        "booking_type": audit["booking_type"],
        "booking_engine": audit["booking_engine"],
        "cms": audit["cms"],
        "copyright_year": audit["copyright_year"] or None,
        "load_ms": audit["load_ms"],
        "final_url": audit["final_url"],
    })

    if do_whois and audit["site_status"] in ("ok", "blocked"):
        lead.update(whois_check.check(lead.get("website", "")))

    code, text, missing, pitch, score = scoring.score_lead(lead, audit)
    lead.update({
        "reason_code": code, "reason_text": text, "pitch": pitch, "score": score,
        "missing": json.dumps(missing, ensure_ascii=False),
        "phones": json.dumps(lead["phones"], ensure_ascii=False),
        "contact_source": json.dumps(src, ensure_ascii=False),
        "checked_at": db.now(),
    })
    return lead


# ── полный прогон ────────────────────────────────────────────────────────────

def run(use_osm=True, use_dadata=True, do_whois=False, use_cache=True,
        dadata_discover=False):
    with _lock:
        if _state["running"]:
            return
        _state.update(running=True, log=[], done=0, total=0, error=None)

    db.init()
    run_id = db.run_start()
    with _lock:
        _state["run_id"] = run_id

    try:
        osm_leads = overpass.fetch(_log, use_cache) if use_osm else []
        dadata_leads = dadata.fetch(_log) if dadata_discover else []

        leads, merged = merge_sources(osm_leads, dadata_leads)
        if merged:
            _log(f"Склейка: {merged} компаний из ЕГРЮЛ совпали с объектами на карте")

        db.run_update(run_id, found=len(leads), stage="проверка сайтов")
        with _lock:
            _state["total"] = len(leads)

        egrul = use_dadata and bool(config.DADATA_TOKEN)
        if use_dadata and not egrul:
            _log("ЕГРЮЛ пропускаю: не задан DADATA_TOKEN")

        _log(f"Проверяю {len(leads)} объектов в {config.HTTP_WORKERS} потоков"
             + (" + сверка с ЕГРЮЛ" if egrul else ""))

        # Пишем в базу по мере готовности: медленный сайт не тормозит очередь,
        # а обрыв на середине не обнуляет всю работу.
        added = updated = done = 0
        with ThreadPoolExecutor(max_workers=config.HTTP_WORKERS) as pool:
            futures = [pool.submit(process_lead, l, do_whois, egrul) for l in leads]
            for fut in as_completed(futures):
                done += 1
                try:
                    lead = fut.result()
                except Exception as exc:
                    _log(f"Пропускаю объект: {type(exc).__name__}: {exc}")
                    continue

                if db.upsert(lead) == "added":
                    added += 1
                else:
                    updated += 1

                with _lock:
                    _state["done"] = done
                if done % 25 == 0:
                    _log(f"Проверено {done} из {len(leads)}")

        db.run_update(run_id, added=added, updated=updated, audited=done)
        db.run_finish(run_id)
        _log(f"Готово: новых {added}, обновлено {updated}")

    except Exception as exc:
        db.run_finish(run_id, error=f"{type(exc).__name__}: {exc}")
        with _lock:
            _state["error"] = str(exc)
        _log(f"Ошибка: {exc}")
    finally:
        with _lock:
            _state["running"] = False


def recheck_lead(lead_id, drop_site_contacts=False):
    """Перепроверяет один лид по текущему адресу сайта и пересчитывает балл.

    `drop_site_contacts` нужен после смены домена: контакты, снятые со
    старого сайта, к новому отношения не имеют, а из OSM и ЕГРЮЛ — имеют.
    """
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return None

    lead = db.row_to_dict(row)

    if drop_site_contacts:
        src = dict(lead.get("contact_source") or {})
        for field in ("phone", "telegram", "vk", "whatsapp", "email"):
            if src.get(field) == "сайт":
                lead[field] = ""
                src.pop(field, None)
        lead["contact_source"] = src
        lead["phones"] = []

    processed = process_lead(lead, do_dadata=bool(config.DADATA_TOKEN))

    fields = [f for f in db.UPSERT_FIELDS if f in processed]
    sets = ", ".join(f"{f}=?" for f in fields)
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              [processed[f] for f in fields] + [db.now(), lead_id])
    c.commit()

    return db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


AUDIT_FIELDS = ("site_status", "http_code", "https", "mobile_ready", "online_booking",
                "booking_type", "booking_engine", "cms", "copyright_year", "load_ms",
                "final_url")


def rescore_one(lead_id):
    """Пересчитывает признак и балл одного лида по сохранённым данным."""
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return None

    lead = db.row_to_dict(row)
    audit = {f: lead.get(f) for f in AUDIT_FIELDS}
    audit["error"] = ""
    code, text, missing, pitch, score = scoring.score_lead(lead, audit)
    c.execute(
        "UPDATE leads SET reason_code=?, reason_text=?, missing=?, pitch=?, score=?, "
        "updated_at=? WHERE id=?",
        (code, text, json.dumps(missing, ensure_ascii=False), pitch, score,
         db.now(), lead_id),
    )
    c.commit()
    return db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


def rescore_all():
    """Пересчитывает признак и балл по уже сохранённым данным.

    Нужен после правки весов в scoring.py — сайты заново не обходятся.
    """
    db.init()
    c = db.conn()
    rows = c.execute("SELECT * FROM leads").fetchall()

    for r in rows:
        lead = db.row_to_dict(r)
        audit = {f: lead.get(f) for f in AUDIT_FIELDS}
        audit["error"] = ""
        code, text, missing, pitch, score = scoring.score_lead(lead, audit)
        c.execute(
            "UPDATE leads SET reason_code=?, reason_text=?, missing=?, pitch=?, score=? "
            "WHERE id=?",
            (code, text, json.dumps(missing, ensure_ascii=False), pitch, score, lead["id"]),
        )
    c.commit()
    return len(rows)


def run_async(**kw):
    t = threading.Thread(target=run, kwargs=kw, daemon=True)
    t.start()
    return t


if __name__ == "__main__":
    run()
