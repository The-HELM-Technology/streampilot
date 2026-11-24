# SPDX-License-Identifier: LGPL-2.1-or-later
"""Database access layer.

Single place that knows *how* to open a connection. SQLite (default) and
Postgres share one interface, so call sites use
``with connect() as c: c.execute(sql, params)`` on either. SQL is written once in
SQLite style (``?`` placeholders, ``INTEGER PRIMARY KEY AUTOINCREMENT`` ...) and
translated on the fly for Postgres (see :func:`translate`).

Backend selection (first match wins):
  1. ``STREAMPILOT_DB_URL`` env var (set by the ``-db_url`` CLI flag)
       sqlite:///relative/path.db   or   sqlite:////absolute/path.db
  2. an explicit ``path`` argument to :func:`connect` (used by ``LiveLogger``)
  3. the default SQLite path registered with :func:`configure`

Semantics preserved from the original raw ``sqlite3`` usage:
  * autocommit -- each statement commits on its own (also on Postgres, so a
    failed statement never poisons the following ones in the same block)
  * WAL journal + the same PRAGMA tuning (SQLite)
  * ``?`` placeholders
Differences: leaving a ``with`` block now *closes* the SQLite connection / returns
the Postgres connection to the pool.
"""
import atexit
import functools
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

ENV_URL = "STREAMPILOT_DB_URL"

_default_sqlite_path: Optional[Path] = None


def configure(default_sqlite_path) -> None:
    """Register the SQLite file used when no URL/path override is given."""
    global _default_sqlite_path
    _default_sqlite_path = Path(default_sqlite_path)


def get_url() -> Optional[str]:
    url = (os.getenv(ENV_URL) or "").strip()
    return url or None


def backend() -> str:
    """'sqlite' or 'postgres' for the currently configured database."""
    url = get_url()
    if not url:
        return "sqlite"
    scheme = url.split(":", 1)[0].lower()
    if scheme == "sqlite":
        return "sqlite"
    if scheme in ("postgres", "postgresql"):
        return "postgres"
    raise ValueError(f"Unsupported database URL scheme: {scheme!r} (use sqlite:/// or postgresql://)")


def _sqlite_path(path=None) -> Path:
    url = get_url()
    if url:
        prefix = "sqlite:///"
        if not url.lower().startswith(prefix):
            raise ValueError(f"Malformed SQLite URL: {url!r} (expected sqlite:///path/to/file.db)")
        return Path(url[len(prefix):])
    if path is not None:
        return Path(path)
    if _default_sqlite_path is None:
        raise RuntimeError("db.configure(default_sqlite_path) was not called")
    return _default_sqlite_path


# --------------------------------------------------------------------------- SQL translation
_LITERAL = re.compile(r"('(?:[^']|'')*')")
_DDL_START = ("CREATE", "ALTER", "DROP")


@functools.lru_cache(maxsize=2048)
def translate(sql: str, with_params: bool = True) -> str:
    """Translate SQLite-flavoured SQL for Postgres (psycopg).

    * ``?`` -> ``%s`` and literal ``%`` -> ``%%`` (only when parameters are passed)
    * DDL types: ``INTEGER PRIMARY KEY AUTOINCREMENT`` -> ``BIGSERIAL PRIMARY KEY``,
      ``INTEGER`` -> ``BIGINT``, ``REAL`` -> ``DOUBLE PRECISION`` (a 4-byte float
      would lose GPS precision), ``DATETIME`` -> ``TEXT`` (timestamps are stored
      and compared as text, exactly as with SQLite).
    String literals are never modified (apart from ``%`` escaping).
    """
    is_ddl = sql.lstrip().upper().startswith(_DDL_START)
    parts = _LITERAL.split(sql)
    for i, part in enumerate(parts):
        if i % 2:  # a quoted literal
            if with_params:
                parts[i] = part.replace("%", "%%")
            continue
        if with_params:
            part = part.replace("%", "%%").replace("?", "%s")
        if is_ddl:
            part = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", part)
            part = re.sub(r"\bDATETIME\b", "TEXT", part)
            part = re.sub(r"\bREAL\b", "DOUBLE PRECISION", part)
            part = re.sub(r"\bINTEGER\b", "BIGINT", part)
        parts[i] = part
    return "".join(parts)


# --------------------------------------------------------------------------- Postgres pool
_pool = None
_pool_url = None
_pool_lock = threading.Lock()


def _get_pool(url: str):
    global _pool, _pool_url
    with _pool_lock:
        if _pool is not None and _pool_url == url:
            return _pool
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "The Postgres backend needs psycopg: pip install 'psycopg[binary,pool]'"
            ) from e
        if _pool is not None:
            _pool.close()
        _pool = ConnectionPool(
            url,
            min_size=int(os.getenv("STREAMPILOT_DB_POOL_MIN", "2")),
            max_size=int(os.getenv("STREAMPILOT_DB_POOL_MAX", "20")),
            kwargs={"autocommit": True},
            check=ConnectionPool.check_connection,  # drop connections the server closed
            open=True,
            name="streampilot",
        )
        _pool_url = url
        _pool.wait(timeout=15)  # fail fast on a bad URL / unreachable server
        atexit.register(close_pool)
        return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            finally:
                _pool = None


class DBConn:
    """Thin wrapper over a DB-API connection.

    * ``with`` commits on success, rolls back on error, and always releases the
      connection (closed for SQLite, returned to the pool for Postgres).
    * ``execute`` / ``executemany`` accept SQLite-style SQL on both backends and
      return the driver's cursor (``fetchone`` / ``fetchall`` / ``rowcount``).
    * Anything else is proxied to the underlying connection.
    """

    def __init__(self, raw, kind: str, pool=None):
        self._raw = raw
        self._pool = pool
        self.backend = kind

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def execute(self, sql, params=None):
        if self.backend == "sqlite":
            return self._raw.execute(sql, params if params is not None else ())
        if params is None or len(params) == 0:
            return self._raw.execute(translate(sql, False))
        return self._raw.execute(translate(sql, True), params)

    def executemany(self, sql, seq):
        if self.backend == "sqlite":
            return self._raw.executemany(sql, seq)
        cur = self._raw.cursor()
        cur.executemany(translate(sql, True), seq)
        return cur

    def release(self):
        if self._pool is not None:
            self._pool.putconn(self._raw)
        else:
            self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self.release()
        return False


def connect(path=None, timeout: float = 5.0, busy_ms: int = 2000) -> DBConn:
    """Open a connection to the configured database.

    ``timeout`` / ``busy_ms`` are the SQLite lock-wait settings (ignored by
    other backends); callers keep their historical values.
    """
    kind = backend()
    if kind == "postgres":
        pool = _get_pool(get_url())
        return DBConn(pool.getconn(timeout=float(os.getenv("STREAMPILOT_DB_POOL_TIMEOUT", "30"))), "postgres", pool)
    p = _sqlite_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None, timeout=timeout)
    try:
        raw.execute("PRAGMA journal_mode=WAL;")
        raw.execute("PRAGMA synchronous=NORMAL;")
        raw.execute(f"PRAGMA busy_timeout={int(busy_ms)};")
        raw.execute("PRAGMA temp_store=MEMORY;")
        raw.execute("PRAGMA cache_size=-20000;")  # ~20MB
    except Exception:
        pass
    return DBConn(raw, "sqlite")


def column_exists(conn, table: str, column: str) -> bool:
    """True if ``table`` has ``column`` (replaces ``PRAGMA table_info`` checks)."""
    if getattr(conn, "backend", "sqlite") == "sqlite":
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    return conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = ? AND column_name = ?",
        (table, column),
    ).fetchone() is not None


def describe() -> dict:
    """{'kind', 'location', 'size_bytes'} for display (never includes credentials)."""
    kind = backend()
    if kind == "sqlite":
        p = _sqlite_path()
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return {"kind": "sqlite", "location": str(p), "size_bytes": size}
    u = urlsplit(get_url())
    location = f"{u.hostname or 'localhost'}:{u.port or 5432}{u.path}"
    try:
        with connect() as c:
            size = c.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    except Exception:
        size = 0
    return {"kind": "postgres", "location": location, "size_bytes": int(size)}
