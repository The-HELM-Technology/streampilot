# SPDX-License-Identifier: LGPL-2.1-or-later
"""Database access layer.

Single place that knows *how* to open a connection. Today only SQLite is
implemented; a Postgres backend is added behind the same interface later, so
call sites keep using ``with connect() as c: c.execute(sql, params)``.

Backend selection (first match wins):
  1. ``STREAMPILOT_DB_URL`` env var (set by the ``-db_url`` CLI flag)
       sqlite:///relative/path.db   or   sqlite:////absolute/path.db
  2. an explicit ``path`` argument to :func:`connect` (used by ``LiveLogger``)
  3. the default SQLite path registered with :func:`configure`

Semantics preserved from the original raw ``sqlite3`` usage:
  * autocommit (``isolation_level=None``) -- each statement commits on its own
  * WAL journal + the same PRAGMA tuning
  * ``?`` placeholders
Differences: leaving a ``with`` block now *closes* the connection (sqlite3's
own ``with`` only commits), which a pooled server connection requires.
"""
import os
import sqlite3
from pathlib import Path
from typing import Optional

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


class DBConn:
    """Thin wrapper over a DB-API connection.

    * ``with`` commits on success, rolls back on error, and always closes.
    * Everything else is proxied to the underlying connection (``execute``
      returns the driver's cursor, so ``fetchone`` / ``fetchall`` /
      ``lastrowid`` behave exactly as before).
    """

    def __init__(self, raw, kind: str):
        self._raw = raw
        self.backend = kind

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()
        return False


def connect(path=None, timeout: float = 5.0, busy_ms: int = 2000) -> DBConn:
    """Open a connection to the configured database.

    ``timeout`` / ``busy_ms`` are the SQLite lock-wait settings (ignored by
    other backends); callers keep their historical values.
    """
    kind = backend()
    if kind == "postgres":
        raise NotImplementedError("The Postgres backend is not implemented yet")
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
