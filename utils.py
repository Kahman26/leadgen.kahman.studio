# -*- coding: utf-8 -*-
"""Мелкие нормализаторы, общие для источников и аудита."""

import re
from urllib.parse import urlsplit, urlunsplit

RE_PHONE = re.compile(r"(?:\+7|8|7)[\s\-(]*(\d{3})[\s\-)]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})")


def split_phones(raw):
    """Разбирает значение тега телефона в список номеров формата +7XXXXXXXXXX.

    В OSM в одном теге часто лежит несколько номеров через «;» или «,»:
    «+7 343 3726471;+7 912 6726471». Если их не разделить, ссылка tel:
    получается нерабочей.
    """
    out = []
    for chunk in re.split(r"[;,/]| или ", raw or ""):
        m = RE_PHONE.search(chunk)
        if not m:
            continue
        phone = "+7" + "".join(m.groups())
        if phone not in out:
            out.append(phone)
    return out


def decode_idna(url):
    """Возвращает URL с доменом в читаемом виде: xn--80a… → кириллица.

    Маркетологу нужно видеть «баня-паровозов.рф», а не punycode.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url if "//" in url else "http://" + url)
        host = parts.hostname or ""
        if "xn--" not in host:
            return url
        decoded = ".".join(
            lbl.encode("ascii").decode("idna") if lbl.startswith("xn--") else lbl
            for lbl in host.split(".")
        )
        netloc = decoded + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return url
