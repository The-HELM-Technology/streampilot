"""Backend-agnostic smoke-test harness for StreamPilot.

The app is started as a real subprocess from a *temporary copy* of the package
(DB_PATH resolves to <copy>/mini.db), so tests never touch a developer's mini.db
and exercise the same HTTP surface a user does.

BACKENDS lists the DB backends the suite runs against. Phase 3 adds "postgres".
"""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

REPO = Path(__file__).resolve().parents[1]
USER, PASSWORD = "tester", "s3cret"

# Postgres runs only when STREAMPILOT_TEST_PG_URL points at a server we may create
# databases on, e.g. postgresql://postgres:test@127.0.0.1:54329/postgres
PG_URL = os.getenv("STREAMPILOT_TEST_PG_URL")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])

sys.path.insert(0, str(REPO))
from streampilot import db as _db  # noqa: E402  (translate() reused for test-side SQL)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class App:
    def __init__(self, root: Path, port: int, backend: str, db_url: str = None):
        self.root, self.port, self.backend, self.db_url = root, port, backend, db_url
        self.base = f"http://127.0.0.1:{port}"
        self.db_path = root / "mini.db"

    # -- direct DB access for data that has no HTTP entry point (SRT samples, logs).
    #    SQL is written SQLite-style ('?' placeholders) and translated for Postgres.
    def _run(self, statements, fetch=False):
        if self.backend == "sqlite":
            con = sqlite3.connect(self.db_path, timeout=10)
            try:
                out = None
                for sql, params in statements:
                    cur = con.execute(sql, params)
                    out = cur.fetchall() if fetch else None
                con.commit()
                return out
            finally:
                con.close()
        import psycopg
        with psycopg.connect(self.db_url, autocommit=True) as con:
            out = None
            for sql, params in statements:
                cur = con.execute(_db.translate(sql, bool(params)), params or None)
                out = cur.fetchall() if fetch else None
            return out

    def seed_sql(self, statements):
        """statements: list of (sql, params)."""
        self._run(statements)

    def query(self, sql, params=()):
        return self._run([(sql, params)], fetch=True)


@pytest.fixture(scope="session", params=BACKENDS)
def app(request, tmp_path_factory):
    backend = request.param
    root = tmp_path_factory.mktemp(f"sp_{backend}")
    pg_dbname = pg_url = None
    if backend == "postgres":
        import psycopg
        from urllib.parse import urlsplit
        pg_dbname = "sp_test_" + os.urandom(4).hex()
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{pg_dbname}"')
        pg_url = urlsplit(PG_URL)._replace(path="/" + pg_dbname).geturl()
    shutil.copytree(
        REPO / "streampilot", root / "streampilot",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    port = _free_port()
    env = dict(
        os.environ,
        SP_PORT=str(port), SP_USER=USER, SP_PASSWORD=PASSWORD,
        MAX_STREAMHUB="5", SP_SESSION_SECRET="test-secret",
    )
    env.pop(_db.ENV_URL, None)
    if pg_url:
        env[_db.ENV_URL] = pg_url
        env["STREAMPILOT_DB_POOL_MAX"] = "4"       # small on purpose: a leaked connection would hang tests
        env["STREAMPILOT_DB_POOL_TIMEOUT"] = "10"
    log = open(root / "server.log", "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "streampilot"], cwd=root, env=env,
        stdout=log, stderr=subprocess.STDOUT,
    )
    a = App(root, port, backend, pg_url)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"server exited early:\n{(root / 'server.log').read_text()}")
            try:
                if requests.get(a.base + "/login", timeout=1).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.3)
        else:
            raise RuntimeError("server did not start in 30s")
        yield a
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        if pg_dbname:
            with psycopg.connect(PG_URL, autocommit=True) as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{pg_dbname}" WITH (FORCE)')


@pytest.fixture(scope="session")
def client(app):
    """Logged-in requests.Session (redirects NOT followed, so tests can assert on them)."""
    s = requests.Session()
    s.base = app.base
    r = s.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD},
               allow_redirects=False)
    assert r.status_code in (302, 303) and r.headers["Location"].endswith("/"), r.text[:200]
    return s


import itertools
_seq = itertools.count()


def sample_session_json(n_ticks=5, links=("modem1", "eth1")):
    """A session in the format produced by /log_download and consumed by /log_import."""
    # unique started_at per call: live_session has UNIQUE(device_id, input_key, started_at)
    n = next(_seq)
    started = f"2026-01-01T10:{n // 60:02d}:{n % 60:02d}"
    samples = []
    for i in range(n_ticks):
        samples.append({
            "ts": f"2026-01-01T10:00:{i:02d}",
            "year": 2026, "month": 1, "day": 1, "hour": 10, "minute": 0, "second": i,
            "latitude": 48.85 + i * 1e-4, "longitude": 2.35 + i * 1e-4,
            "drops_video": i, "drops_ts": 0,
            "links": [
                {"name": n, "owdR": 20 + i, "rx_bitrate": 4_000_000,
                 "rx_percent_lost": 0, "rx_lost_nb_packets": 0}
                for n in links
            ],
        })
    return {
        "session": {
            "device_id": "1", "device_host": "127.0.0.1", "input_key": "0", "input_index": 1,
            "input_identifier": "input 1", "input_display_name": "Cam A",
            "started_at": started, "ended_at": "2026-01-01T12:00:10",
            "title": "smoke session",
        },
        "samples": samples,
    }


@pytest.fixture
def imported_session(app, client):
    """Import a fresh session through the real /log_import endpoint; return its id."""
    before = {r[0] for r in app.query("SELECT id FROM live_session")}
    payload = json.dumps(sample_session_json()).encode()
    r = client.post(app.base + "/log_import", files={"file": ("s.json", payload, "application/json")},
                    allow_redirects=False)
    assert r.status_code in (302, 303), r.text[:300]
    new = {r[0] for r in app.query("SELECT id FROM live_session")} - before
    assert len(new) == 1
    return new.pop()


# ---------------------------------------------------------------- per-test databases
def _new_pg_db():
    import psycopg
    from urllib.parse import urlsplit
    name = "sp_t_" + os.urandom(4).hex()
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    return name, urlsplit(PG_URL)._replace(path="/" + name).geturl()


@pytest.fixture(params=BACKENDS)
def db_url(request, tmp_path, monkeypatch):
    """A fresh, empty database URL per test, also exported as STREAMPILOT_DB_URL."""
    if request.param == "sqlite":
        url, name = f"sqlite:///{tmp_path}/t.db", None
    else:
        name, url = _new_pg_db()
    monkeypatch.setenv(_db.ENV_URL, url)
    try:
        yield url
    finally:
        _db.close_pool()
        if name:
            import psycopg
            with psycopg.connect(PG_URL, autocommit=True) as admin:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def run_py(code, url):
    """Run python code with streampilot importable and the DB set to `url`; return stdout."""
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=90,
                       env=dict(os.environ, **{_db.ENV_URL: url}))
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stdout.strip()
