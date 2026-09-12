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

    -- реквизиты (приходят из Dadata)
    inn             TEXT,
    ogrn            TEXT,
    director        TEXT,
    okved           TEXT,
    registered_at   TEXT,

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
    online_booking  INTEGER,             -- найден модуль бронирования
    booking_engine  TEXT,                -- TravelLine / Bnovo / ...
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
    hidden          INTEGER DEFAULT 0,   -- убран из списка (закрылись и т.п.)
    hidden_reason   TEXT DEFAULT '',

    -- ручная правка адреса сайта
    previous_website TEXT DEFAULT '',    -- что было до правки
    website_manual  INTEGER DEFAULT 0,   -- адрес задан руками, сбор его не трогает

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
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
"""

# Колонки, добавленные после первого релиза. CREATE TABLE IF NOT EXISTS
# не достроит их в уже существующей базе, поэтому досыпаем вручную.
MIGRATIONS = [
    ("hidden", "INTEGER DEFAULT 0"),
    ("hidden_reason", "TEXT DEFAULT ''"),
    ("previous_website", "TEXT DEFAULT ''"),
    ("website_manual", "INTEGER DEFAULT 0"),
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


def now():
    return datetime.now().isoformat(timespec="seconds")


# ── запись лидов ─────────────────────────────────────────────────────────────

# Поля, которые заполняет сборщик и которые можно безопасно обновлять.
UPSERT_FIELDS = [
    "name", "category", "address", "lat", "lon", "source_detail",
    "inn", "ogrn", "director", "okved", "registered_at",
    "phone", "phones", "telegram", "vk", "whatsapp", "email", "contact_source",
    "website", "final_url", "site_status", "http_code", "https", "mobile_ready",
    "online_booking", "booking_engine", "cms", "copyright_year", "load_ms",
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
        "SELECT id, website_manual FROM leads WHERE source=? AND source_ref=?",
        (lead.get("source"), lead.get("source_ref")),
    ).fetchone()

    if row:
        fields = [f for f in UPSERT_FIELDS if f in lead]
        # Адрес, исправленный руками, сбор перезаписывать не должен:
        # иначе следующий прогон вернёт мёртвую ссылку из OSM.
        if row["website_manual"]:
            fields = [f for f in fields if f != "website"]
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


def row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
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
