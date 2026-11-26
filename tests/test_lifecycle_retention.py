"""Orphaned-session cleanup, session/log retention (incl. window protection), and the /data poller cache."""
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

from conftest import REPO, run_py
from test_cli_and_import import _serve

sys.path.insert(0, str(REPO))
from streampilot import db  # noqa: E402
from streampilot.logger import LiveLogger  # noqa: E402


# ------------------------------------------------------------------ orphaned sessions (in-process, both backends)
def _sess(c, key, started, ended=None):
    c.execute("INSERT INTO live_session(device_id,device_host,input_key,input_index,started_at,ended_at) "
              "VALUES('1','h',?,1,?,?)", (key, started, ended))
    return c.execute("SELECT id FROM live_session WHERE input_key=?", (key,)).fetchone()[0]


def test_orphaned_sessions_are_closed_at_startup(db_url, tmp_path):
    first = LiveLogger(tmp_path / "x.db")           # creates the schema
    first._stop = True
    with db.connect() as c:
        with_samples = _sess(c, "a", "2026-01-01T10:00:00.123456")
        no_samples = _sess(c, "b", "2026-01-02T10:00:00.5")
        already_over = _sess(c, "c", "2026-01-03T10:00:00", "2026-01-03T11:00:00")
        for sec in (1, 2, 3):
            c.execute("INSERT INTO live_sample(session_id,ts) VALUES(?,?)", (with_samples, f"2026-01-01T10:00:0{sec}.9"))
    second = LiveLogger(tmp_path / "x.db")          # a restart
    second._stop = True
    with db.connect() as c:
        rows = {r[0]: r[1] for r in c.execute("SELECT id, ended_at FROM live_session").fetchall()}
    assert rows[with_samples] == "2026-01-01T10:00:03.9"      # last sample
    assert rows[no_samples] == "2026-01-02T10:00:00.5"        # no samples: started_at
    assert rows[already_over] == "2026-01-03T11:00:00"        # untouched


def test_no_session_is_left_active_after_restart(db_url, tmp_path):
    a = LiveLogger(tmp_path / "x.db"); a._stop = True
    with db.connect() as c:
        _sess(c, "a", "2026-01-01T10:00:00")
    b = LiveLogger(tmp_path / "x.db"); b._stop = True
    with db.connect() as c:
        assert c.execute("SELECT COUNT(*) FROM live_session WHERE ended_at IS NULL").fetchone()[0] == 0


# ------------------------------------------------------------------ retention (subprocess, both backends)
_RETENTION = r'''
import json, os
import streampilot.server as s
from datetime import datetime, timedelta
s._init_db()
now = datetime.utcnow()
iso = lambda d: (now - timedelta(days=d)).isoformat()                       # live_session style (T, microseconds)
txt = lambda d: (now - timedelta(days=d)).strftime('%Y-%m-%d %H:%M:%S')     # log / SRT style
def sess(c, key, host, start_d, end_d):
    c.execute("INSERT INTO live_session(device_id,device_host,input_key,input_index,started_at,ended_at) VALUES('1',?,?,1,?,?)",
              (host, key, iso(start_d), iso(end_d) if end_d is not None else None))
    sid = c.execute("SELECT id FROM live_session WHERE input_key=?", (key,)).fetchone()[0]
    c.execute("INSERT INTO live_sample(session_id,ts) VALUES(?,?)", (sid, iso(start_d)))
def log(c, name, host, ts):
    c.execute("INSERT INTO streamhub_log(device_host,ts,message,fp) VALUES(?,?,?,?)", (host, ts, name, name))
with s.connect_db() as c:
    sess(c, 'old-ended',  'h', 92, 90)      # ended 90 d ago
    sess(c, 'new-ended',  'h', 6, 5)        # ended 5 d ago
    sess(c, 'active-old', 'h2', 100, None)  # still active, started 100 d ago: never purged
    log(c, 'L-outside',   'h',  txt(60))    # old, in no session window                      -> purge
    log(c, 'L-in-old',    'h',  txt(91))    # old, inside old-ended's window                   -> kept while that session exists
    log(c, 'L-in-active', 'h2', txt(95))    # old, inside the active session's window          -> kept
    log(c, 'L-other-host','h3', txt(91))    # same time as old-ended but another device        -> purge
    log(c, 'L-recent',    'h',  txt(2))     # recent                                           -> kept
    log(c, 'L-null',      'h',  None)       # no parseable timestamp                           -> kept
    for d in (90, 60, 2):
        c.execute("INSERT INTO srt_route_sample(device_host,route_id,ts) VALUES('g','r',?)", (txt(d),))
        c.execute("INSERT INTO srt_event(device_host,route_id,ts,event_type) VALUES('g','r',?,'e')", (txt(d),))
s.SRTGatewayPoller(None)._purge_old_samples()
with s.connect_db() as c:
    out = {
      'sessions': sorted(r[0] for r in c.execute("SELECT input_key FROM live_session").fetchall()),
      'samples':  c.execute("SELECT COUNT(*) FROM live_sample").fetchone()[0],
      'logs':     sorted(r[0] for r in c.execute("SELECT message FROM streamhub_log").fetchall()),
      'srt_route': c.execute("SELECT COUNT(*) FROM srt_route_sample").fetchone()[0],
      'srt_event': c.execute("SELECT COUNT(*) FROM srt_event").fetchone()[0],
    }
print(json.dumps(out))
'''
ALL_SESSIONS = ["active-old", "new-ended", "old-ended"]
ALL_LOGS = ["L-in-active", "L-in-old", "L-null", "L-other-host", "L-outside", "L-recent"]


def _retain(db_url, **env):
    return json.loads(run_py(_RETENTION, db_url, **env).splitlines()[-1])


def test_nothing_but_old_srt_data_is_purged_by_default(db_url):
    out = _retain(db_url)
    assert out["sessions"] == ALL_SESSIONS and out["samples"] == 3
    assert out["logs"] == ALL_LOGS
    assert (out["srt_route"], out["srt_event"]) == (1, 1)      # the 60- and 90-day-old SRT rows went (30 d default)


@pytest.mark.parametrize("bad", ["0", "-5", "abc", ""])
def test_session_and_log_retention_ignore_disabled_or_invalid_values(db_url, bad):
    out = _retain(db_url, SESSION_RETENTION_DAYS=bad, STREAMHUB_LOG_RETENTION_DAYS=bad)
    assert out["sessions"] == ALL_SESSIONS and out["logs"] == ALL_LOGS


@pytest.mark.parametrize("bad", ["0", "-5", "abc"])
def test_bad_srt_retention_falls_back_to_default_instead_of_deleting_everything(db_url, bad):
    out = _retain(db_url, SRT_RETENTION_DAYS=bad)
    assert (out["srt_route"], out["srt_event"]) == (1, 1)


def test_log_retention_keeps_logs_inside_existing_session_windows(db_url):
    out = _retain(db_url, STREAMHUB_LOG_RETENTION_DAYS="30")
    # purged: old and outside every session window of that device. kept: recent, NULL ts, inside a session window
    assert out["logs"] == ["L-in-active", "L-in-old", "L-null", "L-recent"]
    assert out["sessions"] == ALL_SESSIONS


def test_session_retention_deletes_old_ended_sessions_and_their_samples_only(db_url):
    out = _retain(db_url, SESSION_RETENTION_DAYS="30")
    assert out["sessions"] == ["active-old", "new-ended"]      # ended 90 d ago went; active one (100 d old) stays
    assert out["samples"] == 2                                  # old-ended's sample went with it
    assert out["logs"] == ALL_LOGS                              # log retention is separate and off


def test_purging_a_session_releases_the_logs_it_was_protecting(db_url):
    out = _retain(db_url, SESSION_RETENTION_DAYS="30", STREAMHUB_LOG_RETENTION_DAYS="30")
    assert out["sessions"] == ["active-old", "new-ended"]
    assert "L-in-old" not in out["logs"]                        # its session is gone, so the log can go too
    assert out["logs"] == ["L-in-active", "L-null", "L-recent"]


# ------------------------------------------------------------------ poller cache behind /data
_CACHE = r'''
import json, time
import streampilot.server as s
p = s.BackgroundPoller(s.DB_PATH, interval=0.2)
out = {}
out["empty"] = p.get_latest(1, "http://a:1") is None
p._set_latest(1, "http://a:1", True, {"x": 1})
hit = p.get_latest(1, "http://a:1")
out["hit"] = [hit[0], hit[1]]
out["other_base"] = p.get_latest(1, "http://b:1") is None          # device was edited (host/port/protocol)
out["bad_id"] = p.get_latest("nope", "http://a:1") is None
time.sleep(0.7)                                                     # > 3 poll intervals (0.6 s)
out["stale"] = p.get_latest(1, "http://a:1") is None
out["max_age_override"] = p.get_latest(1, "http://a:1", max_age=60) is not None
p._set_latest(2, "http://c:1", False, "boom")
p._prune_latest([2])
out["pruned"] = p.get_latest(1, "http://a:1", max_age=60) is None and p.get_latest(2, "http://c:1") is not None
print(json.dumps(out))
'''


def test_poller_latest_result_cache(db_url):
    out = json.loads(run_py(_CACHE, db_url).splitlines()[-1])
    assert out == {"empty": True, "hit": [True, {"x": 1}], "other_base": True, "bad_id": True, "stale": True,
                   "max_age_override": True, "pruned": True}


def test_data_endpoint_serves_the_pollers_fresh_result(tmp_path):
    """Once the background poller has fetched a device, /data answers from that result (same timestamp for
    repeated calls) instead of contacting the device again for every browser."""
    proc, base = _serve([], tmp_path)
    try:
        s = requests.Session()
        s.post(base + "/login_post", data={"username": "u", "password": "p"}, allow_redirects=False)
        s.post(base + "/devices_add", data={"name": "Cam", "protocol": "http", "host": "127.0.0.7", "port": "1"},
               allow_redirects=False)
        dev_id = 1  # the only device in this fresh database
        deadline, same = time.time() + 30, False
        while time.time() < deadline and not same:
            a = s.get(base + "/data", params={"id": dev_id}).json()
            b = s.get(base + "/data", params={"id": dev_id}).json()
            assert a["ok"] is False and "timestamp" in a      # unreachable device: both paths report ok=false
            same = a["timestamp"] == b["timestamp"]
            time.sleep(0.3)
        assert same, "/data never returned the poller's cached result"
    finally:
        proc.terminate(); proc.wait(10)
