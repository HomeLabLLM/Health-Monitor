"""Users, sessions and API tokens for the web server.

Passwords are hashed with scrypt (stdlib -- no dependency) and a random
per-user salt; nothing recoverable is stored anywhere, and the config
file never holds a password.  Sessions are server-side rows, so a
password change or an admin action revokes them for real rather than
waiting for a cookie to expire.  API tokens (for the TUI) are stored as
SHA-256 hashes; the clear token is shown once at creation.

Roles: ``admin`` (users, monitor settings, nicknames) and ``user`` (view,
own profiles, own password).  The first account created is the admin;
the web UI's first-run page and ``health-monitor users add --role admin``
both go through ``add``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

# N=2**14, r=8 costs 16 MiB per hash.  OpenSSL refuses scrypt above its
# default 32 MiB cap ("memory limit exceeded"), and N=2**15 sits exactly
# on it; maxmem is raised as well so the parameters can be tuned later
# without tripping the same wall.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SCRYPT_MAXMEM = 128 * 1024 * 1024
TOKEN_BYTES = 32

_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    role      TEXT NOT NULL DEFAULT 'user',
    salt      BLOB NOT NULL,
    hash      BLOB NOT NULL,
    created   TEXT NOT NULL,
    disabled  INTEGER NOT NULL DEFAULT 0,
    pw_changed REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token     TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    created   REAL NOT NULL,
    expires   REAL NOT NULL,
    addr      TEXT NOT NULL DEFAULT '',
    agent     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions (user_id);
CREATE TABLE IF NOT EXISTS api_tokens (
    hash      TEXT PRIMARY KEY,
    user_id   INTEGER NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    last_used REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_attempts (
    addr      TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attempts_addr ON login_attempts (addr, ts);
"""


class AuthError(Exception):
    pass


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                          p=SCRYPT_P, maxmem=SCRYPT_MAXMEM, dklen=32)


class Users:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(path, timeout=5.0, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # accounts
    # ------------------------------------------------------------------ #
    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT id, name, role, created, disabled FROM users ORDER BY name")]

    def get(self, name: str) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
            return dict(r) if r else None

    def get_id(self, uid: int) -> dict | None:
        with self._lock:
            r = self._conn.execute("SELECT id, name, role, disabled FROM users WHERE id = ?",
                                   (uid,)).fetchone()
            return dict(r) if r else None

    def add(self, name: str, password: str, role: str = "user") -> int:
        name = name.strip()
        if not name or len(name) > 64 or "/" in name:
            raise AuthError("bad user name")
        if role not in ("admin", "user"):
            raise AuthError("role must be admin or user")
        if len(password) < 8:
            raise AuthError("password must be at least 8 characters")
        salt = secrets.token_bytes(16)
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO users (name, role, salt, hash, created, pw_changed) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, role, salt, _hash(password, salt),
                     datetime.now(timezone.utc).isoformat(), time.time()))
            except sqlite3.IntegrityError:
                raise AuthError(f"user {name!r} already exists") from None
            return cur.lastrowid

    def set_password(self, name: str, password: str) -> None:
        if len(password) < 8:
            raise AuthError("password must be at least 8 characters")
        salt = secrets.token_bytes(16)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET salt = ?, hash = ?, pw_changed = ? WHERE name = ?",
                (salt, _hash(password, salt), time.time(), name))
            if cur.rowcount == 0:
                raise AuthError(f"no such user {name!r}")
            uid = self._conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()[0]
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))

    def set_role(self, name: str, role: str) -> None:
        if role not in ("admin", "user"):
            raise AuthError("role must be admin or user")
        with self._lock:
            if self._conn.execute("UPDATE users SET role = ? WHERE name = ?",
                                  (role, name)).rowcount == 0:
                raise AuthError(f"no such user {name!r}")

    def set_disabled(self, name: str, disabled: bool) -> None:
        with self._lock:
            if self._conn.execute("UPDATE users SET disabled = ? WHERE name = ?",
                                  (1 if disabled else 0, name)).rowcount == 0:
                raise AuthError(f"no such user {name!r}")
            if disabled:
                uid = self._conn.execute("SELECT id FROM users WHERE name = ?",
                                         (name,)).fetchone()[0]
                self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))

    def delete(self, name: str) -> int:
        """Returns the deleted user's id so the caller can cascade profiles."""
        with self._lock:
            r = self._conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
            if r is None:
                raise AuthError(f"no such user {name!r}")
            uid = r[0]
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
            self._conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (uid,))
            self._conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            return uid

    # ------------------------------------------------------------------ #
    # authentication
    # ------------------------------------------------------------------ #
    def check_password(self, name: str, password: str) -> dict | None:
        u = self.get(name)
        if u is None:
            _hash(password, b"x" * 16)          # constant-time-ish: hash anyway
            return None
        if u["disabled"]:
            return None
        if hmac.compare_digest(_hash(password, u["salt"]), u["hash"]):
            return {"id": u["id"], "name": u["name"], "role": u["role"]}
        return None

    def new_session(self, uid: int, hours: float, addr: str = "", agent: str = "") -> str:
        tok = secrets.token_urlsafe(TOKEN_BYTES)
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (token, user_id, created, expires, addr, agent) "
                "VALUES (?, ?, ?, ?, ?, ?)", (tok, uid, now, now + hours * 3600, addr, agent[:200]))
            self._conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
        return tok

    def session_user(self, tok: str) -> dict | None:
        if not tok:
            return None
        with self._lock:
            r = self._conn.execute(
                "SELECT u.id, u.name, u.role, u.disabled FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires > ?",
                (tok, time.time())).fetchone()
        if r is None or r["disabled"]:
            return None
        return {"id": r["id"], "name": r["name"], "role": r["role"]}

    def end_session(self, tok: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE token = ?", (tok,))

    def end_all_sessions(self, uid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))

    # ------------------------------------------------------------------ #
    # API tokens (TUI)
    # ------------------------------------------------------------------ #
    def new_token(self, name: str, label: str = "") -> str:
        u = self.get(name)
        if u is None:
            raise AuthError(f"no such user {name!r}")
        tok = "hm_" + secrets.token_urlsafe(TOKEN_BYTES)
        with self._lock:
            self._conn.execute(
                "INSERT INTO api_tokens (hash, user_id, label, created) VALUES (?, ?, ?, ?)",
                (hashlib.sha256(tok.encode()).hexdigest(), u["id"], label, time.time()))
        return tok

    def token_user(self, tok: str) -> dict | None:
        if not tok or not tok.startswith("hm_"):
            return None
        h = hashlib.sha256(tok.encode()).hexdigest()
        with self._lock:
            r = self._conn.execute(
                "SELECT u.id, u.name, u.role, u.disabled FROM api_tokens t "
                "JOIN users u ON u.id = t.user_id WHERE t.hash = ?", (h,)).fetchone()
            if r is None or r["disabled"]:
                return None
            self._conn.execute("UPDATE api_tokens SET last_used = ? WHERE hash = ?",
                               (time.time(), h))
        return {"id": r["id"], "name": r["name"], "role": r["role"]}

    def tokens(self, uid: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT label, created, last_used FROM api_tokens WHERE user_id = ?", (uid,))]

    def revoke_tokens(self, uid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (uid,))

    # ------------------------------------------------------------------ #
    # login throttling
    # ------------------------------------------------------------------ #
    def note_attempt(self, addr: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO login_attempts (addr, ts) VALUES (?, ?)",
                               (addr, time.time()))
            self._conn.execute("DELETE FROM login_attempts WHERE ts < ?", (time.time() - 3600,))

    def recent_attempts(self, addr: str, window: float = 600.0) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE addr = ? AND ts > ?",
                (addr, time.time() - window)).fetchone()[0]

    def clear_attempts(self, addr: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM login_attempts WHERE addr = ?", (addr,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
