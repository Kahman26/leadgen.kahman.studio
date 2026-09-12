# -*- coding: utf-8 -*-
"""Источник №1 — OpenStreetMap через Overpass API.

Данные под лицензией ODbL: их можно легально хранить и использовать,
в отличие от API Яндекс.Карт (там хранение результатов прямо запрещено)
и 2ГИС (платный доступ с 2021 года).

У публичного Overpass жёсткий лимит на число слотов с одного IP, поэтому
запросы идут по одному, с паузами и экспоненциальным отступом при отказе,
а результат кешируется на диск — повторный запуск не дёргает сервер зря.
"""

import json
import time
from pathlib import Path

import requests

import config
import utils

ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

CACHE_DIR = config.BASE_DIR / ".cache"
CACHE_TTL = 24 * 3600          # сутки: OSM меняется медленно

PAUSE_BETWEEN = 5              # пауза между запросами к Overpass, сек
MAX_ATTEMPTS = 4

# Публичный Overpass просит описательный User-Agent, а не маскировку под браузер:
# с бразуерным UA запросы уходят в самую низкоприоритетную очередь.
OVERPASS_UA = "kahman-leadgen/1.0 (+https://kahman.studio; lead research)"

# Теги OSM, из которых достаём контакты. Порядок важен — первый непустой победит.
PHONE_TAGS = ["contact:phone", "phone", "contact:mobile"]
SITE_TAGS = ["contact:website", "website", "url", "contact:url"]
TG_TAGS = ["contact:telegram", "telegram"]
VK_TAGS = ["contact:vk", "vk", "contact:vkontakte"]
MAIL_TAGS = ["contact:email", "email"]
WA_TAGS = ["contact:whatsapp", "whatsapp"]


def _build_query(key, values):
    """Отдельный запрос на каждый ключ тега.

    Точные значения вместо регулярок — Overpass берёт их по индексу
    и не уходит в таймаут на большом bbox пригорода.
    """
    s, w, n, e = config.CITY_BBOX
    bbox = f"({s},{w},{n},{e})"
    parts = "".join(f'nwr["{key}"="{v}"]{bbox};' for v in values)
    return f"[out:json][timeout:90];({parts});out center tags;"


def _cache_path(key):
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"overpass_{key}.json"


def _request(key, query, progress=None, use_cache=True):
    cache = _cache_path(key)
    if use_cache and cache.exists() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        if progress:
            progress(f"OpenStreetMap: {key} — беру из кеша")
        return json.loads(cache.read_text(encoding="utf-8"))

    last_err = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        url = ENDPOINTS[(attempt - 1) % len(ENDPOINTS)]
        try:
            r = requests.post(url, data={"data": query}, timeout=150,
                              headers={"User-Agent": OVERPASS_UA})
            # 429/504 у Overpass означают «слоты заняты» — надо подождать, а не сдаваться.
            if r.status_code in (429, 502, 503, 504):
                raise requests.HTTPError(f"{r.status_code} от {url.split('/')[2]}")
            r.raise_for_status()
            data = r.json()
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except Exception as exc:
            last_err = exc
            wait = PAUSE_BETWEEN * attempt * 2
            if progress:
                progress(f"OpenStreetMap: {key} — попытка {attempt} не удалась "
                         f"({exc}), жду {wait} с")
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)

    # Если совсем не вышло — отдаём просроченный кеш, лишь бы не потерять запуск.
    if cache.exists():
        if progress:
            progress(f"OpenStreetMap: {key} — сервер недоступен, беру старый кеш")
        return json.loads(cache.read_text(encoding="utf-8"))

    raise RuntimeError(f"Overpass недоступен ({key}): {last_err}")


def _first(tags, keys):
    for k in keys:
        v = (tags.get(k) or "").strip()
        if v:
            return v
    return ""


def _category(tags):
    for key in ("tourism", "leisure", "amenity"):
        v = tags.get(key)
        if v and v in config.CATEGORY_MAP:
            return config.CATEGORY_MAP[v]
    return "Прочее размещение"


def _source_detail(tags):
    for key in ("tourism", "leisure", "amenity"):
        if tags.get(key):
            return f"OpenStreetMap, тег {key}={tags[key]}"
    return "OpenStreetMap"


def _address(tags):
    line = " ".join(p for p in (tags.get("addr:street", ""),
                                tags.get("addr:housenumber", "")) if p).strip()
    city = tags.get("addr:city") or ""
    if city and line:
        return f"{city}, {line}"
    return line or city


def _parse(elements):
    leads = []
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("operator") or "").strip()
        if not name:
            continue                       # безымянные точки для обзвона бесполезны

        contact_source = {}
        phones = utils.split_phones(_first(tags, PHONE_TAGS))
        phone = phones[0] if phones else ""
        tg = _first(tags, TG_TAGS)
        vk = _first(tags, VK_TAGS)
        mail = _first(tags, MAIL_TAGS)
        wa = _first(tags, WA_TAGS)
        for field, val in (("phone", phone), ("telegram", tg), ("vk", vk),
                           ("email", mail), ("whatsapp", wa)):
            if val:
                contact_source[field] = "OSM"

        leads.append({
            "name": name,
            "category": _category(tags),
            "address": _address(tags),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat"),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon"),
            "source": "osm",
            "source_ref": f'{el["type"]}/{el["id"]}',
            "source_detail": _source_detail(tags),
            "website": utils.decode_idna(_first(tags, SITE_TAGS)),
            "phone": phone,
            "phones": phones,
            "telegram": tg,
            "vk": vk,
            "email": mail,
            "whatsapp": wa,
            "contact_source": contact_source,
        })
    return leads


def fetch(progress=None, use_cache=True):
    """Забирает объекты ниши по bbox города. Возвращает список сырых лидов."""
    seen, leads = set(), []
    keys = list(config.OSM_FILTERS.items())

    for i, (key, values) in enumerate(keys):
        if progress:
            progress(f"OpenStreetMap: ищу {key} ({len(values)} типов)")
        data = _request(key, _build_query(key, values), progress, use_cache)

        for lead in _parse(data.get("elements", [])):
            if lead["source_ref"] in seen:
                continue                   # один объект может попасть под два тега
            seen.add(lead["source_ref"])
            leads.append(lead)

        if i < len(keys) - 1:
            time.sleep(PAUSE_BETWEEN)      # не занимаем все слоты публичного сервера

    if progress:
        progress(f"OpenStreetMap: {len(leads)} объектов с названием")
    return leads
