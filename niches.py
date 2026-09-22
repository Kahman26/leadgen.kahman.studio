# -*- coding: utf-8 -*-
"""Ниши и категории объектов.

Ниша — крупное направление (бронирование, авто, интернет-магазины),
категория — тип объекта внутри ниши (отель, баня, детейлинг). У лида ниша
обязательна, категория нет: объект можно завести, пока тип ещё не ясен.

Лид ссылается на них по номеру, а не по названию: переименование сразу видно
у всех лидов, и дубли вида «Баня» и «баня» не появляются. Текстовое поле
leads.category осталось зеркалом названия категории — на него опираются
выгрузка, запрос для проверки через Claude и старые записи журнала.

Создавать ниши и категории может любой сотрудник, переименовывать,
объединять, двигать и удалять — только владелец базы.
"""

import re

import db

SYSTEM = "@system"

# Ниша, в которую попадает всё, что приносит сбор: правила сбора в config.py
# пока написаны только под бронирование.
DEFAULT_CODE = "booking"

# Стартовый справочник. Заводится один раз: если потом владелец что-то
# удалит, при перезапуске оно не вернётся. Порядок здесь — порядок вкладок.
# Коды нужны коду: по ним будут подключаться правила сбора под нишу.
SEED = [
    ("booking", "Бронирование и размещение", [
        "Отель", "Хостел", "Гостевой дом", "Апартаменты", "База отдыха",
        "Домики / коттедж", "Глэмпинг / кемпинг", "Баня / сауна",
        "Площадки: лофты, банкетные залы, фотостудии",
    ]),
    ("repair", "Ремонт и строительство", [
        "Строительство бань и домов под ключ",
        "Ремонт квартир под ключ",
        "Кухни и корпусная мебель на заказ",
        "Окна, двери, натяжные потолки",
        "Кровля, фасады, заборы",
        "Септики, скважины, водоснабжение участка",
        "Ландшафтный дизайн, благоустройство",
    ]),
    ("auto", "Авто", [
        "Детейлинг, полировка, керамика",
        "Кузовной ремонт и покраска",
        "Ремонт АКПП, двигателей",
        "Автосервис, СТО",
        "Тонировка, оклейка плёнкой, шумоизоляция",
        "Шиномонтаж, хранение шин",
        "Автомойка",
        "Автоэлектрик, установка сигнализаций и ГБО",
    ]),
    ("medicine", "Медицина и здоровье", [
        "Стоматология",
        "Косметология и эстетическая медицина",
        "Медицинские центры, узкие специалисты",
        "Ветеринарные клиники",
        "Психологи, массаж, реабилитация",
    ]),
    ("leisure", "Досуг и события", [
        "Квесты, батутные центры, скалодромы",
        "Детские праздники, аниматоры",
        "Прокат: сап, велосипеды, снаряжение",
        "Кейтеринг, выездные банкеты",
        "Ивент-агентства, организация свадеб",
    ]),
    ("education", "Образование и спорт", [
        "Автошколы",
        "Детские центры, развивающие занятия",
        "Языковые школы, курсы",
        "Фитнес-студии, йога, пилатес, единоборства",
        "Танцевальные студии",
    ]),
    ("services", "Услуги для людей и бизнеса", [
        "Юристы: банкротство, споры",
        "Клининг",
        "Грузоперевозки, переезды, вывоз мусора",
        "Дезинсекция",
        "Ремонт техники и электроники",
    ]),
    ("shop", "Интернет-магазины", [
        "Цветы с доставкой",
        "Торты и кондитерские на заказ",
        "Мебель и товары для дома",
        "Стройматериалы",
        "Автозапчасти",
        "Зоотовары, спорттовары",
        "Одежда, свои бренды",
    ]),
]

# Названия из первой версии справочника. В боевой базе они уже заведены
# и к ним могли привязать объекты, поэтому не создаём новые рядом, а
# переименовываем старые: объекты и ручные правки остаются на месте, старое
# название становится синонимом. Если новое уже есть — старое вливается в него.
OLD_NICHE_TITLES = {"booking": "Бронирование", "auto": "Автосервисы"}
OLD_CATEGORY_TITLES = [
    ("auto", "Детейлинг", "Детейлинг, полировка, керамика"),
    ("auto", "Кузовной ремонт", "Кузовной ремонт и покраска"),
    ("auto", "Автосервис", "Автосервис, СТО"),
    ("auto", "Тонировка и оклейка", "Тонировка, оклейка плёнкой, шумоизоляция"),
    ("auto", "Шиномонтаж", "Шиномонтаж, хранение шин"),
    ("shop", "Цветы", "Цветы с доставкой"),
    ("shop", "Торты и кондитерские", "Торты и кондитерские на заказ"),
    ("shop", "Мебель", "Мебель и товары для дома"),
    ("shop", "Товары для дома", "Мебель и товары для дома"),
    ("shop", "Одежда", "Одежда, свои бренды"),
]

# Что раньше писалось в поле категории, но на самом деле говорит об
# источнике лида. Источник и так лежит в leads.source, категорией это не станет.
NOT_CATEGORIES = {"по егрюл", "добавлен вручную", "импорт", "—", "-", ""}

MAX_TITLE = 60


# ── названия ─────────────────────────────────────────────────────────────────

def clean(title):
    """Название как его покажем: без лишних пробелов, с разумной длиной."""
    return re.sub(r"\s+", " ", str(title or "")).strip()[:MAX_TITLE]


def key(title):
    """Ключ для сравнения: регистр, ё и пробелы не делают названия разными.

    lower() в SQLite кириллицу не понимает, поэтому ключ считаем здесь
    и храним рядом с названием.
    """
    return clean(title).casefold().replace("ё", "е")


def usable(title):
    return key(title) not in NOT_CATEGORIES


# ── чтение ───────────────────────────────────────────────────────────────────

def niche(c, niche_id):
    return c.execute("SELECT * FROM niches WHERE id=?", (niche_id,)).fetchone()


def category(c, category_id):
    return c.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()


def default_id(c=None):
    c = c or db.conn()
    r = c.execute("SELECT id FROM niches WHERE code=?", (DEFAULT_CODE,)).fetchone()
    if r:
        return r["id"]
    # Нишу по умолчанию могли переименовать, но не удалить: удалить можно
    # только пустую, а в ней весь собранный сбором массив. На всякий случай
    # берём первую по порядку, чтобы сбор не падал.
    r = c.execute("SELECT id FROM niches ORDER BY sort, id LIMIT 1").fetchone()
    return r["id"] if r else None


def find_category(c, niche_id, title):
    """Категория ниши по названию или по прежнему названию. None, если нет."""
    k = key(title)
    if not niche_id or not k:
        return None
    r = c.execute("SELECT id FROM categories WHERE niche_id=? AND key=?",
                  (niche_id, k)).fetchone()
    if r:
        return r["id"]
    # После переименования и объединения сбор продолжает присылать старые
    # названия из config.CATEGORY_MAP — по ним и находим новую категорию.
    r = c.execute("SELECT category_id FROM category_aliases WHERE niche_id=? AND key=?",
                  (niche_id, k)).fetchone()
    return r["category_id"] if r else None


def listing():
    """Все ниши с категориями и числом видимых лидов — для вкладок и форм."""
    c = db.conn()
    visible = "COALESCE(hidden, 0) = 0"
    by_niche = {r["niche_id"]: r["n"] for r in c.execute(
        f"SELECT niche_id, COUNT(*) n FROM leads WHERE {visible} GROUP BY niche_id")}
    by_cat = {r["category_id"]: r["n"] for r in c.execute(
        f"SELECT category_id, COUNT(*) n FROM leads WHERE {visible} "
        "AND category_id IS NOT NULL GROUP BY category_id")}
    no_cat = {r["niche_id"]: r["n"] for r in c.execute(
        f"SELECT niche_id, COUNT(*) n FROM leads WHERE {visible} "
        "AND category_id IS NULL GROUP BY niche_id")}

    cats = {}
    for r in c.execute("SELECT * FROM categories ORDER BY sort, id"):
        cats.setdefault(r["niche_id"], []).append(
            {"id": r["id"], "title": r["title"], "count": by_cat.get(r["id"], 0)})

    niches = [{
        "id": r["id"], "title": r["title"], "code": r["code"] or "",
        "count": by_niche.get(r["id"], 0),
        "no_category": no_cat.get(r["id"], 0),
        "categories": cats.get(r["id"], []),
    } for r in c.execute("SELECT * FROM niches ORDER BY sort, id")]

    return {"niches": niches, "total": sum(by_niche.values()),
            "default_id": default_id(c)}


# ── создание ─────────────────────────────────────────────────────────────────

def _next_sort(c, table, where="", params=()):
    r = c.execute(f"SELECT COALESCE(MAX(sort), 0) + 1 s FROM {table} {where}",
                  params).fetchone()
    return r["s"]


def ensure_niche(c, title, login, code=None):
    """Возвращает (id, создана_ли). Существующую с тем же названием не дублирует."""
    title = clean(title)
    if not key(title):
        raise ValueError("Название ниши не может быть пустым")
    r = c.execute("SELECT id FROM niches WHERE key=?", (key(title),)).fetchone()
    if r:
        return r["id"], False
    cur = c.execute(
        "INSERT INTO niches (code, title, key, sort, created_at, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (code, title, key(title), _next_sort(c, "niches"), db.now(), login or ""))
    return cur.lastrowid, True


def ensure_category(c, niche_id, title, login):
    """Возвращает (id, создана_ли). Сначала ищет по названию и прежним названиям."""
    title = clean(title)
    if not key(title):
        raise ValueError("Название категории не может быть пустым")
    if not usable(title):
        raise ValueError(f"«{title}» — это источник лида, а не категория")
    if not niche(c, niche_id):
        raise ValueError("Такой ниши нет")
    found = find_category(c, niche_id, title)
    if found:
        return found, False
    cur = c.execute(
        "INSERT INTO categories (niche_id, title, key, sort, created_at, created_by) "
        "VALUES (?,?,?,?,?,?)",
        (niche_id, title, key(title),
         _next_sort(c, "categories", "WHERE niche_id=?", (niche_id,)),
         db.now(), login or ""))
    return cur.lastrowid, True


def pick(c, payload, login, fallback_niche=None):
    """Разбирает выбор ниши и категории из формы.

    Форма присылает либо номер существующей (niche_id, category_id), либо
    название новой (niche_new, category_new) — новая создаётся тут же, вместе
    с объектом. Возвращает (niche_id, category_id, что_создано).
    """
    created = []

    new_niche = clean(payload.get("niche_new"))
    if new_niche:
        niche_id, made = ensure_niche(c, new_niche, login)
        if made:
            created.append(f"нишу «{new_niche}»")
    else:
        niche_id = _int(payload.get("niche_id")) or fallback_niche or default_id(c)
        if not niche(c, niche_id):
            raise ValueError("Такой ниши нет — обновите страницу")

    category_id = None
    new_cat = clean(payload.get("category_new"))
    if new_cat:
        category_id, made = ensure_category(c, niche_id, new_cat, login)
        if made:
            created.append(f"категорию «{new_cat}»")
    elif _int(payload.get("category_id")):
        category_id = _int(payload.get("category_id"))
        cat = category(c, category_id)
        if not cat:
            raise ValueError("Такой категории нет — обновите страницу")
        if cat["niche_id"] != niche_id:
            raise ValueError("Категория относится к другой нише")

    return niche_id, category_id, created


def _int(value):
    try:
        return int(value) or None
    except (TypeError, ValueError):
        return None


# ── лиды ─────────────────────────────────────────────────────────────────────

def apply(c, lead, row=None):
    """Раскладывает текстовую категорию из сбора и импорта по номерам.

    Новый лид (row=None) получает нишу по умолчанию, если её не задали явно,
    а неизвестную категорию — созданной. У существующего ниша от сбора не
    меняется никогда, а категория обновляется, только если такая есть в его
    нише: «По ЕГРЮЛ» или чужой тип не должны стирать то, что выбрал человек.
    """
    if row is None:
        niche_id = lead.get("niche_id") or default_id(c)
        lead["niche_id"] = niche_id
        cat_id = lead.get("category_id")
        if not cat_id and usable(lead.get("category")):
            cat_id, _ = ensure_category(c, niche_id, lead["category"], SYSTEM)
        cat = category(c, cat_id) if cat_id else None
        lead["category_id"] = cat["id"] if cat else None
        lead["category"] = cat["title"] if cat else ""
        return lead

    lead.pop("niche_id", None)
    if "category" not in lead:
        lead.pop("category_id", None)
        return lead
    cat_id = find_category(c, row["niche_id"], lead["category"]) \
        if usable(lead["category"]) else None
    if cat_id:
        lead["category_id"] = cat_id
        lead["category"] = category(c, cat_id)["title"]
    else:
        lead.pop("category", None)
        lead.pop("category_id", None)
    return lead


def assign(c, lead_id, niche_id, category_id, touch=True):
    """Ставит лиду нишу и категорию, обновляя зеркало названия.

    touch=False не двигает updated_at — для переноса данных, который
    объект по сути не меняет."""
    cat = category(c, category_id) if category_id else None
    c.execute("UPDATE leads SET niche_id=?, category_id=?, category=? WHERE id=?",
              (niche_id, cat["id"] if cat else None, cat["title"] if cat else "",
               lead_id))
    if touch:
        c.execute("UPDATE leads SET updated_at=? WHERE id=?", (db.now(), lead_id))


def titles(c, lead):
    """Названия ниши и категории лида — для журнала."""
    n = niche(c, lead["niche_id"]) if lead["niche_id"] else None
    cat = category(c, lead["category_id"]) if lead["category_id"] else None
    return (n["title"] if n else ""), (cat["title"] if cat else "")


def revert(c, lead, field, old_title):
    """Отмена правки ниши или категории из журнала. Возвращает текст ошибки."""
    if field == "niche":
        r = c.execute("SELECT id FROM niches WHERE key=?", (key(old_title),)).fetchone()
        if not r:
            return f"Ниши «{old_title}» больше нет"
        # Категория из другой ниши при возврате ниши теряет смысл
        cat = category(c, lead["category_id"]) if lead["category_id"] else None
        keep = cat["id"] if cat and cat["niche_id"] == r["id"] else None
        assign(c, lead["id"], r["id"], keep)
        return ""
    if not usable(old_title):
        assign(c, lead["id"], lead["niche_id"], None)
        return ""
    cat_id = find_category(c, lead["niche_id"], old_title)
    if not cat_id:
        return f"В нише объекта нет категории «{old_title}»"
    assign(c, lead["id"], lead["niche_id"], cat_id)
    return ""


# ── управление справочником (владелец базы) ──────────────────────────────────

def rename_niche(c, niche_id, title):
    n = niche(c, niche_id)
    if not n:
        raise ValueError("Такой ниши нет")
    title = clean(title)
    if not key(title):
        raise ValueError("Название не может быть пустым")
    other = c.execute("SELECT id FROM niches WHERE key=? AND id<>?",
                      (key(title), niche_id)).fetchone()
    if other:
        raise ValueError(f"Ниша «{title}» уже есть — их можно объединить")
    c.execute("UPDATE niches SET title=?, key=? WHERE id=?", (title, key(title), niche_id))
    return n["title"], title


def rename_category(c, category_id, title):
    cat = category(c, category_id)
    if not cat:
        raise ValueError("Такой категории нет")
    title = clean(title)
    if not key(title):
        raise ValueError("Название не может быть пустым")
    if not usable(title):
        raise ValueError(f"«{title}» — это источник лида, а не категория")
    other = find_category(c, cat["niche_id"], title)
    if other and other != category_id:
        raise ValueError(f"Категория «{title}» уже есть — их можно объединить")
    c.execute("UPDATE categories SET title=?, key=? WHERE id=?",
              (title, key(title), category_id))
    # Старое название остаётся синонимом: сбор продолжит присылать его
    if cat["key"] != key(title):
        _alias(c, cat["niche_id"], cat["key"], category_id)
    c.execute("DELETE FROM category_aliases WHERE niche_id=? AND key=?",
              (cat["niche_id"], key(title)))
    c.execute("UPDATE leads SET category=? WHERE category_id=?", (title, category_id))
    c.execute("UPDATE refs SET category=? WHERE category=?", (title, cat["title"]))
    return cat["title"], title


def _alias(c, niche_id, alias_key, category_id):
    c.execute("INSERT OR REPLACE INTO category_aliases (niche_id, key, category_id) "
              "VALUES (?,?,?)", (niche_id, alias_key, category_id))


def merge_categories(c, source_id, target_id):
    """Переносит всё из одной категории в другую и удаляет первую."""
    src, dst = category(c, source_id), category(c, target_id)
    if not src or not dst:
        raise ValueError("Такой категории нет")
    if src["id"] == dst["id"]:
        raise ValueError("Категорию нельзя объединить саму с собой")
    if src["niche_id"] != dst["niche_id"]:
        raise ValueError("Объединять можно категории одной ниши")
    moved = c.execute("UPDATE leads SET category_id=?, category=? WHERE category_id=?",
                      (dst["id"], dst["title"], src["id"])).rowcount
    c.execute("UPDATE refs SET category=? WHERE category=?", (dst["title"], src["title"]))
    c.execute("UPDATE category_aliases SET category_id=? WHERE category_id=?",
              (dst["id"], src["id"]))
    _alias(c, src["niche_id"], src["key"], dst["id"])
    c.execute("DELETE FROM categories WHERE id=?", (src["id"],))
    return src["title"], dst["title"], moved


def merge_niches(c, source_id, target_id):
    """Переносит лиды и категории одной ниши в другую, одноимённые склеивает."""
    src, dst = niche(c, source_id), niche(c, target_id)
    if not src or not dst:
        raise ValueError("Такой ниши нет")
    if src["id"] == dst["id"]:
        raise ValueError("Нишу нельзя объединить саму с собой")

    for cat in c.execute("SELECT * FROM categories WHERE niche_id=?",
                         (src["id"],)).fetchall():
        twin = c.execute("SELECT id, title FROM categories WHERE niche_id=? AND key=?",
                         (dst["id"], cat["key"])).fetchone()
        if twin:
            c.execute("UPDATE leads SET category_id=?, category=? WHERE category_id=?",
                      (twin["id"], twin["title"], cat["id"]))
            c.execute("UPDATE category_aliases SET category_id=? WHERE category_id=?",
                      (twin["id"], cat["id"]))
            c.execute("DELETE FROM categories WHERE id=?", (cat["id"],))
        else:
            c.execute("UPDATE categories SET niche_id=?, sort=? WHERE id=?",
                      (dst["id"], _next_sort(c, "categories", "WHERE niche_id=?",
                                             (dst["id"],)), cat["id"]))

    # Синонимы переезжают вместе с категориями; совпавшие с уже имеющимися
    # в целевой нише не нужны — там своё значение.
    c.execute("DELETE FROM category_aliases WHERE niche_id=? AND key IN "
              "(SELECT key FROM category_aliases WHERE niche_id=?)", (src["id"], dst["id"]))
    c.execute("UPDATE category_aliases SET niche_id=? WHERE niche_id=?",
              (dst["id"], src["id"]))

    moved = c.execute("UPDATE leads SET niche_id=? WHERE niche_id=?",
                      (dst["id"], src["id"])).rowcount
    # Сбор ищет нишу по коду: код уходит к той, куда всё переехало
    if src["code"] and not dst["code"]:
        c.execute("UPDATE niches SET code=NULL WHERE id=?", (src["id"],))
        c.execute("UPDATE niches SET code=? WHERE id=?", (src["code"], dst["id"]))
    c.execute("DELETE FROM niches WHERE id=?", (src["id"],))
    return src["title"], dst["title"], moved


def _objects(n):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} объект"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} объекта"
    return f"{n} объектов"


def _count(c, column, value):
    return c.execute(f"SELECT COUNT(*) n FROM leads WHERE {column}=?",
                     (value,)).fetchone()["n"]


def delete_category(c, category_id):
    cat = category(c, category_id)
    if not cat:
        raise ValueError("Такой категории нет")
    n = _count(c, "category_id", category_id)
    if n:
        raise ValueError(f"В категории {_objects(n)}. Сначала объедините её с другой — "
                         f"объекты переедут туда")
    c.execute("DELETE FROM category_aliases WHERE category_id=?", (category_id,))
    c.execute("DELETE FROM categories WHERE id=?", (category_id,))
    return cat["title"]


def delete_niche(c, niche_id):
    n = niche(c, niche_id)
    if not n:
        raise ValueError("Такой ниши нет")
    if n["code"] == DEFAULT_CODE:
        raise ValueError("В эту нишу складывает объекты сбор — её нельзя удалить")
    leads = _count(c, "niche_id", niche_id)
    if leads:
        raise ValueError(f"В нише {_objects(leads)}. Сначала объедините её с другой — "
                         f"объекты переедут туда")
    c.execute("DELETE FROM category_aliases WHERE niche_id=?", (niche_id,))
    c.execute("DELETE FROM categories WHERE niche_id=?", (niche_id,))
    c.execute("DELETE FROM niches WHERE id=?", (niche_id,))
    return n["title"]


def move(c, table, item_id, step):
    """Сдвигает нишу или категорию на одну позицию вверх или вниз."""
    if table not in ("niches", "categories"):
        raise ValueError("Неизвестный справочник")
    item = c.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
    if not item:
        raise ValueError("Не найдено")
    scope, params = ("WHERE niche_id=?", [item["niche_id"]]) \
        if table == "categories" else ("", [])
    ids = [r["id"] for r in c.execute(
        f"SELECT id FROM {table} {scope} ORDER BY sort, id", params)]
    i = ids.index(item_id)
    j = max(0, min(len(ids) - 1, i + (1 if step > 0 else -1)))
    ids[i], ids[j] = ids[j], ids[i]
    # Переписываем порядок целиком: так не бывает двух строк с одним sort
    for pos, row_id in enumerate(ids, 1):
        c.execute(f"UPDATE {table} SET sort=? WHERE id=?", (pos, row_id))


# ── перенос старых данных ────────────────────────────────────────────────────

def migrate(c):
    """Переносы справочника по порядку. Каждый выполняется один раз."""
    moved = _migrate_leads(c)
    _expand(c)
    return moved


def _migrate_leads(c):
    """Заводит справочник и раскладывает уже собранных лидов.

    Все лиды до появления ниш собраны под бронирование, поэтому уходят туда.
    Их текстовые категории становятся строками справочника, а «По ЕГРЮЛ»
    и подобные — пустой категорией.
    """
    if c.execute("SELECT 1 FROM meta WHERE key='niches_migrated'").fetchone():
        return 0

    for code, title, cats in SEED:
        niche_id, _ = ensure_niche(c, title, SYSTEM, code=code)
        for cat in cats:
            ensure_category(c, niche_id, cat, SYSTEM)

    booking = default_id(c)
    moved = 0
    for r in c.execute("SELECT id, category FROM leads WHERE niche_id IS NULL").fetchall():
        cat_id = None
        if usable(r["category"]):
            cat_id, _ = ensure_category(c, booking, r["category"], SYSTEM)
        assign(c, r["id"], booking, cat_id, touch=False)
        moved += 1

    c.execute("INSERT INTO meta (key, value) VALUES ('niches_migrated', ?)", (db.now(),))
    c.commit()
    return moved


def _coded_niche(c, code, title):
    """Ниша по коду. Если кода ещё нет — по названию (новому или из первой
    версии), и код ей присваивается; если нет и такой — заводится."""
    r = c.execute("SELECT id FROM niches WHERE code=?", (code,)).fetchone()
    if r:
        return r["id"]
    for t in (title, OLD_NICHE_TITLES.get(code)):
        if not t:
            continue
        r = c.execute("SELECT id FROM niches WHERE key=?", (key(t),)).fetchone()
        if r:
            c.execute("UPDATE niches SET code=? WHERE id=?", (code, r["id"]))
            return r["id"]
    niche_id, _ = ensure_niche(c, title, SYSTEM, code=code)
    return niche_id


def _expand(c):
    """Второй справочник: восемь ниш вместо трёх, подробные категории.

    Идёт поверх того, что уже есть в базе: старые ниши и категории
    переименовываются, а не создаются рядом, дубли вливаются, заведённое
    командой остаётся — в конце своей ниши и в конце списка вкладок.
    """
    if c.execute("SELECT 1 FROM meta WHERE key='niches_v2'").fetchone():
        return

    ids = {}
    for code, title, _ in SEED:
        niche_id = _coded_niche(c, code, title)
        # Одноимённую нишу мог завести кто-то из команды — вливаем её сюда
        twin = c.execute("SELECT id FROM niches WHERE key=? AND id<>?",
                         (key(title), niche_id)).fetchone()
        if twin:
            merge_niches(c, twin["id"], niche_id)
        if niche(c, niche_id)["key"] != key(title):
            rename_niche(c, niche_id, title)
        ids[code] = niche_id

    for code, old, new in OLD_CATEGORY_TITLES:
        niche_id = ids[code]
        src = c.execute("SELECT id FROM categories WHERE niche_id=? AND key=?",
                        (niche_id, key(old))).fetchone()
        if not src:
            continue
        dst = c.execute("SELECT id FROM categories WHERE niche_id=? AND key=?",
                        (niche_id, key(new))).fetchone()
        if dst:
            merge_categories(c, src["id"], dst["id"])
        else:
            rename_category(c, src["id"], new)

    for code, _, cats in SEED:
        for cat in cats:
            ensure_category(c, ids[code], cat, SYSTEM)

    # Порядок как в справочнике; всё, что завела команда, — следом
    _reorder(c, "niches", "", [], [ids[code] for code, _, _ in SEED])
    for code, _, cats in SEED:
        order = [find_category(c, ids[code], cat) for cat in cats]
        _reorder(c, "categories", "WHERE niche_id=?", [ids[code]], order)

    c.execute("INSERT INTO meta (key, value) VALUES ('niches_v2', ?)", (db.now(),))
    c.commit()
    # Импорт внутри: history знает про db, на уровне модуля был бы круг
    import history
    history.log(SYSTEM, "niches", new=f"Справочник ниш расширен: {len(SEED)} ниш, "
                                      f"категории уточнены")


def _reorder(c, table, where, params, first):
    rest = [r["id"] for r in c.execute(
        f"SELECT id FROM {table} {where} ORDER BY sort, id", params)
        if r["id"] not in first]
    for pos, row_id in enumerate(first + rest, 1):
        c.execute(f"UPDATE {table} SET sort=? WHERE id=?", (pos, row_id))
