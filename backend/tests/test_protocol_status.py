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
def test_board_has_six_operational_and_twelve_forensic():
    board = ps.build_board({}, NOW)
    assert len(board["operational"]) == 6
    assert len(board["forensic"]) == 12
    ids = {o["id"] for o in board["operational"]}
    assert ids == {"remoteid", "droneid", "control_link", "fpv_osd", "adsb", "parrot"}


def test_all_ids_unique_across_the_18():
    board = ps.build_board({}, NOW)
    all_ids = [p["id"] for p in board["operational"]] + [p["id"] for p in board["forensic"]]
    assert len(all_ids) == 18
    assert len(set(all_ids)) == 18  # no duplicate ids anywhere on the board


def test_forensic_ids_are_the_expected_twelve():
    board = ps.build_board({}, NOW)
    forensic_ids = {f["id"] for f in board["forensic"]}
    assert forensic_ids == {
        "crsf", "msp", "canopen", "dronecan", "sik_mavlink_wire",
        "ltm", "dshot", "frsky_smartport", "graupner_hott",
        "flysky_afhds", "frsky_accst", "spektrum_dsm",
    }


def test_every_forensic_entry_is_static_forensic():
    board = ps.build_board({}, NOW)
    assert all(f["status"] == "FORENSIC" for f in board["forensic"])


def test_rc_parsers_carry_ota_family_and_requires():
    # The 3 chip-level RC control-link parsers must HONESTLY state that their
    # OTA presence is only surfaced at family level (hobby_rc_2g4) and name the
    # dedicated receiver chip they'd need -- never a phantom airborne radio.
    board = ps.build_board({}, NOW)
    by_id = {f["id"]: f for f in board["forensic"]}
    expected_chip = {
        "flysky_afhds": "A7105",
        "frsky_accst": "CC2500",
        "spektrum_dsm": "CYRF6936",
    }
    for pid, chip in expected_chip.items():
        entry = by_id[pid]
        assert entry["status"] == "FORENSIC"
        assert entry.get("ota_family"), f"{pid} missing ota_family"
        assert "hobby_rc_2g4" in entry["ota_family"]
        assert chip in entry["requires"]
        assert "HackRF" in entry["requires"]


def test_wire_tap_forensic_entries_have_no_ota_family():
    # Non-RC-parser forensic entries are pure wire taps -- no OTA-family claim.
    board = ps.build_board({}, NOW)
    by_id = {f["id"]: f for f in board["forensic"]}
    for pid in ("crsf", "msp", "canopen", "dronecan", "sik_mavlink_wire",
                "ltm", "dshot", "frsky_smartport", "graupner_hott"):
        assert by_id[pid].get("ota_family") is None


def test_adsb_and_parrot_derive_offline_ready_live():
    # New operational protocols derive status the SAME honest way as the others:
    # no report -> OFFLINE, fresh heartbeat only -> READY, fresh decode -> LIVE.
    board_offline = ps.build_board({}, NOW)
    by_id = {o["id"]: o for o in board_offline["operational"]}
    assert by_id["adsb"]["status"] == "OFFLINE"
    assert by_id["parrot"]["status"] == "OFFLINE"

    reports = {
        "adsb": {"last_heartbeat_ts": _iso(3), "last_decode_ts": None},
        "parrot": {"last_heartbeat_ts": _iso(3), "last_decode_ts": _iso(8),
                   "decode_count": 2, "last_decode_summary": "Parrot ARSDK Piloting"},
    }
    board = ps.build_board(reports, NOW)
    by_id = {o["id"]: o for o in board["operational"]}
    assert by_id["adsb"]["status"] == "READY"
    assert by_id["parrot"]["status"] == "LIVE"
    assert by_id["parrot"]["decode_count"] == 2


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
