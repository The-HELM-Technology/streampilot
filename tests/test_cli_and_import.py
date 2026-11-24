"""Regression tests: the -port flag, and /log_import error handling."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

from conftest import REPO, sample_session_json


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_flag_sets_listening_port(tmp_path):
    """`-port N` used to be ignored (it set an env var nothing reads), so the server stayed on 5555."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("SP_PORT", "StreamPilot")}
    env["STREAMPILOT_DB_URL"] = f"sqlite:///{tmp_path}/p.db"
    proc = subprocess.Popen([sys.executable, "-m", "streampilot", "-port", str(port)], cwd=REPO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            assert proc.poll() is None, "server exited early"
            try:
                if requests.get(f"http://127.0.0.1:{port}/login", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                time.sleep(0.3)
        pytest.fail(f"server is not listening on the port given with -port ({port})")
    finally:
        proc.terminate()
        proc.wait(10)


def _post(app, client, payload, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode()
    return client.post(app.base + "/log_import", files={"file": ("s.json", data, "application/json")},
                       allow_redirects=False)


def test_import_same_session_twice_is_reported_not_500(app, client):
    payload = sample_session_json()
    r1 = _post(app, client, payload)
    assert r1.status_code in (302, 303) and "imported" in r1.headers["Location"].lower()
    sid = app.query("SELECT id FROM live_session WHERE started_at=?", (payload["session"]["started_at"],))[0][0]
    n_samples = app.query("SELECT COUNT(*) FROM live_sample WHERE session_id=?", (sid,))[0][0]

    r2 = _post(app, client, payload)
    assert r2.status_code in (302, 303), r2.status_code
    loc = r2.headers["Location"].lower()
    assert "already" in loc and f"id%20{sid}" in loc
    # nothing duplicated
    assert app.query("SELECT COUNT(*) FROM live_session WHERE started_at=?", (payload["session"]["started_at"],))[0][0] == 1
    assert app.query("SELECT COUNT(*) FROM live_sample WHERE session_id=?", (sid,))[0][0] == n_samples


@pytest.mark.parametrize("missing", ["device_id", "input_key", "input_index", "started_at"])
def test_import_rejects_session_missing_required_field(app, client, missing):
    payload = sample_session_json()
    del payload["session"][missing]
    r = _post(app, client, payload)
    assert r.status_code == 400, r.status_code
    assert missing in r.text


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"session": 5}', b'{"samples": []}'])
def test_import_rejects_garbage_without_500(app, client, raw):
    r = _post(app, client, None, raw=raw)
    assert r.status_code in (200, 400), r.status_code  # friendly page, never a 500


@pytest.mark.parametrize("bad", ["abc", "0", "70000", "-1"])
def test_port_flag_rejects_invalid_value(bad):
    r = subprocess.run([sys.executable, "-m", "streampilot", "-port", bad], cwd=REPO,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "-port must be a number" in r.stderr
