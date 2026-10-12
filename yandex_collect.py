# -*- coding: utf-8 -*-
"""Сбор карточек Яндекс Карт браузером — запускается на своём компьютере.

    python yandex_collect.py "бани и сауны" --radius 30
    python yandex_collect.py "бани и сауны" --radius 30 --import     # сразу в локальную базу

Нужен Playwright (`pip install playwright`) и установленный Google Chrome.
Окно браузера открывается видимым: если Яндекс покажет капчу, её решает
человек, сбор ждёт и продолжает сам. Прервали — запустите ту же команду
снова, файл продолжится с места остановки.

Результат — файл в папке yandex/. Его загружают в leadgen на сервере:
меню → «Импорт с Яндекс Карт». Флаг --import кладёт карточки в базу,
на которую смотрит этот компьютер (LEADGEN_DB или leads.db рядом).
"""

import argparse
import sys

from sources import yandex_maps


def main():
    ap = argparse.ArgumentParser(description="Сбор карточек Яндекс Карт по нише вокруг Екатеринбурга")
    ap.add_argument("query", nargs="?", default="бани и сауны", help="что искать на карте")
    ap.add_argument("--radius", type=float, default=30, help="радиус от центра Екатеринбурга, км")
    ap.add_argument("--niche", default="booking", help="код ниши leadgen для загрузки")
    ap.add_argument("--rubric", help="регулярное выражение нужных рубрик (по умолчанию — из набора ниши)")
    ap.add_argument("--limit", type=int, help="не больше стольких карточек (для пробы)")
    ap.add_argument("--out", help="куда сохранить (по умолчанию yandex/<запрос>-<дата>.json)")
    ap.add_argument("--import", dest="do_import", action="store_true",
                    help="после сбора загрузить карточки в локальную базу")
    args = ap.parse_args()

    out = yandex_maps.collect(args.query, radius_km=args.radius, out=args.out, niche=args.niche,
                              include=args.rubric,
                              limit=args.limit, progress=lambda m: print(m, flush=True))
    meta, cards = yandex_maps.load_cards(out)
    print(f"\nГотово: {len(cards)} карточек → {out}", flush=True)
    print("Загрузить на сервер: leadgen → меню → «Импорт с Яндекс Карт» → этот файл.", flush=True)

    if args.do_import:
        import history
        import pipeline
        pipeline.import_cards(cards, niche=args.niche, login=history.SYSTEM)
        for line in pipeline.state()["log"]:
            print(line)


if __name__ == "__main__":
    sys.exit(main())
