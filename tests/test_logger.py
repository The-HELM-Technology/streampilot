"""LiveLogger exercised in-process against a throwaway SQLite file (no server needed)."""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streampilot.logger import LiveLogger  # noqa: E402


def _payload(status="on", proto="sst"):
    return {"inputs": {"0": {"protocol": proto, "status": status, "identifier": "in1",
                             "familyName": "Cam"}}}


@pytest.fixture
def lg(tmp_path):
    l = LiveLogger(tmp_path / "t.db")
    yield l
    l._stop = True


def _count(l, table, where="1=1"):
    import sqlite3
    con = sqlite3.connect(l.db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    finally:
        con.close()


def test_session_opens_and_closes(lg):
    lg.observe_payload("1", "127.0.0.1", _payload("on"))
    assert _count(lg, "live_session", "ended_at IS NULL") == 1
    lg.observe_payload("1", "127.0.0.1", _payload("off"))
    assert _count(lg, "live_session", "ended_at IS NULL") == 0
    assert _count(lg, "live_session") == 1


def test_non_sst_input_ignored(lg):
    lg.observe_payload("1", "127.0.0.1", _payload("on", proto="srt"))
    assert _count(lg, "live_session") == 0
