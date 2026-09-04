"""Unit tests for the governed SDR MAVLink inject endpoint
(deploy_mavlink_sdr_inject) — the no-pairing, adversary-grade MAVLink takeover
path radiated over the air by the pinned TX HackRF.

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_operator_jam_mode.py: importing backend/server.py only needs the
env vars SET (motor is lazy). The endpoint is driven directly with its async
dependencies (token consumption, range-auth, Mongo, WS broadcast) monkeypatched,
so nothing transmits and no Mongo is touched.

Run: pytest backend/tests/test_mavlink_sdr_inject.py -v
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

import pytest

import server as srv

USER = {"email": "cmdr@unused.local", "role": "commander"}


def _confirm() -> str:
    return srv._issue_mavlink_sdr_inject_confirm_token()["mavlink_sdr_inject_confirm_token"]


def _arm(target="det-1") -> str:
    return srv._issue_arm_token("mavlink_sdr_inject", target)["arm_token"]


def _body(**overrides):
    base = dict(
        target_detection_id="det-1",
        command="force_land",
        center_freq_mhz=915.0,
        air_rate_bps=250000.0,
        deviation_hz=62500.0,
        bt=0.5,
        bit_order="msb",
        tx_gain=20,
        repeat=3,
        target_link_legacy_mavlink=True,
        arm_token=overrides.pop("arm_token", None) or _arm(overrides.get("target_detection_id", "det-1")),
        mavlink_sdr_inject_confirm_token=overrides.pop("mavlink_sdr_inject_confirm_token", None) or _confirm(),
    )
    base.update(overrides)
    return srv.MavlinkSdrInjectBody(**base)


class _FakeDetections:
    def __init__(self, doc):
        self._doc = doc

    async def find_one(self, query):
        if self._doc and self._doc.get("id") == query.get("id"):
            return dict(self._doc)
        return None


class _FakeDB:
    def __init__(self, doc):
        self.detections = _FakeDetections(doc)


# A routine (non-friendly), authorized, legacy-MAVLink target that passes every
# gate except whatever a given test deliberately breaks.
_LEGACY_TARGET = {
    "id": "det-1", "callsign": "HOSTILE-1", "protocol": "MAVLink-SiK-Legacy",
    "system_id": 7, "component_id": 1, "authorized_target": True,
    "iff_verified": False, "threat_level": "MEDIUM",
}


def _stub_spine(monkeypatch, *, detection=None, range_ok=True):
    """Neutralize the spine's real side effects (Mongo, range-auth log/broadcast,
    WS, audit) while capturing what the endpoint emits. Token consumption + the
    IFF interlock stay REAL (they are what these tests exercise)."""
    events = []
    broadcasts = []

    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection if detection is not None else _LEGACY_TARGET))

    async def _range(effect, actor):
        if not range_ok:
            raise srv.HTTPException(409, f"Range authorization for effect='{effect}' is OFF")
        return None
    monkeypatch.setattr(srv, "_require_range_authorized", _range)

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message, "meta": meta or {}, "actor": actor})
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)

    return events, broadcasts


# ---------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------
def test_command_pattern_rejects_bogus():
    with pytest.raises(Exception):
        srv.MavlinkSdrInjectBody(target_detection_id="det-1", command="rm_rf",
                                 arm_token="a" * 36, mavlink_sdr_inject_confirm_token="b" * 36)


def test_target_detection_id_is_required():
    with pytest.raises(Exception):
        srv.MavlinkSdrInjectBody(command="force_land", arm_token="a" * 36,
                                 mavlink_sdr_inject_confirm_token="b" * 36)


def test_repeat_bounded_by_model():
    with pytest.raises(Exception):
        srv.MavlinkSdrInjectBody(target_detection_id="det-1", repeat=9999,
                                 arm_token="a" * 36, mavlink_sdr_inject_confirm_token="b" * 36)


# ---------------------------------------------------------------------
# Distinct effect wiring
# ---------------------------------------------------------------------
def test_effect_registered_in_arm_and_range_tuples():
    assert "mavlink_sdr_inject" in srv.ARM_TOKEN_EFFECTS
    assert "mavlink_sdr_inject" in srv.RANGE_AUTH_EFFECTS
    assert "mavlink_sdr_inject" in srv._range_authorization  # lease dict initialized


def test_confirm_token_type_is_separate_from_jam_and_gnss():
    tok = srv._issue_mavlink_sdr_inject_confirm_token()["mavlink_sdr_inject_confirm_token"]
    # It lives ONLY in its own dict — not in the jam / gnss confirm dicts.
    assert tok in srv._mavlink_sdr_inject_confirm_tokens
    assert tok not in srv._jam_confirm_tokens
    assert tok not in srv._gnss_spoof_confirm_tokens


# ---------------------------------------------------------------------
# Spine: tx_halt -> 409
# ---------------------------------------------------------------------
def test_tx_halt_refused_409(monkeypatch):
    _stub_spine(monkeypatch)
    monkeypatch.setattr(srv, "_tx_halted", True)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------
# Spine: arm token — missing / cross-effect rejected
# ---------------------------------------------------------------------
def test_missing_arm_token_refused(monkeypatch):
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(arm_token="   ", mavlink_sdr_inject_confirm_token=_confirm()), user=USER))
    # blank arm token is falsy at consume time
    assert ei.value.status_code == 403


def test_cross_effect_arm_token_refused(monkeypatch):
    _stub_spine(monkeypatch)
    jam_tok = srv._issue_arm_token("jam")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(arm_token=jam_tok, mavlink_sdr_inject_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403
    assert "effect" in ei.value.detail.lower()


def test_wrong_target_arm_token_refused(monkeypatch):
    _stub_spine(monkeypatch)
    tok = srv._issue_arm_token("mavlink_sdr_inject", "other-target")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(arm_token=tok, mavlink_sdr_inject_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403
    assert "target" in ei.value.detail.lower()


# ---------------------------------------------------------------------
# Spine: confirm token — missing rejected; single-use
# ---------------------------------------------------------------------
def test_missing_confirm_token_refused(monkeypatch):
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(mavlink_sdr_inject_confirm_token="   "), user=USER))
    assert ei.value.status_code == 403
    assert "confirmation token" in ei.value.detail.lower()


def test_confirm_token_is_single_use(monkeypatch):
    _stub_spine(monkeypatch)
    tok = _confirm()
    # First use succeeds.
    asyncio.run(srv.deploy_mavlink_sdr_inject(
        _body(arm_token=_arm(), mavlink_sdr_inject_confirm_token=tok), user=USER))
    # Re-presenting the same (now-burned) confirm token with a fresh arm token -> 403.
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(arm_token=_arm(), mavlink_sdr_inject_confirm_token=tok), user=USER))
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------
# Spine: range-authorization off -> 409
# ---------------------------------------------------------------------
def test_range_auth_off_refused_409(monkeypatch):
    _stub_spine(monkeypatch, range_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------
# Honesty gate: encrypted/FHSS target refused as NOT APPLICABLE (never TX)
# ---------------------------------------------------------------------
@pytest.mark.parametrize("proto", ["ELRS 2.4", "DJI OcuSync", "Spektrum DSMX", "CRSF"])
def test_encrypted_target_not_applicable_422(monkeypatch, proto):
    det = {**_LEGACY_TARGET, "protocol": proto}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(target_link_legacy_mavlink=False), user=USER))
    assert ei.value.status_code == 422
    assert "not applicable" in ei.value.detail.lower() or "encrypted" in ei.value.detail.lower()


def test_unknown_protocol_without_attestation_refused_422(monkeypatch):
    det = {**_LEGACY_TARGET, "protocol": "SomeMysteryLink-XYZ"}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(target_link_legacy_mavlink=False), user=USER))
    assert ei.value.status_code == 422


# ---------------------------------------------------------------------
# F-4: target_system 0 refused (broadcast defeats target-bound gates)
# ---------------------------------------------------------------------
def test_target_system_zero_refused_422(monkeypatch):
    det = {**_LEGACY_TARGET, "system_id": 0}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 422
    assert "system_id" in ei.value.detail.lower() or "broadcast" in ei.value.detail.lower()


# ---------------------------------------------------------------------
# IFF fratricide interlock
# ---------------------------------------------------------------------
def test_confirmed_friendly_hard_blocked_without_ack(monkeypatch):
    friendly = {**_LEGACY_TARGET, "callsign": "FRND-1", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    _stub_spine(monkeypatch, detection=friendly)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()


def test_confirmed_friendly_allowed_with_single_use_ack_and_loud_audit(monkeypatch):
    friendly = {**_LEGACY_TARGET, "callsign": "FRND-1", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    events, broadcasts = _stub_spine(monkeypatch, detection=friendly)
    ack = srv._issue_iff_ff_ack("det-1", "cmdr@unused.local")["iff_friendly_fire_ack"]
    resp = asyncio.run(srv.deploy_mavlink_sdr_inject(
        _body(iff_friendly_fire_ack=ack), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    # The loud, un-missable override audit event must be present.
    assert any(e["kind"] == "IFF_FRIENDLY_FIRE_OVERRIDE" for e in events)


def test_friendly_fire_ack_bound_to_target(monkeypatch):
    """An ack minted for a different detection cannot license this target."""
    friendly = {**_LEGACY_TARGET, "callsign": "FRND-1", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    _stub_spine(monkeypatch, detection=friendly)
    ack_other = srv._issue_iff_ff_ack("some-other-det", "cmdr@unused.local")["iff_friendly_fire_ack"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _body(iff_friendly_fire_ack=ack_other), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()


# ---------------------------------------------------------------------
# Happy path: distinct audit + WS request carries the command + params
# ---------------------------------------------------------------------
def test_happy_path_audits_distinctly_and_broadcasts_command(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch)
    resp = asyncio.run(srv.deploy_mavlink_sdr_inject(_body(command="disarm"), user=USER))

    assert resp["status"] == "AWAITING_ACK"
    assert resp["command"] == "disarm"
    assert resp["target_system"] == 7

    # Audited DISTINCTLY: a MAVLINK_SDR_INJECT event whose meta.command == "disarm".
    inj_events = [e for e in events if e["kind"] == "MAVLINK_SDR_INJECT" and e["meta"].get("command")]
    assert inj_events, "expected a MAVLINK_SDR_INJECT audit event carrying meta.command"
    assert inj_events[0]["meta"]["command"] == "disarm"
    assert inj_events[0]["meta"].get("frame_hex")  # byte-accurate frame recorded

    # The WS request routes to the SDR bridge with the command + PHY params.
    reqs = [b for b in broadcasts if b.get("type") == "mavlink_inject_request"]
    assert len(reqs) == 1
    assert reqs[0]["command"] == "disarm"
    assert reqs[0]["center_freq_mhz"] == 915.0
    assert reqs[0]["target_system"] == 7
    # confirm token is forwarded (already consumed) as the bridge's evidence.
    assert len(reqs[0]["mavlink_sdr_inject_confirm_token"]) >= 20


def test_non_friendly_unauthorized_target_refused(monkeypatch):
    det = {**_LEGACY_TARGET, "authorized_target": False}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 403
    assert "not authorized" in ei.value.detail.lower()


def test_unknown_target_detection_404(monkeypatch):
    _stub_spine(monkeypatch, detection={"id": "someone-else"})
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_body(), user=USER))
    assert ei.value.status_code == 404


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
