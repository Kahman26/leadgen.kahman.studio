# -*- coding: utf-8 -*-
"""Автопроверка объекта через Claude с поиском в интернете.

У Anthropic поиск выполняется на их стороне — отдельный поисковый сервис
не нужен, достаточно объявить серверные инструменты web_search и web_fetch.

Главное правило здесь то же, что и во всём проекте: модель не должна
ничего придумывать. Она обязана возвращать только то, что реально видела
в выдаче, и на каждый факт давать ссылку. Не нашла — пишет null.
Выдуманный телефон хуже пустого поля: по нему позвонят.
"""

import json
import re

import config

try:
    import anthropic
except ImportError:                        # сервис должен работать и без пакета
    anthropic = None

MAX_TOKENS = 8000
MAX_RESUMES = 4            # сколько раз продолжаем прерванный серверный цикл

TOOLS = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209", "name": "web_fetch"},
]

SYSTEM = """Ты помогаешь агентству веб-разработки разобраться в потенциальном клиенте.
Это объект размещения или отдыха в городе Екатеринбург: отель, база отдыха,
баня, глэмпинг или посуточные квартиры.

Твоя задача — найти в интернете и проверить:
1. Работает ли бизнес сейчас или закрылся.
2. Какой у него ДЕЙСТВУЮЩИЙ сайт. Старый адрес мог перестать открываться,
   компания могла переехать на новый домен.
3. Через какие площадки-посредники он принимает брони: Суточно.ру, Островок,
   Авито, Яндекс.Путешествия, 101 отель, 2ГИС, YClients и подобные.
4. Контакты руководителя или владельца, если они есть в открытых источниках.
5. Какие у бизнеса проблемы с онлайн-присутствием: нет сайта, сайт не
   открывается, устарел, нет мобильной версии, нет онлайн-бронирования,
   брони идут только через агрегаторы с комиссией, плохие отзывы о сайте.

ЖЁСТКИЕ ПРАВИЛА:
- Пиши только то, что реально видел в результатах поиска. Ничего не достраивай
  по смыслу и не угадывай по названию.
- На каждый факт должна быть ссылка, откуда он взят.
- Не нашёл — ставь null или пустой список. Это нормальный и ожидаемый ответ.
- Телефон и почту бери только со страниц самого бизнеса или из его карточек
  на площадках, а не из справочников-агрегаторов сомнительного качества.
- Соцсети компании (Telegram, ВКонтакте) — это не личные контакты руководителя.

Ответ — ОДИН объект JSON без пояснений вокруг, по схеме:
{
  "is_open": true | false | null,
  "status_note": "коротко, что известно о работе бизнеса",
  "website": "https://... или null",
  "website_source": "ссылка, где увидел этот адрес",
  "aggregators": [{"name": "Суточно.ру", "url": "https://..."}],
  "director": {"name": "ФИО или null", "post": "должность или null",
               "source": "ссылка"},
  "contacts": {"phone": "+7... или null", "telegram": "ник или null",
               "vk": "адрес или null", "email": "почта или null",
               "source": "ссылка"},
  "problems": ["проблема с онлайн-присутствием", "..."],
  "summary": "2-4 предложения о бизнесе и его ситуации",
  "sources": ["все использованные ссылки"]
}"""


def available():
    return bool(anthropic and config.ANTHROPIC_API_KEY)


def _prompt(lead):
    known = [f"Название: {lead.get('name')}"]
    for label, key in (("Категория", "category"), ("Адрес", "address"),
                       ("Известный сайт", "website"), ("Телефон", "phone"),
                       ("В реестре", "org_name"), ("ИНН", "inn"),
                       ("Юр. адрес", "legal_address")):
        if lead.get(key):
            known.append(f"{label}: {lead[key]}")

    site_state = {
        "dead": "известный сайт не открывается — вероятно, сменили домен",
        "none": "сайт неизвестен",
        "blocked": "сайт закрыт защитой, проверить автоматически не удалось",
    }.get(lead.get("site_status"), "")

    tail = f"\nЧто уже знаем о сайте: {site_state}" if site_state else ""
    return ("Найди информацию об этом объекте в Екатеринбурге и верни JSON "
            "по заданной схеме.\n\n" + "\n".join(known) + tail)


def _extract_json(text):
    """Достаёт объект JSON из ответа, даже если вокруг оказался текст."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except ValueError:
        return None


def _call(client, messages, use_fallbacks=True):
    """Один запрос к модели. Серверный поиск может прерываться — продолжаем."""
    extra = {}
    if use_fallbacks:
        # Классификаторы безопасности иногда отклоняют запрос: пусть API сам
        # переключится на запасную модель, а не роняет всю проверку.
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

    for _ in range(MAX_RESUMES):
        response = client.beta.messages.create(
            model=config.AI_MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            tools=TOOLS,
            messages=messages,
            **extra,
        )
        if response.stop_reason != "pause_turn":
            return response
        # Серверный цикл поиска упёрся в лимит шагов: дописываем ответ
        # модели и просим продолжить. Своего текста добавлять нельзя.
        messages.append({"role": "assistant", "content": response.content})

    return response


def research(lead):
    """Возвращает (данные, ошибка). Исключений наружу не бросает."""
    if anthropic is None:
        return None, "Не установлен пакет anthropic"
    if not config.ANTHROPIC_API_KEY:
        return None, "Не задан ANTHROPIC_API_KEY"

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=300.0)
    messages = [{"role": "user", "content": _prompt(lead)}]

    try:
        response = _call(client, messages)
    except anthropic.BadRequestError as exc:
        # Запасные модели — необязательная возможность; если API её не принял,
        # повторяем обычным запросом, чтобы проверка всё равно прошла.
        if "fallback" in str(exc).lower() or "beta" in str(exc).lower():
            try:
                response = _call(client, messages, use_fallbacks=False)
            except Exception as retry_exc:
                return None, f"{type(retry_exc).__name__}: {retry_exc}"
        else:
            return None, f"Запрос отклонён: {exc}"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        return None, f"Модель отказалась отвечать ({getattr(details, 'category', '—')})"

    text = "".join(b.text for b in response.content if b.type == "text")
    data = _extract_json(text)
    if data is None:
        return None, "Модель вернула не JSON"

    usage = getattr(response, "usage", None)
    data["_usage"] = {
        "input": getattr(usage, "input_tokens", 0),
        "output": getattr(usage, "output_tokens", 0),
    }
    data["_model"] = getattr(response, "model", config.AI_MODEL)
    return data, ""
