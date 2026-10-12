# -*- coding: utf-8 -*-
"""Источник — карточки Яндекс Карт, собранные браузером (Playwright).

Работает только на своём компьютере, не на сервере: Яндекс сразу закрывает
капчей адреса дата-центров, а решить её там некому. Браузер видимый, без
маскировки: увидел капчу — сбор останавливается и ждёт, пока человек решит
её в открытом окне. Обходить капчу код не пытается.

Условия Яндекс Карт запрещают автоматический сбор данных. Поэтому сбор
запускает человек, вручную, по одной нише и одному городу, с паузами между
запросами — как если бы листал карту сам. Массовых выгрузок по стране этот
модуль не делает и не должен.

Как устроено:

1. Поиск по запросу («бани и сауны») по сетке участков вокруг центра.
   Один экран карты отдаёт не больше ~50 организаций, поэтому участок, где
   выдача упёрлась в потолок, делится на четыре помельче.
2. В выдаче у каждой организации есть номер и координаты — по ним радиус
   считается точно, а по рубрике отсеиваются «товары для бани» и стройка.
3. Каждая карточка открывается и читается так же, как кнопка «В leadgen»:
   разметка schema.org LocalBusiness.

Прогресс пишется в файл после каждого участка и каждой карточки: оборвался
сбор — следующий запуск с тем же файлом продолжит с места остановки.
"""

import json
import math
import random
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit

PROFILE = Path(__file__).resolve().parent.parent / ".cache" / "yandex-browser"
CITY_URL = "https://yandex.ru/maps/54/yekaterinburg/"      # Екатеринбург
CENTER = (56.8389, 60.6057)                                 # широта, долгота

VIEW_W, VIEW_H = 1280, 900
SPAN_W_PX, SPAN_H_PX = 1000, 760   # видимая часть карты (минус панель поиска), с запасом
START_ZOOM = 12
MAX_ZOOM = 15
CAP = 30                           # столько и больше — участок густой, делим, чтобы не упереться в потолок выдачи
CAPTCHA_WAIT = 15 * 60             # сколько ждать, пока человек решит капчу, сек

# Готовые наборы для ниш: что считать нужной рубрикой, а что — соседней.
# Проверяется каждая рубрика организации: «Баня, Товары для бани» подходит
# по «Бане», а одни «Товары для бани и сауны» — нет.
PRESETS = {
    "бани и сауны": (
        r"бан[яи]|саун|хаммам|хамам|терм|банн",
        r"товар|строител|магазин|печ|сруб|ремонт|оборудован|производ|веник|мебел|бочк",
    ),
}

SNIPPETS_JS = """() => [...document.querySelectorAll('.search-snippet-view__body[data-id]')].map(b => ({
  id: b.dataset.id, coords: b.dataset.coordinates || '',
  name: ((b.querySelector('.search-business-snippet-view__title') || {}).textContent || '').trim(),
  address: ((b.querySelector('.search-business-snippet-view__address') || {}).textContent || '').trim(),
  rubric: ((b.querySelector('.search-business-snippet-view__categories') || {}).textContent || '').trim(),
}))"""

SCROLL_JS = """() => { const s = document.querySelector('.scroll__container');
  if (s) s.scrollTop = s.scrollHeight; }"""

# Конец выдачи: под последней организацией Яндекс предлагает добавить свою
END_JS = """() => [...document.querySelectorAll('.scroll__container a, .scroll__container span')]
  .some(e => /Добавьте организацию/.test(e.textContent || ''))"""

CAPTCHA_JS = """() => location.href.includes('showcaptcha') ||
  !!document.querySelector('form[action*="captcha"], .CheckboxCaptcha, .AdvancedCaptcha, .SmartCaptcha')"""

# Карточка организации — тот же разбор, что в static/bookmarklet.js
CARD_JS = r"""() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const cards = document.querySelectorAll('[itemscope][itemtype*="LocalBusiness"]');
  const card = document.querySelector('.business-card-view[itemscope]') || cards[cards.length - 1];
  if (!card) return null;
  const prop = (n) => [...card.querySelectorAll('[itemprop="' + n + '"]')]
    .filter((e) => e.closest('[itemscope]') === card);
  const val = (e) => e.getAttribute('content') || e.getAttribute('href') || e.textContent;
  const addr = card.querySelector('.business-contacts-view__address-link');
  const rubrics = [...card.querySelectorAll(
    '.orgpage-categories-info-view__link, .business-categories-view__category')].map((e) => clean(e.textContent));
  const rating = card.querySelector('.business-rating-badge-view__rating-text, .business-summary-rating-badge-view__rating');
  const closed = !!card.querySelector('[class*="permanently-closed"], .business-working-status-view._closed-permanently');
  return {
    name: clean(prop('name').map(val)[0]),
    phones: [...new Set(prop('telephone').map((e) => clean(val(e))).filter(Boolean))],
    links: [...new Set(prop('url').concat(prop('sameAs')).map((e) => clean(val(e))).filter(Boolean))],
    address: clean(addr ? addr.textContent : (prop('address').map(val)[0] || '').split('·')[0]),
    rubric: [...new Set(rubrics)].join(', '),
    rating: rating ? clean(rating.textContent) : '',
    closed,
  };
}"""


# ── геометрия ────────────────────────────────────────────────────────────────

def _distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(h))


def _span(lat, zoom):
    """Сколько градусов широты и долготы видно на экране при таком масштабе."""
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** zoom
    return SPAN_H_PX * mpp / 111320, SPAN_W_PX * mpp / (111320 * math.cos(math.radians(lat)))


def _start_tiles(center, radius_km):
    """Сетка участков, накрывающая круг радиуса radius_km."""
    dlat, dlon = _span(center[0], START_ZOOM)
    rlat = radius_km / 111.32
    rlon = radius_km / (111.32 * math.cos(math.radians(center[0])))
    ny, nx = math.ceil(2 * rlat / dlat), math.ceil(2 * rlon / dlon)
    tiles = []
    for iy in range(ny):
        for ix in range(nx):
            lat = center[0] - rlat + dlat * (iy + 0.5)
            lon = center[1] - rlon + dlon * (ix + 0.5)
            # Участок, целиком лежащий за пределами круга, не нужен
            half_diag = _distance_km((lat, lon), (lat + dlat / 2, lon + dlon / 2))
            if _distance_km(center, (lat, lon)) <= radius_km + half_diag:
                tiles.append((round(lat, 6), round(lon, 6), START_ZOOM))
    return tiles


def _split(tile):
    lat, lon, z = tile
    dlat, dlon = _span(lat, z)
    return [(round(lat + sy * dlat / 4, 6), round(lon + sx * dlon / 4, 6), z + 1)
            for sy in (-1, 1) for sx in (-1, 1)]


# ── рубрики и ссылки ─────────────────────────────────────────────────────────

def rubric_ok(rubric, include, exclude):
    if not include:
        return True
    for part in re.split(r",\s*", rubric or ""):
        if re.search(include, part, re.I) and not (exclude and re.search(exclude, part, re.I)):
            return True
    return False


def _card_from(org_id, snippet, raw):
    """Карточка в том же виде, что присылает кнопка «В leadgen»."""
    out = {"id": org_id, "name": raw.get("name") or snippet.get("name") or "",
           "phones": raw.get("phones") or [], "website": "", "address": raw.get("address") or "",
           "telegram": "", "vk": "", "whatsapp": "", "email": "",
           "rubric": raw.get("rubric") or snippet.get("rubric") or "",
           "rating": raw.get("rating") or "", "closed": bool(raw.get("closed")),
           "source": "yandex", "map_url": f"https://yandex.ru/maps/org/{org_id}/",
           "coords": snippet.get("coords") or "", "links": []}
    for href in raw.get("links") or []:
        try:
            u = urlsplit(href)
        except ValueError:
            continue
        host = (u.hostname or "").lower().removeprefix("www.")
        first = (u.path.strip("/").split("/") or [""])[0]
        if host in ("t.me", "telegram.me"):
            if first and not first.startswith("+") and not out["telegram"]:
                out["telegram"] = first
        elif host in ("vk.com", "vk.ru", "m.vk.com"):
            if first and not out["vk"]:
                out["vk"] = first
        elif host in ("wa.me", "api.whatsapp.com", "whatsapp.com"):
            digits = re.sub(r"\D", "", first)
            if digits and not out["whatsapp"]:
                out["whatsapp"] = digits
        elif re.search(r"(^|\.)(ok\.ru|instagram\.com|youtube\.com|youtu\.be|dzen\.ru|rutube\.ru)$", host):
            continue
        elif href.startswith(("http://", "https://")) and not re.search(r"(^|\.)(yandex|2gis)\.", host):
            if href not in out["links"]:
                out["links"].append(href)
    out["website"] = out["links"][0] if out["links"] else ""
    return out


# ── браузер ──────────────────────────────────────────────────────────────────

def _pause(a=2.0, b=4.5):
    time.sleep(random.uniform(a, b))


def _captcha_gate(page, progress):
    """Капча: ждём, пока человек решит её в окне. False — не дождались."""
    if not page.evaluate(CAPTCHA_JS):
        return True
    progress("Яндекс показал капчу — решите её в окне браузера, сбор продолжится сам")
    deadline = time.time() + CAPTCHA_WAIT
    while time.time() < deadline:
        time.sleep(3)
        try:
            if not page.evaluate(CAPTCHA_JS):
                progress("Капча решена, продолжаю")
                _pause(3, 6)
                return True
        except Exception:                                  # страница перезагружается
            continue
    return False


def _goto(page, url, progress, tries=3):
    """Переход с повторами: сеть или Яндекс иногда не отвечают с первого раза."""
    for attempt in range(1, tries + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return
        except Exception as exc:                           # таймаут, обрыв связи
            if attempt == tries:
                raise
            progress(f"Страница не открылась ({type(exc).__name__}), повтор {attempt}/{tries - 1}")
            time.sleep(15 * attempt)


def _search_tile(page, query, tile, progress):
    lat, lon, z = tile
    # Без sll/sspn Яндекс ищет в маленьком окне вокруг точки и отдаёт ~10
    # организаций; с явной областью поиска — всё, что видно на экране
    dlat, dlon = _span(lat, z)
    url = (f"{CITY_URL}search/{quote(query)}/?ll={lon:.6f}%2C{lat:.6f}&z={z}"
           f"&sll={lon:.6f}%2C{lat:.6f}&sspn={dlon:.6f}%2C{dlat:.6f}")
    _goto(page, url, progress)
    _pause(3.5, 5.5)
    if not _captcha_gate(page, progress):
        raise RuntimeError("капча не решена")
    # Список догружается, только когда его листают колесом и ждут: резкий
    # прыжок в конец и короткие паузы обрывали выдачу на первой порции
    seen, still = {}, 0
    page.mouse.move(200, 600)
    for _ in range(80):
        for s in page.evaluate(SNIPPETS_JS):
            seen[s["id"]] = s
        before = len(seen)
        page.mouse.wheel(0, 2500)
        _pause(3.2, 4.8)
        for s in page.evaluate(SNIPPETS_JS):
            seen[s["id"]] = s
        still = still + 1 if len(seen) == before else 0
        at_end = page.evaluate(END_JS)
        if (at_end and still >= 1) or still >= 4:
            break
    return seen


def _read_card(page, org_id, progress):
    _goto(page, f"https://yandex.ru/maps/org/{org_id}/", progress)
    if not _captcha_gate(page, progress):
        raise RuntimeError("капча не решена")
    try:
        page.wait_for_selector('[itemtype*="LocalBusiness"]', timeout=15000)
    except Exception:
        pass
    time.sleep(1.2)
    return page.evaluate(CARD_JS)


def _save(path, state):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def default_out(query):
    folder = Path(__file__).resolve().parent.parent / "yandex"
    folder.mkdir(exist_ok=True)
    slug = re.sub(r"[^a-zа-я0-9]+", "-", query.lower()).strip("-")
    return folder / f"{slug}-{date.today().isoformat()}.json"


def collect(query, radius_km=30, out=None, center=CENTER, include=None, exclude=None,
            niche="booking", limit=None, progress=print, headless=False):
    """Собирает карточки организаций по запросу в радиусе от центра.

    Возвращает путь к файлу с результатом: {"meta": …, "cards": [...]}.
    """
    from playwright.sync_api import sync_playwright   # нужен только здесь и только локально

    if include is None and query.lower() in PRESETS:
        include, exclude = PRESETS[query.lower()]
    out = Path(out) if out else default_out(query)
    state = {"meta": {"query": query, "radius_km": radius_km, "center": list(center),
                      "niche": niche, "include": include, "exclude": exclude,
                      "started": time.strftime("%Y-%m-%d %H:%M")},
             "tiles_done": [], "tile_counts": {}, "found": {}, "cards": {}, "skipped": {}}
    if out.exists():
        old = json.loads(out.read_text(encoding="utf-8"))
        if old.get("meta", {}).get("query") == query:
            state.update({k: old.get(k, state[k])
                          for k in ("tiles_done", "tile_counts", "found", "cards", "skipped")})
            progress(f"Продолжаю {out.name}: участков {len(state['tiles_done'])}, "
                     f"карточек {len(state['cards'])}")

    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=headless, locale="ru-RU",
            viewport={"width": VIEW_W, "height": VIEW_H})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            # 1. Поиск по участкам
            queue = [tuple(t) for t in _start_tiles(center, radius_km)]
            done = {tuple(t) for t in state["tiles_done"]}
            # Густой участок из прошлого запуска, который тогда не делили
            # (порог мог быть другим), — делим сейчас
            for t in list(done):
                key = ",".join(map(str, t))
                if state["tile_counts"].get(key, 0) >= CAP and t[2] < MAX_ZOOM:
                    queue.extend(s for s in _split(t) if s not in done)
            n = 0
            while queue:
                tile = queue.pop(0)
                if tile in done:
                    continue
                n += 1
                found = _search_tile(page, query, tile, progress)
                fresh = sum(1 for k in found if k not in state["found"])
                state["found"].update(found)
                state["tiles_done"].append(list(tile))
                state["tile_counts"][",".join(map(str, tile))] = len(found)
                done.add(tile)
                extra = ""
                if len(found) >= CAP and tile[2] < MAX_ZOOM:
                    queue.extend(_split(tile))
                    extra = " — много, делю участок на четыре"
                progress(f"Участок {n} (осталось {len(queue)}): в выдаче {len(found)}, "
                         f"новых {fresh}, всего {len(state['found'])}{extra}")
                _save(out, state)

            # 2. Радиус и рубрика
            todo = []
            for oid, s in state["found"].items():
                try:
                    lon, lat = map(float, s["coords"].split(","))
                    dist = _distance_km(center, (lat, lon))
                except (ValueError, KeyError):
                    dist = None
                if dist is None or dist > radius_km:
                    state["skipped"][oid] = f"дальше {radius_km} км"
                elif not rubric_ok(s.get("rubric"), include, exclude):
                    state["skipped"][oid] = f"рубрика: {s.get('rubric')}"
                elif oid not in state["cards"]:
                    todo.append(oid)
            if limit:
                todo = todo[:max(0, limit - len(state["cards"]))]
            progress(f"В радиусе {radius_km} км и по рубрике подходят "
                     f"{len(todo) + len(state['cards'])}, отсеяно {len(state['skipped'])}. "
                     f"Открываю карточки: {len(todo)}")
            _save(out, state)

            # 3. Карточки
            for i, oid in enumerate(todo, 1):
                try:
                    raw = _read_card(page, oid, progress) or {}
                except RuntimeError:
                    raise                                  # капча не решена — стоп
                except Exception as exc:
                    # Карточка так и не открылась — не валим весь сбор, следующий
                    # запуск с тем же файлом попробует её снова
                    progress(f"Карточка {i}/{len(todo)}: не открылась ({type(exc).__name__}), пропускаю")
                    continue
                state["cards"][oid] = _card_from(oid, state["found"][oid], raw)
                c = state["cards"][oid]
                progress(f"Карточка {i}/{len(todo)}: {c['name']} — "
                         f"{', '.join(c['phones']) or 'без телефона'}")
                _save(out, state)
                _pause(2.0, 4.5)
        finally:
            state["meta"]["finished"] = time.strftime("%Y-%m-%d %H:%M")
            _save(out, state)
            ctx.close()
    return out


def load_cards(path_or_data):
    """Карточки из файла сборщика (или уже прочитанного словаря)."""
    data = path_or_data
    if not isinstance(data, dict):
        data = json.loads(Path(path_or_data).read_text(encoding="utf-8"))
    cards = data.get("cards") or {}
    return data.get("meta") or {}, list(cards.values()) if isinstance(cards, dict) else list(cards)
