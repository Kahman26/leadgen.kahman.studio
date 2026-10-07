# -*- coding: utf-8 -*-
"""Настройки сборщика лидов. Город — здесь, правила ниш — в niche_rules.py."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path):
    """Локальный .env рядом с кодом: КЛЮЧ=значение по строке.

    На сервере переменные подставляет systemd из /etc/leadgen.env, а на своём
    компьютере удобнее положить их в файл. Заданное явно (в окружении или
    в bat-файле, даже пустым) важнее файла.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_env_file(BASE_DIR / ".env")

DB_PATH = os.getenv("LEADGEN_DB", str(BASE_DIR / "leads.db"))

# Часовой пояс города, в часах от UTC. Сервер живёт по UTC, а отчёт о рабочем
# времени должен ложиться на дни так, как их видит сотрудник. Екатеринбург +5.
TZ_OFFSET_HOURS = int(os.getenv("LEADGEN_TZ_OFFSET", "5"))

# ── Город ────────────────────────────────────────────────────────────────────
CITY_NAME = "Екатеринбург"
CITY_REGION = "Свердловская"
# Код региона по КЛАДР. Dadata молча игнорирует фильтр по НАЗВАНИЮ региона
# и отдаёт компании со всей страны — работает только код.
# Свердловская область — 66.
CITY_KLADR = "66"
# bbox: (мин.широта, мин.долгота, макс.широта, макс.долгота)
# Взят с запасом — захватывает пригород: Сысерть, Верхнюю Пышму, Чусовское озеро,
# где стоит основная масса загородных баз, домиков и бань.
CITY_BBOX = (56.55, 60.10, 57.10, 61.10)

# ── Ниши ─────────────────────────────────────────────────────────────────────
# Что искать под каждую нишу — теги OpenStreetMap, запросы и ОКВЭД для
# ЕГРЮЛ — описано в niche_rules.py.

# ── Dadata (ЕГРЮЛ) ───────────────────────────────────────────────────────────
# Бесплатно до 10 000 запросов в сутки. Ключ: https://dadata.ru/profile/#info
DADATA_TOKEN = os.getenv("DADATA_TOKEN", "")
DADATA_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/party"

# ── Аудит сайтов ─────────────────────────────────────────────────────────────
HTTP_TIMEOUT = 15
HTTP_WORKERS = 8           # параллельных проверок сайтов
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
CRAWL_DELAY = 0.4          # пауза между запросами к одному хосту, сек
OUTDATED_YEARS = 3         # копирайт старше стольки лет считаем устаревшим

# PageSpeed Insights — опционально, без ключа шаг просто пропускается.
PAGESPEED_KEY = os.getenv("PAGESPEED_KEY", "")

# ── Веб-интерфейс ────────────────────────────────────────────────────────────
HOST = os.getenv("LEADGEN_HOST", "127.0.0.1")
PORT = int(os.getenv("LEADGEN_PORT", "8765"))
