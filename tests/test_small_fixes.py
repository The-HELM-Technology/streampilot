"""Regression tests for small fixes: log URL query string, UTC sample age, device recovery alert."""
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from conftest import REPO, run_py
from test_cli_and_import import _serve

sys.path.insert(0, str(REPO / "streampilot"))
from collect.scripts import streamhub  # noqa: E402


# ------------------------------------------------------------------ StreamHub log URL
class _Resp:
    status_code = 404
    headers = {"content-type": "text/plain"}
    text = ""


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _Resp()


@pytest.mark.parametrize("env_path", [None, "/custom/logs"])
def test_fetch_logs_urls_have_a_single_query_string(monkeypatch, env_path):
    """Used to be .../logs?limit=200?api_key=TOKEN (two '?'), so the token never reached the server."""
    if env_path:
        monkeypatch.setenv("SP_SH_LOG_PATH", env_path)
    else:
        monkeypatch.delenv("SP_SH_LOG_PATH", raising=False)
    s = _Session()
    streamhub._fetch_logs(s, "https://10.0.0.1:8896/", "TOK", timeout=1, limit=123)
    assert s.urls, "no request was made"
    for u in s.urls:
        assert u.count("?") == 1, u
        assert "api_key=TOK" in u and u.endswith("&limit=123"), u
    if env_path:
        assert s.urls[0].startswith("https://10.0.0.1:8896/custom/logs?")


# ------------------------------------------------------------------ /metrics sample age is UTC
def test_metrics_sample_age_ignores_server_timezone(tmp_path):
    """DB timestamps are naive UTC. On a server in UTC+13 the age used to be ~13 hours too large."""
    proc, base = _serve([], tmp_path, {"TZ": "Pacific/Auckland"})
    try:
        s = requests.Session()
        s.post(base + "/login_post", data={"username": "u", "password": "p"}, allow_redirects=False)
        s.post(base + "/devices_add", data={"name": "Cam", "protocol": "http", "host": "127.0.0.7", "port": "1"},
               allow_redirects=False)
        ts = (datetime.now(timezone.utc) - timedelta(seconds=7)).replace(tzinfo=None).isoformat(timespec="seconds")
        con = sqlite3.connect(tmp_path / "s.db", timeout=10)
        con.execute("INSERT INTO live_session(device_id,device_host,input_key,input_index,started_at) "
                    "VALUES('1','127.0.0.7','0',1,?)", (ts,))
        con.execute("INSERT INTO live_sample(session_id,ts) VALUES(1,?)", (ts,))
        con.commit(); con.close()
        text = s.get(base + "/metrics").text
        line = [l for l in text.splitlines() if l.startswith("device_last_sample_age_seconds{")][0]
        age = int(line.rsplit(" ", 1)[1])
        assert 0 <= age < 120, f"age {age}s (a timezone bug would give ~46800)"
    finally:
        proc.terminate(); proc.wait(10)


# ------------------------------------------------------------------ "unreachable" / "reachable again"
_RECOVERY = r'''
import json, time
import streampilot.server as s
import collect.scripts.streamhub as sh
s._init_db()
with s.connect_db() as c:
    c.execute("INSERT INTO devices(name,protocol,host,port,api_path,token) VALUES('Cam','http','127.0.0.7',1,'/rest-api/','t')")
    c.execute("UPDATE slack_config SET webhook_url='http://hook.invalid/x' WHERE id=1")
    PAUSE
msgs = []
s.BackgroundPoller._slack_post = staticmethod(lambda webhook, text, channel, username, color=None, title=None: msgs.append(text))
state = {"ok": False}
# same shapes the real fetch_streamhub returns: (False, "<error string>") / (True, payload dict)
calls = []
def fake_fetch(base, token, timeout=5):
    calls.append(state["ok"])
    return (True, {"inputs": {}}) if state["ok"] else (False, "REQUEST_ERROR: boom")
s.fetch_streamhub = fake_fetch
sh.fetch_logs_structured = lambda *a, **k: (False, [])      # no real network call (it would block ~2s per poll)
p = s.BackgroundPoller(s.DB_PATH, interval=0.05)
p.start()
def wait_msgs(n, timeout=15):
    end = time.time() + timeout
    while len(msgs) < n and time.time() < end:
        time.sleep(0.05)
WAIT_ONLY = WAITONLY
if WAIT_ONLY:                                              # fail, recover, fail again: all silent
    time.sleep(0.8); state["ok"] = True; time.sleep(0.8); state["ok"] = False; time.sleep(0.8)
    snapshots = [len(msgs)]
else:
    wait_msgs(1); time.sleep(0.6); first = len(msgs)       # many failed polls -> still exactly one "unreachable"
    state["ok"] = True;  wait_msgs(2)                      # recovered         -> one "reachable again"
    state["ok"] = False; wait_msgs(3)                      # fails again       -> a NEW "unreachable" (flag cleared)
    state["ok"] = True;  wait_msgs(4); time.sleep(0.6)
    snapshots = [first, len(msgs)]
p.stop()
print(json.dumps({"msgs": msgs, "snapshots": snapshots, "ok_polls": calls.count(True), "failed_polls": calls.count(False)}))
'''


def _kinds(msgs):
    out = []
    for m in msgs:
        out.append("down" if "unreachable" in m else "up" if "reachable again" in m else m)
    return out


def test_device_recovery_is_announced_and_rearms_the_alert(db_url):
    out = json.loads(run_py(_RECOVERY.replace("PAUSE", "pass").replace("WAITONLY", "False"), db_url).splitlines()[-1])
    msgs = out["msgs"]
    assert _kinds(msgs) == ["down", "up", "down", "up"], msgs
    assert all("*Cam*" in m for m in msgs)
    assert out["snapshots"][0] == 1, "repeated failed polls must not repeat the 'unreachable' alert"


def test_no_messages_when_notifications_are_paused(db_url):
    paused = "c.execute('INSERT INTO device_slack(device_id, notify_paused) VALUES(1, 1) ON CONFLICT DO NOTHING')"
    out = json.loads(run_py(_RECOVERY.replace("PAUSE", paused).replace("WAITONLY", "True"), db_url).splitlines()[-1])
    assert out["ok_polls"] > 3 and out["failed_polls"] > 3, out   # the poller really cycled through both states
    assert out["msgs"] == []
