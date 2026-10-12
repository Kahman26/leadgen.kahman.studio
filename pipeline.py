# -*- coding: utf-8 -*-
"""Оркестратор: собрать → склеить → проверить сайты → оценить → сохранить."""

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

import config
import db
import history
import niche_rules
import scoring
import utils
from enrich import aggregators, dadata_lookup, site_audit, whois_check
from sources import dadata, overpass, registry

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


def _host(url):
    host = (urlsplit(url if "//" in url else "http://" + url).hostname or "") if url else ""
    return host[4:] if host.startswith("www.") else host


def find_duplicate(name, website, phones=(), map_url=""):
    """Ищет уже заведённый объект — по карточке на картах, домену, телефону
    или названию. Телефон и карточка важны для объектов с карт: там название
    часто записано иначе, чем в OSM («Форд 96» и «Ford-96»)."""
    c = db.conn()

    if map_url:
        row = c.execute("SELECT id, name FROM leads WHERE map_url = ?", (map_url,)).fetchone()
        if row:
            return row

    host = _host(website)
    if host:
        row = c.execute(
            "SELECT id, name FROM leads WHERE website LIKE ? OR final_url LIKE ?",
            (f"%{host}%", f"%{host}%"),
        ).fetchone()
        if row:
            return row

    # Телефоны хранятся одним видом (+7XXXXXXXXXX), поэтому LIKE по строке
    # JSON-списка находит номер, даже если у объекта он не основной
    for phone in phones:
        row = c.execute("SELECT id, name FROM leads WHERE phone = ? OR phones LIKE ?",
                        (phone, f'%"{phone}"%')).fetchone()
        if row:
            return row

    key = norm_name(name)
    if key:
        for row in c.execute("SELECT id, name FROM leads"):
            if norm_name(row["name"]) == key:
                return row
    return None


def merge_registry(leads, registry_leads):
    """Объекты реестра подклеиваем к уже собранным, а не дублируем.

    Совпадение ищем по сайту, телефону и названию — в реестре объект часто
    записан полнее («Бутик отель "ПЛЮШ"» против «Плюш» в OSM), поэтому одно
    название ловит не всех. Совпавшему объекту реестр дописывает пустые
    контакты и ИНН владельца. Что не совпало с текущим сбором, сверяем
    с базой: объект, заведённый раньше вручную или с карты, второй раз
    не появится. Возвращает (список, склеено, пропущено как уже известные).
    """
    by_host, by_phone, by_name = {}, {}, {}
    for lead in leads:
        if _host(lead.get("website")):
            by_host.setdefault(_host(lead["website"]), lead)
        for p in [lead.get("phone")] + list(lead.get("phones") or []):
            if p:
                by_phone.setdefault(p, lead)
        if norm_name(lead["name"]):
            by_name.setdefault(norm_name(lead["name"]), lead)

    result, merged, known = list(leads), 0, 0
    c = db.conn()
    for lead in registry_leads:
        target = (by_host.get(_host(lead["website"]))
                  or next((by_phone[p] for p in lead["phones"] if p in by_phone), None)
                  or by_name.get(norm_name(lead["name"])))
        if target:
            for field in ("website", "phone", "email"):
                if lead.get(field) and not target.get(field):
                    target[field] = lead[field]
                    target.setdefault("contact_source", {})[field] = "реестр"
            phones = list(target.get("phones") or [])
            target["phones"] = phones + [p for p in lead["phones"] if p not in phones]
            if lead.get("inn") and not target.get("inn"):
                for field in ("inn", "ogrn", "org_name", "dadata_confidence", "dadata_match"):
                    target[field] = lead[field]
            target["source_detail"] = (target.get("source_detail") or "") + " + " + lead["source_detail"]
            merged += 1
            continue

        mine = c.execute("SELECT 1 FROM leads WHERE source=? AND source_ref=?",
                         (lead["source"], lead["source_ref"])).fetchone()
        if not mine and find_duplicate(lead["name"], lead["website"], lead["phones"]):
            known += 1
            continue
        result.append(lead)

    return result, merged, known


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
                 else dadata_lookup.by_name(lead.get("name", ""), lead.get("address") or "",
                                            okved=niche_rules.okved_for(lead.get("niche_id"))))
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


def city_only_matches():
    """Сверки с ЕГРЮЛ, где «адрес совпал» лишь словом «Екатеринбург» — до
    исправления street_tokens такие засчитывались как уверенные."""
    from enrich.dadata_lookup import PLACE_WORDS
    out = []
    for row in db.conn().execute("SELECT id, dadata_match FROM leads "
                                 "WHERE COALESCE(dadata_match, '') LIKE '%адрес совпадает:%'"):
        m = re.search(r"адрес совпадает: ([^;]+)", row["dadata_match"])
        if m and {t.strip() for t in m.group(1).split(",")} <= PLACE_WORDS:
            out.append(row["id"])
    return out


def reverify_egrul(ids):
    """Заново сверяет с ЕГРЮЛ — без доверия к прежнему ИНН — и пересчитывает балл."""
    c = db.conn()
    fixed = 0
    for lead_id in ids:
        row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            continue
        lead = db.row_to_dict(row)
        lead["dadata_confidence"] = ""           # прежнему совпадению не верим
        enrich_from_egrul(lead)
        changes = {f: lead.get(f) for f in EGRUL_FIELDS}
        sets = ", ".join(f"{f}=?" for f in changes)
        c.execute(f"UPDATE leads SET {sets} WHERE id=?", list(changes.values()) + [lead_id])
        c.commit()
        history.log_changes(history.SYSTEM, row, changes, action="recheck",
                            only={"org_name", "org_status", "inn"})
        rescore_one(lead_id)
        fixed += 1
    return fixed


def process_lead(lead, do_whois=False, do_dadata=False):
    """Аудит сайта + добор контактов со страницы + скоринг."""
    if do_dadata:
        enrich_from_egrul(lead)

    # Страница площадки в поле «сайт» — не сайт объекта: уводим её в площадки
    split_platform_site(lead)
    audit = site_audit.audit(lead.get("website", ""))
    if audit["site_status"] == "ok":
        lead["aggregators"] = aggregators.merge(lead.get("aggregators"), audit["aggregators"],
                                                replace_source="сайт")

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
        "parked_reason": audit.get("parked_reason") or "",
    })

    if do_whois and audit["site_status"] in ("ok", "blocked"):
        lead.update(whois_check.check(lead.get("website", "")))

    code, text, missing, pitch, score = scoring.score_lead(lead, audit)
    lead.update({
        "reason_code": code, "reason_text": text, "pitch": pitch, "score": score,
        "missing": json.dumps(missing, ensure_ascii=False),
        "phones": json.dumps(lead["phones"], ensure_ascii=False),
        "contact_source": json.dumps(src, ensure_ascii=False),
        "aggregators": json.dumps(aggregators.as_list(lead.get("aggregators")),
                                  ensure_ascii=False),
        "checked_at": db.now(),
    })
    return lead


PLATFORM_SOURCE = {"osm": "OSM", "dadata": "ЕГРЮЛ", "registry": "реестр", "manual": "вручную"}


def split_platform_site(lead):
    """Если в поле «сайт» стоит площадка — переносит её в список площадок."""
    site = lead.get("website") or ""
    own, found = aggregators.split(site, source=PLATFORM_SOURCE.get(lead.get("source"), "сбор"))
    if site and aggregators.match(site):
        lead["website"] = own
        lead["aggregators"] = aggregators.merge(lead.get("aggregators"), found)
        # Контакты «с сайта» сняты со страницы площадки — это её телефон и её
        # ВКонтакте, а не объекта. Звонить туда незачем
        src = lead.get("contact_source") or {}
        if isinstance(src, str):
            src = json.loads(src or "{}")
        for field in ("phone", "telegram", "vk", "whatsapp", "email"):
            if src.get(field) == "сайт":
                lead[field] = ""
                src.pop(field)
        lead["contact_source"] = src
        lead["phones"] = [lead["phone"]] if lead.get("phone") else []
        # Страница ВКонтакте вместо сайта — заодно и контакт
        vk = re.search(r"vk\.(?:com|ru|link)/([A-Za-z0-9_.]+)", site)
        if vk and not lead.get("vk"):
            lead["vk"] = vk.group(1)
        return True
    return False


def move_platform_sites():
    """Разовая чистка: площадки, записанные в поле «сайт» до того, как
    сервис научился их отличать. Повторный запуск ничего не делает."""
    c = db.conn()
    moved = 0
    for row in c.execute("SELECT * FROM leads WHERE COALESCE(website,'') <> ''").fetchall():
        if not aggregators.match(row["website"]):
            continue
        lead = db.row_to_dict(row)
        split_platform_site(lead)
        c.execute(
            "UPDATE leads SET website=?, aggregators=?, phone=?, phones=?, telegram=?, vk=?, "
            "whatsapp=?, email=?, contact_source=?, site_status='none', final_url='', "
            "http_code=NULL, https=0, mobile_ready=0, online_booking=0, booking_type='none', "
            "booking_engine='', cms='', copyright_year=NULL, load_ms=NULL, parked_reason='' "
            "WHERE id=?",
            (lead["website"], json.dumps(lead["aggregators"], ensure_ascii=False),
             lead.get("phone") or "", json.dumps(lead["phones"], ensure_ascii=False),
             lead.get("telegram") or "", lead.get("vk") or "", lead.get("whatsapp") or "",
             lead.get("email") or "", json.dumps(lead["contact_source"], ensure_ascii=False),
             row["id"]))
        c.commit()
        history.log(history.SYSTEM, "edit", lead=row, field="website",
                    old=row["website"], new="")
        rescore_one(row["id"])
        moved += 1
    return moved


# ── полный прогон ────────────────────────────────────────────────────────────

def run(use_osm=True, use_dadata=True, do_whois=False, use_cache=True,
        dadata_discover=False, use_registry=True, niche=niche_rules.DEFAULT):
    """Сбор по одной нише: niche — код из niche_rules."""
    with _lock:
        if _state["running"]:
            return
        _state.update(running=True, log=[], done=0, total=0, error=None)

    db.init()
    run_id = db.run_start()
    with _lock:
        _state["run_id"] = run_id

    try:
        niche_id = niche_rules.niche_id_of(niche)
        if not niche_id:
            raise RuntimeError(f"Нет ниши с кодом {niche} — сбор некуда складывать")
        _log(f"Ниша: {niche_rules.title_of(niche_id)}")

        osm_leads = overpass.fetch(_log, use_cache, niche=niche) if use_osm else []
        dadata_leads = dadata.fetch(_log, niche=niche) if dadata_discover else []

        leads, merged = merge_sources(osm_leads, dadata_leads)
        if merged:
            _log(f"Склейка: {merged} компаний из ЕГРЮЛ совпали с объектами на карте")

        # Реестр есть только у размещения: бань, автосервисов и клиник в нём нет
        if use_registry and niche_rules.get(niche).get("registry"):
            reg_leads = registry.fetch(_log, use_cache)
            leads, merged, known = merge_registry(leads, reg_leads)
            _log(f"Склейка с реестром: {merged} совпали с картой и ЕГРЮЛ, "
                 f"{known} уже были в базе, {len(reg_leads) - merged - known} новых")
        # Ниша нужна уже при проверке: от неё зависят ОКВЭД для сверки
        # с ЕГРЮЛ и то, проверять ли на сайте бронирование
        for lead in leads:
            lead["niche_id"] = niche_id

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


def set_website(login, row, new):
    """Новый адрес сайта: старый уходит в «Было», лид перепроверяется."""
    c = db.conn()
    lead_id = row["id"]
    old = (row["website"] or "").strip()
    # Старый адрес не затираем пустым: если сайт убрали совсем,
    # прежняя ссылка всё равно пригодится для истории.
    previous = old or (row["previous_website"] or "")
    c.execute(
        "UPDATE leads SET website=?, previous_website=?, website_manual=?, updated_at=? "
        "WHERE id=?",
        (new, previous, 1, db.now(), lead_id),
    )
    c.commit()
    history.log(login, "website", lead=row, field="website", old=old, new=new)
    return recheck_lead(lead_id, drop_site_contacts=True)


# ── карточки Яндекс Карт из файла сборщика ───────────────────────────────────

# Рубрика с карт → категория ниши «Бронирование». Проверяются по порядку
MAP_RUBRICS = [
    (r"бан[яи]|саун|хаммам|хамам|терм", "Баня / сауна"),
    (r"хостел", "Хостел"),
    (r"апарт", "Апартаменты"),
    (r"гостевой дом", "Гостевой дом"),
    (r"база отдыха|турбаз|загородн", "База отдыха"),
    (r"глэмпинг|кемпинг", "Глэмпинг / кемпинг"),
    (r"гостиниц|отел", "Отель"),
]


def _map_category(rubric, niche):
    if niche != "booking":
        return ""
    for pattern, title in MAP_RUBRICS:
        if re.search(pattern, rubric or "", re.I):
            return title
    return ""


def _same_place(lead_id, card, vals):
    """Совпадение по названию — тот же ли это объект: рядом или по тому же адресу."""
    import mapmerge
    from sources import yandex_maps
    row = db.conn().execute("SELECT lat, lon, address FROM leads WHERE id=?", (lead_id,)).fetchone()
    try:
        lon, lat = map(float, (card.get("coords") or "").split(","))
        if row["lat"] and row["lon"]:
            return yandex_maps._distance_km((lat, lon), (row["lat"], row["lon"])) <= 1.0
    except ValueError:
        pass
    return bool(row["address"] and vals["address"]
                and mapmerge._same("address", row["address"], vals["address"]))


def _card_lead(card, niche_id, niche):
    """Новый лид из карточки сборщика."""
    import mapmerge
    vals = mapmerge.card_values(card)
    try:
        lon, lat = map(float, (card.get("coords") or "").split(","))
    except ValueError:
        lat = lon = None
    src = {f: "Яндекс Карты" for f in ("telegram", "vk", "whatsapp", "email") if vals.get(f)}
    if vals["phones"]:
        src["phone"] = "Яндекс Карты"
    detail = "Яндекс Карты" + (f": {card['rubric']}" if card.get("rubric") else "")
    if card.get("rating"):
        detail += f", рейтинг {card['rating']}"
    return {
        "name": vals["name"], "address": vals["address"], "lat": lat, "lon": lon,
        "source": "yandex", "source_ref": str(card.get("id") or vals["map_url"]),
        "source_detail": detail, "map_url": vals["map_url"],
        "website": vals["website"], "aggregators": vals["aggregators"],
        "phone": vals["phones"][0] if vals["phones"] else "", "phones": vals["phones"],
        "telegram": vals["telegram"], "vk": vals["vk"], "whatsapp": vals["whatsapp"],
        "email": vals["email"], "contact_source": src,
        "category": _map_category(card.get("rubric"), niche), "niche_id": niche_id,
    }


def import_cards(cards, niche=niche_rules.DEFAULT, login=history.SYSTEM):
    """Карточки с карт — в базу. Новые заводятся и проверяются как при сборе.

    Уже известный объект (по ссылке на карточку, сайту, телефону, названию)
    дополняется без перезаписи — по тем же правилам, что окно «Уже есть
    в базе»: пустое заполняется, несовпавшее ложится рядом с пометкой
    «с карт», телефоны дописываются. Название базы не трогаем."""
    import mapmerge
    with _lock:
        if _state["running"]:
            return
        _state.update(running=True, log=[], done=0, total=0, error=None)
    db.init()
    run_id = db.run_start()
    with _lock:
        _state["run_id"] = run_id
    try:
        niche_id = niche_rules.niche_id_of(niche)
        if not niche_id:
            raise RuntimeError(f"Нет ниши с кодом {niche}")
        _log(f"Яндекс Карты: {len(cards)} карточек → «{niche_rules.title_of(niche_id)}»")

        new, merged, same, closed = [], 0, 0, 0
        for card in cards:
            if card.get("closed"):
                closed += 1
                continue
            vals = mapmerge.card_values(card)
            if not vals["name"]:
                continue
            c = db.conn()
            mine = c.execute("SELECT id FROM leads WHERE source='yandex' AND source_ref=?",
                             (str(card.get("id") or ""),)).fetchone()
            # Карточка, сайт, телефон — надёжно. Одно название («Сауна», «Баня»)
            # — нет: его подтверждаем местом, иначе склеим разные объекты
            dup = mine or find_duplicate("", vals["website"], vals["phones"], vals["map_url"])
            if not dup:
                cand = find_duplicate(vals["name"], "", [], "")
                if cand and _same_place(cand["id"], card, vals):
                    dup = cand
            if not dup:
                new.append(_card_lead(card, niche_id, niche))
                continue
            lead = db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (dup["id"],)).fetchone())
            d = mapmerge.diff(lead, card)
            if not d["news"]:
                same += 1
                continue
            choices = {r["field"]: ("fill" if r["kind"] == "fill" else "alt")
                       for r in d["rows"] if r["kind"] in ("fill", "conflict") and r["field"] != "name"}
            site, _ = mapmerge.apply(dup["id"], card, choices, True, login)
            if site:
                set_website(login, c.execute("SELECT * FROM leads WHERE id=?", (dup["id"],)).fetchone(), site)
            else:
                rescore_one(dup["id"])
            merged += 1

        _log(f"Уже в базе: дополнено {merged}, без новых данных {same}; "
             f"закрыты навсегда {closed}; новых {len(new)} — проверяю сайты")
        db.run_update(run_id, found=len(cards), stage="проверка сайтов")
        with _lock:
            _state["total"] = len(new)

        egrul = bool(config.DADATA_TOKEN)
        added = done = 0
        with ThreadPoolExecutor(max_workers=config.HTTP_WORKERS) as pool:
            futures = [pool.submit(process_lead, l, False, egrul) for l in new]
            for fut in as_completed(futures):
                done += 1
                try:
                    lead = fut.result()
                except Exception as exc:
                    _log(f"Пропускаю объект: {type(exc).__name__}: {exc}")
                    continue
                if db.upsert(lead) == "added":
                    added += 1
                with _lock:
                    _state["done"] = done
                if done % 25 == 0:
                    _log(f"Проверено {done} из {len(new)}")

        db.run_update(run_id, added=added, updated=merged, audited=done)
        db.run_finish(run_id)
        _log(f"Готово: новых {added}, дополнено {merged}, без изменений {same}")
    except Exception as exc:
        db.run_finish(run_id, error=f"{type(exc).__name__}: {exc}")
        with _lock:
            _state["error"] = str(exc)
        _log(f"Ошибка: {exc}")
    finally:
        with _lock:
            _state["running"] = False


def import_cards_async(cards, **kw):
    t = threading.Thread(target=import_cards, args=(cards,), kwargs=kw, daemon=True)
    t.start()
    return t


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

    # Перепроверка идёт мимо upsert, поэтому журнал зовём здесь же: иначе
    # смена состояния сайта окажется единственным изменением без автора.
    history.log_changes(history.SYSTEM, row, {f: processed[f] for f in fields},
                        action="recheck", only=history.WATCHED)

    return db.row_to_dict(c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone())


AUDIT_FIELDS = ("site_status", "http_code", "https", "mobile_ready", "online_booking",
                "booking_type", "booking_engine", "cms", "copyright_year", "load_ms",
                "final_url", "parked_reason")


def _clean(value):
    value = (value or "").strip() if isinstance(value, str) else ""
    return "" if value.lower() in ("null", "none", "нет", "не найдено") else value


def apply_research(lead_id, found):
    """Записывает результат автопроверки. Ничего не затирает молча.

    Контакты ставятся только в пустые поля и помечаются источником «Claude» —
    маркетолог должен видеть, что это не подтверждённые данные, а находка,
    которую он собирается проверить. Поля, исправленные руками, не трогаем.
    """
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        return None
    lead = db.row_to_dict(row)
    protected = set(lead.get("manual_fields") or [])

    contacts = found.get("contacts") or {}
    director = found.get("director") or {}
    company = found.get("company") or {}
    changes = {
        "ai_summary": _clean(found.get("summary")),
        "ai_problems": json.dumps([p for p in (found.get("problems") or []) if p],
                                  ensure_ascii=False),
        "ai_sources": json.dumps([s for s in (found.get("sources") or []) if s][:12],
                                 ensure_ascii=False),
        "ai_aggregators": json.dumps(found.get("aggregators") or [], ensure_ascii=False),
        "ai_found_site": _clean(found.get("website")),
        "ai_company": json.dumps(company, ensure_ascii=False) if company else "",
        "ai_is_open": {True: "true", False: "false"}.get(found.get("is_open"), ""),
        "ai_checked_at": db.now(),
        "ai_model": found.get("_model") or "чат",
        "ai_error": "",
        "status": "auto_checked",
    }

    source = dict(lead.get("contact_source") or {})
    for field, key in (("phone", "phone"), ("telegram", "telegram"),
                       ("vk", "vk"), ("email", "email")):
        value = _clean(contacts.get(key))
        if not value or lead.get(field) or field in protected:
            continue                       # своё и ручное важнее найденного
        if field == "phone":
            phones = utils.split_phones(value)
            value = phones[0] if phones else ""
        elif field == "telegram":
            value = value.lstrip("@").split("/")[-1]
        elif field == "vk":
            value = value.rstrip("/").split("/")[-1]
        if value:
            changes[field] = value
            source[field] = "Claude"

    # Руководителя из поиска держим отдельно от реестрового: поле director
    # принадлежит ЕГРЮЛ и очищается при каждой сверке, да и доверие к нему другое.
    boss = _clean(director.get("name"))
    if boss:
        post = _clean(director.get("post"))
        changes["ai_director"] = f"{boss}, {post}" if post else boss

    # Реквизиты из поиска кладём только в пустые поля. Данные Dadata точнее:
    # затирать сверку с ЕГРЮЛ находкой из выдачи нельзя.
    for field, key in (("inn", "inn"), ("ogrn", "ogrn"),
                       ("org_name", "legal_name"),
                       ("legal_address", "legal_address"), ("address", "address")):
        value = _clean(company.get(key))
        if value and not lead.get(field) and field not in protected:
            changes[field] = value

    # Дополнительные телефоны дописываем к уже известным, не теряя старые.
    extra_phones = [p for p in (company.get("phones") or []) if _clean(p)]
    if extra_phones:
        known = list(lead.get("phones") or [])
        for raw in extra_phones:
            for p in utils.split_phones(raw):
                if p and p not in known:
                    known.append(p)
        if known != list(lead.get("phones") or []):
            changes["phones"] = json.dumps(known[:12], ensure_ascii=False)

    changes["contact_source"] = json.dumps(source, ensure_ascii=False)

    # Новый адрес ставим, только если своего рабочего нет: у живого сайта
    # менять адрес по находке модели нельзя.
    new_site = _clean(found.get("website"))
    replace_site = (new_site and "website" not in protected
                    and utils.looks_like_url(new_site)
                    and new_site.rstrip("/") != (lead.get("website") or "").rstrip("/")
                    and lead.get("site_status") in ("none", "dead", "blocked", "parked"))
    if replace_site:
        changes["previous_website"] = lead.get("website") or lead.get("previous_website") or ""
        changes["website"] = utils.decode_idna(new_site)
        changes["website_manual"] = 1      # сбор не должен вернуть мёртвый адрес

    sets = ", ".join(f"{f}=?" for f in changes)
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              list(changes.values()) + [db.now(), lead_id])
    c.commit()

    # Сайт сменился — перепроверяем его и пересчитываем балл
    return recheck_lead(lead_id) if replace_site else rescore_one(lead_id)


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
