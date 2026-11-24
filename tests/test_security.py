"""Web-UI hardening: POST-only state changes, same-origin (CSRF) check, CORS, cookies, HTML escaping."""
import json
import re

import pytest
import requests

from conftest import USER, PASSWORD, sample_session_json
from test_smoke import srt_gateway  # noqa: F401  (fixture: seeded SRT gateway + route + events)
from test_cli_and_import import _serve

# Every endpoint that changes data. They must refuse GET.
POST_ONLY = [
    "login_post", "devices_add", "devices_delete", "gateway_add_device", "gateway_delete_device",
    "gateway_geoip_save", "gateway_geoip_delete", "gateway_logs_delete", "gateway_report_create",
    "gateway_report_delete", "log_delete", "log_purge", "log_rename", "log_stop",
    "settings_slack_save", "settings_slack_test", "settings_device_save", "settings_device_pause",
    "settings_device_resume", "settings_gw_save", "settings_gw_pause", "settings_gw_resume",
]
XSS = '<img src=x onerror=alert(1)>'
BREAKOUT = '</script><script>alert(2)</script>'


# ------------------------------------------------------------------ POST only
@pytest.mark.parametrize("ep", POST_ONLY)
def test_state_changing_endpoints_reject_get(app, client, ep):
    r = client.get(app.base + "/" + ep, allow_redirects=False)
    assert r.status_code == 405, f"GET /{ep} -> {r.status_code}"


def test_get_delete_with_valid_params_does_nothing(app, client, imported_session):
    """The classic CSRF shape: an <img src=/log_delete?session_id=N> must not delete anything."""
    r = client.get(app.base + "/log_delete", params={"session_id": imported_session}, allow_redirects=False)
    assert r.status_code == 405
    assert app.query("SELECT COUNT(*) FROM live_session WHERE id=?", (imported_session,))[0][0] == 1


def test_get_report_delete_does_nothing(app, client, srt_gateway):
    app.seed_sql([("INSERT INTO srt_report_session(device_host, route_id, name, started_at, ended_at) "
                   "VALUES(?,?,?,?,?)", (srt_gateway, "route-1", "r", "2026-01-01 00:00:00", "2026-01-01 01:00:00"))])
    rid = app.query("SELECT id FROM srt_report_session WHERE device_host=?", (srt_gateway,))[0][0]
    r = client.get(app.base + "/gateway_report_delete", params={"report_id": rid}, allow_redirects=False)
    assert r.status_code == 405
    assert app.query("SELECT COUNT(*) FROM srt_report_session WHERE id=?", (rid,))[0][0] == 1


def test_no_page_links_by_get_to_post_only_endpoints(app, client, imported_session, srt_gateway):
    """Every action link in the UI must be a form POST, or the button would now 405."""
    pages = ["/", "/devices", "/logs_ui", "/settings", "/health", "/gateway_dashboard", "/gateway_logs",
             f"/gateway_logs_route?host={srt_gateway}&route_id=route-1",
             f"/gateway_route?host={srt_gateway}&route_id=route-1", "/gateway_geoip",
             f"/log_view?session_id={imported_session}"]
    bad = []
    for path in pages:
        html = client.get(app.base + path).text
        for m in re.finditer(r"""(?:href|src)\s*=\s*["'](/[A-Za-z_]+)""", html):
            if m.group(1).lstrip("/") in POST_ONLY:
                bad.append((path, m.group(1)))
    assert not bad, f"GET links to POST-only endpoints: {bad}"


# ------------------------------------------------------------------ CSRF (same-origin check)
def _add_device(app, client, name, headers=None):
    return client.post(app.base + "/devices_add", data={"name": name, "protocol": "http", "host": "127.0.0.9",
                       "port": "1"}, headers=headers or {}, allow_redirects=False)


def _device_exists(app, name):
    return bool(app.query("SELECT 1 FROM devices WHERE name=?", (name,)))


@pytest.mark.parametrize("hdr", [
    {"Origin": "http://evil.example"},
    {"Referer": "http://evil.example/attack.html"},
    {"Origin": "null"},
    {"Origin": "http://127.0.0.1:1"},                       # right host, wrong port
])
def test_cross_origin_post_is_rejected_and_has_no_effect(app, client, hdr):
    r = _add_device(app, client, "csrf-victim", hdr)
    assert r.status_code == 403, r.status_code
    assert not _device_exists(app, "csrf-victim")


def test_same_origin_and_headerless_posts_are_accepted(app, client):
    r = _add_device(app, client, "same-origin", {"Origin": app.base})
    assert r.status_code in (302, 303)
    assert _device_exists(app, "same-origin")
    r = _add_device(app, client, "referer-ok", {"Referer": app.base + "/devices"})
    assert r.status_code in (302, 303) and _device_exists(app, "referer-ok")
    r = _add_device(app, client, "no-headers")  # non-browser client: no Origin/Referer
    assert r.status_code in (302, 303) and _device_exists(app, "no-headers")
    for n in ("same-origin", "referer-ok", "no-headers"):
        app.seed_sql([("DELETE FROM devices WHERE name=?", (n,))])


def test_cross_origin_login_post_rejected(app):
    r = requests.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD},
                      headers={"Origin": "http://evil.example"}, allow_redirects=False)
    assert r.status_code == 403
    assert "session_id" not in r.cookies


def test_proxy_mode_uses_forwarded_host(tmp_path):
    proc, base = _serve(["-mode", "proxy"], tmp_path)
    try:
        def post(origin, fwd):
            return requests.post(base + "/devices_add", data={"host": "h"}, allow_redirects=False,
                                 headers={"Origin": origin, "X-Forwarded-Host": fwd, "X-Forwarded-Proto": "https"})
        assert post("https://sp.example.com", "sp.example.com").status_code != 403   # (302 -> /login)
        assert post("https://evil.example", "sp.example.com").status_code == 403
        assert post("https://sp.example.com:8443", "sp.example.com").status_code == 403
    finally:
        proc.terminate(); proc.wait(10)


# ------------------------------------------------------------------ CORS / cookies
@pytest.mark.parametrize("path", ["/login", "/devices", "/logs_ui", "/health_json"])
def test_no_wildcard_cors(app, client, path):
    r = client.get(app.base + path, headers={"Origin": "http://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_session_cookie_is_httponly_and_samesite_lax(app):
    r = requests.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD}, allow_redirects=False)
    set_cookie = r.headers.get("Set-Cookie", "")
    assert "session_id=" in set_cookie, set_cookie
    assert "samesite=lax" in set_cookie.lower()
    assert "httponly" in set_cookie.lower()


def test_login_issues_a_fresh_session_id(app):
    """Session fixation: the id presented before login must not be the one authenticated afterwards."""
    s = requests.Session()
    s.post(app.base + "/login_post", data={"username": "nope", "password": "nope"}, allow_redirects=False)
    # Establish a pre-login session server-side by logging in once, logging out, and reusing that id.
    s.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD}, allow_redirects=False)
    first = s.cookies.get("session_id")
    s.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD}, allow_redirects=False)
    assert first and s.cookies.get("session_id") != first


def test_wrong_credentials_rejected_and_right_ones_accepted(app):
    bad = requests.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD + "x"},
                        allow_redirects=False)
    assert "msg=error" in bad.headers["Location"]
    # non-ASCII input must not crash the constant-time comparison
    odd = requests.post(app.base + "/login_post", data={"username": "é中", "password": "ü"}, allow_redirects=False)
    assert odd.status_code in (302, 303) and "msg=error" in odd.headers["Location"]
    ok = requests.post(app.base + "/login_post", data={"username": USER, "password": PASSWORD}, allow_redirects=False)
    assert ok.headers["Location"].endswith("/") and "msg=error" not in ok.headers["Location"]


# ------------------------------------------------------------------ output escaping (XSS)
@pytest.fixture
def hostile_data(app, client):
    """Entities whose every user-controlled string carries markup."""
    name = f"dev {XSS}"
    client.post(app.base + "/devices_add", data={"name": name, "protocol": "http", "host": "127.0.0.7",
                "port": "1"}, allow_redirects=False)
    sj = sample_session_json(links=(XSS, BREAKOUT))
    sj["session"].update(title=f"t {XSS} {BREAKOUT}", input_display_name=f"in {XSS}", input_identifier=BREAKOUT,
                         device_host="127.0.0.7")
    client.post(app.base + "/log_import", files={"file": ("s.json", json.dumps(sj).encode(), "application/json")},
                allow_redirects=False)
    sid = app.query("SELECT id FROM live_session WHERE title LIKE ?", ("t <img%",))[0][0]
    yield sid
    app.seed_sql([("DELETE FROM devices WHERE host=?", ("127.0.0.7",)),
                  ("DELETE FROM live_sample WHERE session_id=?", (sid,)),
                  ("DELETE FROM live_session WHERE id=?", (sid,))])


def _assert_clean(html, where):
    assert XSS not in html, f"unescaped markup on {where}"
    assert BREAKOUT not in html, f"script breakout on {where}"


@pytest.mark.parametrize("path", ["/devices", "/", "/settings", "/health", "/logs_ui"])
def test_device_and_session_names_are_escaped(app, client, hostile_data, path):
    _assert_clean(client.get(app.base + path).text, path)


def test_log_view_embeds_values_script_safely(app, client, hostile_data):
    html = client.get(app.base + "/log_view", params={"session_id": hostile_data}).text
    _assert_clean(html, "/log_view")


def test_log_view_script_context_values_cannot_break_out(app, client):
    """started_at / ended_at are written into an inline <script>; an imported file controls them."""
    sj = sample_session_json()
    sj["session"]["started_at"] = "2026-01-01T10:00:00" + BREAKOUT
    sj["session"]["ended_at"] = "2026-01-01T11:00:00" + BREAKOUT
    r = client.post(app.base + "/log_import", files={"file": ("s.json", json.dumps(sj).encode(), "application/json")},
                    allow_redirects=False)
    assert r.status_code in (302, 303)
    sid = app.query("SELECT id FROM live_session WHERE started_at LIKE ?", ("2026-01-01T10:00:00</script>%",))[0][0]
    try:
        for path, params in (("/log_view", {"session_id": sid}), ("/logs_ui", {})):
            r = client.get(app.base + path, params=params)
            assert r.status_code == 200, f"{path}: {r.status_code}"
            _assert_clean(r.text, path)
    finally:
        app.seed_sql([("DELETE FROM live_sample WHERE session_id=?", (sid,)),
                      ("DELETE FROM live_session WHERE id=?", (sid,))])


def test_gateway_pages_escape_names_and_events(app, client, srt_gateway):
    app.seed_sql([
        ("UPDATE srt_gateway_device SET name=? WHERE host=?", (f"gw {XSS}", srt_gateway)),
        ("UPDATE srt_route_sample SET route_name=? WHERE device_host=?", (f"route {XSS}", srt_gateway)),
        ("UPDATE srt_event SET route_name=?, detail=? WHERE device_host=?", (f"route {XSS}", f"{XSS}{BREAKOUT}", srt_gateway)),
    ])
    for path in ["/gateway_dashboard", "/gateway_logs", "/devices",
                 f"/gateway_logs_route?host={srt_gateway}&route_id=route-1",
                 f"/gateway_route?host={srt_gateway}&route_id=route-1"]:
        _assert_clean(client.get(app.base + path).text, path)


def test_login_page_has_no_reflected_injection(app):
    r = requests.get(app.base + "/login", params={"msg": XSS + BREAKOUT})
    _assert_clean(r.text, "/login")
