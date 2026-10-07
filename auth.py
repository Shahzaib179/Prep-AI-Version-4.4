"""Accounts: email + password sign-up/login for students and tutors, lockout, change password, session tokens.

No Streamlit import here (pure logic, fully unit-tested). Passwords are hashed with scrypt from Python's standard
library (memory-hard, OWASP-accepted, nothing extra to install). The stored format is self-describing
(``scrypt$N$r$p$salt$hash``) so the algorithm can be upgraded later without locking anyone out.

Student data stays keyed by ``student_id``; an account simply owns one student_id. Existing v4.3 students can
attach their old Student ID to a new account so their history is kept.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from db import _meta_get, _meta_set, connect, ensure_student, set_role, update_student, IntegrityError

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_LEN = 2 ** 14, 8, 1, 32
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
SESSION_DAYS = 30
MIN_PASSWORD, MAX_PASSWORD = 8, 128
INVITE_MAX_FAILS, INVITE_LOCK_MINUTES = 10, 15
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_COMMON = {"password", "password1", "12345678", "123456789", "qwertyui", "iloveyou", "11111111", "abc12345", "pakistan", "mdcat2026", "prepai123"}


class AuthError(Exception):
    """Message is safe to show to the user."""


def _now() -> datetime:
    return datetime.utcnow()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    try:
        _, n, r, p, *_ = stored.split("$")
        return (int(n), int(r), int(p)) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
    except Exception:
        return True


_DUMMY = None


def _dummy_verify(password: str) -> None:
    """Spend the same time as a real check so 'unknown email' cannot be told apart by speed."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password("not-a-real-password")
    verify_password(password, _DUMMY)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise AuthError("Please enter a valid email address.")
    return email


def validate_password(password: str, email: str = "") -> None:
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"Password must be at most {MAX_PASSWORD} characters.")
    if password.lower() in _COMMON or (email and password.lower() == normalize_email(email)):
        raise AuthError("That password is too easy to guess. Please choose another one.")
    if password.isdigit() or password.isalpha():
        raise AuthError("Use a mix of letters and numbers in your password.")


# ------------------------------------------------------------------ helpers
def _user_row(con: Any, where: str, params: tuple) -> dict[str, Any] | None:
    row = con.execute(f"SELECT id,email,password_hash,role,student_id,display_name,failed_attempts,locked_until,created_at,last_login_at FROM users WHERE {where}", params).fetchone()
    return dict(row) if row else None


def public_user(u: dict[str, Any]) -> dict[str, Any]:
    return {k: u[k] for k in ("id", "email", "role", "student_id", "display_name")}


def _new_student_id(prefix: str) -> str:
    for _ in range(20):
        sid = f"{prefix}_{secrets.token_hex(4)}"
        with connect() as con:
            if not con.execute("SELECT 1 FROM students WHERE id=?", (sid,)).fetchone() and not con.execute("SELECT 1 FROM users WHERE student_id=?", (sid,)).fetchone():
                return sid
    raise AuthError("Could not create an account right now. Please try again.")


def _insert_user(email: str, password: str, role: str, student_id: str, name: str) -> dict[str, Any]:
    now = _now().isoformat()
    try:
        with connect() as con:
            uid = con.insert("INSERT INTO users(email,password_hash,role,student_id,display_name,created_at) VALUES(?,?,?,?,?,?)",
                             (email, hash_password(password), role, student_id, name, now))
    except IntegrityError as exc:
        raise AuthError("An account with this email already exists. Please log in instead.") from exc
    return {"id": uid, "email": email, "role": role, "student_id": student_id, "display_name": name}


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not (2 <= len(name) <= 80):
        raise AuthError("Please enter your full name (2-80 characters).")
    return name


# ------------------------------------------------------------------ sign up
def register_student(email: str, password: str, name: str, level: str = "MDCAT", legacy_student_id: str = "") -> dict[str, Any]:
    email = validate_email(email); validate_password(password, email); name = _clean_name(name)
    legacy = "".join(ch for ch in (legacy_student_id or "").strip() if ch.isalnum() or ch in "_-. ").replace(" ", "_")[:64]
    if legacy:
        if legacy.lower().startswith("tutor_"):
            raise AuthError("That Student ID cannot be linked.")
        with connect() as con:
            s = con.execute("SELECT role FROM students WHERE id=?", (legacy,)).fetchone()
            taken = con.execute("SELECT 1 FROM users WHERE student_id=?", (legacy,)).fetchone()
        if not s or (s["role"] or "student") != "student" or taken:
            raise AuthError("That old Student ID was not found, or it already belongs to an account.")
        sid = legacy
    else:
        sid = _new_student_id("stu")
    user = _insert_user(email, password, "student", sid, name)
    ensure_student(sid, name)
    update_student(sid, name=name, level=level)
    return user


def _invite_gate(ok_check: bool) -> None:
    """Global brute-force guard for the tutor invite code."""
    with connect() as con:
        locked = _meta_get(con, "invite_locked_until")
        if locked and datetime.fromisoformat(locked) > _now():
            raise AuthError("Too many wrong invite codes. Please try again in a few minutes.")
        fails = int(_meta_get(con, "invite_fails") or 0)
        if ok_check:
            _meta_set(con, "invite_fails", "0")
            return
        fails += 1
        _meta_set(con, "invite_fails", str(fails))
        if fails >= INVITE_MAX_FAILS:
            _meta_set(con, "invite_locked_until", (_now() + timedelta(minutes=INVITE_LOCK_MINUTES)).isoformat())
            _meta_set(con, "invite_fails", "0")


def legacy_tutor_id(invite_code: str) -> str:
    return "tutor_" + hashlib.sha256(invite_code.encode()).hexdigest()[:10]


def register_tutor(email: str, password: str, name: str, invite_code: str, expected_code: str) -> dict[str, Any]:
    if not expected_code:
        raise AuthError("Tutor sign-up is not enabled. The administrator must set TUTOR_ACCESS_CODE in the app secrets.")
    email = validate_email(email); validate_password(password, email); name = _clean_name(name)
    ok = hmac.compare_digest((invite_code or "").strip().encode(), expected_code.encode())
    _invite_gate(ok)
    if not ok:
        raise AuthError("Incorrect tutor invite code.")
    sid = _new_student_id("tutor")
    user = _insert_user(email, password, "tutor", sid, name)
    ensure_student(sid, name); set_role(sid, "tutor")
    # Quizzes published under the old shared-code tutor identity move to the first tutor account created.
    with connect() as con:
        con.execute("UPDATE shared_quizzes SET created_by=? WHERE created_by=?", (sid, legacy_tutor_id(expected_code)))
    return user


# ------------------------------------------------------------------ login
def login(email: str, password: str) -> dict[str, Any]:
    generic = AuthError("Incorrect email or password.")
    email = normalize_email(email)
    with connect() as con:
        u = _user_row(con, "email=?", (email,))
    if not u:
        _dummy_verify(password or "")
        raise generic
    if u["locked_until"] and datetime.fromisoformat(u["locked_until"]) > _now():
        mins = max(1, int((datetime.fromisoformat(u["locked_until"]) - _now()).total_seconds() // 60) + 1)
        raise AuthError(f"Too many failed attempts. This account is locked for about {mins} more minute(s).")
    if not verify_password(password or "", u["password_hash"]):
        fails = int(u["failed_attempts"] or 0) + 1
        locked = (_now() + timedelta(minutes=LOCK_MINUTES)).isoformat() if fails >= MAX_FAILED_LOGINS else None
        with connect() as con:
            con.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?", (0 if locked else fails, locked, u["id"]))
        if locked:
            raise AuthError(f"Too many failed attempts. This account is locked for {LOCK_MINUTES} minutes.")
        raise generic
    with connect() as con:
        con.execute("UPDATE users SET failed_attempts=0, locked_until=NULL, last_login_at=? WHERE id=?", (_now().isoformat(), u["id"]))
        if needs_rehash(u["password_hash"]):
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), u["id"]))
    return public_user(u)


def change_password(user_id: int, old: str, new: str) -> None:
    with connect() as con:
        u = _user_row(con, "id=?", (user_id,))
    if not u or not verify_password(old or "", u["password_hash"]):
        raise AuthError("Your current password is incorrect.")
    validate_password(new, u["email"])
    with connect() as con:
        con.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE id=?", (hash_password(new), user_id))
        con.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))     # sign out every other device


def set_password_admin(email: str, new: str) -> bool:
    """Used by scripts/reset_password.py (app owner only)."""
    validate_password(new, email)
    with connect() as con:
        cur = con.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE email=?", (hash_password(new), normalize_email(email)))
        n = cur.rowcount
        if n:
            con.execute("DELETE FROM auth_sessions WHERE user_id IN (SELECT id FROM users WHERE email=?)", (normalize_email(email),))
    return bool(n)


# ------------------------------------------------------------------ persistent sessions (token stored only as a hash)
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int, days: int = SESSION_DAYS) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    with connect() as con:
        con.execute("DELETE FROM auth_sessions WHERE expires_at<?", (now.isoformat(),))
        con.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                    (_hash_token(token), user_id, now.isoformat(), (now + timedelta(days=days)).isoformat()))
    return token


def user_from_token(token: str) -> dict[str, Any] | None:
    if not token or len(token) > 200:
        return None
    with connect() as con:
        row = con.execute("SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>?", (_hash_token(token), _now().isoformat())).fetchone()
        if not row:
            return None
        u = _user_row(con, "id=?", (row["user_id"],))
    return public_user(u) if u else None


def revoke_session(token: str) -> None:
    if token:
        with connect() as con:
            con.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_hash_token(token),))
