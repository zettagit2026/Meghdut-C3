"""Phase-0 refactor guard: the three kinetic/TX deploy endpoints (deploy_jam,
deploy_mavlink_sdr_inject, deploy_wifi_defeat) MUST route their shared
post-authorization execution through the single internal primitive
`_execute_engagement`, and the fire-time gate chain each effect enforces must be
byte-identical to the pre-refactor behavior (same 403/409/422 on tx_halt /
bad-token / confirmed-friendly / broadcast-BSSID / lease-off).

This is the anti-drift test for the extraction: it proves (a) every endpoint
delegates to `_execute_engagement` with the correct effect AFTER its own
`_check_tx_not_halted()` gate, and (b) driving the REAL `_execute_engagement`
still produces each effect's OWN gate outcomes (jam freq-scoped, no target;
sdr-inject + wifi target-bound with IFF; wifi BSSID/no-broadcast). It does NOT
merge the effects into a lowest-common-denominator.

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_wifi_defeat_endpoint.py / test_mavlink_sdr_inject.py: importing
backend/server.py only needs the env vars SET (motor is lazy). Token consumption
+ the IFF interlock stay REAL; Mongo, range-auth, WS broadcast and audit are
stubbed so nothing transmits.

Run: pytest backend/tests/test_execute_engagement_routing.py -v
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


# ---------------------------------------------------------------------
# Per-effect body factories (real single-use tokens by default)
# ---------------------------------------------------------------------
def _jam_body(**ov):
    base = dict(
        band=ov.pop("band", "915"),
        arm_token=ov.pop("arm_token", None) or srv._issue_arm_token("jam")["arm_token"],
        jam_confirm_token=ov.pop("jam_confirm_token", None)
        or srv._issue_jam_confirm_token()["jam_confirm_token"],
    )
    base.update(ov)
    return srv.JamRequestBody(**base)


def _mav_body(**ov):
    target = ov.get("target_detection_id", "det-1")
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
        arm_token=ov.pop("arm_token", None) or srv._issue_arm_token("mavlink_sdr_inject", target)["arm_token"],
        mavlink_sdr_inject_confirm_token=ov.pop("mavlink_sdr_inject_confirm_token", None)
        or srv._issue_mavlink_sdr_inject_confirm_token()["mavlink_sdr_inject_confirm_token"],
    )
    base.update(ov)
    return srv.MavlinkSdrInjectBody(**base)


def _wifi_body(**ov):
    mode = ov.get("mode", "deauth")
    effect = srv._wifi_defeat_effect_for_mode(mode)
    target = ov.get("target_detection_id", "det-1")
    base = dict(
        target_detection_id=target,
        mode=mode,
        arm_token=ov.pop("arm_token", None) or srv._issue_arm_token(effect, target)["arm_token"],
        wifi_defeat_confirm_token=ov.pop("wifi_defeat_confirm_token", None)
        or srv._issue_wifi_defeat_confirm_token()["wifi_defeat_confirm_token"],
    )
    base.update(ov)
    return srv.WifiDefeatBody(**base)


# Routine (non-friendly), authorized, applicable targets that pass every gate
# except whatever a given test deliberately breaks.
_LEGACY_TARGET = {
    "id": "det-1", "callsign": "HOSTILE-1", "protocol": "MAVLink-SiK-Legacy",
    "system_id": 7, "component_id": 1, "authorized_target": True,
    "iff_verified": False, "threat_level": "MEDIUM",
}
_PARROT_TARGET = {
    "id": "det-1", "callsign": "ANAFI-AB12", "make": "Parrot", "model": "ANAFI",
    "control_link_family": "Wi-Fi/ARSDK", "ssid": "ANAFI-AB12",
    "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
    "bssid": "90:3A:E6:00:11:22", "channel": 6,
    "authorized_target": True, "iff_verified": False, "threat_level": "MEDIUM",
}


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


def _stub_spine(monkeypatch, *, detection=None, range_ok=True):
    """Neutralize the spine's real side effects (Mongo, range-auth log/broadcast,
    WS, audit) while capturing what the endpoint emits. Token consumption + the
    IFF interlock stay REAL (they are what these tests exercise)."""
    events = []
    broadcasts = []

    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection if detection is not None else _PARROT_TARGET))

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


# =====================================================================
# 1. Every endpoint routes through the single _execute_engagement primitive,
#    with the correct effect, AFTER its own _check_tx_not_halted() gate.
# =====================================================================
def _spy_execute(monkeypatch):
    calls = []
    sentinel = {"__sentinel__": True, "status": "AWAITING_ACK"}

    async def _spy(effect, body, user):
        calls.append({"effect": effect, "body": body, "user": user})
        return sentinel
    monkeypatch.setattr(srv, "_execute_engagement", _spy)
    monkeypatch.setattr(srv, "_tx_halted", False)
    return calls, sentinel


def test_deploy_jam_delegates_to_execute_engagement_with_effect_jam(monkeypatch):
    calls, sentinel = _spy_execute(monkeypatch)
    resp = asyncio.run(srv.deploy_jam(_jam_body(), user=USER))
    assert resp is sentinel
    assert len(calls) == 1
    assert calls[0]["effect"] == "jam"
    assert calls[0]["user"] is USER


def test_deploy_mavlink_delegates_with_effect_mavlink_sdr_inject(monkeypatch):
    calls, sentinel = _spy_execute(monkeypatch)
    resp = asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert resp is sentinel
    assert len(calls) == 1
    assert calls[0]["effect"] == "mavlink_sdr_inject"


def test_deploy_wifi_delegates_with_mode_derived_effect(monkeypatch):
    # deauth -> wifi_deauth ; arsdk_land -> arsdk_inject (effect is mode-derived,
    # NOT a fixed per-endpoint constant — the wifi endpoint owns two effects).
    calls, sentinel = _spy_execute(monkeypatch)
    resp = asyncio.run(srv.deploy_wifi_defeat(_wifi_body(mode="deauth"), user=USER))
    assert resp is sentinel
    assert calls[-1]["effect"] == "wifi_deauth"

    asyncio.run(srv.deploy_wifi_defeat(
        _wifi_body(mode="arsdk_land",
                   arm_token=srv._issue_arm_token("arsdk_inject", "det-1")["arm_token"]),
        user=USER))
    assert calls[-1]["effect"] == "arsdk_inject"


def test_tx_halt_gate_runs_in_endpoint_BEFORE_delegation(monkeypatch):
    """_check_tx_not_halted() is the endpoint's own gate (design step 2), run
    BEFORE _execute_engagement (steps 5-10). With TX halted, the primitive must
    never be reached — for ALL three effects, same 409."""
    for effect_call in (
        lambda: srv.deploy_jam(_jam_body(), user=USER),
        lambda: srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER),
        lambda: srv.deploy_wifi_defeat(_wifi_body(), user=USER),
    ):
        calls, _ = _spy_execute(monkeypatch)
        monkeypatch.setattr(srv, "_tx_halted", True)
        with pytest.raises(srv.HTTPException) as ei:
            asyncio.run(effect_call())
        assert ei.value.status_code == 409
        assert calls == [], "primitive must NOT be reached when TX is halted"


def test_execute_engagement_rejects_unknown_effect(monkeypatch):
    # Defensive default — unreachable from the three endpoints (they pass valid
    # effects) but proves the dispatcher fails closed rather than silently.
    _stub_spine(monkeypatch, detection=_LEGACY_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv._execute_engagement("not_a_real_effect", _mav_body(), USER))
    assert ei.value.status_code == 500


# =====================================================================
# 2. Gate order/enforcement is byte-identical per effect through the REAL
#    _execute_engagement (driven via the endpoints).
# =====================================================================
def _forwarded(broadcasts, msg_type):
    return [b for b in broadcasts if b.get("type") == msg_type]


# ---- tx_halt -> 409 (already covered above via the spy) and lease-off -> 409 ----
def test_lease_off_refused_409_all_three_effects(monkeypatch):
    # jam (no target/detection)
    _stub_spine(monkeypatch, range_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(_jam_body(), user=USER))
    assert ei.value.status_code == 409

    # mavlink_sdr_inject
    _stub_spine(monkeypatch, detection=_LEGACY_TARGET, range_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert ei.value.status_code == 409

    # wifi
    _stub_spine(monkeypatch, detection=_PARROT_TARGET, range_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 409


# ---- bad/missing arm token -> 403 for every effect (target/effect binding) ----
def test_bad_arm_token_refused_403_all_three_effects(monkeypatch):
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(_jam_body(arm_token="   "), user=USER))
    assert ei.value.status_code == 403

    _stub_spine(monkeypatch, detection=_LEGACY_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(arm_token="   "), user=USER))
    assert ei.value.status_code == 403

    _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(arm_token="   "), user=USER))
    assert ei.value.status_code == 403


def test_cross_effect_arm_token_refused_403(monkeypatch):
    """A jam arm token cannot fire a target-bound effect and vice-versa — each
    effect keeps its OWN effect-bound arm token (no lowest-common-denominator)."""
    _stub_spine(monkeypatch, detection=_LEGACY_TARGET)
    jam_tok = srv._issue_arm_token("jam")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(arm_token=jam_tok), user=USER))
    assert ei.value.status_code == 403
    assert "effect" in ei.value.detail.lower()


# ---- effect-specific confirm-token type (NOT interchangeable) ----
def test_confirm_token_types_are_not_interchangeable(monkeypatch):
    """Each effect keeps its OWN confirm-token type. jam/sdr reject a foreign
    token with 403; wifi rejects a foreign token with 422 — the exact per-effect
    codes are preserved by the extraction."""
    jam_tok = srv._issue_jam_confirm_token()["jam_confirm_token"]
    sdr_tok = srv._issue_mavlink_sdr_inject_confirm_token()["mavlink_sdr_inject_confirm_token"]
    wifi_tok = srv._issue_wifi_defeat_confirm_token()["wifi_defeat_confirm_token"]

    # jam endpoint rejects a wifi confirm token -> 403
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(_jam_body(jam_confirm_token=wifi_tok), user=USER))
    assert ei.value.status_code == 403

    # sdr-inject endpoint rejects a jam confirm token -> 403
    _stub_spine(monkeypatch, detection=_LEGACY_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _mav_body(mavlink_sdr_inject_confirm_token=jam_tok), user=USER))
    assert ei.value.status_code == 403

    # wifi endpoint rejects a sdr-inject confirm token -> 422
    _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _wifi_body(wifi_defeat_confirm_token=sdr_tok), user=USER))
    assert ei.value.status_code == 422


# ---- confirmed-friendly hard-blocked (fratricide) for the target-bound effects ----
def test_confirmed_friendly_hard_blocked_403_for_target_bound_effects(monkeypatch):
    mav_friendly = {**_LEGACY_TARGET, "iff_verified": True,
                    "threat_level": "FRIENDLY (IFF verified)"}
    _stub_spine(monkeypatch, detection=mav_friendly)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()

    wifi_friendly = {**_PARROT_TARGET, "iff_verified": True,
                     "threat_level": "FRIENDLY (IFF verified)"}
    events, broadcasts = _stub_spine(monkeypatch, detection=wifi_friendly)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "wifi_defeat_request")


# ---- wifi FRATRICIDE-CRITICAL broadcast/absent BSSID -> 422 (wifi-specific scope) ----
def test_broadcast_bssid_refused_422_wifi_only(monkeypatch):
    det = {**_PARROT_TARGET, "bssid": "FF:FF:FF:FF:FF:FF"}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(mode="deauth"), user=USER))
    assert ei.value.status_code == 422
    assert not _forwarded(broadcasts, "wifi_defeat_request")


# ---- sdr-inject system_id-0 broadcast -> 422 (sdr-specific scope) ----
def test_sdr_inject_system_id_zero_refused_422(monkeypatch):
    det = {**_LEGACY_TARGET, "system_id": 0}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert ei.value.status_code == 422
    assert not _forwarded(broadcasts, "mavlink_inject_request")


# ---- happy path: each effect forwards exactly its own request type ----
def test_happy_path_each_effect_dispatches_its_own_request_type(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch)  # default PARROT (unused by jam)
    resp = asyncio.run(srv.deploy_jam(_jam_body(), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "jam_request")) == 1

    events, broadcasts = _stub_spine(monkeypatch, detection=_LEGACY_TARGET)
    resp = asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "mavlink_inject_request")) == 1

    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(_wifi_body(mode="deauth"), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
