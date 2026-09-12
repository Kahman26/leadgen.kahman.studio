# -*- coding: utf-8 -*-
"""Проверка домена: возраст и дата окончания регистрации.

Домен, который истекает через месяц, — отличный повод для звонка:
разговор начинается не с продажи, а с предупреждения.
"""

from datetime import datetime
from urllib.parse import urlparse

try:
    import whois as _whois
except ImportError:
    _whois = None


def _as_date(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, datetime):
        return value
    return None


def check(url):
    """Возвращает {'domain_expires': ISO-дата|None, 'domain_age_days': int|None}."""
    out = {"domain_expires": None, "domain_age_days": None}
    if not url or _whois is None:
        return out

    host = urlparse(url if url.startswith("http") else "http://" + url).netloc
    host = host.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return out

    try:
        w = _whois.whois(host)
    except Exception:
        return out

    exp = _as_date(getattr(w, "expiration_date", None))
    created = _as_date(getattr(w, "creation_date", None))

    if exp:
        out["domain_expires"] = exp.date().isoformat()
    if created:
        out["domain_age_days"] = (datetime.now() - created.replace(tzinfo=None)).days
    return out
