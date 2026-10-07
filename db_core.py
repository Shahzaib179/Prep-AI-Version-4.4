"""Database connection layer: one API, two engines.

* ``DATABASE_URL`` empty / ``sqlite:///...``  -> local SQLite file (development, tests, offline demo).
* ``DATABASE_URL`` = ``postgresql://...``      -> hosted Postgres (Supabase, Neon, ...): permanent storage.

Application code writes plain SQL with ``?`` placeholders and portable constructs only
(``ON CONFLICT ... DO NOTHING/UPDATE``, ``COALESCE``, ``LOWER`` ...). This module translates
``?`` to ``%s`` for Postgres and hides the other differences (new row id, row objects, pooling).

Usage stays exactly the same as before::

    with connect() as con:                  # commits on success, rolls back on error, then releases
        con.execute("UPDATE students SET name=? WHERE id=?", (name, sid))
        new_id = con.insert("INSERT INTO goals(student_id,goal) VALUES(?,?)", (sid, text))
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from collections.abc import Sequence
from typing import Any, Iterable

# --------------------------------------------------------------------------- errors
try:  # psycopg is only needed when DATABASE_URL points to Postgres
    import psycopg  # type: ignore
    from psycopg import errors as _pg_errors  # type: ignore
    IntegrityError: tuple[type[BaseException], ...] = (sqlite3.IntegrityError, psycopg.IntegrityError)
    _UniqueViolation: Any = _pg_errors.UniqueViolation
except Exception:  # pragma: no cover - exercised only where psycopg is missing
    psycopg = None  # type: ignore
    IntegrityError = (sqlite3.IntegrityError,)
    _UniqueViolation = sqlite3.IntegrityError


# --------------------------------------------------------------------------- configuration
def _secret(name: str) -> str:
    """Environment variable first, then Streamlit secrets (never logged)."""
    value = os.environ.get(name, "")
    if value:
        return value.strip()
    try:
        import streamlit as st
        return str(st.secrets.get(name, "") or "").strip()
    except Exception:
        return ""


def database_url() -> str:
    return _secret("DATABASE_URL")


def is_postgres() -> bool:
    url = database_url().lower()
    return url.startswith("postgres://") or url.startswith("postgresql://")


def dialect() -> str:
    return "postgres" if is_postgres() else "sqlite"


def describe_backend() -> str:
    """Safe, human-readable backend name (never includes the password)."""
    if not is_postgres():
        return "SQLite (local file)"
    m = re.match(r"^[a-z]+://[^@]*@([^/?]+)", database_url(), re.I)
    return f"Postgres ({m.group(1) if m else 'hosted'})"


# --------------------------------------------------------------------------- SQL translation
def translate_for_postgres(sql: str) -> str:
    """Convert ``?`` placeholders to ``%s`` (outside string literals) and escape literal ``%``."""
    out: list[str] = []
    in_str = False
    for ch in sql:
        if ch == "'":
            in_str = not in_str
            out.append(ch)
        elif ch == "%":
            out.append("%%")
        elif ch == "?" and not in_str:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


def split_statements(script: str) -> list[str]:
    """Split a multi-statement script on ``;`` (our migration scripts contain no ``;`` inside strings)."""
    return [s.strip() for s in script.split(";") if s.strip()]


# --------------------------------------------------------------------------- rows
class PgRow(Sequence):
    """Row that behaves like ``sqlite3.Row``: ``row[0]``, ``row["col"]``, ``dict(row)``, ``row.keys()``."""

    __slots__ = ("_cols", "_vals")

    def __init__(self, cols: list[str], vals: list[Any]):
        self._cols, self._vals = cols, vals

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, (int, slice)):
            return self._vals[key]
        try:
            return self._vals[self._cols.index(key)]
        except ValueError:
            raise IndexError(f"No item with that key: {key!r}") from None

    def __len__(self) -> int:
        return len(self._vals)

    def keys(self) -> list[str]:
        return list(self._cols)


def _convert(value: Any) -> Any:
    from decimal import Decimal
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _pg_row_factory(cursor: Any):
    cols = [d.name for d in (cursor.description or [])]

    def make(values: Iterable[Any]) -> PgRow:
        return PgRow(cols, [_convert(v) for v in values])

    return make


# --------------------------------------------------------------------------- wrappers
class Cursor:
    def __init__(self, raw: Any):
        self._raw = raw

    def fetchone(self):
        return self._raw.fetchone()

    def fetchall(self):
        return self._raw.fetchall()

    def __iter__(self):
        return iter(self._raw)

    @property
    def rowcount(self) -> int:
        return self._raw.rowcount

    @property
    def lastrowid(self):  # SQLite only; use Connection.insert() for portable code
        return getattr(self._raw, "lastrowid", None)


class Connection:
    """Thin wrapper over a sqlite3 / psycopg connection."""

    def __init__(self, raw: Any, dialect_name: str, release=None):
        self._raw, self.dialect, self._release = raw, dialect_name, release

    # -- statements
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        if self.dialect == "postgres":
            cur = self._raw.cursor()
            cur.execute(translate_for_postgres(sql), tuple(params))
            return Cursor(cur)
        return Cursor(self._raw.execute(sql, tuple(params)))

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        if self.dialect == "postgres":
            cur = self._raw.cursor()
            cur.executemany(translate_for_postgres(sql), [tuple(p) for p in seq])
        else:
            self._raw.executemany(sql, [tuple(p) for p in seq])

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run an INSERT into a table with an ``id`` column and return the new id."""
        if self.dialect == "postgres":
            cur = self.execute(sql.rstrip().rstrip(";") + " RETURNING id", params)
            return int(cur.fetchone()[0])
        return int(self.execute(sql, params).lastrowid)

    def script(self, script: str) -> None:
        for stmt in split_statements(script):
            self.execute(stmt)

    def columns(self, table: str) -> set[str]:
        if self.dialect == "postgres":
            rows = self.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?", (table,)).fetchall()
            return {r[0] for r in rows}
        return {r[1] for r in self.execute(f"PRAGMA table_info({table})").fetchall()}

    def add_column(self, table: str, column: str, definition: str) -> None:
        if column not in self.columns(table):
            self.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # -- transaction scope
    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        raw, release, self._raw = self._raw, self._release, None
        if raw is None:
            return
        if release is not None:
            release(raw)
        else:
            raw.close()

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
        return False


# --------------------------------------------------------------------------- engines
_pool: Any = None
_pool_url = ""
_pool_lock = threading.Lock()


def _get_pool(url: str):
    """One small pool per process (Streamlit reruns must not open a new connection each time)."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None and _pool_url == url:
            return _pool
        if psycopg is None:
            raise RuntimeError("DATABASE_URL points to Postgres but 'psycopg' is not installed. Run: pip install \"psycopg[binary]\" psycopg-pool")
        kwargs = {"row_factory": _pg_row_factory, "prepare_threshold": None, "autocommit": False}  # None = safe with Supabase/Neon poolers
        try:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(url, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "5")), kwargs=kwargs,
                                   check=ConnectionPool.check_connection, open=True, timeout=20)
        except ImportError:
            _pool = None
            raise RuntimeError("Postgres pooling needs 'psycopg-pool'. Run: pip install psycopg-pool")
        _pool_url = url
        return _pool


def reset_pool() -> None:
    """Close the pool (used by tests and by the verification script)."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            finally:
                _pool, _pool_url = None, ""


def _sqlite_path() -> str:
    url = database_url()
    if url.lower().startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    import config  # read at call time so tests can point config.DB_PATH at a temp file
    return str(config.DB_PATH)


def db_identity() -> str:
    """Stable id of the database currently in use (cache keys must not leak between databases)."""
    return database_url() if is_postgres() else _sqlite_path()


def connect() -> Connection:
    if is_postgres():
        pool = _get_pool(database_url())
        raw = pool.getconn()
        return Connection(raw, "postgres", release=pool.putconn)
    raw = sqlite3.connect(_sqlite_path(), check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 5000")
    return Connection(raw, "sqlite")
