# -*- coding: utf-8 -*-
"""Площадки, на которых объект есть, но которые не его сайт.

Отель, баня или база отдыха часто «есть в интернете» только страницей на
чужой площадке: Островок, каталог саун, магазин подарков-впечатлений,
визитка из Яндекс Бизнеса. Для нас это разные вещи:

- в поле «сайт» стоит страница площадки — своего сайта у объекта нет,
  а гости приходят через посредника с комиссией;
- свой сайт есть, но объект продаётся ещё и через площадки — тоже повод
  для разговора о прямом бронировании.

Тип площадки нужен для текстов: «бронирование» — комиссия с каждой брони,
«каталог» — страница среди конкурентов, «подарки» — сертификаты с наценкой.
Соцсети и мессенджеры здесь только для поля «сайт»: ссылка на ВКонтакте
со своего сайта — нормальное дело, а ВКонтакте вместо сайта — нет.
"""

import json
from urllib.parse import urlsplit

# (домен, название, тип). Домен совпадает сам и со всеми поддоменами:
# ekb.sauna.ru, imper-bani.gorpom.ru. path — когда площадка живёт в разделе
# большого сайта (travel.yandex.ru — да, yandex.ru целиком — нет).
PLATFORMS = [
    # бронирование жилья
    ("ostrovok.ru", "Островок", "booking"),
    ("travel.yandex.ru", "Яндекс Путешествия", "booking"),
    ("sutochno.ru", "Суточно", "booking"),
    ("tvil.ru", "Твил", "booking"),
    ("101hotels.com", "101 Отель", "booking"),
    ("101hotels.ru", "101 Отель", "booking"),
    ("bronevik.com", "Броневик", "booking"),
    ("otello.ru", "Отелло", "booking"),
    ("zhilibyli.ru", "ЖилиБыли", "booking"),
    ("onetwotrip.com", "OneTwoTrip", "booking"),
    ("trip.com", "Trip.com", "booking"),
    ("booking.com", "Booking", "booking"),
    ("airbnb.ru", "Airbnb", "booking"),
    ("airbnb.com", "Airbnb", "booking"),
    ("kvartirka.com", "Квартирка", "booking"),
    ("hotellook.ru", "Hotellook", "booking"),
    ("roomguru.ru", "RoomGuru", "booking"),
    ("putevka.com", "Putevka.com", "booking"),
    ("komandirovka.ru", "Командировка.ру", "booking"),
    ("edem-v-gosti.ru", "Едем в гости", "booking"),
    ("mirturbaz.ru", "Мир турбаз", "booking"),
    ("oteli96.ru", "Отели 96", "booking"),
    ("ozon.ru/travel", "Ozon Travel", "booking"),
    ("tutu.ru/hotel", "Туту", "booking"),
    # объявления
    ("avito.ru", "Авито", "classifieds"),
    ("cian.ru", "Циан", "classifieds"),
    ("youla.ru", "Юла", "classifieds"),
    # каталоги и справочники: страница среди конкурентов
    ("sauna.ru", "Sauna.ru", "catalog"),
    ("zoon.ru", "Zoon", "catalog"),
    ("yell.ru", "Yell", "catalog"),
    ("flamp.ru", "Флэмп", "catalog"),
    ("orgpage.ru", "Orgpage", "catalog"),
    ("spr.ru", "СПР", "catalog"),
    ("gorpom.ru", "Gorpom", "catalog"),
    ("tapki.com", "Tapki", "catalog"),
    # подарки и впечатления: сертификаты с наценкой площадки
    ("f911.ru", "F911", "gifts"),
    ("xpresent.ru", "Xpresent", "gifts"),
    ("bonodono.ru", "BonoDono", "gifts"),
    ("vpechatleniya.ru", "Впечатления", "gifts"),
    ("biglion.ru", "Биглион", "gifts"),
    # страницы-визитки на чужой платформе
    ("clients.site", "Визитка Яндекс Бизнеса", "page"),
    ("2gis.biz", "Визитка 2ГИС", "page"),
    ("navse360.ru", "Виртуальный тур НА ВСЕ 360", "page"),
    ("taplink.cc", "Taplink", "page"),
    ("taplink.ws", "Taplink", "page"),
    ("taplink.at", "Taplink", "page"),
    ("yclients.com", "YCLIENTS", "page"),
    ("dikidi.net", "DIKIDI", "page"),
    ("dikidi.ru", "DIKIDI", "page"),
    # соцсети: не сайт, но и не площадка продаж — только для поля «сайт»
    ("vk.com", "ВКонтакте", "social"),
    ("vk.link", "ВКонтакте", "social"),
    ("vk.ru", "ВКонтакте", "social"),
    ("ok.ru", "Одноклассники", "social"),
    ("t.me", "Telegram", "social"),
    ("instagram.com", "Instagram", "social"),
]

KIND_TEXT = {
    "booking": "бронирование", "classifieds": "объявления", "catalog": "каталог",
    "gifts": "подарки", "page": "страница-визитка", "social": "соцсеть",
}
# Площадки, через которые объект продаёт: для признака и текстов
SALES_KINDS = {"booking", "classifieds", "catalog", "gifts"}
# Что ищем ссылками на своём сайте. Каталоги не ищем: Zoon и Флэмп на своём
# сайте — это почти всегда виджет отзывов, а не продажа через посредника
SITE_LINK_KINDS = {"booking", "classifieds", "gifts"}


def _split(url):
    url = (url or "").strip()
    if not url:
        return "", ""
    parts = urlsplit(url if "//" in url else "http://" + url)
    host = (parts.hostname or "").lower()
    return (host[4:] if host.startswith("www.") else host), parts.path or "/"


def match(url):
    """Площадка по ссылке: {name, kind, url} или None, если это не площадка."""
    host, path = _split(url)
    if not host:
        return None
    for domain, name, kind in PLATFORMS:
        dom, _, section = domain.partition("/")
        if host != dom and not host.endswith("." + dom):
            continue
        if section and not path.lstrip("/").startswith(section):
            continue
        return {"name": name, "kind": kind, "url": url.strip()}
    return None


def split(website, links=(), source=""):
    """Делит ссылки объекта на свой сайт и площадки.

    Возвращает (свой сайт или "", [площадки]). Если в «сайте» стоит
    площадка, а среди остальных ссылок есть настоящий сайт — он и станет
    сайтом. Соцсеть сайтом не считается, но ссылка на неё сохраняется.
    """
    own, found = "", []
    for url in [website, *links]:
        if not url:
            continue
        m = match(url)
        if m is None:
            own = own or url.strip()
        else:
            found.append(dict(m, source=source) if source else m)
    return own, merge([], found)


def as_list(value):
    if isinstance(value, list):
        return value
    try:
        data = json.loads(value or "[]")
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def merge(old, new, replace_source=None):
    """Объединяет два списка площадок без повторов.

    replace_source — источник, записи которого из old заменяются новыми
    целиком (перепроверка сайта: ссылку убрали — площадка уходит).
    """
    out, seen = [], set()
    old = [e for e in as_list(old) if not replace_source or e.get("source") != replace_source]
    for e in old + as_list(new):
        host, path = _split(e.get("url"))
        key = (host, path.rstrip("/"))
        if not host or key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def names(entries):
    """«F911 (подарки), Островок (бронирование)» — для текстов."""
    seen, out = set(), []
    for e in as_list(entries):
        if e.get("name") in seen:
            continue
        seen.add(e.get("name"))
        out.append(f"{e.get('name')} ({KIND_TEXT.get(e.get('kind'), e.get('kind'))})")
    return ", ".join(out)
