# -*- coding: utf-8 -*-
"""Хранилище лидов: SQLite, одна таблица + журнал запусков."""

import json
import sqlite3
import threading
import time
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
    site_status     TEXT,                -- none | dead | parked | blocked | ok
    parked_reason   TEXT DEFAULT '',     -- почему домен отвечает, но сайта нет
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
    status          TEXT DEFAULT 'new',  -- см. STATUSES в app.py
    note            TEXT DEFAULT '',
    priority        INTEGER,             -- оценка 1–10, которую ставит человек

    -- попытки дозвона: дубль из calls, чтобы список рисовался одним запросом
    call_count      INTEGER DEFAULT 0,
    last_call_at    TEXT DEFAULT '',     -- unix-время UTC строкой, '' если не звонили

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
    ai_company      TEXT DEFAULT '',     -- JSON: реквизиты по версии поиска
    ai_is_open      TEXT DEFAULT '',     -- true | false: работает ли бизнес по версии поиска

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


-- Рабочее время сотрудников. Пишем не каждый сигнал браузера, а сразу
-- отрезки: пока сигналы идут подряд, у последнего отрезка сдвигается конец.
-- Так за смену остаётся несколько строк вместо нескольких сотен, и отрезок
-- сам по себе отвечает на вопрос «в какие промежутки человек работал».
CREATE TABLE IF NOT EXISTS activity (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    login    TEXT NOT NULL,
    started  INTEGER NOT NULL,     -- unix-время первого сигнала отрезка, UTC
    ended    INTEGER NOT NULL      -- unix-время последнего сигнала отрезка
);


-- Попытки дозвона. Отдельная таблица, а не счётчик в leads: по ней считается
-- воронка и процент дозвона в разрезе сотрудников и периодов, а счётчик такого
-- вопроса не переживёт. Счётчики в leads — только для быстрой отрисовки списка,
-- источник правды здесь.
CREATE TABLE IF NOT EXISTS calls (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    login   TEXT NOT NULL,        -- кто звонил
    ts      INTEGER NOT NULL,     -- unix-время, UTC
    outcome TEXT NOT NULL         -- no_answer | answered
);


-- Сделки по лиду. Цена договора и фактические поступления разведены
-- намеренно: по договору с продажником вознаграждение считается от того,
-- что реально пришло, а не от того, что подписали. Источник правды для всех
-- денежных расчётов — таблица payments, а deals.amount только план.
CREATE TABLE IF NOT EXISTS deals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,          -- project — разовая разработка, retainer — абонентка
    title       TEXT DEFAULT '',
    amount      INTEGER NOT NULL,       -- целые рубли; для retainer — месячный платёж
    contract_at TEXT,                   -- дата договора, ISO
    owner_login TEXT DEFAULT '',        -- кто привёл: по нему считается вознаграждение
    is_repeat   INTEGER DEFAULT 0,      -- повторная сделка: по ней своя ставка
    state       TEXT DEFAULT 'active',  -- active | done | cancelled
    created_at  TEXT,
    created_by  TEXT DEFAULT ''
);


-- Фактические поступления. Деньги целыми рублями: через плавающую точку
-- их считать нельзя — копейки расходятся на первой же сотне строк.
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id    INTEGER NOT NULL,
    amount     INTEGER NOT NULL,
    paid_at    TEXT,                    -- дата поступления, ISO
    kind       TEXT DEFAULT 'other',    -- first | final | monthly | other
    note       TEXT DEFAULT '',
    created_at TEXT,
    created_by TEXT DEFAULT ''
);


-- Заметки по лиду лентой, а не одним полем: с базой работают несколько
-- человек, и в общем поле каждый затирал бы чужой текст. Удаление мягкое —
-- журнал изменений не должен ссылаться на исчезнувшие записи.
CREATE TABLE IF NOT EXISTS lead_notes (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL,
    login   TEXT NOT NULL,
    ts      INTEGER NOT NULL,        -- unix-время, UTC
    text    TEXT NOT NULL,
    deleted INTEGER DEFAULT 0
);


-- Настройки расчётов: ставки вознаграждения, доли конвертов, пороги найма.
-- В базе, а не в коде: договор с продажником прямо предусматривает пересмотр
-- ставок, и менять их должен владелец через интерфейс, а не правка исходников
-- с деплоем. Значения лежат строками JSON — так в одной таблице уживаются
-- и числа, и словари долей.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);


-- Отметки о разовых переделках базы. Нужна, чтобы перенос данных не повторялся
-- при каждом запуске: «в таблице пусто» плохой признак — человек мог всё
-- удалить сам, и тогда перенос вернул бы убранное.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);


-- Журнал изменений: кто, когда и что поменял. Пишется на уровне приложения,
-- а не триггером: триггер не знает, какой человек вошёл, а именно это и нужно,
-- когда разбираешь чужую ошибку. Имя объекта продублировано намеренно —
-- лид могут переименовать, а лента должна читаться и через полгода.
CREATE TABLE IF NOT EXISTS history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,      -- unix-время, UTC
    login     TEXT NOT NULL,         -- кто; @system — сбор и перепроверка
    lead_id   INTEGER,               -- NULL у общих действий: импорт, запуск сбора
    lead_name TEXT DEFAULT '',
    action    TEXT NOT NULL,
    field     TEXT DEFAULT '',
    old_value TEXT DEFAULT '',
    new_value TEXT DEFAULT '',
    reverted  INTEGER DEFAULT 0      -- эту правку уже вернули назад
);


-- Ниши и категории. Подробности — в niches.py. key — название в нижнем
-- регистре и без ё: lower() в SQLite кириллицу не понимает, а дубли
-- «Баня» и «баня» нужно ловить на уровне базы.
CREATE TABLE IF NOT EXISTS niches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT UNIQUE,             -- для кода: booking, auto; у заведённых людьми NULL
    title      TEXT NOT NULL,
    key        TEXT NOT NULL UNIQUE,
    sort       INTEGER DEFAULT 0,
    created_at TEXT,
    created_by TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS categories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    niche_id   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    key        TEXT NOT NULL,
    sort       INTEGER DEFAULT 0,
    created_at TEXT,
    created_by TEXT DEFAULT '',
    UNIQUE(niche_id, key)
);

-- Прежние названия категорий после переименования и объединения. Сбор
-- продолжает присылать «Отель» из config.CATEGORY_MAP, даже если категорию
-- переименовали в «Отели», — по синониму он попадёт куда надо.
CREATE TABLE IF NOT EXISTS category_aliases (
    niche_id    INTEGER NOT NULL,
    key         TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    PRIMARY KEY (niche_id, key)
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
CREATE INDEX IF NOT EXISTS idx_activity_login ON activity(login, started);
CREATE INDEX IF NOT EXISTS idx_history_lead  ON history(lead_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_history_ts    ON history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_history_login ON history(login, ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_lead ON calls(lead_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_calls_ts   ON calls(ts DESC);
CREATE INDEX IF NOT EXISTS idx_notes_lead ON lead_notes(lead_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_deals_lead ON deals(lead_id);
CREATE INDEX IF NOT EXISTS idx_deals_date ON deals(contract_at);
CREATE INDEX IF NOT EXISTS idx_pay_deal   ON payments(deal_id);
CREATE INDEX IF NOT EXISTS idx_pay_date   ON payments(paid_at);
CREATE INDEX IF NOT EXISTS idx_leads_niche    ON leads(niche_id);
CREATE INDEX IF NOT EXISTS idx_leads_category ON leads(category_id);
CREATE INDEX IF NOT EXISTS idx_categories_niche ON categories(niche_id, sort);
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
    ("ai_company", "TEXT DEFAULT ''"),
    ("ai_is_open", "TEXT DEFAULT ''"),
    ("parked_reason", "TEXT DEFAULT ''"),
    ("call_count", "INTEGER DEFAULT 0"),
    ("last_call_at", "TEXT DEFAULT ''"),
    ("niche_id", "INTEGER"),
    ("category_id", "INTEGER"),
    ("map_url", "TEXT DEFAULT ''"),
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
    _migrate_notes(c)
    _seed_settings(c)
    # Импорт внутри функции: niches знает про db, на уровне модуля был бы круг
    import niches
    niches.migrate(c)


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
    "name", "category", "category_id", "address", "lat", "lon", "source_detail",
    "inn", "ogrn", "org_name", "org_status", "org_status_text",
    "director", "director_post", "okved", "legal_address", "registered_at",
    "liquidated_at", "employee_count", "dadata_confidence", "dadata_match",
    "phone", "phones", "telegram", "vk", "whatsapp", "email", "contact_source",
    "website", "final_url", "site_status", "parked_reason",
    "http_code", "https", "mobile_ready",
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

    # Тянем строку целиком: журналу нужны прежние значения, чтобы записать,
    # что именно переписал сбор.
    row = c.execute(
        "SELECT * FROM leads WHERE source=? AND source_ref=?",
        (lead.get("source"), lead.get("source_ref")),
    ).fetchone()

    # Текстовую категорию сбора раскладываем по справочнику ниш
    import niches
    niches.apply(c, lead, row)

    if row:
        fields = [f for f in UPSERT_FIELDS if f in lead]
        # Всё, что человек исправил руками, сбор перезаписывать не должен:
        # иначе следующий прогон вернёт мёртвую ссылку или чужой телефон.
        protected = set(manual_list(row["manual_fields"]))
        if row["website_manual"]:
            protected.add("website")
        # Категория — это и название, и номер: защищаем обе колонки разом
        if "category" in protected:
            protected.add("category_id")
        if protected:
            fields = [f for f in fields if f not in protected]
        sets = ", ".join(f"{f}=?" for f in fields) + ", updated_at=?"
        vals = [lead[f] for f in fields] + [lead["updated_at"], row["id"]]
        c.execute(f"UPDATE leads SET {sets} WHERE id=?", vals)
        c.commit()
        # Импорт внутри функции: history знает про db, и на уровне модуля
        # получился бы круг.
        import history
        history.log_changes(history.SYSTEM, row,
                            {f: lead[f] for f in fields},
                            action="recheck", only=history.WATCHED)
        return "updated"

    lead["created_at"] = lead["updated_at"]
    fields = [f for f in UPSERT_FIELDS + ["niche_id", "source", "source_ref",
                                          "created_at", "updated_at"]
              if f in lead]
    ph = ", ".join("?" * len(fields))
    cur = c.execute(
        f"INSERT INTO leads ({', '.join(fields)}) VALUES ({ph})",
        [lead[f] for f in fields],
    )
    c.commit()
    import history
    history.log(history.SYSTEM, "create", lead=cur.lastrowid,
                new=lead.get("name") or "")
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


# ── заметки ──────────────────────────────────────────────────────────────────

# Тот же автор-автомат, что и в history.SYSTEM. Литерал, а не импорт: history
# знает про db, и на уровне модуля получился бы круг.
SYSTEM_LOGIN = "@system"


def _migrate_notes(c):
    """Переносит старую одиночную заметку каждого лида в ленту. Один раз.

    Автор берётся из журнала: последняя правка поля note и есть тот, кто писал.
    Если правки нет — заметка старше журнала, и автора честнее не выдумывать.
    """
    if c.execute("SELECT 1 FROM meta WHERE key='notes_migrated'").fetchone():
        return 0
    moved = 0
    for r in c.execute("SELECT id, note FROM leads WHERE COALESCE(note,'') <> ''").fetchall():
        h = c.execute("SELECT login, ts FROM history WHERE lead_id=? AND field='note' "
                      "ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        c.execute("INSERT INTO lead_notes (lead_id, login, ts, text) VALUES (?,?,?,?)",
                  (r["id"], h["login"] if h else SYSTEM_LOGIN,
                   h["ts"] if h else int(time.time()), r["note"]))
        moved += 1
    c.execute("INSERT INTO meta (key, value) VALUES ('notes_migrated', ?)", (now(),))
    c.commit()
    return moved


def notes_for(lead_id: int) -> list:
    """Лента заметок лида, новые сверху. Удалённые не отдаём."""
    # Сортируем по времени, а не по id: у перенесённых заметок дата взята
    # из журнала и может быть старше, чем у записанных раньше них строк.
    return [dict(r) for r in conn().execute(
        "SELECT id, lead_id, login, ts, text FROM lead_notes "
        "WHERE lead_id=? AND deleted=0 ORDER BY ts DESC, id DESC", (lead_id,))]


def _sync_note(c, lead_id: int):
    """leads.note — зеркало последней заметки.

    Колонку не убираем: на неё опираются выгрузка в CSV и поиск по списку.
    Пересчитываем и при удалении тоже, иначе в выгрузке останется текст,
    которого в ленте уже нет.
    """
    r = c.execute("SELECT text FROM lead_notes WHERE lead_id=? AND deleted=0 "
                  "ORDER BY ts DESC, id DESC LIMIT 1", (lead_id,)).fetchone()
    c.execute("UPDATE leads SET note=? WHERE id=?", (r["text"] if r else "", lead_id))


def add_note(lead_id: int, login: str, text: str) -> dict:
    c = conn()
    ts = int(time.time())
    cur = c.execute("INSERT INTO lead_notes (lead_id, login, ts, text) VALUES (?,?,?,?)",
                    (lead_id, login or "", ts, text))
    _sync_note(c, lead_id)
    c.commit()
    return {"id": cur.lastrowid, "lead_id": lead_id, "login": login, "ts": ts, "text": text}


def note_by_id(note_id: int):
    return conn().execute(
        "SELECT * FROM lead_notes WHERE id=? AND deleted=0", (note_id,)).fetchone()


def delete_note(note) -> None:
    c = conn()
    c.execute("UPDATE lead_notes SET deleted=1 WHERE id=?", (note["id"],))
    _sync_note(c, note["lead_id"])
    c.commit()


def last_note_authors() -> dict:
    """{id лида: автор последней заметки} — одним запросом, для выгрузки."""
    return {r["lead_id"]: r["login"] for r in conn().execute(
        "SELECT lead_id, login, MAX(ts * 1000000 + id) FROM lead_notes "
        "WHERE deleted=0 GROUP BY lead_id")}


# ── настройки расчётов ───────────────────────────────────────────────────────

# Значения по умолчанию. Здесь они нужны дважды: чтобы заполнить пустую базу
# и чтобы ответить, если ключа в базе почему-то нет — раздел метрик не должен
# падать из-за одной недостающей строки.
DEFAULT_SETTINGS = {
    # Договор с продажником
    "rate_new": 20,                # % вознаграждения по новым сделкам
    "rate_repeat": 10,             # % по повторным
    "first_share_min": 30,         # доля первой оплаты, ниже — сделка не в зачёт премии
    "bonus_levels": [[6, 8000], [10, 20000]],   # [сделок за месяц, премия]

    # Конверты: раскладка каждого поступления. Сумма должна быть 100.
    "envelopes": {
        "Продажи": 20, "Производство": 25, "Сопровождение": 10,
        "Налоги": 7, "Операционка": 5, "Резерв": 10, "Владелец": 23,
    },
    # Во что обходится роль в месяц: по этому считается, хватает ли в конверте
    # на три месяца её работы.
    "role_costs": {"Производство": 60000, "Сопровождение": 40000},

    # Пороги найма
    "hiring": {
        "presale_deals": 3,        # сделок в месяц
        "presale_months": 2,       # столько месяцев подряд
        "context_retainers": 3,    # активных абонентских сделок
        "dev_projects": 5,         # проектов в месяц
        "dev_months": 2,
        "marketer_mrr": 150000,
    },

    # Налоговый режим
    "npd_limit": 2400000,          # лимит дохода по НПД за календарный год
    "npd_warn": 70,                # с какого % заполнения предупреждать
}


def _seed_settings(c):
    """Досыпает недостающие настройки. Существующие не трогает —
    иначе правка владельца откатывалась бы при каждом перезапуске."""
    for key, value in DEFAULT_SETTINGS.items():
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)",
                  (key, json.dumps(value, ensure_ascii=False)))
    c.commit()


def settings() -> dict:
    """Настройки поверх значений по умолчанию: недостающий ключ не должен
    ронять весь раздел метрик."""
    out = dict(DEFAULT_SETTINGS)
    for r in conn().execute("SELECT key, value FROM settings"):
        if r["key"] not in DEFAULT_SETTINGS:
            continue
        try:
            out[r["key"]] = json.loads(r["value"])
        except (ValueError, TypeError):
            pass          # битое значение — остаётся умолчание
    return out


def save_setting(key: str, value) -> None:
    c = conn()
    c.execute("INSERT INTO settings (key, value) VALUES (?,?) "
              "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value, ensure_ascii=False)))
    c.commit()


# ── попытки дозвона ──────────────────────────────────────────────────────────

def add_call(lead_id: int, login: str, outcome: str) -> dict:
    """Пишет попытку дозвона и обновляет счётчики лида. Отдаёт новые счётчики.

    Счётчики в leads дублируют calls намеренно: список рисуется одним запросом,
    и подзапрос на каждую строку там не нужен. Источник правды — таблица calls,
    счётчики живут только ради показа.
    """
    c = conn()
    ts = int(time.time())
    c.execute("INSERT INTO calls (lead_id, login, ts, outcome) VALUES (?,?,?,?)",
              (lead_id, login or "", ts, outcome))
    c.execute("UPDATE leads SET call_count = COALESCE(call_count, 0) + 1, "
              "last_call_at = ? WHERE id = ?", (str(ts), lead_id))
    c.commit()
    return {"call_count": call_count(lead_id), "last_call_at": str(ts)}


def call_count(lead_id: int) -> int:
    """Считаем по calls, а не по счётчику: счётчик мог отстать от правды."""
    return conn().execute(
        "SELECT COUNT(*) n FROM calls WHERE lead_id=?", (lead_id,)).fetchone()["n"]


def call_ts(value) -> int:
    """last_call_at лежит строкой (так объявлена колонка) — приводим к числу."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ── сделки и платежи ─────────────────────────────────────────────────────────

DEAL_KINDS = ("project", "retainer")
DEAL_STATES = ("active", "done", "cancelled")
PAYMENT_KINDS = ("first", "final", "monthly", "other")


def _deal_totals(deal: dict, pays: list) -> dict:
    """Считает по сделке то, что нужно и в карточке, и в договоре с продажником.

    Всё считается от фактических поступлений: цена договора — это план,
    а вознаграждение и премия завязаны на пришедшие деньги.
    """
    paid = sum(p["amount"] for p in pays)
    amount = deal["amount"] or 0
    # Первой считается самая ранняя по дате поступления, а не по порядку ввода:
    # платежи заводят задним числом.
    first = min(pays, key=lambda p: (p["paid_at"] or "", p["id"]))["amount"] if pays else 0
    deal["payments"] = pays
    deal["paid"] = paid
    # Для абонентки остаток бессмыслен: там платят помесячно и бесконечно.
    deal["left"] = max(amount - paid, 0) if deal["kind"] == "project" else 0
    deal["first_amount"] = first
    deal["months"] = len(pays) if deal["kind"] == "retainer" else 0
    # Доля первой оплаты и полоса выполнения имеют смысл только для проекта:
    # у абонентки amount — это месячный платёж, а не план по договору, и
    # делить поступления на него нельзя.
    if deal["kind"] == "project" and amount:
        deal["first_share"] = round(first * 100 / amount) if first else 0
        deal["progress"] = min(round(paid * 100 / amount), 100)
    else:
        deal["first_share"] = 0
        deal["progress"] = 0
    return deal


def deals_for(lead_id: int) -> list:
    """Сделки лида с платежами. Два запроса вместо запроса на сделку."""
    c = conn()
    deals = [dict(r) for r in c.execute(
        "SELECT * FROM deals WHERE lead_id=? ORDER BY COALESCE(contract_at,'') DESC, id DESC",
        (lead_id,))]
    if not deals:
        return []
    ids = tuple(d["id"] for d in deals)
    ph = ",".join("?" * len(ids))
    by_deal = {}
    for r in c.execute(f"SELECT * FROM payments WHERE deal_id IN ({ph}) "
                       "ORDER BY COALESCE(paid_at,'') DESC, id DESC", ids):
        by_deal.setdefault(r["deal_id"], []).append(dict(r))
    return [_deal_totals(d, by_deal.get(d["id"], [])) for d in deals]


def deal_by_id(deal_id: int):
    return conn().execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()


def add_deal(lead_id: int, login: str, **f) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO deals (lead_id, kind, title, amount, contract_at, owner_login, "
        "is_repeat, state, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (lead_id, f["kind"], f.get("title", ""), f["amount"], f.get("contract_at", ""),
         f.get("owner_login", ""), 1 if f.get("is_repeat") else 0,
         f.get("state", "active"), now(), login or ""))
    c.commit()
    return cur.lastrowid


def update_deal(deal_id: int, fields: dict) -> None:
    if not fields:
        return
    c = conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    c.execute(f"UPDATE deals SET {sets} WHERE id=?", list(fields.values()) + [deal_id])
    c.commit()


def payments_count(deal_id: int) -> int:
    return conn().execute(
        "SELECT COUNT(*) n FROM payments WHERE deal_id=?", (deal_id,)).fetchone()["n"]


def delete_deal(deal_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    c.commit()


def add_payment(deal_id: int, login: str, **f) -> int:
    c = conn()
    cur = c.execute(
        "INSERT INTO payments (deal_id, amount, paid_at, kind, note, created_at, created_by) "
        "VALUES (?,?,?,?,?,?,?)",
        (deal_id, f["amount"], f.get("paid_at", ""), f.get("kind", "other"),
         f.get("note", ""), now(), login or ""))
    c.commit()
    return cur.lastrowid


def payment_by_id(payment_id: int):
    return conn().execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()


def delete_payment(payment_id: int) -> None:
    c = conn()
    c.execute("DELETE FROM payments WHERE id=?", (payment_id,))
    c.commit()


def first_payment_at(lead_id: int) -> str:
    """Дата самого раннего поступления по лиду. По договору это и есть дата,
    когда сделка считается закрытой."""
    r = conn().execute(
        "SELECT MIN(COALESCE(p.paid_at,'')) d FROM payments p "
        "JOIN deals dl ON dl.id = p.deal_id WHERE dl.lead_id=?", (lead_id,)).fetchone()
    return (r["d"] or "") if r else ""


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
