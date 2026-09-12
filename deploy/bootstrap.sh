#!/usr/bin/env bash
# Первичная настройка сервера под leadgen. Запускается один раз, от root.
# Повторный запуск безопасен: всё, что уже сделано, пропускается.
#
# Дальше обновления идут сами через GitHub Actions → deploy/deploy.sh.

set -euo pipefail

REPO=https://github.com/Kahman26/leadgen.kahman.studio.git
DOMAIN=leadgen.kahman.studio
EMAIL=andrey.ivanov53563@gmail.com
APP_DIR=/opt/leadgen
DATA_DIR=/var/lib/leadgen
APP_USER=leadgen

echo "→ Пакеты"
apt-get update -qq
apt-get install -y -qq python3-venv apache2-utils git curl

echo "→ Системный пользователь $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"

echo "→ Каталоги"
mkdir -p "$APP_DIR" "$DATA_DIR" /var/www/"$DOMAIN"

echo "→ Код"
if [ ! -d "$APP_DIR/.git" ]; then
    git clone "$REPO" "$APP_DIR" || {
        # Репозиторий ещё пустой — готовим каталог под будущий пуш
        git init -q "$APP_DIR"
        git -C "$APP_DIR" remote add origin "$REPO"
    }
fi
git config --global --add safe.directory "$APP_DIR"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"

echo "→ Виртуальное окружение"
[ -d "$APP_DIR/venv" ] || sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
[ -f "$APP_DIR/requirements.txt" ] && \
    sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "→ Пароль для входа"
if [ ! -f /etc/nginx/.htpasswd-leadgen ]; then
    PASS=$(openssl rand -base64 12)
    htpasswd -bc /etc/nginx/.htpasswd-leadgen kahman "$PASS"
    chmod 640 /etc/nginx/.htpasswd-leadgen
    chown root:www-data /etc/nginx/.htpasswd-leadgen
    echo "   логин kahman, пароль: $PASS  ← запишите, больше не покажется"
fi

echo "→ systemd"
cp "$APP_DIR/deploy/leadgen.service" /etc/systemd/system/leadgen.service
systemctl daemon-reload
systemctl enable --now leadgen

echo "→ Сертификат"
if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    # Временный HTTP-блок: certbot должен куда-то положить проверочный файл,
    # а боевой конфиг без сертификата не поднимется.
    cat > /etc/nginx/sites-available/"$DOMAIN" <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    root /var/www/$DOMAIN;
    location ^~ /.well-known/acme-challenge/ { default_type "text/plain"; }
}
NGINX
    ln -sf /etc/nginx/sites-available/"$DOMAIN" /etc/nginx/sites-enabled/"$DOMAIN"
    nginx -t && systemctl reload nginx
    certbot certonly --webroot -w /var/www/"$DOMAIN" -d "$DOMAIN" \
        --non-interactive --agree-tos -m "$EMAIL"
fi

echo "→ Боевой конфиг nginx"
cp "$APP_DIR/deploy/nginx-leadgen.conf" /etc/nginx/sites-available/"$DOMAIN"
ln -sf /etc/nginx/sites-available/"$DOMAIN" /etc/nginx/sites-enabled/"$DOMAIN"
nginx -t && systemctl reload nginx

echo
echo "Готово: https://$DOMAIN"
systemctl is-active leadgen
