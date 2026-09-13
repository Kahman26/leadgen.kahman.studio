# -*- coding: utf-8 -*-
"""Хранилище лидов: SQLite, одна таблица + журнал запусков."""

import json
import sqlite3
import threading
from datetime import datetime

import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- кто это
    name            TEXT NOT NULL,
    category        TEXT,                -- Отель / Баня / Домики ...
    address         TEXT,
    lat             REAL,
    lon             REAL,

    -- откуда мы про них узнали
    source          TEXT NOT NULL,       -- osm | dadata | manual
    source_ref      TEXT,                -- ссылка на объект OSM или ИНН
    source_detail   TEXT,                -- человеко-читаемо: «OSM, tourism=hotel»

    -- реквизиты из ЕГРЮЛ (Dadata)
    inn             TEXT,
    ogrn            TEXT,
    org_name        TEXT,                -- как компания называется в реестре
    org_status      TEXT,                -- ACTIVE | LIQUIDATING | LIQUIDATED | ...
    org_status_text TEXT,                -- то же словами
    director        TEXT,
    director_post   TEXT,
    okved           TEXT,
    legal_address   TEXT,
    registered_at   TEXT,
    liquidated_at   TEXT,
    employee_count  INTEGER,
    dadata_confidence TEXT,              -- high | medium: насколько верим совпадению
    dadata_match    TEXT,                -- чем именно подтверждено

    -- контакты
    phone           TEXT,                -- основной
    phones          TEXT,                -- JSON-массив всех найденных
    telegram        TEXT,
    vk              TEXT,
    whatsapp        TEXT,
    email           TEXT,
    contact_source  TEXT,                -- JSON: {"phone":"сайт","telegram":"OSM"}

    -- сайт и его состояние
    website         TEXT,
    final_url       TEXT,
    site_status     TEXT,                -- none | dead | ok
    http_code       INTEGER,
    https           INTEGER,
    mobile_ready    INTEGER,
    online_booking  INTEGER,             -- 1 только для настоящей системы брони
    booking_type    TEXT,                -- engine | request | none
    booking_engine  TEXT,                -- TravelLine / Форма заявки / ...
    cms             TEXT,                -- Wix / uKit / Tilda / Bitrix ...
    copyright_year  INTEGER,
    load_ms         INTEGER,
    domain_expires  TEXT,
    domain_age_days INTEGER,

    -- ради чего всё затевалось
    reason_code     TEXT,                -- главный признак, по которому взяли в базу
    reason_text     TEXT,                -- то же словами, для маркетолога
    missing         TEXT,                -- JSON-массив: чего не хватает
    pitch           TEXT,                -- с чего начать разговор
    score           INTEGER DEFAULT 0,

    -- работа маркетолога
    status          TEXT DEFAULT 'new',  -- new | in_work | contacted | refused | deal
    note            TEXT DEFAULT '',
    priority        INTEGER,             -- оценка 1–10, которую ставит человек

    -- что нашла автопроверка через Claude
    ai_summary      TEXT DEFAULT '',     -- короткий рассказ про бизнес
    ai_problems     TEXT DEFAULT '',     -- JSON-список проблем
    ai_sources      TEXT DEFAULT '',     -- JSON-список ссылок, откуда это взято
    ai_found_site   TEXT DEFAULT '',     -- найденный актуальный сайт
    ai_director     TEXT DEFAULT '',     -- руководитель по версии поиска, не ЕГРЮЛ
    ai_aggregators  TEXT DEFAULT '',     -- JSON: где принимает брони (Суточно и т.п.)
    ai_checked_at   TEXT DEFAULT '',
    ai_model        TEXT DEFAULT '',
    ai_error        TEXT DEFAULT '',

    hidden          INTEGER DEFAULT 0,   -- убран из списка (закрылись и т.п.)
    hidden_reason   TEXT DEFAULT '',

    -- ручные правки
    previous_website TEXT DEFAULT '',    -- что было до правки адреса
    website_manual  INTEGER DEFAULT 0,   -- адрес задан руками, сбор его не трогает
    manual_fields   TEXT DEFAULT '',     -- JSON-список полей, исправленных руками

    created_at      TEXT,
    updated_at      TEXT,
    checked_at      TEXT,
    UNIQUE(source, source_ref)
);


-- Сессии входа. Лежат в базе, а не в памяти, чтобы перезапуск сервиса
-- не выкидывал всю команду обратно на форму логина.
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    login      TEXT NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    user_agent TEXT
);


-- Референсы: хорошие сайты по каждому типу объектов, на которые можно
-- ориентироваться при разговоре с клиентом и при проектировании.
CREATE TABLE IF NOT EXISTS refs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT NOT NULL,       -- те же категории, что и у лидов
    name          TEXT NOT NULL,
    url           TEXT NOT NULL UNIQUE,
    city          TEXT DEFAULT '',
    note          TEXT DEFAULT '',     -- чем хорош, словами
    strengths     TEXT DEFAULT '',     -- JSON-список: что именно подсмотреть

    -- объективная часть: тот же аудит, что и для лидов
    site_status   TEXT,
    http_code     INTEGER,
    https         INTEGER,
    mobile_ready  INTEGER,
    booking_type  TEXT,
    booking_engine TEXT,
    cms           TEXT,
    load_ms       INTEGER,
    page_title    TEXT DEFAULT '',

    checked_at    TEXT,
    created_at    TEXT
);


CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    stage       TEXT,
    found       INTEGER DEFAULT 0,
    added       INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    audited     INTEGER DEFAULT 0,
    error       TEXT
);
"""


def conn():
    """Отдельное соединение на поток — pipeline работает в фоне."""
    if not hasattr(_local, "c"):
        c = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c = c
    return _local.c


INDEXES = """
CREATE INDEX IF NOT EXISTS idx_leads_score  ON leads(score DESC);
CREATE INDEX IF NOT EXISTS idx_leads_reason ON leads(reason_code);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_leads_hidden ON leads(hidden);
CREATE INDEX IF NOT EXISTS idx_leads_org ON leads(org_status);
CREATE INDEX IF NOT EXISTS idx_leads_priority ON leads(priority DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_refs_category ON refs(category);
"""

# Колонки, добавленные после первого релиза. CREATE TABLE IF NOT EXISTS
# не достроит их в уже существующей базе, поэтому досыпаем вручную.
MIGRATIONS = [
    ("hidden", "INTEGER DEFAULT 0"),
    ("hidden_reason", "TEXT DEFAULT ''"),
    ("previous_website", "TEXT DEFAULT ''"),
    ("website_manual", "INTEGER DEFAULT 0"),
    ("booking_type", "TEXT DEFAULT ''"),
    ("org_name", "TEXT DEFAULT ''"),
    ("org_status", "TEXT DEFAULT ''"),
    ("org_status_text", "TEXT DEFAULT ''"),
    ("director_post", "TEXT DEFAULT ''"),
    ("legal_address", "TEXT DEFAULT ''"),
    ("liquidated_at", "TEXT DEFAULT ''"),
    ("employee_count", "INTEGER"),
    ("dadata_confidence", "TEXT DEFAULT ''"),
    ("dadata_match", "TEXT DEFAULT ''"),
    ("manual_fields", "TEXT DEFAULT ''"),
    ("priority", "INTEGER"),
    ("ai_summary", "TEXT DEFAULT ''"),
    ("ai_problems", "TEXT DEFAULT ''"),
    ("ai_sources", "TEXT DEFAULT ''"),
    ("ai_found_site", "TEXT DEFAULT ''"),
    ("ai_director", "TEXT DEFAULT ''"),
    ("ai_aggregators", "TEXT DEFAULT ''"),
    ("ai_checked_at", "TEXT DEFAULT ''"),
    ("ai_model", "TEXT DEFAULT ''"),
    ("ai_error", "TEXT DEFAULT ''"),
]


def init():
    c = conn()
    c.executescript(SCHEMA)

    existing = {r["name"] for r in c.execute("PRAGMA table_info(leads)")}
    for column, decl in MIGRATIONS:
        if column not in existing:
            c.execute(f"ALTER TABLE leads ADD COLUMN {column} {decl}")

    # Индексы строим только после ALTER TABLE: часть из них ссылается
    # на колонки, которых в старой базе ещё не было.
    c.executescript(INDEXES)
    c.commit()


SERVER_DB = "/var/lib/leadgen/leads.db"


def resolve_path(quiet=False):
    """Находит боевую базу и не даёт молча создать пустую рядом с кодом.

    Путь сервис получает из systemd (LEADGEN_DB), а в обычной консоли этой
    переменной нет. Без проверки sqlite создаст новый пустой файл, и скрипт
    отработает «успешно» на пустой базе — ошибку заметить почти невозможно.
    """
    import os
    from pathlib import Path

    if not os.getenv("LEADGEN_DB") and Path(SERVER_DB).exists():
        config.DB_PATH = SERVER_DB

    if not Path(config.DB_PATH).exists():
        raise SystemExit(f"Базы нет: {config.DB_PATH}\n"
                         f"Укажите её явно: LEADGEN_DB=/путь/к/leads.db")

    if not quiet:
        import sys
        print(f"# база: {config.DB_PATH}", file=sys.stderr)
    return config.DB_PATH


def now():
    return datetime.now().isoformat(timespec="seconds")


# ── запись лидов ─────────────────────────────────────────────────────────────

# Поля, которые заполняет сборщик и которые можно безопасно обновлять.
UPSERT_FIELDS = [
    "name", "category", "address", "lat", "lon", "source_detail",
    "inn", "ogrn", "org_name", "org_status", "org_status_text",
    "director", "director_post", "okved", "legal_address", "registered_at",
    "liquidated_at", "employee_count", "dadata_confidence", "dadata_match",
    "phone", "phones", "telegram", "vk", "whatsapp", "email", "contact_source",
    "website", "final_url", "site_status", "http_code", "https", "mobile_ready",
    "online_booking", "booking_type", "booking_engine", "cms", "copyright_year", "load_ms",
    "domain_expires", "domain_age_days",
    "reason_code", "reason_text", "missing", "pitch", "score", "checked_at",
]


def upsert(lead: dict) -> str:
    """Добавляет лида или обновляет существующего. Возвращает 'added'/'updated'.

    Поля `status` и `note` принадлежат маркетологу и при пересборе не трогаются.
    """
    c = conn()
    lead = dict(lead)
    lead["updated_at"] = now()

    row = c.execute(
        "SELECT id, website_manual, manual_fields FROM leads "
        "WHERE source=? AND source_ref=?",
        (lead.get("source"), lead.get("source_ref")),
    ).fetchone()

    if row:
        fields = [f for f in UPSERT_FIELDS if f in lead]
        # Всё, что человек исправил руками, сбор перезаписывать не должен:
        # иначе следующий прогон вернёт мёртвую ссылку или чужой телефон.
        protected = set(manual_list(row["manual_fields"]))
        if row["website_manual"]:
            protected.add("website")
        if protected:
            fields = [f for f in fields if f not in protected]
        sets = ", ".join(f"{f}=?" for f in fields) + ", updated_at=?"
        vals = [lead[f] for f in fields] + [lead["updated_at"], row["id"]]
        c.execute(f"UPDATE leads SET {sets} WHERE id=?", vals)
        c.commit()
        return "updated"

    lead["created_at"] = lead["updated_at"]
    fields = [f for f in UPSERT_FIELDS + ["source", "source_ref", "created_at", "updated_at"]
              if f in lead]
    ph = ", ".join("?" * len(fields))
    c.execute(
        f"INSERT INTO leads ({', '.join(fields)}) VALUES ({ph})",
        [lead[f] for f in fields],
    )
    c.commit()
    return "added"


def json_list(value):
    """Разбирает JSON-список как есть. Битые данные не должны ломать сбор."""
    try:
        parsed = json.loads(value or "[]")
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def manual_list(value):
    """Список имён полей: элементы приводим к строке.

    Для списков объектов (например, площадок бронирования) это не подходит —
    там нужен json_list, иначе словари превратятся в строки.
    """
    return [str(x) for x in json_list(value)]


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["manual_fields"] = manual_list(d.get("manual_fields"))
    for f in ("ai_problems", "ai_sources", "ai_aggregators"):
        d[f] = json_list(d.get(f))
    for f in ("phones", "missing", "contact_source"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except (ValueError, TypeError):
                d[f] = []
        else:
            d[f] = [] if f != "contact_source" else {}
    return d


# ── журнал запусков ──────────────────────────────────────────────────────────

def run_start() -> int:
    c = conn()
    cur = c.execute("INSERT INTO runs (started_at, stage) VALUES (?,?)", (now(), "старт"))
    c.commit()
    return cur.lastrowid


def run_update(run_id: int, **kw):
    if not kw:
        return
    c = conn()
    sets = ", ".join(f"{k}=?" for k in kw)
    c.execute(f"UPDATE runs SET {sets} WHERE id=?", list(kw.values()) + [run_id])
    c.commit()


def run_finish(run_id: int, error: str = None):
    run_update(run_id, finished_at=now(), stage="готово" if not error else "ошибка", error=error)


def last_run():
    r = conn().execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None
