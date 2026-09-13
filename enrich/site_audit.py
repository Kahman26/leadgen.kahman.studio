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

# ── Заявки: гость оставляет контакты, менеджер перезванивает ─────────────────
# Это не бронирование. Для отеля разница принципиальная: заявку надо обработать
# руками, ночью и в выходные она остывает.
REQUEST_WIDGETS = {
    "Битрикс24": [r"bitrix24", r"b24form"],
    "amoCRM": [r"amocrm"],
    "JivoSite": [r"jivosite", r"jivo\.(ru|chat)"],
    "Envybox": [r"envybox", r"envycdn"],
    "Callibri": [r"callibri"],
    "Calltouch": [r"calltouch"],
    "Marquiz": [r"marquiz"],
    "Verbox": [r"verbox"],
}

REQUEST_WORDS = [
    r"оставить заявку", r"оставьте заявку", r"отправить заявку",
    r"заявка на бронирован", r"заказать звонок", r"обратный звонок",
    r"закажите звонок", r"мы перезвоним", r"свяжемся с вами",
    r"перезвоним вам", r"забронирова",
]

# ── Признаки настоящего календаря на своём сайте ─────────────────────────────
# Ключевое отличие от заявки: гость сам выбирает даты заезда и выезда.
DATE_FIELD_HINTS = re.compile(
    r"check[_-]?in|check[_-]?out|arrival|departure|date[_-]?from|date[_-]?to|"
    r"zaezd|vyezd|заезд|выезд|дата", re.I)
DATEPICKER_LIBS = [r"daterangepicker", r"air-datepicker", r"litepicker",
                   r"flatpickr", r"datepicker", r"fullcalendar"]
DATE_WORDS = [r"дата заезда", r"дата выезда", r"выберите даты", r"даты проживания"]

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


def decode_body(response):
    """Возвращает текст ответа с правильной кодировкой.

    Для text/html без указанной кодировки requests по стандарту берёт
    ISO-8859-1, и русский текст превращается в крякозябры. На таком HTML
    не находятся ни «забронировать», ни «дата заезда» — проверка молча
    считает, что бронирования нет.
    """
    ctype = (response.headers.get("content-type") or "").lower()
    if "charset=" not in ctype:
        response.encoding = response.apparent_encoding or response.encoding
    return response.text or ""


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


# Заглушка, которая ставит cookie скриптом и перезагружает страницу.
# Так делает, например, Beget: без этого в аудит попадают 300 байт пустого
# html, и сайт ошибочно выглядит как «без мобильной версии и без контактов».
RE_COOKIE_SET = re.compile(r"document\.cookie\s*=\s*['\"]([A-Za-z0-9_\-]+)=([A-Za-z0-9_\-]+)")


def _is_cookie_challenge(html):
    return (len(html) < 3000
            and "document.cookie" in html
            and ("location.reload" in html or "location.href" in html))


def _solve_cookie_challenge(sess, resp):
    """Ставит cookie, которую страница просила выставить, и грузит её заново."""
    m = RE_COOKIE_SET.search(resp.text)
    if not m:
        return None
    # Домен намеренно не указываем: с явным доменом requests не отдаёт cookie
    # обратно на хосты с www. Сессия и так своя на каждый сайт.
    sess.cookies.set(m.group(1), m.group(2))
    try:
        return sess.get(resp.url, timeout=config.HTTP_TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return None


def _has_date_fields(soup, html, text):
    """Есть ли на странице выбор дат заезда и выезда.

    Одних слов «заезд» и «выезд» мало: они есть почти на каждом сайте
    отеля («заезд с 14:00»). Нужно именно поле ввода.
    """
    for el in soup.find_all(["input", "select"]):
        if (el.get("type") or "").lower() == "date":
            return True
        blob = " ".join(filter(None, [
            el.get("name", ""), el.get("id", ""), el.get("placeholder", ""),
            " ".join(el.get("class") or []),
        ]))
        if blob and DATE_FIELD_HINTS.search(blob):
            return True

    # Календарь подключён скриптом, а поля рисуются на лету
    low = html.lower()
    if any(re.search(lib, low) for lib in DATEPICKER_LIBS) and \
            any(re.search(w, text, re.I) for w in DATE_WORDS):
        return True
    return False


def _has_request_form(soup):
    """Форма, куда гость оставляет контакты."""
    for form in soup.find_all("form"):
        fields = form.find_all(["input", "textarea"])
        if not fields:
            continue

        blob = " ".join(
            " ".join(filter(None, [f.get("name", ""), f.get("type", ""),
                                   f.get("placeholder", ""), f.get("id", "")]))
            for f in fields
        ).lower()

        if "password" in blob:
            continue                       # форма входа, а не заявка
        if re.search(r"search|поиск", blob) and not re.search(r"phone|tel|телефон", blob):
            continue                       # поисковая строка

        if re.search(r"phone|tel|телефон|имя|\bname\b|email|mail", blob):
            return True
    return False


def detect_booking(html, soup, text):
    """Как гость может забронировать: сам, через заявку или никак.

    Возвращает (тип, чем именно): engine | request | none.
    """
    engine = _detect(html, BOOKING_ENGINES)
    if engine:
        return "engine", engine

    if _has_date_fields(soup, html, text):
        return "engine", "Свой модуль с выбором дат"

    widget = _detect(html, REQUEST_WIDGETS)
    if widget:
        return "request", widget

    if _has_request_form(soup):
        return "request", "Форма заявки"

    # Форму могло дорисовать скриптом — смотрим на текст кнопок
    if any(re.search(w, text, re.I) for w in REQUEST_WORDS):
        return "request", "Кнопка заявки"

    return "none", ""


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
        "online_booking": 0, "booking_type": "none", "booking_engine": "",
        "cms": "", "copyright_year": 0,
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

    html = decode_body(resp)

    # Страница могла оказаться заглушкой, которая ставит cookie и перезагружается
    if _is_cookie_challenge(html):
        retried = _solve_cookie_challenge(sess, resp)
        if retried is not None and retried.status_code < 400:
            resp = retried
            html = decode_body(resp)
            res["http_code"] = resp.status_code
            res["final_url"] = utils.decode_idna(resp.url)

    res["site_status"] = "ok"

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    # Адаптивность: без viewport сайт на телефоне — уменьшенный десктоп.
    vp = soup.find("meta", attrs={"name": re.compile("^viewport$", re.I)})
    res["mobile_ready"] = 1 if vp and "width" in (vp.get("content") or "") else 0

    res["cms"] = _detect(html, CMS_MARKERS)

    text = soup.get_text(" ", strip=True)
    booking_type, how = detect_booking(html, soup, text)
    res["booking_type"] = booking_type
    res["booking_engine"] = how
    # Отдельный флаг оставлен для совместимости: 1 — только настоящая бронь,
    # заявка сюда не считается.
    res["online_booking"] = 1 if booking_type == "engine" else 0
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
                    body2 = decode_body(r2)
                    try:
                        t2 = BeautifulSoup(body2, "lxml").get_text(" ", strip=True)
                    except Exception:
                        t2 = body2
                    extra = extract_contacts(body2, t2)
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
