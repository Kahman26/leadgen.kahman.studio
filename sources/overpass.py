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
import re
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import requests

import config
import niche_rules
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


def _source_detail(tags, keys):
    for key in keys:
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


def _parse(elements, niche):
    leads = []
    keys = list(niche_rules.get(niche)["osm"])
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or tags.get("operator") or "").strip()
        if not name:
            continue                       # безымянные точки для обзвона бесполезны

        # Слишком общий тег (shop=beauty) без подсказки в названии —
        # не наша ниша: парикмахерская к косметологии не относится
        category, fits = niche_rules.category_for(niche, tags, name)
        if not fits:
            continue

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
            "category": category,
            "address": _address(tags),
            "lat": el.get("lat") or (el.get("center") or {}).get("lat"),
            "lon": el.get("lon") or (el.get("center") or {}).get("lon"),
            "source": "osm",
            "source_ref": f'{el["type"]}/{el["id"]}',
            "source_detail": _source_detail(tags, keys),
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


def fetch(progress=None, use_cache=True, niche=niche_rules.DEFAULT):
    """Забирает объекты ниши по bbox города. Возвращает список сырых лидов.

    niche — код ниши из niche_rules: по нему выбираются теги и категории.
    """
    seen, leads = set(), []
    keys = list(niche_rules.get(niche)["osm"].items())

    for i, (key, values) in enumerate(keys):
        if progress:
            progress(f"OpenStreetMap: ищу {key} ({len(values)} типов)")
        # Кеш у каждой ниши свой: у автосервисов и отелей ключ amenity один,
        # а значения разные
        data = _request(f"{niche}_{key}", _build_query(key, list(values)), progress, use_cache)

        for lead in _parse(data.get("elements", []), niche):
            if lead["source_ref"] in seen:
                continue                   # один объект может попасть под два тега
            seen.add(lead["source_ref"])
            leads.append(lead)

        if i < len(keys) - 1:
            time.sleep(PAUSE_BETWEEN)      # не занимаем все слоты публичного сервера

    if not niche_rules.get(niche).get("chains"):
        leads, dropped = _drop_chains(leads)
        if dropped and progress:
            progress(f"OpenStreetMap: пропускаю сети — {dropped}")

    if progress:
        progress(f"OpenStreetMap: {len(leads)} объектов с названием")
    return leads


def _drop_chains(leads):
    """Убирает сети: три и больше точек с одним названием и общим сайтом.

    Отметка бренда (brand:wikidata) в OSM Екатеринбурга стоит редко, поэтому
    сеть узнаём по повторам. Одного названия мало: «Шиномонтаж» и «Цветы» —
    это разные маленькие точки без своего имени, их как раз берём. У сети
    же на всех точках один сайт. Возвращает (оставшиеся, «Инвитро 38, …»).
    """
    def norm(name):
        return re.sub(r"[^a-zа-я0-9]+", " ", name.lower().replace("ё", "е")).strip()

    def host(url):
        if not url:
            return ""
        h = urlsplit(url if "//" in url else "http://" + url).hostname or ""
        return h[4:] if h.startswith("www.") else h

    groups = {}
    for lead in leads:
        groups.setdefault(norm(lead["name"]), []).append(lead)

    chains = {}
    for key, group in groups.items():
        if len(group) < 3:
            continue
        hosts = Counter(host(l["website"]) for l in group if l["website"])
        if hosts and hosts.most_common(1)[0][1] >= 2:
            chains[key] = (group[0]["name"], len(group))

    kept = [l for l in leads if norm(l["name"]) not in chains]
    top = sorted(chains.values(), key=lambda x: -x[1])
    text = ", ".join(f"{name} {n}" for name, n in top[:8]) + (" …" if len(top) > 8 else "")
    return kept, text
