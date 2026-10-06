# -*- coding: utf-8 -*-
"""Карточка с карт против объекта, который уже есть в базе.

Кнопка «В leadgen» часто приносит объект, который сбор уже завёл, — но
с карты данные свежее и полнее. Здесь решается, что с ними делать:

- поле в базе пустое — заполняем с карт;
- совпадает — показывать нечего;
- не совпадает — ничего не перезаписываем, а кладём значение с карт
  рядом, в map_alt лида, с пометкой источника. В карточке его видно,
  и одним нажатием его можно сделать основным или убрать.

Телефоны — список: новые номера дописываются к нему, основной остаётся.
"""

import json
import re

import db
import history
import pipeline
import utils

FIELDS = (
    ("name", "Название"),
    ("website", "Сайт"),
    ("address", "Адрес"),
    ("telegram", "Telegram"),
    ("vk", "ВКонтакте"),
    ("whatsapp", "WhatsApp"),
    ("email", "Почта"),
    ("map_url", "Карточка на карте"),
)
LABELS = dict(FIELDS, phones="Телефоны")
CONTACTS = ("telegram", "vk", "whatsapp", "email")

# Что выкинуть из адреса перед сравнением: «620075, г. Екатеринбург, ул.
# Ленина, д. 1» и «улица Ленина, 1» — один и тот же дом
ADDR_NOISE = re.compile(
    r"\b(\d{6}|россия|российская федерация|свердловская|область|обл|екатеринбург|город|"
    r"г|ул|улица|д|дом|пр-т|проспект|пер|переулок|стр|строение|корп|корпус|"
    r"офис|оф|помещ|помещение|этаж)\b\.?", re.I)


def source_title(card):
    return {"2gis": "2ГИС", "yandex": "Яндекс Карты"}.get(card.get("source"), "форма объекта")


def card_values(card):
    """Значения карточки в том виде, в каком их хранит база."""
    tg = (card.get("telegram") or "").strip().lstrip("@").split("/")[-1]
    vk = (card.get("vk") or "").strip().rstrip("/").split("/")[-1]
    wa = utils.split_phones(card.get("whatsapp") or "")
    site = utils.decode_idna((card.get("website") or "").strip())
    return {
        "name": re.sub(r"\s+", " ", card.get("name") or "").strip(),
        "website": site if utils.looks_like_url(site) else "",
        "address": re.sub(r"\s+", " ", card.get("address") or "").strip(),
        "telegram": tg,
        "vk": vk,
        "whatsapp": wa[0] if wa else "",
        "email": (card.get("email") or "").strip().lower(),
        "map_url": card.get("map_url") or "",
        "phones": utils.split_phones(", ".join(card.get("phones") or [])),
    }


def _addr_key(value):
    s = ADDR_NOISE.sub(" ", (value or "").lower().replace("ё", "е"))
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def _same(field, base, new):
    if field == "name":
        a, b = pipeline.norm_name(base), pipeline.norm_name(new)
        return bool(a and b) and (a in b or b in a)
    if field == "website":
        return pipeline._host(base) == pipeline._host(new)
    if field == "address":
        a, b = _addr_key(base), _addr_key(new)
        return bool(a and b) and (a in b or b in a)
    return (base or "").strip().lower() == (new or "").strip().lower()


def _known_phones(lead):
    phones = set(lead.get("phones") or [])
    for f in ("phone", "whatsapp"):
        if lead.get(f):
            phones.add(lead[f])
    return phones


def diff(lead, card):
    """Что карточка с карт добавляет к лиду. lead — словарь из row_to_dict."""
    vals = card_values(card)
    alt = lead.get("map_alt") or {}
    manual = set(lead.get("manual_fields") or [])
    if lead.get("website_manual"):
        manual.add("website")

    rows = []
    for field, label in FIELDS:
        new = vals[field]
        if not new:
            continue
        base = lead.get(field) or ""
        if not base:
            kind = "fill"
        elif _same(field, base, new) or (
                # старый адрес переадресует на тот, что указан на карте
                field == "website" and lead.get("final_url") and _same(field, lead["final_url"], new)):
            kind = "same"
        elif (alt.get(field) or {}).get("value") and _same(field, alt[field]["value"], new):
            kind = "known"                  # уже лежит в данных с карт
        else:
            kind = "conflict"
        rows.append({"field": field, "label": label, "base": base, "card": new,
                     "kind": kind, "manual": field in manual})

    known = _known_phones(lead)
    phones_new = [p for p in vals["phones"] if p not in known]
    news = sum(1 for r in rows if r["kind"] in ("fill", "conflict")) + len(phones_new)
    return {"rows": rows, "phones_new": phones_new, "phones_card": vals["phones"],
            "source": source_title(card), "news": news}


def apply(lead_id, card, choices, add_phones, login):
    """Записывает выбор человека. choices: поле → fill | alt | replace | skip.

    Возвращает (новый адрес сайта или None, прежний адрес) — если сайт
    сменился, вызывающий его перепроверит.
    """
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    lead = db.row_to_dict(row)
    vals = card_values(card)
    src = source_title(card)
    stamp = {"source": src, "at": db.now(), "by": login}

    alt = dict(lead.get("map_alt") or {})
    manual = set(lead.get("manual_fields") or [])
    contact_source = dict(lead.get("contact_source") or {})
    changes, site = {}, None

    for field, _ in FIELDS:
        new, how = vals[field], choices.get(field) or "skip"
        if not new or how == "skip":
            continue
        if how == "alt":
            alt[field] = dict(stamp, value=new)
            continue
        # fill и replace: значение становится основным
        if field == "website":
            site = new
            continue
        changes[field] = new
        alt.pop(field, None)
        if field != "map_url":
            manual.add(field)               # чтобы сбор не вернул старое
        if field in CONTACTS:
            contact_source[field] = src

    if add_phones:
        known = _known_phones(lead)
        added = [p for p in vals["phones"] if p not in known]
        if added:
            phones = list(lead.get("phones") or [])
            if lead.get("phone") and lead["phone"] not in phones:
                phones.insert(0, lead["phone"])
            phones += added
            changes["phones"] = json.dumps(phones[:12], ensure_ascii=False)
            if not lead.get("phone"):
                changes["phone"] = added[0]
                contact_source["phone"] = src
                manual.add("phone")
            # Помечаем, какие номера пришли с карт, — карточка покажет
            had = (alt.get("phones") or {}).get("value") or []
            alt["phones"] = dict(stamp, value=had + [p for p in added if p not in had])

    changes["map_alt"] = json.dumps(alt, ensure_ascii=False)
    changes["manual_fields"] = json.dumps(sorted(manual), ensure_ascii=False)
    changes["contact_source"] = json.dumps(contact_source, ensure_ascii=False)

    sets = ", ".join(f"{f}=?" for f in changes)
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              list(changes.values()) + [db.now(), lead_id])
    c.commit()

    history.log_changes(login, row, changes, only=set(LABELS) | {"phone"})
    saved = [LABELS[f] for f in alt if f not in (lead.get("map_alt") or {})
             or alt[f] != lead["map_alt"].get(f)]
    if saved:
        history.log(login, "map", lead=row, new=f"{src}: сохранено рядом — {', '.join(saved)}")
    return site, lead.get("website") or ""


def drop_alt(lead_id, field, login, promote=False):
    """Убирает значение с карт из лида; promote — сначала делает его основным.

    Для сайта возвращает новый адрес: смену сайта проводит вызывающий,
    со всей её обвязкой (прежний адрес, перепроверка).
    """
    c = db.conn()
    row = c.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    lead = db.row_to_dict(row)
    alt = dict(lead.get("map_alt") or {})
    item = alt.pop(field, None)
    if not item:
        return None
    changes = {"map_alt": json.dumps(alt, ensure_ascii=False)}
    site = None

    if promote and field == "website":
        site = item["value"]
    elif promote and field != "phones":
        changes[field] = item["value"]
        if field != "map_url":
            manual = set(lead.get("manual_fields") or []) | {field}
            changes["manual_fields"] = json.dumps(sorted(manual), ensure_ascii=False)
        if field in CONTACTS:
            src = dict(lead.get("contact_source") or {}, **{field: item.get("source") or "карты"})
            changes["contact_source"] = json.dumps(src, ensure_ascii=False)

    sets = ", ".join(f"{f}=?" for f in changes)
    c.execute(f"UPDATE leads SET {sets}, updated_at=? WHERE id=?",
              list(changes.values()) + [db.now(), lead_id])
    c.commit()
    if promote:
        history.log_changes(login, row, changes, only=set(LABELS))
    else:
        value = ", ".join(item["value"]) if isinstance(item["value"], list) else item["value"]
        history.log(login, "map", lead=row, new=f"Убрано с карт — {LABELS.get(field, field)}: {value}")
    return site
