"""LiveLogger exercised in-process on every configured backend (no server needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streampilot import db  # noqa: E402
from streampilot.logger import LiveLogger  # noqa: E402


def _payload(status="on", proto="sst"):
    return {"inputs": {"0": {"protocol": proto, "status": status, "identifier": "in1",
                             "familyName": "Cam"}}}


@pytest.fixture
def lg(db_url, tmp_path):
    l = LiveLogger(tmp_path / "ignored-when-url-set.db")
    yield l
    l._stop = True


def _count(table, where="1=1"):
    with db.connect() as c:
        return c.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def test_session_opens_and_closes(lg):
    lg.observe_payload("1", "127.0.0.1", _payload("on"))
    assert _count("live_session", "ended_at IS NULL") == 1
    lg.observe_payload("1", "127.0.0.1", _payload("off"))
    assert _count("live_session", "ended_at IS NULL") == 0
    assert _count("live_session") == 1


def test_non_sst_input_ignored(lg):
    lg.observe_payload("1", "127.0.0.1", _payload("on", proto="srt"))
    assert _count("live_session") == 0


def test_start_session_returns_distinct_ids(lg):
    a = lg._start_session("1", "h", "0", 1, "i1", "A")
    b = lg._start_session("1", "h", "1", 2, "i2", "B")
    assert isinstance(a, int) and isinstance(b, int) and a != b


def test_samples_batch_roundtrip_with_floats(lg):
    sid = lg._start_session("1", "h", "0", 1, "i1", "A")
    rows = lg._build_rows(sid, {"lat": 48.856614123456, "lng": 2.352222123456}, {"video": 3, "ts": 1},
                          [{"name": "m1", "owdR": 25, "rx_bitrate": 4_000_000,
                            "rx_percent_lost": 0, "rx_lost_nb_packets": 0}])
    lg._insert_samples_batch(rows)
    with db.connect() as c:
        lat, lng, dv, name, rb = c.execute(
            "SELECT latitude, longitude, drops_video, link_name, rx_bitrate FROM live_sample WHERE session_id=?",
            (sid,)).fetchone()
    assert (lat, lng) == (48.856614123456, 2.352222123456)  # full double precision kept
    assert (dv, name, rb) == (3, "m1", 4_000_000)
