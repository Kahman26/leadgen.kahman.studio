#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Консольный доступ к автопроверке — для работы из чата.

Нужен, чтобы проверять объекты не по одному через интерфейс, а пачкой:
помощник в чате получает список, берёт запрос по каждому объекту, ищет
в интернете и возвращает результат сюда же.

    python research_cli.py list "База отдыха"      # что проверять
    python research_cli.py list "База отдыха" all  # включая уже в работе
    python research_cli.py brief 239               # запрос для поиска
    python research_cli.py apply 239 < found.json  # записать найденное

Запись идёт той же функцией, что и кнопка в карточке: контакты ложатся
только в пустые поля, ручные правки не трогаются, статус становится
«Автопроверка». Обойти эти правила через консоль нельзя намеренно.
"""

import io
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import config                                            # noqa: E402
import db                                                # noqa: E402
import pipeline                                          # noqa: E402
from enrich import research_brief                        # noqa: E402

# Кириллица в выводе не должна ломаться о кодировку консоли сервера
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def cmd_list(category="", scope="new"):
    where = ["COALESCE(hidden, 0) = 0"]
    params = []
    if category:
        where.append("category = ?")
        params.append(category)
    if scope == "new":
        where.append("status = 'new'")
    elif scope == "unchecked":
        where.append("COALESCE(ai_checked_at, '') = ''")

    rows = db.conn().execute(
        f"SELECT id, name, category, website, site_status, phone, status, score "
        f"FROM leads WHERE {' AND '.join(where)} ORDER BY score DESC, id",
        params,
    ).fetchall()

    print(json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2))


def cmd_brief(lead_id):
    row = db.conn().execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if not row:
        sys.exit(f"Лид {lead_id} не найден")
    print(research_brief.build(db.row_to_dict(row), config.CITY_NAME))


def cmd_apply(lead_id):
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    found, error = research_brief.parse(raw)
    if error:
        sys.exit(f"Лид {lead_id}: {error}")

    lead = pipeline.apply_research(lead_id, found)
    if not lead:
        sys.exit(f"Лид {lead_id} не найден")

    print(json.dumps({
        "id": lead["id"],
        "name": lead["name"],
        "status": lead["status"],
        "website": lead["website"],
        "previous_website": lead["previous_website"],
        "ai_director": lead["ai_director"],
        "problems": lead["ai_problems"],
        "score": lead["score"],
        "reason": lead["reason_text"],
    }, ensure_ascii=False, indent=2))


SERVER_DB = "/var/lib/leadgen/leads.db"


def resolve_db():
    """Находит боевую базу и не даёт молча создать пустую рядом с кодом.

    Путь к базе сервис получает из systemd (LEADGEN_DB), а в обычной
    консоли этой переменной нет. Без проверки sqlite просто создаст новый
    пустой файл, и список объектов окажется пустым без всякой ошибки.
    """
    import os
    from pathlib import Path

    if not os.getenv("LEADGEN_DB") and Path(SERVER_DB).exists():
        config.DB_PATH = SERVER_DB           # обычный случай на сервере

    if not Path(config.DB_PATH).exists():
        sys.exit(f"Базы нет: {config.DB_PATH}\n"
                 f"Укажите её явно: LEADGEN_DB=/путь/к/leads.db")

    print(f"# база: {config.DB_PATH}", file=sys.stderr)


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    resolve_db()
    db.init()

    command = args[0]
    if command == "list":
        cmd_list(args[1] if len(args) > 1 else "",
                 args[2] if len(args) > 2 else "new")
    elif command == "brief" and len(args) > 1:
        cmd_brief(int(args[1]))
    elif command == "apply" and len(args) > 1:
        cmd_apply(int(args[1]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
