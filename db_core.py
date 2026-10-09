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
import time as _time
import copy
import functools
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


STATS = {"connects": 0, "queries": 0}   # cheap counters used by tests/perf_probe.py to count database round trips
_WRITE_GEN = 0                            # bumped by every non-SELECT statement; invalidates cached_read() results


def _is_read(sql: str) -> bool:
    return sql.lstrip()[:6].upper() == "SELECT"


class Connection:
    """Thin wrapper over a sqlite3 / psycopg connection."""

    def __init__(self, raw: Any, dialect_name: str, release=None):
        self._raw, self.dialect, self._release = raw, dialect_name, release
        self._wrote = False   # did this connection change data? (read-only connections must not invalidate the read cache)
        self._in_tx = False   # Postgres connections run in autocommit; a transaction is opened only when the first write arrives

    # -- statements
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        global _WRITE_GEN
        STATS["queries"] += 1
        read = _is_read(sql)
        if not read:
            _WRITE_GEN += 1
            self._wrote = True
        if self.dialect == "postgres":
            if not read and not self._in_tx:
                self._raw.execute("BEGIN")
                self._in_tx = True
            cur = self._raw.cursor()
            cur.execute(translate_for_postgres(sql), tuple(params))
            return Cursor(cur)
        return Cursor(self._raw.execute(sql, tuple(params)))

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        global _WRITE_GEN
        _WRITE_GEN += 1
        self._wrote = True
        if self.dialect == "postgres":
            if not self._in_tx:
                self._raw.execute("BEGIN")
                self._in_tx = True
            cur = self._raw.cursor()
            cur.executemany(translate_for_postgres(sql), [tuple(p) for p in seq])
        else:
            self._raw.executemany(sql, [tuple(p) for p in seq])

    def begin(self) -> None:
        """Open a transaction now (Postgres connections are autocommit until the first write; use this before a lock)."""
        if self.dialect == "postgres" and not self._in_tx:
            self._raw.execute("BEGIN")
            self._in_tx = True

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
        global _WRITE_GEN
        if self._wrote:
            _WRITE_GEN += 1     # data became visible to other connections: cached reads taken before this are stale
            self._wrote = False
        if self.dialect == "postgres":
            if self._in_tx:
                self._in_tx = False
                self._raw.execute("COMMIT")
        else:
            self._raw.commit()

    def rollback(self) -> None:
        if self.dialect == "postgres":
            if self._in_tx:
                self._in_tx = False
                self._raw.execute("ROLLBACK")
        else:
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
_LAST_USED: dict[int, float] = {}
IDLE_PROBE_SEC = 20.0
_pool_url = ""
_pool_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """The configured Postgres database cannot be reached. The message is safe to show (no password, no host)."""


def diagnose_connection_error(exc: BaseException) -> str:
    """Turn a raw libpq / pool error into a plain-language hint for the app owner."""
    t = str(exc).lower()
    if "password authentication failed" in t or "authentication failed" in t:
        return "The database rejected the username or password. Re-copy DATABASE_URL from your provider; if the password contains characters such as @ : / # ? %, URL-encode them (for example @ becomes %40)."
    if "tenant or user not found" in t or "circuit breaker" in t:
        return "The pooler does not recognise the user. With Supabase's pooler the user must be postgres.PROJECT_REF (not just postgres). Copy the full string from Project Settings -> Database -> Connection string."
    if "network is unreachable" in t or "cannot assign requested address" in t or "no route to host" in t:
        return "The database host is not reachable over IPv4. Streamlit Cloud has no IPv6, so use the Supabase *pooler* connection string (host ends in pooler.supabase.com), not the 'direct connection' string (db.PROJECT_REF.supabase.co)."
    if "could not translate host name" in t or "name or service not known" in t or "nodename nor servname" in t:
        return "The database host name in DATABASE_URL could not be found. Check it for typos."
    if "timeout" in t or "timed out" in t:
        return "The connection timed out. Check that the project is not paused (Supabase pauses free projects after inactivity) and that DATABASE_URL uses the pooler host and the right port (6543 or 5432)."
    if "ssl" in t:
        return "SSL negotiation failed. Add ?sslmode=require to the end of DATABASE_URL."
    if "does not exist" in t and "database" in t:
        return "The database name in DATABASE_URL does not exist (Supabase uses 'postgres')."
    return "The database could not be reached. Check DATABASE_URL, that the project is running, and the Streamlit logs for the detailed error."


def _preflight(url: str) -> None:
    """One direct connection attempt so a bad URL fails in seconds with the REAL reason (a pool hides it behind a timeout)."""
    import logging
    try:
        with psycopg.connect(url, connect_timeout=10, prepare_threshold=None) as c:  # type: ignore[union-attr]
            c.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("prepai.db").error("Postgres connection failed: %s", exc)    # full detail goes to the server log only
        raise DatabaseUnavailable(diagnose_connection_error(exc)) from None


def _get_pool(url: str):
    """One small pool per process (Streamlit reruns must not open a new connection each time)."""
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None and _pool_url == url:
            return _pool
        if psycopg is None:
            raise RuntimeError("DATABASE_URL points to Postgres but 'psycopg' is not installed. Run: pip install \"psycopg[binary]\" psycopg-pool")
        _preflight(url)
        kwargs = {"row_factory": _pg_row_factory, "prepare_threshold": None, "autocommit": True}  # None = safe with Supabase/Neon poolers
        try:
            from psycopg_pool import ConnectionPool
            _pool = ConnectionPool(url, min_size=1, max_size=int(os.environ.get("DB_POOL_MAX", "5")), kwargs=kwargs,
                                   open=True, timeout=20, max_idle=240)
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
    STATS["connects"] += 1
    if is_postgres():
        pool = _get_pool(database_url())
        for attempt in range(3):
            try:
                raw = pool.getconn()
            except Exception as exc:  # noqa: BLE001 - PoolTimeout etc.
                raise DatabaseUnavailable(diagnose_connection_error(exc)) from None
            # A connection used a moment ago is trusted (saves one round trip per query); one that sat idle is probed first.
            if _time.monotonic() - _LAST_USED.get(id(raw), 0.0) > IDLE_PROBE_SEC:
                try:
                    raw.execute("SELECT 1")
                except Exception:  # noqa: BLE001 - dead connection: hand it back (the pool discards it) and take another
                    pool.putconn(raw)
                    continue
            break

        def _release(c, _put=pool.putconn):
            _LAST_USED[id(c)] = _time.monotonic()
            _put(c)
        return Connection(raw, "postgres", release=_release)
    raw = sqlite3.connect(_sqlite_path(), check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA busy_timeout = 5000")
    return Connection(raw, "sqlite")


# --------------------------------------------------------------------------- read cache
_READ_CACHE: dict[Any, tuple[int, float, Any]] = {}
_READ_LOCK = threading.Lock()


def cached_read(ttl: float = 30.0):
    """Cache a read-only database function for a short time.

    The cached value is dropped as soon as ANY write goes through this process (so a student always sees their own
    changes at once) or after ``ttl`` seconds (so changes made by another server process show up soon). It turns the
    ~20 repeated look-ups a page makes on every click into a handful of real round trips when the database is remote.
    Never use it for security or deadline decisions, only for data shown on screen.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (db_identity(), fn.__module__, fn.__qualname__, args, tuple(sorted(kwargs.items())))
            now = _time.monotonic()
            with _READ_LOCK:
                hit = _READ_CACHE.get(key)
            if hit and hit[0] == _WRITE_GEN and now - hit[1] < ttl:
                return copy.deepcopy(hit[2])
            gen = _WRITE_GEN
            value = fn(*args, **kwargs)
            with _READ_LOCK:
                if len(_READ_CACHE) > 2000:
                    _READ_CACHE.clear()
                _READ_CACHE[key] = (gen, now, copy.deepcopy(value))
            return value
        wrapper.__wrapped__ = fn
        return wrapper
    return deco


def clear_read_cache() -> None:
    with _READ_LOCK:
        _READ_CACHE.clear()
