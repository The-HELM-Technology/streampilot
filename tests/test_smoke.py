"""Smoke tests: every major page/endpoint responds sanely on the configured backend(s).

These are deliberately shallow (status + a content marker). Their job is to catch
breakage while the DB layer is refactored (Phases 1-3), not to test business logic.
"""
import json

import pytest

from conftest import sample_session_json

PUBLIC_REDIRECT = ["/", "/devices", "/logs_ui", "/settings", "/health", "/gateway_dashboard"]


# ---------------------------------------------------------------- auth
@pytest.mark.parametrize("path", PUBLIC_REDIRECT)
def test_requires_login(app, path):
    import requests
    r = requests.get(app.base + path, allow_redirects=False)
    assert r.status_code in (302, 303)
    assert "/login" in r.headers["Location"]


def test_bad_login_rejected(app):
    import requests
    r = requests.post(app.base + "/login_post", data={"username": "x", "password": "y"},
                      allow_redirects=False)
    assert "/login?msg=error" in r.headers["Location"]


# ---------------------------------------------------------------- main pages
@pytest.mark.parametrize("path", ["/", "/devices", "/logs_ui", "/settings", "/health",
                                  "/health_json", "/metrics", "/gateway_dashboard",
                                  "/gateway_logs", "/gateway_geoip", "/geoip_all", "/log_import"])
def test_page_renders(app, client, path):
    r = client.get(app.base + path, allow_redirects=False)
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:200]}"
    assert r.content


def test_health_json_shape(app, client):
    r = client.get(app.base + "/health_json")
    assert isinstance(r.json(), dict)


# ---------------------------------------------------------------- StreamHub devices
def test_device_add_and_delete(app, client):
    r = client.post(app.base + "/devices_add", data={
        "name": "SH-test", "protocol": "http", "host": "127.0.0.1", "port": "1",
        "api_path": "/rest-api/", "token": "tok"}, allow_redirects=False)
    assert r.status_code in (302, 303)
    rows = app.query("SELECT id, name FROM devices WHERE name='SH-test'")
    assert len(rows) == 1
    assert "SH-test" in client.get(app.base + "/devices").text
    client.post(app.base + "/devices_delete", data={"id": rows[0][0]}, allow_redirects=False)
    assert app.query("SELECT id FROM devices WHERE name='SH-test'") == []


# ---------------------------------------------------------------- sessions
def test_import_then_list(app, client, imported_session):
    assert f"session_id={imported_session}" in client.get(app.base + "/logs_ui").text


def test_download_json_roundtrip(app, client, imported_session):
    r = client.get(app.base + "/log_download", params={"session_id": imported_session})
    assert r.status_code == 200
    data = r.json()
    assert data["session"]["title"] == "smoke session"
    assert len(data["samples"]) == len(sample_session_json()["samples"])
    assert {l["name"] for l in data["samples"][0]["links"]} == {"modem1", "eth1"}


def test_download_csv_and_geojson(app, client, imported_session):
    r = client.get(app.base + "/log_download_csv", params={"session_id": imported_session})
    assert r.status_code == 200 and "modem1" in r.text
    r = client.get(app.base + "/log_download_geojson", params={"session_id": imported_session})
    assert r.status_code == 200 and json.loads(r.text)


def test_session_view_data_and_events(app, client, imported_session):
    r = client.get(app.base + "/data", params={"session_id": imported_session})
    assert r.status_code == 200 and r.json()
    r = client.get(app.base + "/session_events", params={"session_id": imported_session})
    assert r.status_code == 200 and isinstance(r.json(), dict)


def test_session_pdf(app, client, imported_session):
    r = client.get(app.base + "/log_pdf", params={"session_id": imported_session, "tz_offset": "0"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_rename_stop_delete(app, client, imported_session):
    client.post(app.base + "/log_rename", data={"session_id": imported_session, "title": "renamed"},
               allow_redirects=False)
    assert app.query("SELECT title FROM live_session WHERE id=?", (imported_session,))[0][0] == "renamed"
    client.post(app.base + "/log_stop", data={"session_id": imported_session}, allow_redirects=False)
    assert app.query("SELECT ended_at FROM live_session WHERE id=?", (imported_session,))[0][0]
    client.post(app.base + "/log_delete", data={"session_id": imported_session}, allow_redirects=False)
    assert app.query("SELECT id FROM live_session WHERE id=?", (imported_session,)) == []
    assert app.query("SELECT id FROM live_sample WHERE session_id=?", (imported_session,)) == []


def test_purge_all(app, client, imported_session):
    client.post(app.base + "/log_purge", allow_redirects=False)
    assert app.query("SELECT COUNT(*) FROM live_session")[0][0] == 0
    assert app.query("SELECT COUNT(*) FROM live_sample")[0][0] == 0


# ---------------------------------------------------------------- SRT Gateway
GW = "gw.test"
ROUTE = "route-1"


@pytest.fixture
def srt_gateway(app, client):
    client.post(app.base + "/gateway_add_device", data={
        "name": "GW-test", "host": GW, "port": "443", "protocol": "https",
        "username": "u", "password": "p"}, allow_redirects=False)
    stmts = []
    for i in range(6):
        ts = f"2026-01-01 10:00:{i:02d}"
        stmts.append(("INSERT INTO srt_route_sample(device_host, route_id, route_name, ts, state,"
                      " total_bitrate_mbps, src_rtt_ms, src_loss_pct, src_retransmit_bps, active_connections)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (GW, ROUTE, "Route One", ts, "connected", 5.0, 20.0, 0.0, 0.0, 1)))
        stmts.append(("INSERT INTO srt_connection_sample(device_host, route_id, ts, direction, address, port,"
                      " bitrate_mbps, rtt_ms, loss_pct, retransmit_bps, buffer_ms, state, srt_version)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (GW, ROUTE, ts, "in", "203.0.113.9", 9000, 5.0, 20.0, 0.0, 0.0, 120.0, "connected", "1.5")))
    stmts.append(("INSERT INTO srt_event(device_host, route_id, route_name, ts, event_type, detail, ip_address)"
                  " VALUES(?,?,?,?,?,?,?)", (GW, ROUTE, "Route One", "2026-01-01 10:00:00", "connect", "client up", "203.0.113.9")))
    stmts.append(("INSERT INTO srt_event(device_host, route_id, route_name, ts, event_type, detail, ip_address)"
                  " VALUES(?,?,?,?,?,?,?)", (GW, ROUTE, "Route One", "2026-01-01 10:00:05", "disconnect", "client down", "203.0.113.9")))
    app.seed_sql(stmts)
    yield GW
    app.seed_sql([("DELETE FROM srt_route_sample WHERE device_host=?", (GW,)),
                  ("DELETE FROM srt_connection_sample WHERE device_host=?", (GW,)),
                  ("DELETE FROM srt_event WHERE device_host=?", (GW,)),
                  ("DELETE FROM srt_gateway_device WHERE host=?", (GW,)),
                  ("DELETE FROM srt_report_session WHERE device_host=?", (GW,))])


def test_gateway_pages(app, client, srt_gateway):
    for path, params in [
        ("/gateway_dashboard", {}),
        ("/gateway_route", {"host": GW, "route_id": ROUTE}),
        ("/gateway_logs", {}),
        ("/gateway_logs_route", {"host": GW, "route_id": ROUTE}),
    ]:
        r = client.get(app.base + path, params=params, allow_redirects=False)
        assert r.status_code == 200, f"{path}: {r.status_code}"
    assert "GW-test" in client.get(app.base + "/devices").text


def test_gateway_json_endpoints(app, client, srt_gateway):
    r = client.get(app.base + "/gateway_events", params={"host": GW, "route_id": ROUTE})
    assert r.status_code == 200 and r.json()
    r = client.get(app.base + "/gateway_route_stats", params={"host": GW, "route_id": ROUTE})
    assert r.status_code == 200 and r.json()


def test_gateway_logs_pdf(app, client, srt_gateway):
    r = client.get(app.base + "/gateway_logs_pdf", params={"host": GW, "route_id": ROUTE, "tz_offset": "0"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")


def test_gateway_report_lifecycle(app, client, srt_gateway):
    r = client.post(app.base + "/gateway_report_create", data={
        "host": GW, "route_id": ROUTE, "route_name": "Route One", "name": "rep1",
        "started_at": "2026-01-01T10:00:00", "ended_at": "2026-01-01T10:00:10", "tz_offset": "0"},
        allow_redirects=False)
    assert r.status_code in (200, 302, 303), r.text[:200]
    rows = app.query("SELECT id FROM srt_report_session WHERE device_host=?", (GW,))
    assert len(rows) == 1
    rid = rows[0][0]
    r = client.get(app.base + "/gateway_reports_json", params={"host": GW, "route_id": ROUTE})
    assert r.status_code == 200 and r.json()
    r = client.get(app.base + "/gateway_report_pdf", params={"report_id": rid, "tz_offset": "0"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    client.post(app.base + "/gateway_report_delete", data={"report_id": rid, "host": GW, "route_id": ROUTE},
               allow_redirects=False)
    assert app.query("SELECT id FROM srt_report_session WHERE id=?", (rid,)) == []


def test_gateway_logs_delete(app, client, srt_gateway):
    client.post(app.base + "/gateway_logs_delete", data={"host": GW, "route_id": ROUTE}, allow_redirects=False)
    assert app.query("SELECT COUNT(*) FROM srt_event WHERE device_host=?", (GW,))[0][0] == 0


def test_geoip_save_delete(app, client):
    client.post(app.base + "/gateway_geoip_save",
                data={"ip": "198.51.100.7", "lat": "1.5", "lng": "2.5", "label": "lab"}, allow_redirects=False)
    assert app.query("SELECT label FROM srt_endpoint_location WHERE address=?", ("198.51.100.7",))[0][0] == "lab"
    # upsert path (INSERT OR REPLACE today)
    client.post(app.base + "/gateway_geoip_save",
                data={"ip": "198.51.100.7", "lat": "3", "lng": "4", "label": "lab2"}, allow_redirects=False)
    assert app.query("SELECT label FROM srt_endpoint_location WHERE address=?", ("198.51.100.7",))[0][0] == "lab2"
    client.post(app.base + "/gateway_geoip_delete", data={"ip": "198.51.100.7"}, allow_redirects=False)
    assert app.query("SELECT 1 FROM srt_endpoint_location WHERE address=?", ("198.51.100.7",)) == []


# ---------------------------------------------------------------- settings / slack
def test_settings_save_paths(app, client):
    client.post(app.base + "/devices_add", data={"name": "SH-slack", "protocol": "http",
                "host": "127.0.0.2", "port": "1"}, allow_redirects=False)
    did = app.query("SELECT id FROM devices WHERE name='SH-slack'")[0][0]
    assert client.get(app.base + "/settings").status_code == 200
    r = client.post(app.base + "/settings_slack_save", data={
        "webhook_url": "https://hooks.invalid/x", "channel": "#c", "username": "u"}, allow_redirects=False)
    assert r.status_code in (200, 302, 303)
    r = client.post(app.base + "/settings_device_save", data={"device_id": did, "notify_session": "1"},
                    allow_redirects=False)
    assert r.status_code in (200, 302, 303)
    for p in ("/settings_device_pause", "/settings_device_resume"):
        r = client.post(app.base + p, data={"device_id": did}, allow_redirects=False)
        assert r.status_code in (200, 302, 303)
    assert app.query("SELECT notify_paused FROM device_slack WHERE device_id=?", (did,))[0][0] == 0
    client.post(app.base + "/devices_delete", data={"id": did}, allow_redirects=False)


# ---------------------------------------------------------------- concurrency / pooling
def test_concurrent_requests_do_not_leak_connections(app, client, imported_session):
    """On Postgres the app runs with a pool of 4 (see conftest): 120 requests over 4 threads only
    succeed if every `with connect_db()` block hands its connection back."""
    from concurrent.futures import ThreadPoolExecutor
    urls = [app.base + "/logs_ui", app.base + "/devices", app.base + "/health",
            app.base + f"/log_download?session_id={imported_session}"]

    def hit(i):
        r = client.get(urls[i % len(urls)], timeout=60)
        return r.status_code

    with ThreadPoolExecutor(max_workers=4) as ex:  # CherryPy's listen backlog is 5; more workers cause TCP resets
        codes = list(ex.map(hit, range(120)))
    assert set(codes) == {200}, {c: codes.count(c) for c in set(codes)}
