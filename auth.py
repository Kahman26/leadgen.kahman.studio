# -*- coding: utf-8 -*-
"""Вход по логину и паролю.

Пользователи задаются переменной LEADGEN_USERS — по одному на запись,
логин и пароль через двоеточие, записи через точку с запятой:

    LEADGEN_USERS=andrey:пароль;marketolog:другой-пароль

Пароль можно хранить и хэшем, если не хочется держать его открытым:

    python auth.py hash мой-пароль
    LEADGEN_USERS=andrey:pbkdf2$200000$a1b2...$c3d4...

Сессии лежат в таблице sessions: перезапуск сервиса не выкидывает команду
обратно на форму входа.
"""

import hashlib
import hmac
import os
import secrets
import sys
import time
from datetime import datetime, timedelta

import db

COOKIE_NAME = "leadgen_session"
SESSION_DAYS = 30

PBKDF2_ROUNDS = 200_000

# Защита от перебора: после стольких неудач с одного адреса — пауза.
MAX_ATTEMPTS = 7
LOCKOUT_SECONDS = 300
_attempts = {}


# ── пользователи ─────────────────────────────────────────────────────────────

def load_users():
    """Возвращает {логин: пароль-или-хэш} из LEADGEN_USERS."""
    raw = os.getenv("LEADGEN_USERS", "").strip()
    users = {}
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        login, password = chunk.split(":", 1)
        login, password = login.strip(), password.strip()
        if login and password:
            users[login] = password
    return users


def make_hash(password, rounds=PBKDF2_ROUNDS):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return f"pbkdf2${rounds}${salt}${digest.hex()}"


def _check_password(given, stored):
    """Сравнение без утечки времени. Понимает и открытый пароль, и хэш."""
    if stored.startswith("pbkdf2$"):
        try:
            _, rounds, salt, expected = stored.split("$", 3)
            digest = hashlib.pbkdf2_hmac(
                "sha256", given.encode(), salt.encode(), int(rounds))
            return hmac.compare_digest(digest.hex(), expected)
        except (ValueError, TypeError):
            return False
    # Сравниваем байты, а не строки: compare_digest отказывается работать
    # со строками, где есть не-ASCII — то есть с любым русским паролем.
    return hmac.compare_digest(given.encode("utf-8"), stored.encode("utf-8"))


# ── защита от перебора ───────────────────────────────────────────────────────

def _locked_for(ip):
    """Сколько секунд осталось ждать этому адресу. 0 — можно пробовать."""
    fails, until = _attempts.get(ip, (0, 0))
    remaining = int(until - time.time())
    return remaining if remaining > 0 else 0


def _note_failure(ip):
    fails, _ = _attempts.get(ip, (0, 0))
    fails += 1
    until = time.time() + LOCKOUT_SECONDS if fails >= MAX_ATTEMPTS else 0
    _attempts[ip] = (fails, until)


def _note_success(ip):
    _attempts.pop(ip, None)


# ── сессии ───────────────────────────────────────────────────────────────────

def create_session(login, user_agent=""):
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    c = db.conn()
    c.execute(
        "INSERT INTO sessions (token, login, created_at, expires_at, user_agent) "
        "VALUES (?,?,?,?,?)",
        (token, login, now.isoformat(timespec="seconds"),
         (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"),
         (user_agent or "")[:200]),
    )
    c.commit()
    return token


def session_login(token):
    """Логин по токену сессии или None, если её нет либо истекла."""
    if not token:
        return None
    row = db.conn().execute(
        "SELECT login, expires_at FROM sessions WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        drop_session(token)
        return None
    return row["login"]


def drop_session(token):
    if not token:
        return
    c = db.conn()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()


def cleanup_sessions():
    c = db.conn()
    c.execute("DELETE FROM sessions WHERE expires_at < ?",
              (datetime.now().isoformat(timespec="seconds"),))
    c.commit()


# ── вход ─────────────────────────────────────────────────────────────────────

def authenticate(login, password, ip):
    """Возвращает (логин, None) при успехе или (None, текст ошибки)."""
    wait = _locked_for(ip)
    if wait:
        return None, f"Слишком много попыток. Повторите через {wait // 60 + 1} мин."

    users = load_users()
    if not users:
        return None, ("Ни одного пользователя не заведено. "
                      "Задайте LEADGEN_USERS в /etc/leadgen.env")

    stored = users.get((login or "").strip())
    # Проверяем пароль даже для несуществующего логина, чтобы по времени
    # ответа нельзя было понять, какие логины существуют.
    ok = _check_password(password or "", stored) if stored else \
        _check_password(password or "", make_hash("никогда-не-совпадёт"))

    if not ok:
        _note_failure(ip)
        return None, "Неверный логин или пароль"

    _note_success(ip)
    return login.strip(), None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "hash":
        print(make_hash(sys.argv[2]))
    else:
        print("Использование: python auth.py hash <пароль>")
