"""Account credentials and revocable browser sessions for the hosted workspace."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time


SESSION_TTL = 7 * 24 * 60 * 60
MIN_PASSWORD_LENGTH = 10
_PASSWORD_ITERATIONS = 600_000
_USER_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}", re.ASCII)
_SESSION_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)


@dataclass(frozen=True)
class Account:
    account_id: str
    username: str


def _password_hash(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).digest()


class AccountStore:
    def __init__(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.path = root / "accounts.sqlite3"
        self._unknown_user_salt = secrets.token_bytes(16)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    iterations INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    token_hash BLOB PRIMARY KEY,
                    account_id TEXT NOT NULL REFERENCES accounts(account_id),
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_by_expiry ON login_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    address TEXT PRIMARY KEY,
                    window_start INTEGER NOT NULL,
                    attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chat_usage (
                    account_id TEXT PRIMARY KEY REFERENCES accounts(account_id),
                    window_start INTEGER NOT NULL,
                    attempts INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def reserve_auth_attempt(self, address, limit=24, period=60):
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM auth_attempts WHERE window_start <= ?", (now - period,))
            db.execute("""
                INSERT INTO auth_attempts VALUES (?, ?, 1)
                ON CONFLICT(address) DO UPDATE SET attempts = attempts + 1
                WHERE attempts < ?
            """, (address, now, limit))
            return bool(db.execute("SELECT changes()").fetchone()[0])

    def register(self, username, password):
        username = username.strip().lower()
        if not _USER_NAME.fullmatch(username):
            raise ValueError("账号须为 3–32 位字母、数字、点、短横线或下划线，并以字母或数字开头。")
        if not MIN_PASSWORD_LENGTH <= len(password) <= 128:
            raise ValueError(f"请使用 {MIN_PASSWORD_LENGTH}–128 个字符的密码。")

        account = Account(secrets.token_hex(16), username)
        salt = secrets.token_bytes(16)
        digest = _password_hash(password, salt, _PASSWORD_ITERATIONS)
        try:
            with self._connect() as db:
                db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)",
                           (account.account_id, account.username, salt, digest, _PASSWORD_ITERATIONS))
        except sqlite3.IntegrityError as exc:
            raise ValueError("该账号已被使用。可以换一个账号，或使用已有账号登录。") from exc
        return account

    def reserve_chat_message(self, account, limit):
        if limit <= 0:
            return True
        now = int(time.time())
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM chat_usage WHERE account_id=? AND window_start <= ?",
                       (account.account_id, now - 86400))
            db.execute("""
                INSERT INTO chat_usage VALUES (?, ?, 1)
                ON CONFLICT(account_id) DO UPDATE SET attempts = attempts + 1 WHERE attempts < ?
            """, (account.account_id, now, limit))
            return bool(db.execute("SELECT changes()").fetchone()[0])

    def authenticate(self, username, password):
        username = username.strip().lower()
        if not _USER_NAME.fullmatch(username) or not MIN_PASSWORD_LENGTH <= len(password) <= 128:
            return None
        with self._connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        if not row:
            _password_hash(password, self._unknown_user_salt, _PASSWORD_ITERATIONS)
            return None
        digest = _password_hash(password, row["password_salt"], row["iterations"])
        if hmac.compare_digest(digest, row["password_hash"]):
            return Account(row["account_id"], row["username"])
        return None

    def start_session(self, account):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._connect() as db:
            db.execute("DELETE FROM login_sessions WHERE expires_at <= ?", (now,))
            db.execute("INSERT INTO login_sessions VALUES (?, ?, ?)",
                       (_token_hash(token), account.account_id, now + SESSION_TTL))
        return token

    def account_for_session(self, token):
        if not _SESSION_TOKEN.fullmatch(token):
            return None
        with self._connect() as db:
            row = db.execute("""
                SELECT a.account_id, a.username
                FROM accounts a JOIN login_sessions s ON s.account_id = a.account_id
                WHERE s.token_hash = ? AND s.expires_at > ?
            """, (_token_hash(token), int(time.time()))).fetchone()
        return Account(row["account_id"], row["username"]) if row else None

    def end_session(self, token):
        if _SESSION_TOKEN.fullmatch(token):
            with self._connect() as db:
                db.execute("DELETE FROM login_sessions WHERE token_hash = ?", (_token_hash(token),))
