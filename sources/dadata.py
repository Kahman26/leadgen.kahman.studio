# -*- coding: utf-8 -*-
"""Источник №2 — Dadata (данные ЕГРЮЛ/ЕГРИП).

Бесплатно до 10 000 запросов в сутки. Даёт то, чего нет в OSM:
ИНН, ОКВЭД, дату регистрации и ФИО руководителя — с директором
разговор идёт совсем иначе, чем с администратором на ресепшене.

Важное ограничение: это API подсказок, а не выгрузка. Оно не умеет
отдавать «всех подряд» и возвращает максимум 20 записей на запрос,
поэтому перебираем список формулировок из config.DADATA_QUERIES.
"""

import time

import requests

import config

MAX_COUNT = 20


def _is_niche(okved):
    return bool(okved) and okved.startswith(config.NICHE_OKVED_PREFIXES)


def _clean_phone(value):
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return ""


def fetch(progress=None, token=None):
    """Ищет компании ниши в городе. Возвращает список сырых лидов."""
    token = token or config.DADATA_TOKEN
    if not token:
        if progress:
            progress("Dadata: токен не задан, источник пропущен")
        return []

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Token {token}",
    }

    leads, seen = [], set()
    total = len(config.DADATA_QUERIES)

    for i, query in enumerate(config.DADATA_QUERIES, 1):
        if progress:
            progress(f"Dadata: «{query}» ({i}/{total}), найдено {len(leads)}")
        body = {
            "query": query,
            "count": MAX_COUNT,
            "status": ["ACTIVE"],
            "locations": [{"kladr_id": config.CITY_KLADR}],
        }
        try:
            r = requests.post(config.DADATA_URL, json=body, headers=headers, timeout=20)
            if r.status_code == 403:
                if progress:
                    progress("Dadata: токен отклонён (403) — проверьте ключ")
                break
            if r.status_code == 429:
                time.sleep(3)
                continue
            r.raise_for_status()
            suggestions = r.json().get("suggestions", [])
        except requests.RequestException as exc:
            if progress:
                progress(f"Dadata: ошибка на «{query}» — {type(exc).__name__}")
            continue

        for s in suggestions:
            d = s.get("data") or {}
            inn = d.get("inn")
            if not inn or inn in seen:
                continue
            seen.add(inn)

            okved = d.get("okved") or ""
            if not _is_niche(okved):
                continue                    # отсекаем всё, что не про размещение

            mgmt = d.get("management") or {}
            address = ((d.get("address") or {}).get("value")) or ""
            state = d.get("state") or {}
            reg = state.get("registration_date")
            reg_iso = ""
            if reg:
                reg_iso = time.strftime("%Y-%m-%d", time.gmtime(reg / 1000))

            phone = _clean_phone((d.get("phones") or [{}])[0].get("value")
                                 if d.get("phones") else "")

            leads.append({
                "name": (s.get("value") or "").strip(),
                "category": "По ЕГРЮЛ",
                "address": address,
                "lat": None, "lon": None,
                "source": "dadata",
                "source_ref": inn,
                "source_detail": f"Dadata / ЕГРЮЛ, ОКВЭД {okved}, запрос «{query}»",
                "inn": inn,
                "ogrn": d.get("ogrn") or "",
                "director": mgmt.get("name") or "",
                "okved": okved,
                "registered_at": reg_iso,
                "website": "",
                "phone": phone,
                "telegram": "", "vk": "", "email": "", "whatsapp": "",
                "contact_source": {"phone": "Dadata"} if phone else {},
            })

        time.sleep(0.25)                    # бережём суточный лимит и не долбим API

    if progress:
        progress(f"Dadata: {len(leads)} компаний ниши")
    return leads
