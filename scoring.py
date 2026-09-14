# -*- coding: utf-8 -*-
"""Скоринг лида: главный признак, список пробелов и заготовка для разговора.

Логика намеренно простая и читаемая — маркетолог должен понимать,
почему лид оказался наверху списка.
"""

from datetime import datetime

import config
from enrich.site_audit import CHEAP_BUILDERS

SLOW_MS = 3000

# Код признака -> (человеко-читаемо, приоритет, вклад в балл)
# Приоритет решает, какой признак станет ГЛАВНЫМ в карточке лида.
REASONS = {
    "SITE_DEAD":     ("Сайт не открывается",        100, 55),
    "SITE_PARKED":   ("На домене нет сайта",          95, 52),
    "NO_SITE":       ("Нет сайта",                   90, 50),
    "NO_MOBILE":     ("Нет мобильной версии",        80, 30),
    "CHEAP_BUILDER": ("Сайт на дешёвом конструкторе", 70, 18),
    "OUTDATED":      ("Сайт устарел",                65, 16),
    "NO_HTTPS":      ("Нет HTTPS",                   55, 10),
    "NO_BOOKING":    ("Ни брони, ни заявки",         52, 20),
    "REQUEST_ONLY":  ("Бронь только через заявку",    48, 12),
    "SLOW":          ("Долго грузится",              35,  8),
    "LIQUIDATED":    ("Ликвидирована по ЕГРЮЛ",     120,  0),
    "BLOCKED":       ("Сайт не удалось проверить",   10,  0),
    "OK":            ("Сайт в порядке",               0,  0),
}

# Порог «горячего» лида. Откалиброван так, чтобы в него попадал объект
# с реальной проблемой И живым контактом: нет сайта (50) + телефон (10) = 60.
HOT = 60


def _pitch(code, lead, audit):
    """С чего начать разговор. Проблема клиента, а не наш оффер."""
    name = lead.get("name") or "вас"
    site = audit.get("final_url") or lead.get("website") or ""
    cms = audit.get("cms") or "конструкторе"
    year = audit.get("copyright_year")

    if code == "LIQUIDATED":
        return (f"По ЕГРЮЛ организация ликвидирована — звонить некуда. "
                f"Стоит скрыть лид, чтобы он не мешал в работе.")
    if code == "NO_SITE":
        return (f"У «{name}» нет своего сайта — значит все брони идут через Суточно, "
                f"Авито и Островок, а это 15–20% комиссии с каждой. Свой сайт с прямым "
                f"бронированием окупается за несколько заездов.")
    if code == "SITE_PARKED":
        why = audit.get("parked_reason") or "там парковка"
        return (f"Домен {site} у «{name}» отвечает, но сайта компании на нём уже нет — "
                f"{why}. Гость из поиска попадает на чужую страницу, а адрес при этом "
                f"до сих пор указан в справочниках и карточках площадок.")
    if code == "SITE_DEAD":
        return (f"Сайт {site} сейчас не открывается — гость, который ищет вас в Яндексе, "
                f"попадает в пустоту и уходит к соседям. Показать, что именно сломалось?")
    if code == "NO_MOBILE":
        return (f"Сайт не адаптирован под телефон — а жильё смотрят с мобильного примерно "
                f"в 7 случаях из 10. Гость видит мелкий текст, не может нажать «забронировать» "
                f"и закрывает вкладку.")
    if code == "CHEAP_BUILDER":
        return (f"Сайт собран на {cms} — это считывается гостем за секунду и бьёт по доверию "
                f"ровно в момент, когда он выбирает между вами и конкурентом с нормальным сайтом.")
    if code == "OUTDATED":
        y = f" (копирайт {year} года)" if year else ""
        return (f"Сайт выглядит заброшенным{y} — гость думает, что объект уже не работает, "
                f"и даже не звонит, чтобы уточнить.")
    if code == "NO_BOOKING":
        return (f"На сайте нельзя ни забронировать, ни даже оставить заявку — только "
                f"звонить. Всё, что приходит вне рабочего времени, уходит в агрегаторы "
                f"с их комиссией.")
    if code == "REQUEST_ONLY":
        return (f"Гость оставляет заявку и ждёт, пока менеджер перезвонит. Ночью и в "
                f"выходные такая заявка остывает, а на Суточно и Островке рядом можно "
                f"забронировать и оплатить сразу — выбирают это.")
    if code == "NO_HTTPS":
        return (f"Сайт без HTTPS — браузер пишет «Не защищено» прямо перед формой брони. "
                f"Это прямая потеря заявок.")
    return f"Сайт рабочий. Стоит смотреть на другие точки роста — трафик, контент, скорость."


def score_lead(lead, audit):
    """Возвращает (reason_code, reason_text, missing[], pitch, score)."""
    problems = []          # коды найденных проблем
    missing = []           # чего не хватает, словами

    # Ликвидированной компании сайт не нужен. Но обнулять лид можно только
    # при уверенном совпадении: при неточном мы рискуем выбросить живой
    # объект из-за ликвидированного однофамильца.
    dead_org = (lead.get("org_status") or "") in ("LIQUIDATED", "BANKRUPT")
    if dead_org and lead.get("dadata_confidence") == "high":
        when = lead.get("liquidated_at") or ""
        gaps = [f"Организация ликвидирована по ЕГРЮЛ{f' ({when})' if when else ''}"]
        if lead.get("org_name"):
            gaps.append(f"В реестре: {lead['org_name']}")
        return ("LIQUIDATED", REASONS["LIQUIDATED"][0], gaps,
                _pitch("LIQUIDATED", lead, audit), 0)

    status = audit.get("site_status")
    has_site = bool((lead.get("website") or "").strip())

    if status == "none" or not has_site:
        problems.append("NO_SITE")
        missing.append("Сайта нет вообще")
    elif status == "dead":
        problems.append("SITE_DEAD")
        code = audit.get("http_code")
        missing.append(f"Сайт не отвечает (код {code})" if code
                       else f"Сайт не отвечает ({audit.get('error') or 'нет соединения'})")
    elif status == "parked":
        problems.append("SITE_PARKED")
        missing.append(f"Домен отвечает, но сайта компании там нет: "
                       f"{audit.get('parked_reason') or 'парковка'}")
    elif status == "blocked":
        problems.append("BLOCKED")
        missing.append("Сайт закрыт защитой — нужна ручная проверка")
    else:
        if not audit.get("mobile_ready"):
            problems.append("NO_MOBILE")
            missing.append("Нет мобильной версии (нет meta viewport)")

        cms = audit.get("cms") or ""
        if cms in CHEAP_BUILDERS:
            problems.append("CHEAP_BUILDER")
            missing.append(f"Сделан на конструкторе {cms}")

        year = audit.get("copyright_year") or 0
        if year and year <= datetime.now().year - config.OUTDATED_YEARS:
            problems.append("OUTDATED")
            missing.append(f"Копирайт {year} года — сайт давно не обновляли")

        if not audit.get("https"):
            problems.append("NO_HTTPS")
            missing.append("Нет HTTPS — браузер помечает сайт как небезопасный")

        # Три состояния вместо «есть/нет»: настоящая система бронирования,
        # заявка с обратным звонком и полное отсутствие того и другого.
        booking = audit.get("booking_type") or "none"
        if booking == "none":
            problems.append("NO_BOOKING")
            missing.append("Гость не может ни забронировать, ни оставить заявку — "
                           "только звонить")
        elif booking == "request":
            problems.append("REQUEST_ONLY")
            how = audit.get("booking_engine") or "форма заявки"
            missing.append(f"Нет онлайн-бронирования, только заявка ({how}) — "
                           f"гость ждёт ответа менеджера")

        load = audit.get("load_ms") or 0
        if load > SLOW_MS:
            problems.append("SLOW")
            missing.append(f"Главная грузится {load / 1000:.1f} с")

    if (lead.get("org_status") or "") == "LIQUIDATING":
        missing.append("По ЕГРЮЛ организация в стадии ликвидации")
    elif dead_org:
        missing.append(f"Возможно ликвидирована: в реестре {lead.get('org_name')} "
                       f"числится закрытой, но совпадение неточное — проверьте")

    if not problems:
        problems.append("OK")

    # Главный признак — самый «болезненный» из найденных.
    code = max(problems, key=lambda p: REASONS[p][1])
    reason_text = REASONS[code][0]

    score = sum(REASONS[p][2] for p in problems)

    # Лид без контактов маркетологу бесполезен, каким бы плохим ни был сайт.
    contacts = [lead.get("phone"), lead.get("telegram"), lead.get("vk"),
                lead.get("whatsapp"), lead.get("email")]
    if not any(contacts):
        score -= 30
        missing.append("Контакты не найдены — нужен ручной поиск")
    else:
        if lead.get("phone"):
            score += 10
        if lead.get("telegram") or lead.get("whatsapp"):
            score += 5
        if lead.get("vk"):
            score += 3

    score = max(0, min(100, score))
    return code, reason_text, missing, _pitch(code, lead, audit), score
