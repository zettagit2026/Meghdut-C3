"""P3 ADVERSARIAL suite for the FIRE-TIME CIVILIAN HARD BLOCK
(no-strike-registry.md §2B / P3): the actual civilian-protection floor behind
every shot. Proves THE FLOOR INVARIANT — civilian infrastructure CANNOT be
struck (no override/ack token exists) — and its two enforcement layers:

  * the ROE floor (`_detection_is_classified_hostile`) refuses a civilian /
    NON_THREAT one-tap target BEFORE any token is minted; and
  * `_enforce_fire_time_no_strike` HARD-blocks (403, NO_STRIKE_FIRE_REFUSED) at
    the instant of transmission in EVERY `_execute_engagement` path AND the
    manual /payloads/deploy path, evaluated BEFORE the IFF interlock.

Adversarial cases covered:
  * one-tap /api/engage on a CIVILIAN-matched contact -> 403 at the ROE floor,
    no token minted, no dispatch, audited;
  * a NON_THREAT contact -> ROE floor 403 before mint;
  * manual /payloads/mavlink-sdr-inject | wifi-defeat | deploy on a
    civilian-matched contact -> 403, no dispatch, even when authorize-target was
    (mistakenly) set;
  * a NEUTRAL match -> blocked;
  * a no_strike_conflict (civilian + decoded drone) -> STILL 403 at fire time,
    with the conflict NAMED for human adjudication;
  * the single-use friendly-fire ack does NOT let a CIVILIAN through, and is NOT
    consumed (assert no code path from the ack to firing on a civilian);
  * a NON-matched hostile fires normally through all three effects + jam
    (regression: the floor is purely additive);
  * FRIENDLY_OWN_FORCE-without-beacon coverage (blocked; overridable ONLY by the
    ack), and a beacon-verified friendly is DEFERRED to the IFF interlock.

True unit tests (no live server / Mongo / websockets): token consumption + the
REAL `_enforce_fire_time_no_strike` / `_detection_is_classified_hostile` stay
live; the registry read (`_no_strike_entries`), Mongo, range-auth, WS broadcast
and audit are stubbed so nothing transmits.

Run: pytest backend/tests/test_no_strike_fire_block.py -v
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

import no_strike
import server as srv

USER = {"email": "cmdr@unused.local", "role": "commander", "id": "u-cmdr"}

# ---------------------------------------------------------------------------
# Registry entries — GOVERNING: TEST-Org / synthetic only, NEVER a real org
# name (feedback-test-data-no-real-org-names). Matched by ssid_prefix.
# ---------------------------------------------------------------------------
CIV_ENTRY = {"id": "ns-civ", "category": "CIVILIAN_INFRASTRUCTURE", "hard": True,
             "label": "TEST-Org civilian AP", "match": {"ssid_prefix": "TESTCIV-"}}
NEUTRAL_ENTRY = {"id": "ns-neu", "category": "NEUTRAL", "hard": False,
                 "label": "TEST-Org neutral AP", "match": {"ssid_prefix": "TESTNEU-"}}
FRIENDLY_ENTRY = {"id": "ns-frd", "category": "FRIENDLY_OWN_FORCE", "hard": False,
                  "label": "TEST-Org own-force AP", "match": {"ssid_prefix": "TESTOWN-"}}

# ---------------------------------------------------------------------------
# Targets. A civilian target is (adversarially) marked authorized_target=True to
# prove the floor beats a MISTAKEN authorize-target — the civilian block runs
# regardless of a per-target authorization.
# ---------------------------------------------------------------------------
def _civ_mav(**ov):
    d = {"id": "det-civ", "callsign": "CIV-AP", "ssid": "TESTCIV-AP01",
         "threat_level": "LOW", "iff_verified": False, "authorized_target": True,
         "protocol": "MAVLink-SiK-Legacy", "system_id": 7, "component_id": 1}
    d.update(ov)
    return d


def _civ_wifi(**ov):
    d = {"id": "det-civ", "callsign": "CIV-AP", "ssid": "TESTCIV-AP01",
         "threat_level": "LOW", "iff_verified": False, "authorized_target": True,
         "make": "TestVendor", "model": "AP", "control_link_family": "Wi-Fi/ARSDK",
         "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
         "bssid": "90:3A:E6:00:11:22", "channel": 6}
    d.update(ov)
    return d


# A routine hostile (matches NOTHING in the registry) that passes every gate.
_HOSTILE_MAV = {"id": "det-1", "callsign": "HOSTILE-M", "ssid": "NOTLISTED-1",
                "protocol": "MAVLink-SiK-Legacy", "system_id": 7, "component_id": 1,
                "authorized_target": True, "iff_verified": False, "threat_level": "MEDIUM"}
_HOSTILE_WIFI = {"id": "det-1", "callsign": "ANAFI-AB12", "make": "Parrot", "model": "ANAFI",
                 "control_link_family": "Wi-Fi/ARSDK", "ssid": "ANAFI-AB12",
                 "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
                 "bssid": "90:3A:E6:00:11:22", "channel": 6, "authorized_target": True,
                 "iff_verified": False, "threat_level": "MEDIUM"}


# ---------------------------------------------------------------------------
# Fakes / stubs (pattern from test_execute_engagement_routing.py).
# ---------------------------------------------------------------------------
class _FakeDetections:
    def __init__(self, doc):
        self._doc = doc

    async def find_one(self, query, projection=None):
        if self._doc and self._doc.get("id") == query.get("id"):
            return dict(self._doc)
        return None


class _FakeDB:
    def __init__(self, doc):
        self.detections = _FakeDetections(doc)


def _stub(monkeypatch, *, detection, entries, range_ok=True):
    """Neutralize side effects; keep token consumption + the REAL fire-time
    no-strike/IFF functions live. `entries` is what `_no_strike_entries` returns."""
    events, broadcasts = [], []
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection))

    async def _range(effect, actor):
        if not range_ok:
            raise srv.HTTPException(409, "range off")
    monkeypatch.setattr(srv, "_require_range_authorized", _range)

    async def _ns_entries():
        return list(entries)
    monkeypatch.setattr(srv, "_no_strike_entries", _ns_entries)

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message, "meta": meta or {}, "actor": actor})
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)
    return events, broadcasts


def _forwarded(broadcasts, msg_type):
    return [b for b in broadcasts if b.get("type") == msg_type]


@pytest.fixture(autouse=True)
def _reset_state():
    srv._arm_tokens.clear()
    srv._jam_confirm_tokens.clear()
    srv._mavlink_sdr_inject_confirm_tokens.clear()
    srv._wifi_defeat_confirm_tokens.clear()
    srv._iff_ff_acks.clear()
    srv._reset_weapons_posture_fields()
    yield
    srv._reset_weapons_posture_fields()
    srv._tx_halted = True


# ---- body factories (real single-use tokens) ------------------------------
def _mav_body(target="det-civ", **ov):
    base = dict(
        target_detection_id=target, command="force_land", center_freq_mhz=915.0,
        air_rate_bps=250000.0, deviation_hz=62500.0, bt=0.5, bit_order="msb",
        tx_gain=20, repeat=3, target_link_legacy_mavlink=True,
        arm_token=srv._issue_arm_token("mavlink_sdr_inject", target)["arm_token"],
        mavlink_sdr_inject_confirm_token=srv._issue_mavlink_sdr_inject_confirm_token()[
            "mavlink_sdr_inject_confirm_token"],
    )
    base.update(ov)
    return srv.MavlinkSdrInjectBody(**base)


def _wifi_body(target="det-civ", mode="deauth", **ov):
    effect = srv._wifi_defeat_effect_for_mode(mode)
    base = dict(
        target_detection_id=target, mode=mode,
        arm_token=srv._issue_arm_token(effect, target)["arm_token"],
        wifi_defeat_confirm_token=srv._issue_wifi_defeat_confirm_token()[
            "wifi_defeat_confirm_token"],
    )
    base.update(ov)
    return srv.WifiDefeatBody(**base)


def _deploy_body(target="det-civ", **ov):
    base = dict(
        payload_id="PL-008", target_detection_id=target,
        arm_token=srv._issue_arm_token("deploy", target)["arm_token"],
    )
    base.update(ov)
    return srv.DeployPayloadBody(**base)


# ===========================================================================
# 1. ROE floor predicate — a civilian / NON_THREAT contact is NOT a valid target.
# ===========================================================================
def test_roe_floor_predicate_refuses_no_strike_matched():
    civ = {"threat_level": "LOW", "no_strike": {"matched": True,
           "category": "CIVILIAN_INFRASTRUCTURE"}}
    assert srv._detection_is_classified_hostile(civ) is False


def test_roe_floor_predicate_refuses_non_threat_sentinel():
    assert srv._detection_is_classified_hostile(
        {"threat_level": srv._NON_THREAT_LEVEL}) is False


def test_roe_floor_predicate_still_true_for_plain_hostile_and_conflict():
    # A plain hostile stays engageable...
    assert srv._detection_is_classified_hostile({"threat_level": "HIGH"}) is True
    # ...and a no_strike_CONFLICT keeps its hostile weight at the ROE floor (it is
    # a corroborated hostile) — it is the FIRE-TIME block, not the ROE floor, that
    # stops it. It carries NO no_strike.matched stamp, so it is not demoted here.
    conflict = {"threat_level": "HIGH", "no_strike_conflict":
                {"category": "CIVILIAN_INFRASTRUCTURE", "drone_basis": "protocol_confirmed"}}
    assert srv._detection_is_classified_hostile(conflict) is True


# ===========================================================================
# 2. one-tap /api/engage — civilian / NON_THREAT refused at the ROE floor,
#    BEFORE any token is minted and BEFORE any effect is composed.
# ===========================================================================
def _arm_posture(effects=("mavlink_sdr_inject", "wifi_deauth")):
    now = srv.datetime.now(srv.timezone.utc)
    srv._weapons_posture.update({
        "state": "TIGHT", "ao": "AO-1", "permitted_effects": list(effects),
        "expires_at": now + srv.timedelta(seconds=1800), "armed_by": "cmdr",
        "armed_at": now, "safety_ack": {}, "iff_registry_loaded": True,
    })


def _engage(target="det-civ"):
    return asyncio.run(srv.one_tap_engage(
        srv.EngageBody(target_detection_id=target, engage_confirm=True), user=USER))


def test_one_tap_civilian_refused_at_roe_floor_no_mint_no_dispatch(monkeypatch):
    civ = _civ_wifi(no_strike={"matched": True, "category": "CIVILIAN_INFRASTRUCTURE",
                               "entry_id": "ns-civ", "label": "TEST-Org civilian AP"})
    events, broadcasts = _stub(monkeypatch, detection=civ, entries=[CIV_ENTRY])
    _arm_posture()
    with pytest.raises(srv.HTTPException) as ei:
        _engage()
    assert ei.value.status_code == 403
    assert "roe floor" in ei.value.detail.lower()
    # No token minted (the ROE floor precedes mint) and nothing radiated.
    assert srv._arm_tokens == {}
    assert broadcasts == []
    assert any(e["kind"] == "ENGAGE_REFUSED"
               and e["meta"].get("reason") == "roe_floor_not_hostile" for e in events)


def test_one_tap_non_threat_refused_at_roe_floor(monkeypatch):
    nt = _civ_wifi(threat_level=srv._NON_THREAT_LEVEL)
    events, broadcasts = _stub(monkeypatch, detection=nt, entries=[])
    _arm_posture()
    with pytest.raises(srv.HTTPException) as ei:
        _engage()
    assert ei.value.status_code == 403
    assert srv._arm_tokens == {}
    assert broadcasts == []


# ===========================================================================
# 3. Manual /payloads/* — the fire-time hard block (403), no dispatch.
# ===========================================================================
def test_manual_mavlink_civilian_hard_blocked_403(monkeypatch):
    events, broadcasts = _stub(monkeypatch, detection=_civ_mav(), entries=[CIV_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert ei.value.status_code == 403
    assert "no-strike floor" in ei.value.detail.lower()
    assert "no override token" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "mavlink_inject_request")
    ref = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert ref and ref[0]["meta"]["category"] == "CIVILIAN_INFRASTRUCTURE"


def test_manual_wifi_civilian_hard_blocked_403(monkeypatch):
    events, broadcasts = _stub(monkeypatch, detection=_civ_wifi(), entries=[CIV_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 403
    assert "no-strike floor" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    assert any(e["kind"] == "NO_STRIKE_FIRE_REFUSED" for e in events)


def test_manual_deploy_civilian_hard_blocked_403(monkeypatch):
    events, broadcasts = _stub(monkeypatch, detection=_civ_mav(), entries=[CIV_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_payload(_deploy_body(), user=USER))
    assert ei.value.status_code == 403
    assert "no-strike floor" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "payload_deploy")
    assert any(e["kind"] == "NO_STRIKE_FIRE_REFUSED" for e in events)


# ---- NEUTRAL match is likewise blocked from auto/manual fire ----
def test_manual_wifi_neutral_hard_blocked_403(monkeypatch):
    neu = _civ_wifi(ssid="TESTNEU-AP01")
    events, broadcasts = _stub(monkeypatch, detection=neu, entries=[NEUTRAL_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 403
    ref = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert ref and ref[0]["meta"]["category"] == "NEUTRAL"
    assert not _forwarded(broadcasts, "wifi_defeat_request")


# ===========================================================================
# 4. no_strike_CONFLICT (civilian + decoded drone) -> STILL 403, conflict NAMED.
# ===========================================================================
def test_conflict_still_hard_blocked_and_named(monkeypatch):
    conflict = _civ_mav(
        threat_level="HIGH", protocol_confirmed=True,
        no_strike_conflict={"registry_entry_id": "ns-civ",
                            "category": "CIVILIAN_INFRASTRUCTURE",
                            "drone_basis": "protocol_confirmed"})
    events, broadcasts = _stub(monkeypatch, detection=conflict, entries=[CIV_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(), user=USER))
    assert ei.value.status_code == 403
    assert "conflict" in ei.value.detail.lower()
    assert "adjudicate" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "mavlink_inject_request")
    ref = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert ref and ref[0]["meta"]["conflict"] is True
    assert ref[0]["meta"]["drone_basis"] == "protocol_confirmed"


# ===========================================================================
# 5. THE FLOOR INVARIANT — the friendly-fire ack does NOT let a CIVILIAN through,
#    and is NOT consumed (no code path from any commander token to a civilian).
# ===========================================================================
def test_friendly_fire_ack_does_not_bypass_civilian_floor(monkeypatch):
    _stub(monkeypatch, detection=_civ_mav(), entries=[CIV_ENTRY])
    ack = srv._issue_iff_ff_ack("det-civ", "cmdr")["iff_friendly_fire_ack"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(
            _mav_body(iff_friendly_fire_ack=ack), user=USER))
    assert ei.value.status_code == 403
    assert "no override token" in ei.value.detail.lower()
    # The ack is UNTOUCHED — a civilian match never consults or burns it.
    assert ack in srv._iff_ff_acks, "friendly-fire ack must NOT be consumed by a civilian block"


def test_no_civilian_fire_through_source_has_no_override_branch():
    """Static guarantee: the fire-time function's CIVILIAN/NEUTRAL branch raises
    with NO ack/override consult in scope — there is literally no code path from a
    token to firing on a civilian. Assert the source shape (belt-and-braces to the
    behavioural test above)."""
    import inspect
    src = inspect.getsource(srv._enforce_fire_time_no_strike)
    civ_branch = src.split("block_category = next", 1)[1].split(
        "FRIENDLY_OWN_FORCE", 1)[0]
    assert "_consume_iff_ff_ack" not in civ_branch
    assert "friendly_fire_ack" not in civ_branch


# ===========================================================================
# 6. Regression — a NON-matched hostile fires normally through every effect.
# ===========================================================================
def test_non_matched_hostile_fires_mavlink(monkeypatch):
    _, broadcasts = _stub(monkeypatch, detection=_HOSTILE_MAV, entries=[CIV_ENTRY])
    resp = asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(target="det-1"), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "mavlink_inject_request")) == 1


def test_non_matched_hostile_fires_wifi(monkeypatch):
    _, broadcasts = _stub(monkeypatch, detection=_HOSTILE_WIFI, entries=[CIV_ENTRY])
    resp = asyncio.run(srv.deploy_wifi_defeat(_wifi_body(target="det-1", mode="deauth"),
                                              user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1


def test_non_matched_hostile_fires_jam_untargeted(monkeypatch):
    # Jam is an AREA effect with NO target detection — it carries no per-target
    # civilian identity, so the no-strike floor is structurally N/A and jam fires.
    _, broadcasts = _stub(monkeypatch, detection=_HOSTILE_MAV, entries=[CIV_ENTRY])
    jam = srv.JamRequestBody(band="915",
                             arm_token=srv._issue_arm_token("jam")["arm_token"],
                             jam_confirm_token=srv._issue_jam_confirm_token()[
                                 "jam_confirm_token"])
    resp = asyncio.run(srv.deploy_jam(jam, user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "jam_request")) == 1


# ===========================================================================
# 7. FRIENDLY_OWN_FORCE — same posture as IFF (block; ack overrides); a
#    beacon-verified friendly is DEFERRED to the IFF interlock (no double burn).
# ===========================================================================
def test_registry_friendly_without_beacon_blocked_then_ack_overrides(monkeypatch):
    frd = _civ_mav(id="det-frd", ssid="TESTOWN-AP01", authorized_target=True)
    # No ack -> blocked.
    _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_mavlink_sdr_inject(_mav_body(target="det-frd"), user=USER))
    assert ei.value.status_code == 403
    assert "friendly" in ei.value.detail.lower()

    # With a valid single-use ack -> the fire-time no-strike function ALLOWS it
    # (consumes the ack); driven directly so the assertion is on the floor logic.
    _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    ack = srv._issue_iff_ff_ack("det-frd", "cmdr")["iff_friendly_fire_ack"]
    asyncio.run(srv._enforce_fire_time_no_strike(
        frd, USER, context="unit", friendly_fire_ack=ack))
    assert ack not in srv._iff_ff_acks, "registry-friendly ack must be single-use burned"


def test_beacon_verified_friendly_deferred_to_iff(monkeypatch):
    # A friendly that ALSO matches a FRIENDLY registry entry but IS beacon-verified
    # is owned by the IFF interlock: the no-strike function DEFERS (no-op, no burn).
    frd = _civ_mav(id="det-frd", ssid="TESTOWN-AP01",
                   iff_verified=True, threat_level="FRIENDLY (IFF verified)")
    _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    ack = srv._issue_iff_ff_ack("det-frd", "cmdr")["iff_friendly_fire_ack"]
    # Direct call returns None (defer) and does NOT consume the ack — IFF will.
    assert asyncio.run(srv._enforce_fire_time_no_strike(
        frd, USER, context="unit", friendly_fire_ack=ack)) is None
    assert ack in srv._iff_ff_acks, "beacon-verified friendly must be deferred to IFF (no burn here)"


# ===========================================================================
# 8. INDEPENDENT-VERIFIER-REQUESTED ADDITIONS (P3 floor, TEST-ONLY).
# ===========================================================================

# A registry entry that matches ONLY by MAC (bssid), with a locally-administered
# (randomized) bssid on the far side — see no_strike.py's MAC-RANDOMIZATION
# CAVEAT. "AA:BB:CC:..." is the SAME locally-administered example already used
# by test_no_strike.py::test_is_locally_administered_bit.
CIV_ENTRY_RANDOMIZED_MAC = {
    "id": "ns-civ-mac", "category": "CIVILIAN_INFRASTRUCTURE", "hard": True,
    "label": "TEST-Org civilian AP (randomized MAC)",
    "match": {"bssid": "AA:BB:CC:DD:EE:FF"},
}


def test_manual_wifi_civilian_randomized_mac_still_hard_blocked_403(monkeypatch):
    """THE INVARIANT the floor exists for: a civilian match whose ONLY basis is
    a locally-administered (randomized) MAC still hard-blocks at fire time. The
    floor blocks on `category` alone — it never consults `randomized`/`basis` to
    decide whether to open a fire-through."""
    civ = _civ_wifi(ssid="NOTLISTED-RANDMAC", bssid="AA:BB:CC:DD:EE:FF")

    # Prove the randomized path is the one actually exercised: a live match
    # against ONLY this MAC-based entry returns randomized:True.
    verdict = no_strike.match(srv._detection_identity(civ), [CIV_ENTRY_RANDOMIZED_MAC])
    assert verdict["matched"] is True
    assert verdict["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert verdict["randomized"] is True

    events, broadcasts = _stub(monkeypatch, detection=civ, entries=[CIV_ENTRY_RANDOMIZED_MAC])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 403
    assert "no-strike floor" in ei.value.detail.lower()
    assert "no override token" in ei.value.detail.lower()
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    ref = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert ref and ref[0]["meta"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert ref[0]["meta"]["match_source"] == "live_registry"


class _FakeNoStrikeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


class _FakeNoStrikeCollection:
    """Mirrors test_no_strike.py's _FakeCollection.find/to_list shape (only the
    op `_no_strike_entries()` actually calls)."""
    def __init__(self):
        self.docs = []

    def find(self, flt=None, projection=None):
        flt = flt or {}
        matched = [dict(d) for d in self.docs
                  if all(d.get(k) == v for k, v in flt.items())]
        return _FakeNoStrikeCursor(matched)


def test_hotload_registry_add_blocks_previously_unstamped_detection(monkeypatch):
    """The floor is enforced against the LIVE registry, not just stored P2
    stamps: a detection with no `no_strike` stamp that does not initially match
    fires normally; a commander then adds a matching CIVILIAN entry (driving the
    REAL `_bump_no_strike_version()` + `_no_strike_entries()` hot-cache — mirrors
    test_no_strike.py::test_create_bumps_version_and_hotload_reflects_without_restart,
    via a mocked `db.no_strike_registry.find`); a subsequent fire attempt on that
    SAME, still-unstamped detection is then 403'd by the live re-match alone."""
    det = _civ_wifi(id="det-hotload", ssid="TESTCIV-HOTLOAD01",
                    bssid="90:3A:E6:AA:BB:CC")
    assert "no_strike" not in det

    fake_db = _FakeDB(det)
    fake_db.no_strike_registry = _FakeNoStrikeCollection()
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", fake_db)
    # Isolate the hot-load cache from whatever version other tests left it at.
    monkeypatch.setattr(srv, "_no_strike_version", 0)
    monkeypatch.setattr(srv, "_no_strike_cache", {"version": None, "entries": []})

    async def _range(effect, actor):
        return None
    monkeypatch.setattr(srv, "_require_range_authorized", _range)

    events, broadcasts = [], []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message, "meta": meta or {}, "actor": actor})
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)

    # 1. Registry empty -> the detection does not match -> a fire attempt passes
    #    the no-strike floor (no 403 from no-strike) and dispatches normally.
    assert asyncio.run(srv._no_strike_entries()) == []
    resp = asyncio.run(srv.deploy_wifi_defeat(
        _wifi_body(target="det-hotload", mode="deauth"), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1

    # 2. A commander adds a matching CIVILIAN entry via the REAL CRUD hot-load
    #    primitive (_bump_no_strike_version) against a mocked
    #    db.no_strike_registry.find — no re-classification of the detection.
    fake_db.no_strike_registry.docs.append({
        "id": "ns-civ-hotload", "category": "CIVILIAN_INFRASTRUCTURE", "hard": True,
        "label": "TEST-Org civilian AP (hot-loaded)", "enabled": True,
        "match": {"ssid_prefix": "TESTCIV-"},
    })
    srv._bump_no_strike_version()
    entries = asyncio.run(srv._no_strike_entries())
    assert [e["id"] for e in entries] == ["ns-civ-hotload"]

    # 3. Same, still-unstamped detection — the LIVE re-match now catches it.
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _wifi_body(target="det-hotload", mode="deauth"), user=USER))
    assert ei.value.status_code == 403
    assert "no-strike floor" in ei.value.detail.lower()
    # No second dispatch — the count stays at the one from step 1.
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1
    ref = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert ref and ref[-1]["meta"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert ref[-1]["meta"]["match_source"] == "live_registry"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
