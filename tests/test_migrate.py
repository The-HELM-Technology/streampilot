"""SQLite -> Postgres migration tool (needs STREAMPILOT_TEST_PG_URL)."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from conftest import PG_URL, run_py  # noqa: E402

pytestmark = pytest.mark.skipif(not PG_URL, reason="STREAMPILOT_TEST_PG_URL not set")

LAT, LNG = 48.856614123456789, 2.352222123456789

SEED = f"""
import streampilot.server as s
s._init_db()
with s.connect_db() as c:
    c.execute("INSERT INTO devices(name,protocol,host,port,api_path,token) VALUES('SH','https','10.0.0.1',443,'/rest-api/','tok%')")
    c.execute("INSERT INTO devices(name,protocol,host,port,api_path,token) VALUES('SH2','https','10.0.0.2',443,'/rest-api/','t')")
    c.execute("DELETE FROM devices WHERE name='SH'")          # leaves a gap: ids must be preserved, not renumbered
    c.execute("INSERT INTO device_slack(device_id, notify_drops) VALUES(2, 0)")
    c.execute("UPDATE slack_config SET webhook_url='https://hooks/x', channel='#ops' WHERE id=1")
    for i in range(1, 4):
        c.execute("INSERT INTO live_session(device_id,device_host,input_key,input_index,input_identifier,"
                  "input_display_name,started_at,ended_at,title) VALUES(?,?,?,?,?,?,?,?,?)",
                  ('2','10.0.0.2',str(i),i,'in','Caméra é中',f'2026-01-0{{i}}T00:00:00',None,'t%s'))
    c.execute("DELETE FROM live_session WHERE id=2")
    for sid in (1, 3):
        for k in range(50):
            c.execute("INSERT INTO live_sample(session_id,ts,year,month,day,hour,minute,second,latitude,longitude,"
                      "drops_video,drops_ts,link_name,owdR,rx_bitrate,rx_percent_lost,rx_lost_nb_packets) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (sid,f'2026-01-01T00:00:{{k:02d}}',2026,1,1,0,0,k,{LAT},{LNG},k,0,'m1',20+k,4000000,0,None))
    c.execute("INSERT INTO streamhub_log(device_host,ts,node,level,message,raw,fp) VALUES(?,?,?,?,?,?,?)",
              ('h','t','n','info','a'+chr(0)+'b %s ?','r','f1'))
    c.execute("INSERT INTO srt_route_sample(device_host,route_id,route_name,ts,state,total_bitrate_mbps) VALUES('g','r','R','2026-01-01 00:00:00','running',5.5)")
    c.execute("INSERT INTO srt_event(device_host,route_id,ts,event_type,detail,ip_address) VALUES('g','r','2026-01-01 00:00:00','connect','d','1.2.3.4')")
    c.execute("INSERT INTO srt_endpoint_location(address,label,lat,lng) VALUES('1.2.3.4','lab',{LAT},{LNG})")
"""


@pytest.fixture
def source(tmp_path):
    p = tmp_path / "src.db"
    run_py(SEED, f"sqlite:///{p}")
    return p


def _migrate(src, url, *extra):
    return subprocess.run([sys.executable, "-m", "streampilot.migrate", "--from", str(src), "--to", url, *extra],
                          cwd=REPO, capture_output=True, text=True, timeout=120,
                          env={k: v for k, v in os.environ.items() if k != "STREAMPILOT_DB_URL"})


def _pg(url, sql, params=None):
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        return c.execute(sql, params).fetchall()


def test_migrates_data_ids_and_values(source, pg_empty_url):
    r = _migrate(source, pg_empty_url)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "copied and verified" in r.stdout
    # ids preserved (gaps kept)
    assert _pg(pg_empty_url, "SELECT id, name FROM devices") == [(2, "SH2")]
    assert [x[0] for x in _pg(pg_empty_url, "SELECT id FROM live_session ORDER BY id")] == [1, 3]
    assert _pg(pg_empty_url, "SELECT COUNT(*) FROM live_sample")[0][0] == 100
    # full precision + unicode + literal % and ? survive
    assert _pg(pg_empty_url, "SELECT latitude, longitude FROM live_sample LIMIT 1")[0] == (LAT, LNG)
    assert _pg(pg_empty_url, "SELECT input_display_name, title FROM live_session WHERE id=1")[0] == ("Caméra é中", "t%s")
    assert _pg(pg_empty_url, "SELECT owdr, rx_lost_nb_packets FROM live_sample WHERE session_id=1 AND second=7")[0] == (27, None)
    # NUL stripped, rest intact
    assert _pg(pg_empty_url, "SELECT message FROM streamhub_log")[0][0] == "ab %s ?"
    assert "NUL" in r.stdout
    # seeded default row replaced by the real config
    assert _pg(pg_empty_url, "SELECT webhook_url, channel FROM slack_config") == [("https://hooks/x", "#ops")]
    assert _pg(pg_empty_url, "SELECT lat, label FROM srt_endpoint_location")[0] == (LAT, "lab")


def test_sequences_continue_after_copied_ids(source, pg_empty_url):
    assert _migrate(source, pg_empty_url).returncode == 0
    new_dev = _pg(pg_empty_url, "INSERT INTO devices(name,host) VALUES('new','h') RETURNING id")[0][0]
    new_sess = _pg(pg_empty_url, "INSERT INTO live_session(device_id,input_key,input_index,started_at) "
                                 "VALUES('x','k',1,'2030') RETURNING id")[0][0]
    new_sample = _pg(pg_empty_url, "INSERT INTO live_sample(session_id,ts) VALUES(1,'t') RETURNING id")[0][0]
    assert new_dev == 3 and new_sess == 4 and new_sample == 101


def test_refuses_non_empty_target_then_force_replaces(source, pg_empty_url):
    assert _migrate(source, pg_empty_url).returncode == 0
    r = _migrate(source, pg_empty_url)
    assert r.returncode != 0 and "already has" in (r.stdout + r.stderr) and "--force" in (r.stdout + r.stderr)
    assert _pg(pg_empty_url, "SELECT COUNT(*) FROM live_sample")[0][0] == 100  # untouched by the refusal
    r = _migrate(source, pg_empty_url, "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert _pg(pg_empty_url, "SELECT COUNT(*) FROM live_sample")[0][0] == 100  # replaced, not doubled


def test_failure_rolls_back_everything(source, pg_empty_url, tmp_path):
    # a value Postgres rejects in a BIGINT column (SQLite stores anything anywhere)
    con = sqlite3.connect(source)
    con.execute("INSERT INTO srt_event(device_host,route_id,ts,event_type) VALUES('g','r','t','e')")
    con.execute("INSERT INTO srt_gw_slack(gw_id, thr_lost_max) VALUES(1, 'not-a-number')")
    con.commit(); con.close()
    r = _migrate(source, pg_empty_url)
    assert r.returncode != 0 and "srt_gw_slack" in (r.stdout + r.stderr)
    assert "nothing was committed" in (r.stdout + r.stderr)
    for t in ("devices", "live_session", "live_sample", "srt_event"):  # earlier tables rolled back too
        assert _pg(pg_empty_url, f"SELECT COUNT(*) FROM {t}")[0][0] == 0, t


def test_missing_source_and_bad_target_url(tmp_path, pg_empty_url):
    r = _migrate(tmp_path / "nope.db", pg_empty_url)
    assert r.returncode != 0 and "not found" in (r.stdout + r.stderr)
    assert not (tmp_path / "nope.db").exists()  # must not create an empty DB by accident
    src = tmp_path / "s.db"
    sqlite3.connect(src).close()
    r = _migrate(src, "sqlite:///x.db")
    assert r.returncode != 0 and "postgresql://" in (r.stdout + r.stderr)


def test_source_is_not_modified(source, pg_empty_url):
    before = sqlite3.connect(source).execute("SELECT COUNT(*) FROM live_sample").fetchone()[0]
    mtime = source.stat().st_mtime_ns
    assert _migrate(source, pg_empty_url).returncode == 0
    assert sqlite3.connect(source).execute("SELECT COUNT(*) FROM live_sample").fetchone()[0] == before
    assert source.stat().st_mtime_ns == mtime
