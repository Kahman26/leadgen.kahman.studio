# -*- coding: utf-8 -*-
"""Сверка лида с ЕГРЮЛ через Dadata.

Задача — не «найти хоть что-нибудь», а не испортить базу. Названия на карте
торговые («Паровозов», «Черника»), в ЕГРЮЛ они звучат иначе, и одноимённых
фирм в городе десятки. Поэтому совпадение принимается только при нескольких
независимых подтверждениях, а уровень доверия сохраняется рядом с данными.

Что даёт Dadata: ИНН, ОГРН, статус (действует / ликвидирована), юридический
адрес, ФИО руководителя, ОКВЭД, дату регистрации.
Чего не даёт: сайт, телефон и почту — эти поля на обычном тарифе пустые.
"""

import re
import time

import requests

import config

SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"
FIND_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"

# ОКВЭД ниши по умолчанию — бронирования. Для остальных ниш коды приходят
# из niche_rules: совпадение по названию с компанией чужого профиля — это
# посторонняя компания.
NICHE_OKVED = ("55.", "68.20", "68.10", "96.04", "93.29", "79.")

STATUS_TEXT = {
    "ACTIVE": "действует",
    "LIQUIDATING": "в стадии ликвидации",
    "LIQUIDATED": "ликвидирована",
    "REORGANIZING": "реорганизуется",
    "BANKRUPT": "банкротство",
}

# Слова, которые ничего не говорят о конкретной компании
NOISE = re.compile(
    r"\b(ооо|оао|зао|пао|ао|ип|нао|чоу|ано|чу|мбу|гбу|муп|гуп|"
    r"отель|гостиница|гостиничный|комплекс|апарт|апартаменты|хостел|"
    r"база|отдыха|сауна|баня|бани|дом|гостевой|усадьба|парк)\b", re.I)

STREET_NOISE = re.compile(
    r"\b(г|город|ул|улица|пр|пр-кт|проспект|пер|переулок|ш|шоссе|"
    r"д|дом|стр|строение|к|корп|корпус|литер|пом|оф|офис|бульвар|б-р|"
    r"наб|набережная|пл|площадь|тракт)\b\.?", re.I)


def norm_name(value):
    s = (value or "").lower().replace("ё", "е")
    s = NOISE.sub(" ", s)
    return re.sub(r"[^a-zа-я0-9]+", "", s)


def street_tokens(address):
    """Оставляет от адреса только значимые слова: название улицы и номер дома."""
    s = (address or "").lower().replace("ё", "е")
    s = STREET_NOISE.sub(" ", s)
    s = re.sub(r"[^a-zа-я0-9 ]+", " ", s)
    return {t for t in s.split() if len(t) > 2}


def _ts_to_date(value):
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(value / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def _score_match(lead_name, lead_address, suggestion, okved_prefixes=NICHE_OKVED):
    """Складывает независимые подтверждения.

    Возвращает (баллы, чем подтверждено, есть ли профильное подтверждение).
    Совпадения названия и города мало: «Уралэлектромедь» так цепляется
    к одноимённому заводу, а «Лайнер» — к любому ООО «Лайнер» в городе.
    """
    data = suggestion.get("data") or {}
    points, why = 0, []
    strong = False              # ОКВЭД из ниши или совпавший адрес

    a, b = norm_name(lead_name), norm_name(suggestion.get("value"))
    if a and b:
        if a == b:
            points += 3
            why.append("название совпадает")
        elif a in b or b in a:
            points += 1
            why.append("название частично совпадает")

    okved = data.get("okved") or ""
    if okved_prefixes and okved.startswith(tuple(okved_prefixes)):
        points += 2
        strong = True
        why.append(f"ОКВЭД {okved} из ниши")

    addr = ((data.get("address") or {}).get("value")) or ""
    if lead_address and addr:
        common = street_tokens(lead_address) & street_tokens(addr)
        if common:
            points += 3
            strong = True
            why.append("адрес совпадает: " + ", ".join(sorted(common)))
    if config.CITY_NAME.lower() in addr.lower():
        points += 1
        why.append(f"адрес в {config.CITY_NAME}е")

    # Подстраховка на случай, если фильтр региона снова перестанет работать
    if addr and config.CITY_REGION.lower() not in addr.lower()             and config.CITY_NAME.lower() not in addr.lower():
        return 0, ["адрес в другом регионе"], False

    return points, why, strong


def _pack(suggestion, confidence, why):
    data = suggestion.get("data") or {}
    state = data.get("state") or {}
    mgmt = data.get("management") or {}
    status = state.get("status") or ""

    return {
        "inn": data.get("inn") or "",
        "ogrn": str(data.get("ogrn") or ""),
        "org_name": (data.get("name") or {}).get("short_with_opf") or suggestion.get("value") or "",
        "org_status": status,
        "org_status_text": STATUS_TEXT.get(status, status.lower()),
        "director": mgmt.get("name") or "",
        "director_post": (mgmt.get("post") or "").capitalize(),
        "okved": data.get("okved") or "",
        "legal_address": ((data.get("address") or {}).get("value")) or "",
        "registered_at": _ts_to_date(state.get("registration_date")),
        "liquidated_at": _ts_to_date(state.get("liquidation_date")),
        "employee_count": data.get("employee_count"),
        "dadata_confidence": confidence,
        "dadata_match": "; ".join(why),
    }


def _post(url, body, token, timeout=20):
    r = requests.post(url, json=body, timeout=timeout, headers={
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    if r.status_code in (401, 403):
        raise PermissionError("Dadata отклонила токен")
    if r.status_code == 429:
        raise RuntimeError("Dadata: превышен лимит запросов")
    r.raise_for_status()
    return r.json().get("suggestions") or []


def by_inn(inn, token=None):
    """Точные данные по известному ИНН — здесь гадать не о чем."""
    token = token or config.DADATA_TOKEN
    if not token or not inn:
        return None
    found = _post(FIND_URL, {"query": inn, "count": 1}, token)
    if not found:
        return None
    return _pack(found[0], "high", ["найдено по ИНН"])


def by_name(name, address="", token=None, kladr=None, okved=NICHE_OKVED):
    """Ищет компанию по торговому названию. Возвращает None, если неуверенно."""
    token = token or config.DADATA_TOKEN
    if not token or not (name or "").strip():
        return None

    # Именно kladr_id: фильтр по названию региона Dadata принимает,
    # но не применяет, и в выдачу попадает вся страна.
    body = {
        "query": name,
        "count": 10,
        "locations": [{"kladr_id": kladr or config.CITY_KLADR}],
    }
    best, best_points, best_why = None, 0, []
    for suggestion in _post(SUGGEST_URL, body, token):
        points, why, strong = _score_match(name, address, suggestion, okved)
        # Без профильного подтверждения это просто однофамилец
        if not strong:
            continue
        if points > best_points:
            best, best_points, best_why = suggestion, points, why

    # 6+ — совпали название и ниша, либо название и адрес.
    # 4–5 — подтверждений меньше, данные показываем с пометкой «проверьте».
    # Ниже — это просто однофамилец, такое в базу пускать нельзя.
    if best_points >= 6:
        return _pack(best, "high", best_why)
    if best_points >= 4:
        return _pack(best, "medium", best_why)
    return None
