#!/usr/bin/env bash
# Обновление боевого сервиса. Запускается от root.
#
# GitHub Actions не вызывает этот файл на сервере, а передаёт его по stdin:
#   ssh root@host 'bash -s' < deploy/deploy.sh
# Так выполняется всегда свежая версия скрипта, и bash не читает файл,
# который прямо сейчас переписывает git.

set -euo pipefail

APP_DIR=/opt/leadgen
APP_USER=leadgen
BRANCH=main

cd "$APP_DIR"

echo "→ Забираю изменения из origin/$BRANCH"
sudo -u "$APP_USER" git fetch --prune origin

# Если ветки на той стороне нет, --prune уже снёс локальную ссылку, и git
# дальше падает с невнятным «ambiguous argument». Ловим это сами: почти
# всегда причина в том, что в репозиторий ещё ни разу не пушили.
if ! sudo -u "$APP_USER" git rev-parse --verify -q "origin/$BRANCH" >/dev/null; then
    echo "✗ В origin нет ветки $BRANCH — деплоить нечего."
    echo "  Похоже, в репозиторий ещё не было пуша. Код на сервере не тронут."
    exit 1
fi

# reset --hard трогает только отслеживаемые файлы: кеш Overpass в .cache
# и база в /var/lib/leadgen остаются на месте.
sudo -u "$APP_USER" git reset --hard "origin/$BRANCH"

echo "→ Зависимости"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r requirements.txt

echo "→ Перезапуск"
systemctl restart leadgen
sleep 2

if systemctl is-active --quiet leadgen; then
    echo "✓ Готово: $(cd "$APP_DIR" && git log -1 --format='%h %s')"
else
    echo "✗ Сервис не поднялся, последние строки журнала:"
    journalctl -u leadgen -n 40 --no-pager
    exit 1
fi
