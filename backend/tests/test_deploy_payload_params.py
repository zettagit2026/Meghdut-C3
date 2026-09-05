"""Unit tests for the completed per-payload operator parameters on
POST /api/payloads/deploy (deploy_payload):

  * PL-008 RTH HOME-SPOOF: spoof_lat/spoof_lon/spoof_alt are wired into the built
    frame (DO_SET_HOME with the operator coords) and the RTH trigger follows.
  * PL-005 PROPELLER STOP: motor_count iterates DO_MOTOR_TEST across all motors.
  * PL-011 MANEUVER TAKEOVER: duration_s is operator-controlled with NO artificial
    cap, and continuous=True is carried through to the sustain plan. The full
    governed spine (arm-token for CRITICAL, tx_halt, range-auth, IFF) still runs.

Same unit-test convention as test_mavlink_sdr_inject.py: importing server only
needs the env vars set; the spine's real side effects (Mongo/range-auth/WS/audit)
are monkeypatched, so nothing transmits.

Run: pytest backend/tests/test_deploy_payload_params.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import server as srv  # noqa: E402
import mavlink_codec as mc  # noqa: E402

USER = {"email": "cmdr@unused.local", "role": "commander"}

_TARGET = {
    "id": "det-1", "callsign": "HOSTILE-1", "protocol": "MAVLink-SiK-Legacy",
    "system_id": 7, "component_id": 1, "authorized_target": True,
    "iff_verified": False, "threat_level": "MEDIUM",
}


class _FakeDetections:
    def __init__(self, doc):
        self._doc = doc

    async def find_one(self, query):
        return dict(self._doc) if self._doc.get("id") == query.get("id") else None

    async def update_one(self, *a, **k):
        return None

    def find(self, *a, **k):
        class _C:
            async def to_list(self, n):
                return []
        return _C()


class _FakeMavPackets:
    async def insert_one(self, doc):
        return None


class _FakeDB:
    def __init__(self, doc):
        self.detections = _FakeDetections(doc)
        self.mav_packets = _FakeMavPackets()


def _stub_spine(monkeypatch, detection=None):
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection if detection is not None else _TARGET))

    async def _range(effect, actor):
        return None
    monkeypatch.setattr(srv, "_require_range_authorized", _range)

    async def _iff(detection, user, context=None, friendly_fire_ack=None):
        return None
    monkeypatch.setattr(srv, "_enforce_fire_time_iff", _iff)

    async def _log(kind, message, meta=None, actor=None):
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        return None
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)


def _arm(effect="deploy", target="det-1"):
    return srv._issue_arm_token(effect, target)["arm_token"]


# ---- PL-008 spoof coords wired into the frame ----------------------------
def test_pl008_spoof_coords_build_do_set_home_plus_rth(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.DeployPayloadBody(payload_id="PL-008", target_detection_id="det-1",
                                 spoof_lat=12.3456, spoof_lon=-78.9, spoof_alt=55.0)
    pkt = asyncio.run(srv.deploy_payload(body, user=USER))
    frames = mc.iter_frames(bytes.fromhex(pkt["hex"]))
    assert len(frames) == 2, "DO_SET_HOME + RTH trigger"
    sh = mc.decode_command_long(frames[0])
    assert sh["command"] == mc.MAV_CMD["DO_SET_HOME"]
    assert abs(sh["param5"] - 12.3456) < 1e-3 and abs(sh["param6"] - (-78.9)) < 1e-3
    assert abs(sh["param7"] - 55.0) < 1e-3
    assert mc.decode_command_long(frames[1])["command"] == mc.MAV_CMD["NAV_RETURN_TO_LAUNCH"]


def test_pl008_out_of_range_coords_rejected():
    for bad in (dict(spoof_lat=91.0), dict(spoof_lat=-90.1), dict(spoof_lon=181.0),
                dict(spoof_lon=-200.0)):
        with pytest.raises(Exception):
            srv.DeployPayloadBody(payload_id="PL-008", target_detection_id="det-1", **bad)


# ---- PL-005 motor_count iterates all motors ------------------------------
def test_pl005_motor_count_iterates_all_motors(monkeypatch):
    _stub_spine(monkeypatch)
    # PL-005 is CRITICAL -> needs an arm token bound to effect=deploy + target.
    body = srv.DeployPayloadBody(payload_id="PL-005", target_detection_id="det-1",
                                 arm_token=_arm(), motor_count=6)
    pkt = asyncio.run(srv.deploy_payload(body, user=USER))
    frames = mc.iter_frames(bytes.fromhex(pkt["hex"]))
    assert len(frames) == 6
    motors = [int(mc.decode_command_long(f)["param1"]) for f in frames]
    assert motors == [1, 2, 3, 4, 5, 6]
    assert all(mc.decode_command_long(f)["command"] == mc.MAV_CMD["DO_MOTOR_TEST"] for f in frames)


def test_pl005_motor_count_out_of_range_rejected():
    with pytest.raises(Exception):
        srv.DeployPayloadBody(payload_id="PL-005", target_detection_id="det-1",
                              arm_token="x", motor_count=9)
    with pytest.raises(Exception):
        srv.DeployPayloadBody(payload_id="PL-005", target_detection_id="det-1",
                              arm_token="x", motor_count=0)


# ---- PL-011 duration operator-controlled, no cap; continuous carried -----
def test_pl011_duration_not_capped_and_continuous_carried(monkeypatch):
    _stub_spine(monkeypatch)
    # A duration far beyond the old 30 s cap must be carried VERBATIM.
    body = srv.DeployPayloadBody(payload_id="PL-011", target_detection_id="det-1",
                                 arm_token=_arm(), target_link_legacy_mavlink=True,
                                 duration_s=250.0)
    pkt = asyncio.run(srv.deploy_payload(body, user=USER))
    assert pkt["sustained"] is True
    assert pkt["duration_s"] == 250.0, "operator duration must NOT be clamped to the old cap"
    assert pkt["continuous"] is False

    body2 = srv.DeployPayloadBody(payload_id="PL-011", target_detection_id="det-1",
                                  arm_token=_arm(), target_link_legacy_mavlink=True,
                                  continuous=True)
    pkt2 = asyncio.run(srv.deploy_payload(body2, user=USER))
    assert pkt2["continuous"] is True


def test_pl011_still_requires_arm_token(monkeypatch):
    # Governance unchanged: PL-011 is CRITICAL -> deploy with no arm token is refused.
    _stub_spine(monkeypatch)
    body = srv.DeployPayloadBody(payload_id="PL-011", target_detection_id="det-1",
                                 target_link_legacy_mavlink=True)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_payload(body, user=USER))
    assert ei.value.status_code == 403


def test_pl011_encrypted_link_not_applicable(monkeypatch):
    # Honesty gate intact: an encrypted target link is refused (never transmitted).
    det = {**_TARGET, "protocol": "DJI OcuSync"}
    _stub_spine(monkeypatch, detection=det)
    body = srv.DeployPayloadBody(payload_id="PL-011", target_detection_id="det-1",
                                 arm_token=_arm())
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_payload(body, user=USER))
    assert ei.value.status_code == 422


def test_deploy_refused_when_tx_halted(monkeypatch):
    # Kill-switch intact: tx_halt blocks the deploy (409) before any frame.
    _stub_spine(monkeypatch)
    monkeypatch.setattr(srv, "_tx_halted", True)
    body = srv.DeployPayloadBody(payload_id="PL-008", target_detection_id="det-1")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_payload(body, user=USER))
    assert ei.value.status_code == 409


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
