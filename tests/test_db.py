"""Unit tests for streampilot/db.py plus startup/upgrade behaviour that depends on it."""
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from streampilot import db  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(db.ENV_URL, raising=False)
    monkeypatch.setattr(db, "_default_sqlite_path", None)


def test_default_path_used(tmp_path):
    db.configure(tmp_path / "d.db")
    with db.connect() as c:
        c.execute("CREATE TABLE t(x)")
    assert (tmp_path / "d.db").exists()


def test_explicit_path_beats_default(tmp_path):
    db.configure(tmp_path / "default.db")
    with db.connect(tmp_path / "explicit.db") as c:
        c.execute("CREATE TABLE t(x)")
    assert (tmp_path / "explicit.db").exists() and not (tmp_path / "default.db").exists()


def test_url_beats_path(tmp_path, monkeypatch):
    monkeypatch.setenv(db.ENV_URL, f"sqlite:///{tmp_path}/from_url.db")
    with db.connect(tmp_path / "ignored.db") as c:
        c.execute("CREATE TABLE t(x)")
    assert (tmp_path / "from_url.db").exists() and not (tmp_path / "ignored.db").exists()


def test_backend_detection(monkeypatch):
    assert db.backend() == "sqlite"
    monkeypatch.setenv(db.ENV_URL, "sqlite:///x.db")
    assert db.backend() == "sqlite"
    monkeypatch.setenv(db.ENV_URL, "postgresql://u:p@h/db")
    assert db.backend() == "postgres"
    monkeypatch.setenv(db.ENV_URL, "mysql://h/db")
    with pytest.raises(ValueError):
        db.backend()


def test_postgres_unreachable_fails_fast(monkeypatch):
    monkeypatch.setenv(db.ENV_URL, "postgresql://u:p@127.0.0.1:1/nope?connect_timeout=2")
    monkeypatch.setattr(db, "_pool", None)
    t0 = time.time()
    with pytest.raises(Exception):
        db.connect()
    assert time.time() - t0 < 30


# ------------------------------------------------------------------ SQL translation (pure)
def test_translate_placeholders():
    assert db.translate("INSERT INTO t(a,b) VALUES(?,?)") == "INSERT INTO t(a,b) VALUES(%s,%s)"


def test_translate_leaves_literal_question_marks_and_escapes_percent():
    assert db.translate("SELECT 1 WHERE a LIKE '%x?%' AND b=?") == "SELECT 1 WHERE a LIKE '%%x?%%' AND b=%s"


def test_translate_no_params_does_not_escape_percent():
    assert db.translate("SELECT '%'", False) == "SELECT '%'"


def test_translate_ddl_types():
    out = db.translate(
        "CREATE TABLE IF NOT EXISTS x (id INTEGER PRIMARY KEY AUTOINCREMENT, a DATETIME NOT NULL, "
        "b REAL, c INTEGER DEFAULT 0, d TEXT DEFAULT '[\"INTEGER REAL\"]')", False)
    assert "id BIGSERIAL PRIMARY KEY" in out
    assert "a TEXT NOT NULL" in out
    assert "b DOUBLE PRECISION" in out
    assert "c BIGINT DEFAULT 0" in out
    assert "'[\"INTEGER REAL\"]'" in out  # literal untouched


def test_translate_does_not_rewrite_types_in_dml():
    assert db.translate("SELECT CAST(a AS INTEGER) FROM t WHERE b=?") == "SELECT CAST(a AS INTEGER) FROM t WHERE b=%s"


def test_with_block_closes_connection(tmp_path):
    with db.connect(tmp_path / "a.db") as c:
        c.execute("CREATE TABLE t(x)")
        raw = c._raw
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_exception_propagates_and_closes(tmp_path):
    with pytest.raises(RuntimeError):
        with db.connect(tmp_path / "a.db") as c:
            raw = c._raw
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_cursor_semantics_unchanged(tmp_path):
    with db.connect(tmp_path / "a.db") as c:
        c.execute("CREATE TABLE t(id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
        cur = c.execute("INSERT INTO t(v) VALUES(?)", ("a",))
        assert cur.lastrowid == 1
        c.executemany("INSERT INTO t(v) VALUES(?)", [("b",), ("c",)])
        assert c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
        c.commit()  # explicit commit still works (log_import calls it)


def test_writes_visible_to_other_connection_after_with(tmp_path):
    p = tmp_path / "a.db"
    with db.connect(p) as c:
        c.execute("CREATE TABLE t(x)")
        c.execute("INSERT INTO t VALUES (1)")
    with db.connect(p) as c:
        assert c.execute("SELECT x FROM t").fetchone()[0] == 1


def test_column_exists(tmp_path):
    with db.connect(tmp_path / "a.db") as c:
        c.execute("CREATE TABLE t(a, b)")
        assert db.column_exists(c, "t", "a")
        assert not db.column_exists(c, "t", "zzz")
        assert not db.column_exists(c, "no_such_table", "a")


# ------------------------------------------------------------------ startup behaviour
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_init_db_migrates_legacy_schema(tmp_path):
    """DB created by an older release (no tz_offset / srt_version) is upgraded in place."""
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE srt_report_session(id INTEGER PRIMARY KEY AUTOINCREMENT, device_host TEXT NOT NULL,
            route_id TEXT NOT NULL, route_name TEXT, name TEXT, started_at TEXT NOT NULL,
            ended_at TEXT NOT NULL, created_at TEXT);
        CREATE TABLE srt_connection_sample(id INTEGER PRIMARY KEY AUTOINCREMENT, device_host TEXT NOT NULL,
            route_id TEXT NOT NULL, ts TEXT, direction TEXT, address TEXT, port INTEGER, bitrate_mbps REAL,
            rtt_ms REAL, loss_pct REAL, retransmit_bps REAL, buffer_ms REAL, state TEXT);
        INSERT INTO srt_report_session(device_host, route_id, started_at, ended_at)
            VALUES('h','r','2026-01-01 00:00:00','2026-01-01 01:00:00');
    """)
    con.commit(); con.close()
    code = "import streampilot.server as s; s._init_db()"
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                       env=dict(os.environ, **{db.ENV_URL: f"sqlite:///{p}"}), timeout=60)
    assert r.returncode == 0, r.stderr[-800:]
    con = sqlite3.connect(p)
    try:
        assert db.column_exists(con, "srt_report_session", "tz_offset")
        assert db.column_exists(con, "srt_connection_sample", "srt_version")
        assert con.execute("SELECT COUNT(*), MIN(tz_offset) FROM srt_report_session").fetchone() == (1, 0)
    finally:
        con.close()


def test_init_db_is_idempotent(tmp_path):
    p = tmp_path / "fresh.db"
    code = "import streampilot.server as s; s._init_db(); s._init_db()"
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                       env=dict(os.environ, **{db.ENV_URL: f"sqlite:///{p}"}), timeout=60)
    assert r.returncode == 0, r.stderr[-800:]


def test_db_url_cli_flag_starts_server_on_custom_file(tmp_path):
    custom = tmp_path / "custom.db"
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "streampilot", "-db_url", f"sqlite:///{custom}"], cwd=REPO,
        env=dict(os.environ, SP_PORT=str(port)), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if requests.get(f"http://127.0.0.1:{port}/login", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.3)
        else:
            pytest.fail("server did not start")
        assert custom.exists()
        con = sqlite3.connect(custom)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        assert {"live_session", "devices", "srt_report_session"} <= tables
    finally:
        proc.terminate()
        proc.wait(10)
