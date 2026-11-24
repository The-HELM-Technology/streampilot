"""Behavioural checks for the SQL that was rewritten to be portable (phase 2):
INSERT OR IGNORE/REPLACE -> ON CONFLICT, lastrowid -> RETURNING, startup migrations."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from streampilot import db  # noqa: E402
from streampilot.logger import LiveLogger  # noqa: E402


def _run_in_app_env(code, dbfile):
    """Run python code with streampilot.server importable and the DB redirected to dbfile."""
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ, **{db.ENV_URL: f"sqlite:///{dbfile}"}))
    assert r.returncode == 0, r.stderr[-1000:]
    return r.stdout.strip()


# ------------------------------------------------------------------ LiveLogger
def test_legacy_live_tables_are_upgraded(tmp_path):
    """live_session without title / live_sample without rx_* (older releases) gain the columns
    at startup; this used to be done lazily inside request handlers."""
    p = tmp_path / "legacy.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE live_session(id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT NOT NULL,
            device_host TEXT, input_key TEXT NOT NULL, input_index INTEGER NOT NULL,
            input_identifier TEXT, input_display_name TEXT, started_at DATETIME NOT NULL, ended_at DATETIME);
        CREATE TABLE live_sample(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL,
            ts DATETIME NOT NULL, year INTEGER, month INTEGER, day INTEGER, hour INTEGER, minute INTEGER,
            second INTEGER, latitude REAL, longitude REAL, drops_video INTEGER, drops_ts INTEGER,
            link_name TEXT, owdR INTEGER, rx_bitrate INTEGER);
        INSERT INTO live_session(device_id, input_key, input_index, started_at) VALUES('1','0',1,'2026-01-01T00:00:00');
        INSERT INTO live_sample(session_id, ts) VALUES(1,'2026-01-01T00:00:01');
    """)
    con.commit(); con.close()
    lg = LiveLogger(p)
    lg._stop = True
    con = sqlite3.connect(p)
    try:
        assert db.column_exists(con, "live_session", "title")
        assert db.column_exists(con, "live_sample", "rx_percent_lost")
        assert db.column_exists(con, "live_sample", "rx_lost_nb_packets")
        assert con.execute("SELECT COUNT(*) FROM live_sample").fetchone()[0] == 1  # data kept
    finally:
        con.close()


def test_start_session_returns_distinct_ids(tmp_path):
    lg = LiveLogger(tmp_path / "t.db")
    try:
        a = lg._start_session("1", "h", "0", 1, "i1", "A")
        b = lg._start_session("1", "h", "1", 2, "i2", "B")
        assert isinstance(a, int) and isinstance(b, int) and a != b
    finally:
        lg._stop = True


# ------------------------------------------------------------------ server helpers
def test_streamhub_log_dedupes_on_fingerprint(tmp_path):
    p = tmp_path / "x.db"
    out = _run_in_app_env(
        "import streampilot.server as s\n"
        "s._init_db()\n"
        "e={'fp':'abc','ts':'2026-01-01 00:00:00','node':'n','level':'info','message':'m','raw':'r'}\n"
        "s._persist_logs('h',[e,e])\n"
        "s._persist_logs('h',[e,dict(e,fp='def')])\n"
        "with s.connect_db() as c: print(c.execute('SELECT COUNT(*) FROM streamhub_log').fetchone()[0])\n",
        p)
    assert out.splitlines()[-1] == "2"


def test_output_map_upsert_updates_index(tmp_path):
    """_persist_output_map: same (host, output) re-seen on another input moves, never duplicates."""
    p = tmp_path / "x.db"
    out = _run_in_app_env(
        "import streampilot.server as s\n"
        "s._init_db()\n"
        "mk = lambda k: {'inputs': {k: {'ip_outputs': {'0': {'name': 'OUT1', 'status': 'on'}}}}}\n"
        "s._persist_output_map('h', mk('0'))\n"
        "s._persist_output_map('h', mk('0'))\n"
        "s._persist_output_map('h', mk('2'))\n"
        "with s.connect_db() as c:\n"
        "    print(c.execute('SELECT COUNT(*), MAX(input_index), MAX(output_name) FROM device_output_map').fetchone())\n",
        p)
    assert out.splitlines()[-1] == "(1, 3, 'out1')"


# ------------------------------------------------------------------ via the running app
def test_gateway_readd_keeps_id_and_resets_session(app, client):
    host = "readd.test"
    client.post(app.base + "/gateway_add_device", data={"name": "old", "host": host, "port": "443",
                "protocol": "https", "username": "u", "password": "p"}, allow_redirects=False)
    gid = app.query("SELECT id FROM srt_gateway_device WHERE host=?", (host,))[0][0]
    app.seed_sql([("UPDATE srt_gateway_device SET session_id='s1', gw_device_id='d1' WHERE host=?", (host,))])
    client.post(app.base + "/gateway_add_device", data={"name": "new", "host": host, "port": "8443",
                "protocol": "http", "username": "u2", "password": "p2"}, allow_redirects=False)
    rows = app.query("SELECT id, name, port, protocol, username, password, session_id, gw_device_id "
                     "FROM srt_gateway_device WHERE host=?", (host,))
    assert rows == [(gid, "new", 8443, "http", "u2", "p2", None, None)]
    client.get(app.base + "/gateway_delete_device", params={"id": gid}, allow_redirects=False)


def test_import_returns_fresh_ids_each_time(app, client, imported_session):
    # second import must create a new session row (RETURNING id), not reuse/collide
    from conftest import sample_session_json
    import json
    payload = json.dumps(sample_session_json()).encode()
    r = client.post(app.base + "/log_import", files={"file": ("s.json", payload, "application/json")},
                    allow_redirects=False)
    assert r.status_code in (302, 303)
    ids = [x[0] for x in app.query("SELECT id FROM live_session ORDER BY id")]
    assert len(ids) >= 2 and len(set(ids)) == len(ids)
    n = app.query("SELECT COUNT(*) FROM live_sample WHERE session_id=?", (ids[-1],))[0][0]
    assert n == 10  # 5 ticks x 2 links
