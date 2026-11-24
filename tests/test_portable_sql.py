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
from conftest import run_py  # noqa: E402


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


# ------------------------------------------------------------------ server helpers (every backend)
def test_streamhub_log_dedupes_on_fingerprint(db_url):
    out = run_py(
        "import streampilot.server as s\n"
        "s._init_db()\n"
        "e={'fp':'abc','ts':'2026-01-01 00:00:00','node':'n','level':'info','message':'m','raw':'r'}\n"
        "s._persist_logs('h',[e,e])\n"
        "s._persist_logs('h',[e,dict(e,fp='def')])\n"
        "with s.connect_db() as c: print(c.execute('SELECT COUNT(*) FROM streamhub_log').fetchone()[0])\n",
        db_url)
    assert out.splitlines()[-1] == "2"


def test_output_map_upsert_updates_index(db_url):
    """_persist_output_map: same (host, output) re-seen on another input moves, never duplicates."""
    out = run_py(
        "import streampilot.server as s\n"
        "s._init_db()\n"
        "mk = lambda k: {'inputs': {k: {'ip_outputs': {'0': {'name': 'OUT1', 'status': 'on'}}}}}\n"
        "s._persist_output_map('h', mk('0'))\n"
        "s._persist_output_map('h', mk('0'))\n"
        "s._persist_output_map('h', mk('2'))\n"
        "with s.connect_db() as c:\n"
        "    print(c.execute('SELECT COUNT(*), MAX(input_index), MAX(output_name) FROM device_output_map').fetchone())\n",
        db_url)
    assert out.splitlines()[-1] == "(1, 3, 'out1')"


def test_init_db_idempotent(db_url):
    run_py("import streampilot.server as s; s._init_db(); s._init_db(); s._init_db()", db_url)


_PURGE_SEED = (
    "import streampilot.server as s\n"
    "s._init_db()\n"
    "from datetime import datetime, timedelta\n"
    "fmt='%Y-%m-%d %H:%M:%S'\n"
    "ago=lambda d:(datetime.utcnow()-timedelta(days=d)).strftime(fmt)\n"
    "with s.connect_db() as c:\n"
    "    for ts in (ago(90), ago(1)):\n"
    "        c.execute('INSERT INTO srt_route_sample(device_host,route_id,ts) VALUES(?,?,?)',('h','r',ts))\n"
    "        c.execute('INSERT INTO srt_connection_sample(device_host,route_id,ts) VALUES(?,?,?)',('h','r',ts))\n"
    "        c.execute('INSERT INTO srt_event(device_host,route_id,ts,event_type) VALUES(?,?,?,?)',('h','r',ts,'e'))\n"
    "    for i, ts in enumerate((ago(90), ago(1), None)):\n"
    "        c.execute('INSERT INTO streamhub_log(device_host,ts,message,fp) VALUES(?,?,?,?)',('h',ts,'m','f%d'%i))\n"
    "s.SRTGatewayPoller(None)._purge_old_samples()\n"
    "with s.connect_db() as c:\n"
    "    q=lambda t: c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]\n"
    "    print(q('srt_route_sample'), q('srt_connection_sample'), q('srt_event'), q('streamhub_log'))\n"
)


def test_retention_purge_removes_only_old_srt_data(db_url):
    """Samples AND events older than SRT_RETENTION_DAYS go; StreamHub logs are kept by default."""
    out = run_py(_PURGE_SEED, db_url, SRT_RETENTION_DAYS="30")
    assert out.splitlines()[-1] == "1 1 1 3"


def test_streamhub_log_retention_opt_in(db_url):
    """With STREAMHUB_LOG_RETENTION_DAYS set, old logs go; recent and un-timestamped ones stay."""
    out = run_py(_PURGE_SEED, db_url, SRT_RETENTION_DAYS="30", STREAMHUB_LOG_RETENTION_DAYS="30")
    assert out.splitlines()[-1] == "1 1 1 2"


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_streamhub_log_retention_ignores_disabled_or_invalid(db_url, bad):
    out = run_py(_PURGE_SEED, db_url, SRT_RETENTION_DAYS="30", STREAMHUB_LOG_RETENTION_DAYS=bad)
    assert out.splitlines()[-1] == "1 1 1 3"


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
    client.post(app.base + "/gateway_delete_device", data={"id": gid}, allow_redirects=False)


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
