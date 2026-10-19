# -*- coding: utf-8 -*-
"""Вход по логину и паролю.

Пользователи задаются переменной LEADGEN_USERS — по одному на запись,
логин и пароль через двоеточие, записи через точку с запятой:

    LEADGEN_USERS=andrey:пароль;marketolog:другой-пароль

Пароль можно хранить и хэшем, если не хочется держать его открытым:

    python auth.py hash мой-пароль

Завести сотрудника на сервере одной командой (пароль придумается сам,
в файл ляжет хэш): python auth.py adduser ЛОГИН
    LEADGEN_USERS=andrey:pbkdf2$200000$a1b2...$c3d4...

Сессии лежат в таблице sessions: перезапуск сервиса не выкидывает команду
обратно на форму входа.

Аналитику рабочего времени видит только владелец базы. Список таких логинов
задаётся LEADGEN_ADMINS (через запятую); по умолчанию — arkprhrv.
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


# Кто видит аналитику рабочего времени. По умолчанию — владелец базы;
# список расширяется через LEADGEN_ADMINS, логины через запятую.
DEFAULT_ADMINS = "arkprhrv"


def load_admins():
    raw = os.getenv("LEADGEN_ADMINS", DEFAULT_ADMINS)
    return {x.strip() for x in raw.replace(",", ";").split(";") if x.strip()}


def is_admin(login):
    return bool(login) and login.strip() in load_admins()


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


# ── заведение сотрудников ────────────────────────────────────────────────────

ENV_FILE = "/etc/leadgen.env"
# Без похожих знаков (l и 1, O и 0) — пароль диктуют и переписывают руками
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def make_password(length=8):
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def _edit_users(path, fn):
    """Переписывает строку LEADGEN_USERS в env-файле функцией fn(список записей)."""
    lines = open(path, encoding="utf-8").read().split("\n")
    for i, line in enumerate(lines):
        if line.startswith("LEADGEN_USERS="):
            users = [u for u in line.split("=", 1)[1].split(";") if u.strip()]
            lines[i] = "LEADGEN_USERS=" + ";".join(fn(users))
            break
    else:
        lines.append("LEADGEN_USERS=" + ";".join(fn([])))
    open(path, "w", encoding="utf-8").write("\n".join(lines))


def add_user(login, password=None, path=ENV_FILE):
    """Новый сотрудник с правами продажника. Возвращает пароль открытым текстом.

    В файл пишется хэш: открытого пароля на сервере не остаётся."""
    login = login.strip()
    if not login or ":" in login or ";" in login:
        raise ValueError("Логин без двоеточий и точек с запятой")
    password = password or make_password()

    def add(users):
        if any(u.split(":", 1)[0].strip() == login for u in users):
            raise ValueError(f"Логин {login} уже есть — для нового пароля: passwd")
        return users + [f"{login}:{make_hash(password)}"]
    _edit_users(path, add)
    return password


def set_password(login, password=None, path=ENV_FILE):
    """Новый пароль существующему сотруднику. Возвращает его открытым текстом."""
    password = password or make_password()
    found = []

    def change(users):
        out = []
        for u in users:
            if u.split(":", 1)[0].strip() == login:
                found.append(login)
                u = f"{login}:{make_hash(password)}"
            out.append(u)
        return out
    _edit_users(path, change)
    if not found:
        raise ValueError(f"Логина {login} нет — завести: adduser")
    return password


USAGE = """Использование:
  python auth.py adduser ЛОГИН [пароль]   завести сотрудника (права продажника)
  python auth.py passwd  ЛОГИН [пароль]   сменить пароль
  python auth.py users                    кто заведён
  python auth.py hash    пароль           только хэш
Без пароля он придумывается сам. Файл — /etc/leadgen.env (или LEADGEN_ENV_FILE).
После adduser и passwd: systemctl restart leadgen"""


if __name__ == "__main__":
    args, path = sys.argv[1:], os.getenv("LEADGEN_ENV_FILE", ENV_FILE)
    try:
        if len(args) == 2 and args[0] == "hash":
            print(make_hash(args[1]))
        elif len(args) in (2, 3) and args[0] in ("adduser", "passwd"):
            fn = add_user if args[0] == "adduser" else set_password
            pw = fn(args[1], args[2] if len(args) == 3 else None, path)
            print(f"Логин: {args[1]}\nПароль: {pw}")
            print("Перезапустите сервис: systemctl restart leadgen")
        elif args == ["users"]:
            line = next((l for l in open(path, encoding="utf-8").read().split("\n")
                         if l.startswith("LEADGEN_USERS=")), "LEADGEN_USERS=")
            admins = load_admins()
            for u in line.split("=", 1)[1].split(";"):
                if u.strip():
                    name = u.split(":", 1)[0]
                    print(name + ("  (владелец)" if name in admins else ""))
        else:
            print(USAGE)
    except (ValueError, OSError) as exc:
        print("Ошибка:", exc)
        sys.exit(1)
