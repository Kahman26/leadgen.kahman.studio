# -*- coding: utf-8 -*-
"""Источник №3 — единый реестр классифицированных средств размещения.

Ведёт Росаккредитация на портале «Гостеприимство» (tourism.fsa.gov.ru).
С 2025 года гостиница, база отдыха, гостевой дом или кемпинг без записи
в реестре работать не вправе, поэтому для размещения это самый полный
список: в нём есть и те, кого нет в OpenStreetMap. Сведения открытые —
витрина отдаёт их без входа, robots.txt сбор не запрещает.

По каждому объекту реестр даёт тип, адрес, владельца с ИНН и ОГРН,
а отдельным запросом — телефон, почту и сайт, которые владелец сам
указал для публикации. Бань и саун в реестре нет: это не размещение.

Координат реестр не хранит, а регион фильтрует только целиком. Чтобы
не тащить всю область, адрес переводится в координаты подсказками Dadata
(тот же суточный лимит, что и у сверки с ЕГРЮЛ) и сверяется с bbox
города. Координаты адреса кешируются навсегда — дом не переезжает.
"""

import json
import re
import time

import requests

import config
import utils

API = "https://tourism.fsa.gov.ru/api/v1"
GEO_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
UA = "kahman-leadgen/1.0 (+https://kahman.studio; lead research)"

STATUS_ACTIVE = 6              # «Действует»
PAGE = 500                     # больше витрина за раз не отдаёт
PAUSE = 0.3                    # между запросами контактов, сек

CACHE_DIR = config.BASE_DIR / ".cache"
CACHE_TTL = 24 * 3600          # список и контакты — сутки, как у OSM

# Тип объекта в реестре → категория ниши «Бронирование»
TYPES = {
    "Гостиница": "Отель",
    "Гостевой дом": "Гостевой дом",
    "База отдыха": "База отдыха",
    "Кемпинг": "Глэмпинг / кемпинг",
    "Санаторий": "База отдыха",
}
# Гостиницей в реестре записаны и хостелы, и апартаменты — уточняем по названию
NAME_HINTS = (("хостел", "Хостел"), ("hostel", "Хостел"), ("апарт", "Апартаменты"))


def _cache(name):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"registry_{name}.json"


def _read(path, ttl=None):
    if not path.exists() or (ttl and time.time() - path.stat().st_mtime > ttl):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _get(path, params=None):
    r = requests.get(f"{API}/{path}", params=params, timeout=60,
                     headers={"User-Agent": UA, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _showcase(progress, use_cache):
    """Все действующие объекты региона одним списком."""
    cache = _cache(f"showcase_{config.CITY_KLADR}")
    if use_cache:
        data = _read(cache, CACHE_TTL)
        if data is not None:
            if progress:
                progress(f"Реестр: {len(data)} объектов области — беру из кеша")
            return data

    items, page = [], 0
    while True:
        d = _get("resorts/hotels/showcase", {
            "statusIdList": STATUS_ACTIVE, "regionIdList": int(config.CITY_KLADR),
            "page": page, "limit": PAGE,
        })
        items += d.get("data") or []
        if progress:
            progress(f"Реестр: получено {len(items)} из {d.get('total')}")
        if not d.get("data") or len(items) >= (d.get("total") or 0):
            break
        page += 1
        time.sleep(PAUSE)
    _write(cache, items)
    return items


# Подсказки Dadata — это автодополнение, а не геокодер: адрес из реестра
# с индексом, «Российской Федерацией» и «м.о. Сысертский» они не узнают.
# Поэтому адрес чистится, и если дом не находится, запрос укорачивается
# с конца до посёлка или города — для фильтра «город и 30 км» этого хватает.
DROP = re.compile(r"^(\d{6}|российская федерация|свердловская\s+обл(асть|\.)?|"
                  r"обл\.?\s+свердловская)$", re.I)
CITY = re.compile(r"^(г\.?\s?о\.?|городской округ|муниципальное образование)\s+"
                  r"[«\"]?(город\s+)?екатеринбург[»\"]?$", re.I)
AREA = re.compile(r"^(?:м\.?\s?о\.?|м\.?\s?р-н|г\.?\s?о\.?|городской округ|"
                  r"муниципальн\w+ (?:округ|район))\s+[«\"]?(?:город\s+)?(.+?)[»\"]?$", re.I)
NUMBER = re.compile(r"\b(д|дом|строение|стр|зд|здание)\.?\s*(?=\d)", re.I)

# Районы вокруг города: объекту на «тер. базы отдыха Иволга» Dadata
# координат не даёт, а район у него известен — такие берём без координат
NEAR_AREAS = re.compile(r"екатеринбург|верхн\w+ пышм|березовск|берёзовск|арамил|"
                        r"среднеуральск|сысертск|белоярск", re.I)


def _part(p):
    p = re.sub(r"\s+", " ", p).strip()
    p = re.sub(r"гю(?=[А-ЯЁ])", "г ", p)       # опечатка «гюЕкатеринбург» встречается
    if DROP.match(p):
        return ""
    if CITY.match(p):
        return "г Екатеринбург"
    m = AREA.match(p)
    if m:
        name = m.group(1)
        return f"{name} р-н" if name.endswith(("ий", "ой")) else name
    return NUMBER.sub("", p)


def _variants(addr):
    parts = [x for x in (_part(p) for p in re.split(r"[,;]", addr)) if x]
    out = []
    for n in range(len(parts), 0, -1):
        v = ", ".join(parts[:n])
        if v not in out:
            out.append(v)
    return out[:5]


def _geocode(addresses, progress, token):
    """Координаты адресов через подсказки Dadata. None — адрес не найден."""
    cache = _cache("geo")
    known = _read(cache) or {}
    todo = [a for a in addresses if a and a not in known]
    if todo and progress:
        progress(f"Реестр: определяю координаты {len(todo)} адресов")
    headers = {"Authorization": f"Token {token}", "Content-Type": "application/json",
               "Accept": "application/json"}
    for i, addr in enumerate(todo, 1):
        try:
            point = None
            for query in _variants(addr):
                r = requests.post(GEO_URL, headers=headers, timeout=20, json={
                    "query": query, "count": 1,
                    "locations": [{"kladr_id": config.CITY_KLADR}],
                })
                if r.status_code in (401, 403):
                    raise PermissionError("Dadata отклонила токен")
                r.raise_for_status()
                found = (r.json().get("suggestions") or [{}])[0].get("data") or {}
                if found.get("geo_lat") and found.get("geo_lon"):
                    point = [float(found["geo_lat"]), float(found["geo_lon"])]
                    break
                time.sleep(0.05)
            known[addr] = point
        except requests.RequestException:
            continue                       # сбой сети — попробуем в следующий раз
        if i % 50 == 0:
            _write(cache, known)
            if progress:
                progress(f"Реестр: координаты {i} из {len(todo)}")
        time.sleep(0.1)
    _write(cache, known)
    return known


def _in_city(point):
    s, w, n, e = config.CITY_BBOX
    return bool(point) and s <= point[0] <= n and w <= point[1] <= e


def _contacts(ids, progress, use_cache):
    cache = _cache("contacts")
    known = (_read(cache, CACHE_TTL) if use_cache else None) or {}
    todo = [i for i in ids if i not in known]
    if todo and progress:
        progress(f"Реестр: забираю контакты {len(todo)} объектов")
    for n, rid in enumerate(todo, 1):
        try:
            known[rid] = _get(f"resorts/common/{rid}/contacts")
        except requests.RequestException:
            continue
        if n % 50 == 0:
            _write(cache, known)
            if progress:
                progress(f"Реестр: контакты {n} из {len(todo)}")
        time.sleep(PAUSE)
    _write(cache, known)
    return known


# Владелец, дописанный к названию: «Хостел "Арена" Индивидуальный предприниматель …»
OWNER_TAIL = re.compile(r"\s+(индивидуальный предприниматель|ип\s|ооо\b|общество с ограниченной|"
                        r"закрытое акционерное|открытое акционерное|акционерное общество|"
                        r"зао\b|оао\b|пао\b|ао\s).*$", re.I)


def _name(item):
    name = re.sub(r"\s+", " ", item.get("fullName") or "").strip()
    short = OWNER_TAIL.sub("", name).strip(" ,")
    return short or name


def _category(item):
    name = (item.get("fullName") or "").lower()
    for word, cat in NAME_HINTS:
        if word in name:
            return cat
    return TYPES.get((item.get("hotelType") or {}).get("name"), "Отель")


def fetch(progress=None, use_cache=True, token=None):
    """Действующие средства размещения в bbox города. Список сырых лидов."""
    token = token or config.DADATA_TOKEN
    try:
        items = _showcase(progress, use_cache)
    except (requests.RequestException, ValueError) as exc:
        if progress:
            progress(f"Реестр: портал не ответил — {type(exc).__name__}, источник пропущен")
        return []

    def address(item):
        return ((item.get("addressList") or [{}])[0].get("name") or "").strip()

    if token:
        geo = _geocode([address(i) for i in items], progress, token)
        local = [i for i in items if _in_city(geo.get(address(i)))]
        nearby = [i for i in items if geo.get(address(i)) is None
                  and NEAR_AREAS.search(address(i))]
        local += nearby
        lost = sum(1 for i in items if geo.get(address(i)) is None) - len(nearby)
        if progress and (nearby or lost):
            progress(f"Реестр: без координат {len(nearby) + lost} адресов — "
                     f"{len(nearby)} взял по району рядом с городом, {lost} пропускаю")
    else:
        # Без Dadata координат нет: берём только сам город, пригород теряется
        geo = {}
        local = [i for i in items if "екатеринбург" in address(i).lower()]
        if progress:
            progress("Реестр: нет DADATA_TOKEN — беру только адреса в Екатеринбурге")

    if progress:
        progress(f"Реестр: {len(local)} из {len(items)} объектов области в радиусе города")

    contacts = _contacts([i["id"] for i in local], progress, use_cache)

    leads, by_phone = [], {}
    # Порядок витрины меняется, а главным в сети должен оставаться один и тот же объект
    local.sort(key=lambda i: i.get("registerRecord") or "")
    for item in local:
        c = contacts.get(item["id"]) or {}
        phones = utils.split_phones(c.get("phone") or "")
        email = (c.get("email") or "").strip()
        site = utils.decode_idna((c.get("websiteAddress") or "").strip())
        if site and not utils.looks_like_url(site):
            site = ""
        point = geo.get(address(item)) or [None, None]
        # Сеть хостелов записана в реестре по объекту, а телефон у неё один:
        # звонить будут одному человеку, поэтому это один лид
        twin = by_phone.get(phones[0]) if phones else None
        if twin:
            twin["source_detail"] += f"; тот же владелец: {_name(item)}"
            continue
        src = {k: "реестр" for k, v in (("phone", phones), ("email", email)) if v}
        inn = item.get("ownerInn") or ""
        leads.append({
            "name": _name(item),
            "category": _category(item),
            "address": address(item),
            "lat": point[0], "lon": point[1],
            "source": "registry",
            "source_ref": item["id"],
            "source_detail": f"Реестр средств размещения, запись {item.get('registerRecord') or '—'}"
                             f" ({(item.get('hotelType') or {}).get('name') or 'тип не указан'})",
            "website": site,
            "phone": phones[0] if phones else "",
            "phones": phones,
            "telegram": "", "vk": "", "whatsapp": "",
            "email": email,
            "contact_source": src,
            # ИНН владелец указал сам — сверка с ЕГРЮЛ пойдёт по нему, а не
            # угадыванием по названию
            "inn": inn,
            "ogrn": item.get("ownerOgrn") or "",
            "org_name": item.get("ownerName") or "",
            "dadata_confidence": "high" if inn else "",
            "dadata_match": "ИНН из реестра средств размещения" if inn else "",
        })
        if phones:
            by_phone[phones[0]] = leads[-1]

    if progress:
        with_phone = sum(1 for l in leads if l["phone"])
        progress(f"Реестр: {len(leads)} объектов, с телефоном {with_phone}")
    return leads
