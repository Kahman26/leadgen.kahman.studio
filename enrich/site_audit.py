# -*- coding: utf-8 -*-
"""Аудит сайта лида: жив ли, адаптив, движок, онлайн-бронирование, контакты.

Всё делается обычными HTTP-запросами и разбором HTML — платные сервисы
вроде ScrapeGraphAI здесь не нужны.
"""

import re
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import config
import utils

# ── Движки онлайн-бронирования (главный признак ниши) ────────────────────────
BOOKING_ENGINES = {
    "TravelLine": [r"travelline", r"tlbooking", r"tl-booking"],
    "Bnovo": [r"bnovo"],
    "Realty Calendar": [r"realtycalendar", r"realty-calendar"],
    "Суточно.ру": [r"sutochno\.ru"],
    "Островок": [r"ostrovok\.ru", r"emergingtravel"],
    "Яндекс.Путешествия": [r"travel\.yandex"],
    "101 Отель": [r"101hotels"],
    "Броневик": [r"bronevik"],
    "WuBook": [r"wubook"],
    "Контур.Отель": [r"otel\.kontur"],
    "YClients": [r"yclients"],
    "DIKIDI": [r"dikidi"],
    "Shelter": [r"shelter\.pro"],
    "Своя форма брони": [r"booking\.php", r"/booking/", r"bookingform"],
}

# Слова в тексте, которые тоже говорят о живом бронировании.
BOOKING_WORDS = [r"забронирова", r"онлайн-бронь", r"онлайн бронирован", r"book now"]

# ── Движки и конструкторы сайта ──────────────────────────────────────────────
CMS_MARKERS = {
    "Wix": [r"wix\.com", r"wixstatic"],
    "uKit": [r"ukit\.com", r"u-kit\.ru", r"ukit\.me"],
    "uCoz": [r"ucoz\.(ru|net|com)"],
    "Nethouse": [r"nethouse\.(ru|me)"],
    "A5": [r"a5\.ru", r"a5template"],
    "Setup.ru": [r"setup\.ru"],
    "Jimdo": [r"jimdo"],
    "Мегагрупп": [r"megagroup", r"site\.pro"],
    "Tilda": [r"tildacdn", r"tilda\.ws"],
    "Craftum": [r"craftum"],
    "Flexbe": [r"flexbe"],
    "WordPress": [r"wp-content", r"wp-includes"],
    "1С-Битрикс": [r"/bitrix/", r"bitrix/js"],
    "Joomla": [r"/media/system/js/", r"joomla"],
    "DLE": [r"engine/classes", r"dle_root"],
    "MODX": [r"modx"],
    "OpenCart": [r"index\.php\?route="],
}

# Конструкторы-визитки: почти всегда означают «сайт сделан на коленке».
CHEAP_BUILDERS = {"Wix", "uKit", "uCoz", "Nethouse", "A5", "Setup.ru", "Jimdo", "Мегагрупп"}

# ── Контакты ─────────────────────────────────────────────────────────────────
# Границы (?<!\d) / (?!\d) обязательны: без них шаблон выхватывает куски
# случайных цифровых последовательностей из скриптов и id — и маркетолог
# звонит постороннему человеку.
RE_PHONE = re.compile(
    r"(?<!\d)(?:\+7|8|7)[\s\-(]*(\d{3})[\s\-)]*(\d{3})[\s\-]*(\d{2})[\s\-]*(\d{2})(?!\d)")
RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
RE_TG = re.compile(r"(?:t\.me|telegram\.me|tlgg\.ru)/([a-zA-Z0-9_]{4,32})", re.I)
RE_TG_DEEP = re.compile(r"tg://resolve\?domain=([a-zA-Z0-9_]{4,32})", re.I)
RE_VK = re.compile(r"vk\.com/([a-zA-Z0-9_.]{2,64})", re.I)
RE_WA = re.compile(r"(?:wa\.me|api\.whatsapp\.com/send\?phone=)/?(\d{10,15})", re.I)

# Служебные ссылки соцсетей — это не аккаунты компании.
VK_STOP = {"share", "share.php", "widget", "widget_community", "js", "away.php",
           "im", "video_ext.php", "share_url", "login.php", "docs", "vk"}
TG_STOP = {"share", "telegram", "joinchat", "iv"}
MAIL_STOP = ("example.", "sentry.", "noreply", "no-reply", "@sentry", "@wix",
             "@tilda", ".png", ".jpg", ".webp", "@domain", "@2x")

# Страницы, которые дополнительно обходим ради контактов.
# Список намеренно короткий: каждая лишняя страница — это секунды на объект,
# а на 300 объектах они превращаются в десятки минут.
CONTACT_PATHS = ["/contacts", "/kontakty", "/contact"]
CONTACT_TIMEOUT = 8


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


def norm_phone(m):
    return "+7" + "".join(m.groups())


def _pick(text, regex, stop=()):
    for m in regex.finditer(text):
        val = m.group(1)
        if val.lower() in stop:
            continue
        return val
    return ""


def valid_ru_phone(phone):
    """Отсекает мусор, похожий на номер: +79999999999, коды вне российских зон."""
    body = phone[2:]                       # без «+7»
    if len(body) != 10:
        return False
    if body[0] not in "3489":              # 3xx/4xx — география, 8xx — 800, 9xx — мобильные
        return False
    if len(set(body)) <= 2:                # 9999999999 и подобное
        return False
    return True


def extract_contacts(html, text=None):
    """Достаёт контакты. Приоритет: телефон > telegram > vk > почта.

    `text` — видимый текст страницы. Числа ищем именно в нём, а не в сыром
    HTML: иначе в номера попадают куски скриптов, id и base64.
    """
    phones, seen = [], set()

    def add(raw_match):
        p = norm_phone(raw_match)
        if p in seen or not valid_ru_phone(p):
            return
        seen.add(p)
        phones.append(p)

    # tel: — самый надёжный источник, ему верим безоговорочно
    for m in re.finditer(r'href=["\']tel:([+\d\s\-()]+)', html, re.I):
        pm = RE_PHONE.search(re.sub(r"[^\d+]", "", m.group(1)))
        if pm:
            add(pm)

    # К тексту обращаемся, только если разметка ссылок ничего не дала.
    if not phones:
        for m in RE_PHONE.finditer(text if text is not None else html):
            add(m)

    emails = [e for e in RE_EMAIL.findall(html)
              if not any(s in e.lower() for s in MAIL_STOP)]

    tg = _pick(html, RE_TG, TG_STOP) or _pick(html, RE_TG_DEEP, TG_STOP)
    vk = _pick(html, RE_VK, VK_STOP)

    wa = ""
    m = RE_WA.search(html)
    if m:
        wa = "+7" + m.group(1)[-10:]

    return {
        "phones": phones[:8],
        "phone": phones[0] if phones else "",
        "telegram": tg,
        "vk": vk,
        "whatsapp": wa,
        "email": emails[0] if emails else "",
    }


def _detect(html, markers):
    low = html.lower()
    for name, pats in markers.items():
        if any(re.search(p, low) for p in pats):
            return name
    return ""


def _copyright_year(text):
    """Ищет год в подвале рядом со знаком копирайта."""
    years = []
    for m in re.finditer(r"(?:©|&copy;|copyright)[^\d]{0,40}((?:19|20)\d{2})", text, re.I):
        years.append(int(m.group(1)))
    for m in re.finditer(r"((?:19|20)\d{2})\s*[-–—]\s*((?:19|20)\d{2})", text):
        years.append(int(m.group(2)))
    now = datetime.now().year
    years = [y for y in years if 1995 <= y <= now + 1]
    return max(years) if years else 0


def audit(url):
    """Возвращает результат проверки сайта. Исключений не бросает."""
    res = {
        "site_status": "dead", "http_code": None, "https": 0, "mobile_ready": 0,
        "online_booking": 0, "booking_engine": "", "cms": "", "copyright_year": 0,
        "load_ms": None, "final_url": "", "contacts": {}, "error": "",
    }
    url = normalize_url(url)
    if not url:
        res["site_status"] = "none"
        return res

    sess = requests.Session()
    sess.headers.update({"User-Agent": config.USER_AGENT,
                         "Accept-Language": "ru-RU,ru;q=0.9"})

    # Сначала пробуем https — заодно это и есть проверка на HTTPS.
    parsed = urlparse(url)
    candidates = [url]
    if parsed.scheme == "http":
        candidates.insert(0, "https://" + parsed.netloc + parsed.path)

    resp = None
    for cand in candidates:
        try:
            t0 = time.time()
            resp = sess.get(cand, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
            res["load_ms"] = int((time.time() - t0) * 1000)
            break
        except requests.RequestException as exc:
            res["error"] = type(exc).__name__
            resp = None

    if resp is None:
        return res

    res["http_code"] = resp.status_code
    res["final_url"] = utils.decode_idna(resp.url)
    res["https"] = 1 if resp.url.startswith("https://") else 0

    # 401/403/429 — это защита (Cloudflare, антибот), а не поломка сайта.
    # Помечаем отдельно, чтобы не звонить клиенту с ложным «у вас сайт лежит».
    if resp.status_code in (401, 403, 429):
        res["site_status"] = "blocked"
        return res

    if resp.status_code >= 400:
        return res                          # 404/5xx — сайт действительно не работает

    res["site_status"] = "ok"
    html = resp.text or ""

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Адаптивность: без viewport сайт на телефоне — уменьшенный десктоп.
    vp = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    res["mobile_ready"] = 1 if vp and "width" in (vp.get("content") or "") else 0

    res["cms"] = _detect(html, CMS_MARKERS)
    engine = _detect(html, BOOKING_ENGINES)
    res["booking_engine"] = engine

    text = soup.get_text(" ", strip=True)
    res["online_booking"] = 1 if engine or any(
        re.search(w, text, re.I) for w in BOOKING_WORDS) else 0
    res["copyright_year"] = _copyright_year(text[-4000:] or text)

    # Контакты: главная, при нехватке — страница контактов.
    contacts = extract_contacts(html, text)
    if not (contacts["phone"] and (contacts["telegram"] or contacts["vk"])):
        # Собираем уникальные адреса: реальная ссылка со страницы важнее догадки.
        targets = []
        for path in CONTACT_PATHS:
            link = soup.find("a", href=re.compile(path.strip("/"), re.I))
            target = urljoin(resp.url, link["href"]) if link and link.get("href") \
                else urljoin(resp.url, path)
            if target not in targets and target != resp.url:
                targets.append(target)

        for target in targets[:2]:
            try:
                time.sleep(config.CRAWL_DELAY)
                r2 = sess.get(target, timeout=CONTACT_TIMEOUT)
                if r2.status_code == 200:
                    try:
                        t2 = BeautifulSoup(r2.text, "lxml").get_text(" ", strip=True)
                    except Exception:
                        t2 = r2.text
                    extra = extract_contacts(r2.text, t2)
                    for k in ("phone", "telegram", "vk", "whatsapp", "email"):
                        if not contacts[k] and extra[k]:
                            contacts[k] = extra[k]
                    contacts["phones"] = (contacts["phones"] + [
                        p for p in extra["phones"] if p not in contacts["phones"]])[:8]
            except requests.RequestException:
                pass
            if contacts["phone"] and (contacts["telegram"] or contacts["vk"]):
                break

    res["contacts"] = contacts
    return res
