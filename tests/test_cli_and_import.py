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


# ---------------------------------------------------------------- status badge
def _serve(args, tmp_path, extra_env=None):
    """Start `python -m streampilot *args` on a free port; returns (proc, base_url)."""
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if k not in ("CLIENT_STATUS", "CLIENT_NAME")}
    env.update(SP_USER="u", SP_PASSWORD="p", STREAMPILOT_DB_URL=f"sqlite:///{tmp_path}/s.db",
               SP_PORT=str(port), **(extra_env or {}))
    proc = subprocess.Popen([sys.executable, "-m", "streampilot", *args], cwd=REPO, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        assert proc.poll() is None, "server exited early"
        try:
            if requests.get(base + "/login", timeout=1).status_code == 200:
                return proc, base
        except requests.RequestException:
            time.sleep(0.3)
    proc.terminate()
    raise AssertionError("server did not start")


def _header_html(base):
    s = requests.Session()
    s.post(base + "/login_post", data={"username": "u", "password": "p"}, allow_redirects=False)
    return s.get(base + "/devices").text


@pytest.mark.parametrize("status, cls", [("demo", "text-bg-info"), ("Production", "text-bg-success")])
def test_status_flag_shows_badge(tmp_path, status, cls):
    proc, base = _serve(["-name", "Acme", "-status", status], tmp_path)
    try:
        html = _header_html(base)
        assert f'badge {cls} ms-2">{status}<' in html
        assert 'ms-3">Acme<' in html  # the client badge still works
    finally:
        proc.terminate(); proc.wait(10)


def test_no_status_badge_by_default(tmp_path):
    proc, base = _serve(["-name", "Acme"], tmp_path)
    try:
        html = _header_html(base)
        assert 'ms-3">Acme<' in html
        assert "text-bg-info ms-2" not in html and "text-bg-success ms-2" not in html
    finally:
        proc.terminate(); proc.wait(10)


def test_status_from_environment(tmp_path):
    proc, base = _serve([], tmp_path, {"CLIENT_STATUS": "demo"})
    try:
        assert 'badge text-bg-info ms-2">demo<' in _header_html(base)
    finally:
        proc.terminate(); proc.wait(10)
