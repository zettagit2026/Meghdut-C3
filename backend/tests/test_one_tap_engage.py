"""P1 adversarial gate suite for the ONE-TAP ENGAGE path (WEAPONS-HOT posture +
POST /api/engage). Proves the SAFETY-CRITICAL invariants of the build contract
(.omc/plans/one-tap-engage.md §P1):

  * posture arm requires commander + password step-up + confirm phrase +
    AO SafetyGate checklist + iff_registry_loaded + a valid AO zone;
  * /api/engage refuses (no fire) on HOLD / expired / out-of-AO / effect-not-
    permitted / tx-halted (409), on a friendly or not-classified-hostile target
    (403, and NO friendly-fire ack is ever minted), and on a missing
    engage_confirm (422);
  * NOT_FEASIBLE / UNKNOWN / none-clearable are SURFACED, never fired;
  * a FEASIBLE effect_override failover works;
  * the server-minted arm + confirm tokens are single-use, effect+target-bound,
    and NOT cross-effect usable — burned exactly once by `_execute_engagement`;
  * /emergency/abort drops the posture to HOLD;
  * /api/engage reaches a REAL dispatch ONLY when every gate passes;
  * the legacy /payloads/* path is untouched (covered by
    test_execute_engagement_routing.py — this suite adds nothing that changes it).

True unit tests (no live server / Mongo / websockets), same pattern as
test_execute_engagement_routing.py: token consumption + the IFF interlock stay
REAL; Mongo, range-auth, WS broadcast and audit are stubbed so nothing transmits.

Run: pytest backend/tests/test_one_tap_engage.py -v
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

USER = {"email": "cmdr@unused.local", "role": "commander", "id": "u-cmdr"}

# A GeoJSON square AO around (0,0): lon/lat ring.
_AO_ZONE = {
    "id": "AO-1",
    "name": "Test AO",
    "polygon": {"type": "Polygon",
                "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]},
}

# Hostile jam target INSIDE the AO (position 0,0), with a resolvable jam freq.
_JAM_TARGET = {
    "id": "det-1", "callsign": "HOSTILE-1", "threat_level": "MEDIUM",
    "iff_verified": False, "drone_lat": 0.0, "drone_lon": 0.0,
    "center_freq_ghz": 2.45,
}
# Hostile, AUTHORIZED legacy-MAVLink target inside the AO (the primitive's own
# authorize-target interlock is PRESERVED — one-tap does not bypass it).
_MAV_TARGET = {
    "id": "det-1", "callsign": "HOSTILE-M", "threat_level": "HIGH",
    "iff_verified": False, "authorized_target": True,
    "protocol": "MAVLink-SiK-Legacy", "system_id": 7, "component_id": 1,
    "drone_lat": 0.0, "drone_lon": 0.0, "center_freq_ghz": 0.915,
}
# Hostile, AUTHORIZED Parrot Wi-Fi target inside the AO.
_WIFI_TARGET = {
    "id": "det-1", "callsign": "ANAFI-AB12", "threat_level": "MEDIUM",
    "iff_verified": False, "authorized_target": True,
    "make": "Parrot", "model": "ANAFI", "control_link_family": "Wi-Fi/ARSDK",
    "ssid": "ANAFI-AB12", "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
    "bssid": "90:3A:E6:00:11:22", "channel": 6, "drone_lat": 0.0, "drone_lon": 0.0,
}


class _FakeColl:
    def __init__(self, docs_by_id=None, count=0):
        self._docs = docs_by_id or {}
        self._count = count

    async def find_one(self, query, projection=None):
        d = self._docs.get(query.get("id"))
        return dict(d) if d else None

    async def count_documents(self, query):
        return self._count


class _FakeDB:
    def __init__(self, *, detection=None, zone=_AO_ZONE, users=None, iff_count=1):
        self.detections = _FakeColl({detection["id"]: detection} if detection else {})
        self.zones = _FakeColl({zone["id"]: zone} if zone else {})
        self.users = _FakeColl(users or {USER["id"]: {**USER, "password_hash": "hash"}})
        self.iff_friendlies = _FakeColl(count=iff_count)


class _FakeReqClient:
    host = "127.0.0.1"


class _FakeReq:
    client = _FakeReqClient()


def _rec(*, effector="jam", detection_id="det-1", failover=None):
    """A one-target effector-recommendation snapshot for the stubbed recommender."""
    return {
        "recommendations": [{
            "detection_id": detection_id,
            "callsign": "X",
            "recommended_effector": effector,
            "recommended_rationale": "test rationale",
            "feasibility": {"jam": {"verdict": "FEASIBLE_UNVERIFIED_RANGE"}},
            "failover_order": failover or [],
        }],
        "excluded": [],
    }


def _stub_engage_spine(monkeypatch, *, detection, rec=None, range_ok=True,
                       iff_count=1):
    """Neutralize side effects (Mongo, range-auth, WS, audit) while keeping token
    consumption + the IFF interlock REAL. Returns (events, broadcasts)."""
    events, broadcasts = [], []
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection=detection, iff_count=iff_count))

    async def _reco():
        return rec if rec is not None else _rec()
    monkeypatch.setattr(srv, "_compute_effector_recommendations", _reco)

    async def _range(effect, actor):
        if not range_ok:
            raise srv.HTTPException(409, f"Range authorization for effect='{effect}' is OFF")
    monkeypatch.setattr(srv, "_require_range_authorized", _range)

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "meta": meta or {}, "actor": actor})
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)
    return events, broadcasts


def _arm_posture(effects=("jam",), ao="AO-1", ttl_s=1800):
    now = srv.datetime.now(srv.timezone.utc)
    srv._weapons_posture.update({
        "state": "TIGHT", "ao": ao, "permitted_effects": list(effects),
        "expires_at": now + srv.timedelta(seconds=ttl_s),
        "armed_by": "cmdr", "armed_at": now,
        "safety_ack": {"checklist": list(srv.WEAPONS_POSTURE_SAFETY_CHECKLIST)},
        "iff_registry_loaded": True,
    })


@pytest.fixture(autouse=True)
def _reset_state():
    """Every test starts from HOLD with clean token tables + range-auth leases."""
    srv._reset_weapons_posture_fields()
    srv._arm_tokens.clear()
    srv._jam_confirm_tokens.clear()
    srv._mavlink_sdr_inject_confirm_tokens.clear()
    srv._wifi_defeat_confirm_tokens.clear()
    srv._iff_ff_acks.clear()
    for eff in srv.RANGE_AUTH_EFFECTS:
        srv._range_authorization[eff].update(
            {"enabled": False, "expires_at": None, "enabled_by": None, "enabled_at": None})
    srv._range_auth_failures.clear()
    srv._tx_halted = False
    yield
    srv._reset_weapons_posture_fields()
    srv._tx_halted = True  # restore the conservative boot default


def _forwarded(broadcasts, msg_type):
    return [b for b in broadcasts if b.get("type") == msg_type]


# =====================================================================
# 1. Posture arming — the full load-bearing gate set.
# =====================================================================
def _arm_body(**ov):
    base = dict(
        state="TIGHT", ao="AO-1", permitted_effects=["jam"],
        password="correct-horse", confirm_phrase="WEAPONS TIGHT AO-1",
        safety_ack={k: True for k in srv.WEAPONS_POSTURE_SAFETY_CHECKLIST},
        iff_registry_loaded=True,
    )
    base.update(ov)
    return srv.WeaponsPostureBody(**base)


def _arm(monkeypatch, body, *, password_ok=True):
    monkeypatch.setattr(srv, "db", _FakeDB(detection=None))
    monkeypatch.setattr(srv, "verify_password", lambda pw, h: password_ok)

    async def _log(kind, message, meta=None, actor=None):
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        pass
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    return asyncio.run(srv.set_weapons_posture(body, _FakeReq(), user=USER))


def test_free_state_is_rejected_at_the_model():
    # FREE (zone auto-engage) is a deferred, separately-gated phase — not armable.
    with pytest.raises(Exception):
        srv.WeaponsPostureBody(state="FREE", ao="AO-1")


def test_arm_requires_password_step_up(monkeypatch):
    with pytest.raises(srv.HTTPException) as ei:
        _arm(monkeypatch, _arm_body(password=None), password_ok=False)
    assert ei.value.status_code == 401


def test_arm_requires_exact_confirm_phrase(monkeypatch):
    with pytest.raises(srv.HTTPException) as ei:
        _arm(monkeypatch, _arm_body(confirm_phrase="WEAPONS TIGHT wrong"))
    assert ei.value.status_code == 400
    assert srv._weapons_posture["state"] == "HOLD"


def test_arm_requires_iff_registry_loaded(monkeypatch):
    with pytest.raises(srv.HTTPException) as ei:
        _arm(monkeypatch, _arm_body(iff_registry_loaded=False))
    assert ei.value.status_code == 400
    assert "iff_registry_loaded" in ei.value.detail.lower()


def test_arm_requires_complete_safety_checklist(monkeypatch):
    partial = {k: True for k in srv.WEAPONS_POSTURE_SAFETY_CHECKLIST}
    partial[srv.WEAPONS_POSTURE_SAFETY_CHECKLIST[0]] = False
    with pytest.raises(srv.HTTPException) as ei:
        _arm(monkeypatch, _arm_body(safety_ack=partial))
    assert ei.value.status_code == 400
    assert "safetygate" in ei.value.detail.lower()


def test_arm_rejects_non_composable_permitted_effect(monkeypatch):
    # gnss_spoof is never one-tap-composable.
    with pytest.raises(srv.HTTPException) as ei:
        _arm(monkeypatch, _arm_body(permitted_effects=["jam", "gnss_spoof"]))
    assert ei.value.status_code == 400


def test_arm_rejects_missing_ao_zone(monkeypatch):
    monkeypatch.setattr(srv, "db", _FakeDB(detection=None, zone=None))
    monkeypatch.setattr(srv, "verify_password", lambda pw, h: True)

    async def _log(kind, message, meta=None, actor=None):
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _b(msg):
        pass
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _b)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.set_weapons_posture(_arm_body(), _FakeReq(), user=USER))
    assert ei.value.status_code == 404


def test_arm_happy_path_arms_real_range_auth_lease(monkeypatch):
    status = _arm(monkeypatch, _arm_body(permitted_effects=["jam", "wifi_deauth"]))
    assert status["state"] == "TIGHT"
    assert status["ao"] == "AO-1"
    assert set(status["permitted_effects"]) == {"jam", "wifi_deauth"}
    assert status["iff_registry_loaded"] is True
    # The matching per-effect range-auth leases were armed via the REAL gated path.
    assert srv._range_authorization["jam"]["enabled"] is True
    assert srv._range_authorization["wifi_deauth"]["enabled"] is True
    # An unpermitted effect's lease stays OFF.
    assert srv._range_authorization["mavlink_sdr_inject"]["enabled"] is False


# =====================================================================
# 2. /api/engage refusals — NO fire.
# =====================================================================
def _engage(body):
    return asyncio.run(srv.one_tap_engage(body, user=USER))


def test_hold_posture_refuses_409(monkeypatch):
    _stub_engage_spine(monkeypatch, detection=_JAM_TARGET)  # posture left HOLD
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


def test_expired_posture_dropped_to_hold_and_refuses_409(monkeypatch):
    events, _ = _stub_engage_spine(monkeypatch, detection=_JAM_TARGET)
    _arm_posture(effects=("jam",), ttl_s=-1)  # already past its TTL
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409
    assert srv._weapons_posture["state"] == "HOLD"
    assert any(e["kind"] == "WEAPONS_POSTURE_EXPIRED" for e in events)


def test_tx_halt_mid_engage_refuses_409(monkeypatch):
    _stub_engage_spine(monkeypatch, detection=_JAM_TARGET)
    _arm_posture(effects=("jam",))
    monkeypatch.setattr(srv, "_tx_halted", True)  # abort raised AFTER posture armed
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


def test_out_of_ao_target_refuses_409(monkeypatch):
    far = {**_JAM_TARGET, "drone_lat": 5.0, "drone_lon": 5.0}
    _stub_engage_spine(monkeypatch, detection=far)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409
    assert "AO" in str(ei.value.detail)


def test_position_less_target_refuses_409(monkeypatch):
    noposition = {k: v for k, v in _JAM_TARGET.items() if k not in ("drone_lat", "drone_lon")}
    _stub_engage_spine(monkeypatch, detection=noposition)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


def test_effect_not_permitted_refuses_409(monkeypatch):
    # Recommender wants jam, but posture only permits wifi_deauth.
    _stub_engage_spine(monkeypatch, detection=_JAM_TARGET, rec=_rec(effector="jam"))
    _arm_posture(effects=("wifi_deauth",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


def test_friendly_target_refused_403_and_no_ack_minted(monkeypatch):
    friendly = {**_JAM_TARGET, "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    events, broadcasts = _stub_engage_spine(monkeypatch, detection=friendly)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 403
    assert "roe floor" in ei.value.detail.lower()
    # ROE FLOOR: NEVER mints a friendly-fire ack; NEVER dispatches.
    assert srv._iff_ff_acks == {}
    assert not _forwarded(broadcasts, "jam_request")
    assert any(e["kind"] == "ENGAGE_REFUSED" for e in events)


def test_not_classified_hostile_refused_403(monkeypatch):
    unknown = {**_JAM_TARGET, "threat_level": "UNKNOWN"}
    _stub_engage_spine(monkeypatch, detection=unknown)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 403


def test_missing_engage_confirm_refused_422(monkeypatch):
    _stub_engage_spine(monkeypatch, detection=_JAM_TARGET)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=False))
    assert ei.value.status_code == 422


def test_not_feasible_recommendation_surfaced_not_fired(monkeypatch):
    # No feasible+clearable effector -> recommended_effector is None -> surface.
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_JAM_TARGET, rec=_rec(effector=None))
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409
    assert isinstance(ei.value.detail, dict)  # verdict + reason surfaced
    assert not _forwarded(broadcasts, "jam_request")


def test_target_not_in_recommendations_surfaced_409(monkeypatch):
    empty = {"recommendations": [], "excluded": [
        {"detection_id": "det-1", "reason": "not CONFIRMED"}]}
    _stub_engage_spine(monkeypatch, detection=_JAM_TARGET, rec=empty)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


# =====================================================================
# 3. Happy path — a REAL dispatch reached ONLY when every gate passes,
#    with single-use, effect+target-bound tokens burned exactly once.
# =====================================================================
def test_happy_path_jam_reaches_real_dispatch_and_burns_tokens(monkeypatch):
    minted = []
    real_issue = srv._issue_arm_token

    def _spy_issue(effect, target=None):
        out = real_issue(effect, target)
        minted.append({"effect": effect, "target": target, "token": out["arm_token"]})
        return out
    monkeypatch.setattr(srv, "_issue_arm_token", _spy_issue)

    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_JAM_TARGET, rec=_rec(effector="jam"))
    _arm_posture(effects=("jam",))

    resp = _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert resp["engaged"] is True
    assert resp["effect"] == "jam"
    assert resp["status"] == "AWAITING_ACK"
    # A REAL dispatch was reached (the primitive forwarded the jam request).
    assert len(_forwarded(broadcasts, "jam_request")) == 1
    # jam is an area effect — its arm token is minted target-LESS, matching the
    # jam branch of _execute_engagement (which consumes it with no target), so
    # binding is byte-identical to the legacy /payloads/jam path.
    assert minted and minted[-1]["effect"] == "jam" and minted[-1]["target"] is None
    # Single-use: both the arm + jam-confirm tokens are BURNED (empty) after the
    # primitive consumed them exactly once.
    assert srv._arm_tokens == {}
    assert srv._jam_confirm_tokens == {}


def test_happy_path_mavlink_requires_and_uses_authorized_target(monkeypatch):
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_MAV_TARGET, rec=_rec(effector="mavlink_takeover"))
    _arm_posture(effects=("mavlink_sdr_inject",))
    resp = _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert resp["effect"] == "mavlink_sdr_inject"
    assert len(_forwarded(broadcasts, "mavlink_inject_request")) == 1
    assert srv._arm_tokens == {}
    assert srv._mavlink_sdr_inject_confirm_tokens == {}


def test_mavlink_unauthorized_target_blocked_by_primitive_403(monkeypatch):
    # The primitive's OWN authorize-target interlock is PRESERVED — one-tap does
    # not bypass it. A hostile that was never authorize-target'd cannot fire.
    unauth = {**_MAV_TARGET, "authorized_target": False}
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=unauth, rec=_rec(effector="mavlink_takeover"))
    _arm_posture(effects=("mavlink_sdr_inject",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 403
    assert not _forwarded(broadcasts, "mavlink_inject_request")


def test_effect_override_to_feasible_failover_fires_that_effect(monkeypatch):
    rec = _rec(effector="jam", failover=[
        {"effector": "wifi_deauth", "feasible": True, "available": True,
         "reason": "clearable"}])
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_WIFI_TARGET, rec=rec)
    _arm_posture(effects=("jam", "wifi_deauth"))
    resp = _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True,
                                  effect_override="wifi_deauth"))
    assert resp["effect"] == "wifi_deauth"
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1
    assert len(_forwarded(broadcasts, "jam_request")) == 0


def test_infeasible_override_surfaced_not_fired(monkeypatch):
    rec = _rec(effector="jam", failover=[])  # only jam feasible
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_JAM_TARGET, rec=rec)
    _arm_posture(effects=("jam", "wifi_deauth"))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True,
                               effect_override="wifi_deauth"))
    assert ei.value.status_code == 409
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    assert not _forwarded(broadcasts, "jam_request")


def test_gnss_recommendation_is_not_one_tap_composable(monkeypatch):
    # gnss_deny maps to gnss_spoof which is NOT composable — surfaced, never fired.
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_JAM_TARGET, rec=_rec(effector="gnss_deny"))
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 409


# =====================================================================
# 4. Token cross-effect non-interchangeability (mint layer).
# =====================================================================
def test_engage_confirm_token_is_effect_specific_and_not_cross_effect():
    jam_tok = srv._issue_engage_confirm_token("jam")
    # A jam confirm token is a jam-type token — a wifi consume rejects it (422),
    # a mavlink consume rejects it (403). Cross-effect is impossible.
    with pytest.raises(srv.HTTPException) as ei:
        srv._consume_wifi_defeat_confirm_token(jam_tok)
    assert ei.value.status_code == 422
    wifi_tok = srv._issue_engage_confirm_token("wifi_deauth")
    srv._wifi_defeat_confirm_tokens.pop(wifi_tok, None)  # cleanup
    with pytest.raises(srv.HTTPException):
        srv._consume_mavlink_sdr_inject_confirm_token(jam_tok)


# =====================================================================
# 5. /emergency/abort drops the posture to HOLD.
# =====================================================================
def test_emergency_abort_drops_posture_to_hold(monkeypatch):
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append(kind)
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _b(msg):
        pass
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _b)
    _arm_posture(effects=("jam",))
    asyncio.run(srv.emergency_abort(user=USER))
    assert srv._tx_halted is True
    assert srv._weapons_posture["state"] == "HOLD"


# =====================================================================
# 6. GAP 1 — per-target concurrent-engage race guard.
#    Two simultaneous /api/engage calls on the SAME target: exactly ONE
#    reaches dispatch (one token-burn / one WS broadcast), the other gets a
#    clean 409, and the in-flight guard is released afterward (leak-free).
# =====================================================================
def test_concurrent_engage_same_target_one_fires_one_409_and_guard_released(monkeypatch):
    events, broadcasts = _stub_engage_spine(
        monkeypatch, detection=_JAM_TARGET, rec=_rec(effector="jam"))
    _arm_posture(effects=("jam",))

    # Force a REAL suspension point mid-engage so the two coroutines actually
    # interleave under the synchronous test fakes (in production the Mongo/WS
    # awaits suspend on their own; the fakes return without yielding). The guard's
    # add happens in the wrapper BEFORE this await, so the second task is already
    # refused by the time the first reaches dispatch.
    async def _reco_yield():
        await asyncio.sleep(0)
        return _rec(effector="jam")
    monkeypatch.setattr(srv, "_compute_effector_recommendations", _reco_yield)

    async def _both():
        b1 = srv.EngageBody(target_detection_id="det-1", engage_confirm=True)
        b2 = srv.EngageBody(target_detection_id="det-1", engage_confirm=True)
        # gather runs the two coroutines concurrently on one event loop. The first
        # to run registers the target in the in-flight guard BEFORE its first await
        # (the check-and-add is synchronous), so the second sees it and is refused.
        return await asyncio.gather(
            srv.one_tap_engage(b1, user=USER),
            srv.one_tap_engage(b2, user=USER),
            return_exceptions=True,
        )

    results = asyncio.run(_both())

    successes = [r for r in results if isinstance(r, dict)]
    refusals = [r for r in results if isinstance(r, srv.HTTPException)]
    # Exactly ONE engagement went through; the other was a clean 409.
    assert len(successes) == 1, results
    assert len(refusals) == 1, results
    assert refusals[0].status_code == 409
    assert "already in progress" in str(refusals[0].detail).lower()
    # Exactly ONE real dispatch (one token-burn) — no double-fire.
    assert len(_forwarded(broadcasts, "jam_request")) == 1
    assert srv._arm_tokens == {}
    assert srv._jam_confirm_tokens == {}
    # Guard released on all paths — the target is not stuck "in flight".
    assert "det-1" not in srv._engage_in_flight
    # And a subsequent engage on the SAME target still works (proves no leak).
    resp = _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert resp["engaged"] is True
    assert len(_forwarded(broadcasts, "jam_request")) == 2
    assert "det-1" not in srv._engage_in_flight


def test_engage_guard_released_after_refusal_path(monkeypatch):
    # A refused engage (ROE floor 403) must STILL release the guard (finally), so
    # the target is immediately engageable again once mis-classification clears.
    friendly = {**_JAM_TARGET, "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    _stub_engage_spine(monkeypatch, detection=friendly)
    _arm_posture(effects=("jam",))
    with pytest.raises(srv.HTTPException) as ei:
        _engage(srv.EngageBody(target_detection_id="det-1", engage_confirm=True))
    assert ei.value.status_code == 403
    assert "det-1" not in srv._engage_in_flight  # released despite the refusal


# =====================================================================
# 7. GAP 2 — posture disarm/expiry/abort DROPS exactly the leases it armed.
#    HOLD means holstered: the posture-armed range-auth leases go OFF (so the
#    legacy manual /payloads/* path goes dark too), while a lease the commander
#    armed INDEPENDENTLY is never dropped by posture disarm.
# =====================================================================
def test_disarm_cascades_off_only_posture_armed_leases(monkeypatch):
    # An independent lease (mavlink_sdr_inject) the commander armed OUTSIDE the
    # posture — it must survive a posture disarm untouched.
    srv._range_authorization["mavlink_sdr_inject"].update(
        {"enabled": True, "expires_at": srv.datetime.now(srv.timezone.utc)
         + srv.timedelta(seconds=900), "enabled_by": "cmdr", "enabled_at":
         srv.datetime.now(srv.timezone.utc)})

    # Arm a real TIGHT posture permitting jam + wifi_deauth (both newly armed).
    status = _arm(monkeypatch, _arm_body(permitted_effects=["jam", "wifi_deauth"]))
    assert status["state"] == "TIGHT"
    assert srv._range_authorization["jam"]["enabled"] is True
    assert srv._range_authorization["wifi_deauth"]["enabled"] is True
    # The posture recorded EXACTLY the two leases it turned on (not the independent one).
    assert set(srv._weapons_posture["leases_armed_by_posture"]) == {"jam", "wifi_deauth"}

    # Disarm (state=HOLD). _arm re-installs the log/broadcast/verify stubs.
    _arm(monkeypatch, _arm_body(state="HOLD"))
    assert srv._weapons_posture["state"] == "HOLD"
    # HOLD == holstered: the posture-armed leases are now OFF.
    assert srv._range_authorization["jam"]["enabled"] is False
    assert srv._range_authorization["wifi_deauth"]["enabled"] is False
    # The INDEPENDENTLY-armed lease is untouched.
    assert srv._range_authorization["mavlink_sdr_inject"]["enabled"] is True


def test_posture_does_not_claim_a_prearmed_permitted_lease(monkeypatch):
    # A lease the commander armed independently BUT that is also in permitted_effects
    # must NOT be recorded as posture-armed, and must survive a posture disarm.
    srv._range_authorization["jam"].update(
        {"enabled": True, "expires_at": srv.datetime.now(srv.timezone.utc)
         + srv.timedelta(seconds=900), "enabled_by": "cmdr", "enabled_at":
         srv.datetime.now(srv.timezone.utc)})

    _arm(monkeypatch, _arm_body(permitted_effects=["jam", "wifi_deauth"]))
    # jam was already ON (independent) -> NOT claimed; only wifi_deauth was armed here.
    assert srv._weapons_posture["leases_armed_by_posture"] == ["wifi_deauth"]

    _arm(monkeypatch, _arm_body(state="HOLD"))
    assert srv._weapons_posture["state"] == "HOLD"
    # wifi_deauth (posture-armed) OFF; jam (independent) LEFT ON.
    assert srv._range_authorization["wifi_deauth"]["enabled"] is False
    assert srv._range_authorization["jam"]["enabled"] is True


def test_expiry_and_abort_also_cascade_posture_armed_leases(monkeypatch):
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "meta": meta or {}})
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _b(msg):
        pass
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _b)

    # ---- TTL expiry path ----
    _arm_posture(effects=("jam",))
    srv._range_authorization["jam"].update({"enabled": True})
    srv._weapons_posture["leases_armed_by_posture"] = ["jam"]
    srv._weapons_posture["expires_at"] = (
        srv.datetime.now(srv.timezone.utc) - srv.timedelta(seconds=1))
    asyncio.run(srv._expire_weapons_posture())
    assert srv._weapons_posture["state"] == "HOLD"
    assert srv._range_authorization["jam"]["enabled"] is False
    assert any(e["kind"] == "WEAPONS_POSTURE_EXPIRED" for e in events)
    assert any(e["meta"].get("cascade") == "weapons_posture_hold" for e in events)

    # ---- /emergency/abort path ----
    events.clear()
    _arm_posture(effects=("wifi_deauth",))
    srv._range_authorization["wifi_deauth"].update({"enabled": True})
    srv._weapons_posture["leases_armed_by_posture"] = ["wifi_deauth"]
    asyncio.run(srv.emergency_abort(user=USER))
    assert srv._tx_halted is True
    assert srv._weapons_posture["state"] == "HOLD"
    assert srv._range_authorization["wifi_deauth"]["enabled"] is False
    assert any(e["meta"].get("cascade") == "weapons_posture_hold" for e in events)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
