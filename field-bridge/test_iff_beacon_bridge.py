#!/usr/bin/env python3
"""pytest coverage for iff_beacon_bridge.py's verify/replay/registry pipeline.

Same checks as iff_beacon_bridge.py's --self-test (which is aimed at a human
running it standalone), wired into this project's normal pytest suite so it
runs under CI/preflight alongside every other field-bridge test. No LoRa
hardware, no network -- pure synthetic-but-cryptographically-real frames via
iff_crypto.build_frame().
"""
from __future__ import annotations

import secrets
import time

import pytest

import iff_crypto as iff
from iff_beacon_bridge import AssetRegistry, ReplayCache, handle_frame, post_verified_beacon


@pytest.fixture
def mission():
    mission_id = 0xBEEF
    master_secret = secrets.token_bytes(32)
    asset_id = 501
    registry = AssetRegistry(mission_id=mission_id, mission_master_secret=master_secret,
                              callsigns={asset_id: "Falcon-1"})
    asset_secret = iff.derive_asset_secret(master_secret, mission_id, asset_id)
    return {
        "mission_id": mission_id,
        "master_secret": master_secret,
        "asset_id": asset_id,
        "registry": registry,
        "asset_secret": asset_secret,
    }


def test_fresh_valid_beacon_accepted(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    geocell = iff.quantize_geocell(28.6139, 77.2090)
    frame = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"],
                             now_slot, geocell, counter=0)
    result = handle_frame(frame, mission["registry"], ReplayCache(), now=now)
    assert result["ok"] is True
    assert result["callsign"] == "Falcon-1"
    assert result["geocell_known"] is True


def test_exact_replay_rejected(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    frame = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"],
                             now_slot, iff.GEOCELL_UNKNOWN, counter=0)
    cache = ReplayCache()
    r1 = handle_frame(frame, mission["registry"], cache, now=now)
    r2 = handle_frame(frame, mission["registry"], cache, now=now + 1)
    assert r1["ok"] is True
    assert r2["ok"] is False
    assert "replay" in r2["reason"]


def test_new_counter_same_slot_accepted_after_first(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    cache = ReplayCache()
    f1 = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"],
                          now_slot, iff.GEOCELL_UNKNOWN, counter=0)
    f2 = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"],
                          now_slot, iff.GEOCELL_UNKNOWN, counter=1)
    assert handle_frame(f1, mission["registry"], cache, now=now)["ok"] is True
    assert handle_frame(f2, mission["registry"], cache, now=now)["ok"] is True


def test_forged_frame_rejected(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    forged_secret = iff.derive_asset_secret(secrets.token_bytes(32), mission["mission_id"],
                                             mission["asset_id"])
    forged = iff.build_frame(forged_secret, mission["asset_id"], mission["mission_id"],
                              now_slot, iff.GEOCELL_UNKNOWN, counter=0)
    result = handle_frame(forged, mission["registry"], ReplayCache(), now=now)
    assert result["ok"] is False
    assert "HMAC" in result["reason"]


def test_stale_frame_outside_skew_rejected(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    stale = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"],
                             now_slot - 100, iff.GEOCELL_UNKNOWN, counter=0)
    result = handle_frame(stale, mission["registry"], ReplayCache(), now=now)
    assert result["ok"] is False
    assert "tolerance" in result["reason"]


def test_unrostered_but_validly_signed_asset_flagged_not_rejected(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    other_id = 999
    other_secret = iff.derive_asset_secret(mission["master_secret"], mission["mission_id"], other_id)
    frame = iff.build_frame(other_secret, other_id, mission["mission_id"], now_slot,
                             iff.GEOCELL_UNKNOWN, counter=0)
    result = handle_frame(frame, mission["registry"], ReplayCache(), now=now)
    assert result["ok"] is True
    assert result["callsign"] == "unknown-asset-999"


def test_wrong_mission_id_rejected(mission):
    now = time.time()
    now_slot = int(now // iff.INTERVAL_S)
    frame = iff.build_frame(mission["asset_secret"], mission["asset_id"], mission["mission_id"] + 1,
                             now_slot, iff.GEOCELL_UNKNOWN, counter=0)
    result = handle_frame(frame, mission["registry"], ReplayCache(), now=now)
    assert result["ok"] is False
    assert "mission_id" in result["reason"]


def test_post_verified_beacon_sends_expected_body(monkeypatch, mission):
    """Confirms the outbound POST body shape without hitting a real backend --
    mocks requests.post at the iff_beacon_bridge module level, same pattern as
    test_reauth_on_401.py uses for the other bridges."""
    import iff_beacon_bridge as bridge
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        r = type("R", (), {"status_code": 200, "text": "ok"})()
        return r

    monkeypatch.setattr(bridge.requests, "post", fake_post)
    result = {"asset_id": 501, "callsign": "Falcon-1", "mission_id": 0xBEEF,
              "geocell": 123, "geocell_known": True}
    post_verified_beacon("http://console", {"Authorization": "Bearer t"}, "op@example.com",
                          "pw", result, bearing_deg=45.0, distance_m=120.0)
    assert captured["url"] == "http://console/api/iff/beacons/ingest"
    assert captured["json"]["asset_id"] == 501
    assert captured["json"]["callsign"] == "Falcon-1"
    assert captured["json"]["bearing_deg"] == 45.0


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
