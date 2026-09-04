"""Pure unit tests for backend/protocol_status.py -- the Protocol-Library
status-board derivation. No FastAPI, no Mongo, no network: this imports the
pure module directly so the LIVE/READY/OFFLINE logic and the operational-vs-
forensic split are testable in-process.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

# protocol_status lives in backend/ (one level up from backend/tests/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import protocol_status as ps


NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_ago):
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


# --------------------------------------------------------------------------
# derive_operational_status
# --------------------------------------------------------------------------
def test_no_report_is_offline():
    assert ps.derive_operational_status(None, NOW) == "OFFLINE"
    assert ps.derive_operational_status({}, NOW) == "OFFLINE"


def test_fresh_heartbeat_no_decode_is_ready():
    rec = {"last_heartbeat_ts": _iso(5), "last_decode_ts": None}
    assert ps.derive_operational_status(rec, NOW) == "READY"


def test_fresh_heartbeat_and_fresh_decode_is_live():
    rec = {"last_heartbeat_ts": _iso(5), "last_decode_ts": _iso(10)}
    assert ps.derive_operational_status(rec, NOW) == "LIVE"


def test_stale_heartbeat_is_offline_even_with_recent_decode():
    rec = {"last_heartbeat_ts": _iso(300), "last_decode_ts": _iso(10)}
    assert ps.derive_operational_status(rec, NOW) == "OFFLINE"


def test_stale_decode_but_fresh_heartbeat_is_ready_not_live():
    rec = {"last_heartbeat_ts": _iso(5), "last_decode_ts": _iso(9999)}
    assert ps.derive_operational_status(rec, NOW) == "READY"


def test_unparseable_timestamps_are_offline_not_crash():
    assert ps.derive_operational_status({"last_heartbeat_ts": "garbage"}, NOW) == "OFFLINE"


# --------------------------------------------------------------------------
# build_board
# --------------------------------------------------------------------------
def test_board_has_four_operational_and_five_forensic():
    board = ps.build_board({}, NOW)
    assert len(board["operational"]) == 4
    assert len(board["forensic"]) == 5
    ids = {o["id"] for o in board["operational"]}
    assert ids == {"remoteid", "droneid", "control_link", "fpv_osd"}


def test_board_all_operational_offline_when_no_reports():
    board = ps.build_board({}, NOW)
    assert all(o["status"] == "OFFLINE" for o in board["operational"])


def test_board_reflects_live_and_ready_and_forensic():
    reports = {
        "remoteid": {"last_heartbeat_ts": _iso(3), "last_decode_ts": _iso(8),
                     "decode_count": 4, "last_decode_summary": "Remote ID ABC123"},
        "control_link": {"last_heartbeat_ts": _iso(3), "last_decode_ts": None,
                         "decode_count": 0},
    }
    board = ps.build_board(reports, NOW)
    by_id = {o["id"]: o for o in board["operational"]}
    assert by_id["remoteid"]["status"] == "LIVE"
    assert by_id["remoteid"]["decode_count"] == 4
    assert by_id["remoteid"]["last_decode_summary"] == "Remote ID ABC123"
    assert by_id["control_link"]["status"] == "READY"
    assert by_id["droneid"]["status"] == "OFFLINE"
    # Forensic entries are ALWAYS static FORENSIC -- never LIVE/READY -- and
    # always carry a physical-access requirement.
    assert all(f["status"] == "FORENSIC" for f in board["forensic"])
    assert all(f.get("requires") for f in board["forensic"])


def test_forensic_never_contains_operational_ids():
    board = ps.build_board({}, NOW)
    op_ids = {o["id"] for o in board["operational"]}
    forensic_ids = {f["id"] for f in board["forensic"]}
    assert op_ids.isdisjoint(forensic_ids)
    # The wire decoders must be in FORENSIC, not operational.
    assert {"crsf", "msp", "canopen", "dronecan"}.issubset(forensic_ids)


def test_board_carries_doctrine_and_windows():
    board = ps.build_board({}, NOW)
    assert "identifies" in board["doctrine"].lower() or "identif" in board["doctrine"].lower()
    assert board["live_window_s"] == ps.DEFAULT_LIVE_WINDOW_S
    assert board["decode_window_s"] == ps.DEFAULT_DECODE_WINDOW_S
