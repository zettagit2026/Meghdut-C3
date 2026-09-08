"""COMMANDER NO-STRIKE OVERRIDE — the two-tier fire-time floor.

Proves the WEAKENING of the fire-time no-strike floor for the OVERRIDABLE
categories (FRIENDLY_OWN_FORCE + NEUTRAL) ONLY, under a heavily-gated commander
token, while the CIVILIAN_INFRASTRUCTURE HARD FLOOR is untouched:

  * override-token mint/consume: atomic single-use, target+effect+category bound,
    a CIVILIAN token can neither be minted (issuance 403) nor consumed (returns
    None), a BSSID-less token never validates;
  * fire-time TIER A (CIVILIAN_INFRASTRUCTURE) stays a hard 403 — no token is even
    consulted (a planted override token is NOT burned);
  * fire-time TIER B (NEUTRAL): 403 (NO_STRIKE_OVERRIDE_REFUSED) with no token,
    ALLOW (+ NO_STRIKE_CIVILIAN_OVERRIDE audit) with a valid token;
  * FRIENDLY_OWN_FORCE (no beacon): accepts EITHER the override token OR the
    friendly-fire ack — never both (no double burn);
  * ONE-TAP /api/engage on a NEUTRAL no_strike contact is refused at the ROE floor
    EVEN with a valid override token sitting in _no_strike_overrides;
  * designate: CIVILIAN refused, NEUTRAL allowed, non-drone allowed under
    commander_override (honest non-drone candidate);
  * reclassify OUT of CIVILIAN_INFRASTRUCTURE requires password step-up + a real
    justification and emits NO_STRIKE_CIVILIAN_DECLASSIFIED.

True unit tests (no live server / Mongo / websockets): token consumption + the
REAL fire-time functions stay live; Mongo, range-auth, WS and audit are stubbed.

Run: pytest backend/tests/test_no_strike_override.py -v
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

# Registry entries — GOVERNING: TEST-Org / synthetic only.
CIV_ENTRY = {"id": "ns-civ", "category": "CIVILIAN_INFRASTRUCTURE", "hard": True,
             "label": "TEST-Org civilian AP", "match": {"ssid_prefix": "TESTCIV-"}}
NEUTRAL_ENTRY = {"id": "ns-neu", "category": "NEUTRAL", "hard": False,
                 "label": "TEST-Org neutral AP", "match": {"ssid_prefix": "TESTNEU-"}}
FRIENDLY_ENTRY = {"id": "ns-frd", "category": "FRIENDLY_OWN_FORCE", "hard": False,
                  "label": "TEST-Org own-force AP", "match": {"ssid_prefix": "TESTOWN-"}}

GOOD_JUSTIFICATION = "Positive visual ID of a hostile relay masquerading as a neutral AP."


def _wifi(**ov):
    d = {"id": "det-x", "callsign": "AP-X", "ssid": "TESTNEU-AP01",
         "threat_level": "MEDIUM", "iff_verified": False, "authorized_target": True,
         "make": "TestVendor", "model": "AP", "control_link_family": "Wi-Fi/ARSDK",
         "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
         "bssid": "90:3A:E6:00:11:22", "channel": 6}
    d.update(ov)
    return d


# ---------------------------------------------------------------------------
# Fakes / stubs (pattern from test_no_strike_fire_block.py).
# ---------------------------------------------------------------------------
class _FakeDetections:
    def __init__(self, doc):
        self._doc = doc

    async def find_one(self, query, projection=None):
        if self._doc and self._doc.get("id") == query.get("id"):
            return dict(self._doc)
        return None


class _FakeUsers:
    def __init__(self, password="correcthorse"):
        self._password = password

    async def find_one(self, query, projection=None):
        return {"id": "u-cmdr", "email": USER["email"], "password_hash": "hash-unused"}


class _FakeDB:
    def __init__(self, doc, password="correcthorse"):
        self.detections = _FakeDetections(doc)
        self.users = _FakeUsers(password)


class _Req:
    class _C:
        host = "10.0.0.9"
    client = _C()


def _stub(monkeypatch, *, detection, entries, password_ok=True):
    events, broadcasts = [], []
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "db", _FakeDB(detection))

    async def _range(effect, actor):
        return None
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

    monkeypatch.setattr(srv, "verify_password", lambda pw, h: password_ok)
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
    srv._no_strike_overrides.clear()
    srv._range_auth_failures.clear()
    srv._reset_weapons_posture_fields()
    yield
    srv._no_strike_overrides.clear()
    srv._range_auth_failures.clear()
    srv._reset_weapons_posture_fields()
    srv._tx_halted = True


def _wifi_body(target="det-x", mode="deauth", **ov):
    effect = srv._wifi_defeat_effect_for_mode(mode)
    base = dict(
        target_detection_id=target, mode=mode,
        arm_token=srv._issue_arm_token(effect, target)["arm_token"],
        wifi_defeat_confirm_token=srv._issue_wifi_defeat_confirm_token()[
            "wifi_defeat_confirm_token"],
    )
    base.update(ov)
    return srv.WifiDefeatBody(**base)


# ===========================================================================
# 1. Token mint/consume — atomic, single-use, target+effect+category+bssid bound.
# ===========================================================================
def test_override_consume_happy_path_neutral():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    rec = srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth")
    assert rec is not None and rec["category"] == "NEUTRAL"


def test_override_single_use_atomic_second_consume_is_none():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth") is not None
    # Burned — a replay is refused.
    assert srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth") is None


def test_override_target_mismatch_refused_and_burned():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(out["token"], "det-OTHER", "wifi_deauth") is None
    # A mismatch still burns the token (security event, not retryable).
    assert out["token"] not in srv._no_strike_overrides


def test_override_effect_mismatch_refused():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(out["token"], "det-x", "arsdk_inject") is None


def test_override_civilian_category_can_never_consume():
    # Even a token that somehow carried CIVILIAN_INFRASTRUCTURE is rejected — the
    # hard floor has no override.
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "CIVILIAN_INFRASTRUCTURE", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth") is None


def test_override_missing_bssid_never_validates():
    out = srv._issue_no_strike_override("det-x", None, "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth") is None


def test_override_none_token_is_none_without_burning_anything():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    assert srv._consume_no_strike_override(None, "det-x", "wifi_deauth") is None
    # A real token presented later still works — None never touched the store.
    assert srv._consume_no_strike_override(out["token"], "det-x", "wifi_deauth") is not None


# ===========================================================================
# 2. Issuance endpoint — civilian can't mint; neutral mints + LOUD audit.
# ===========================================================================
def _mint(det_id="det-x", **body):
    base = dict(password="correcthorse", justification=GOOD_JUSTIFICATION, effect="wifi_deauth")
    base.update(body)
    return asyncio.run(srv.mint_no_strike_override(
        det_id, srv.NoStrikeOverrideBody(**base), request=_Req(), user=USER))


def test_issuance_civilian_refuses_to_mint_403(monkeypatch):
    civ = _wifi(ssid="TESTCIV-AP01")
    events, _ = _stub(monkeypatch, detection=civ, entries=[CIV_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        _mint()
    assert ei.value.status_code == 403
    assert "civilian infrastructure" in ei.value.detail.lower()
    assert "reclassify" in ei.value.detail.lower()
    assert srv._no_strike_overrides == {}
    assert any(e["kind"] == "NO_STRIKE_OVERRIDE_MINT_FAILED" for e in events)


def test_issuance_neutral_mints_token_and_audits(monkeypatch):
    events, _ = _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    out = _mint()
    assert out["token"] in srv._no_strike_overrides
    rec = srv._no_strike_overrides[out["token"]]
    assert rec["category"] == "NEUTRAL" and rec["effect"] == "wifi_deauth"
    assert rec["bssid"] == "90:3A:E6:00:11:22"  # DERIVED, never client-supplied
    minted = [e for e in events if e["kind"] == "NO_STRIKE_OVERRIDE_MINTED"]
    assert minted and minted[0]["meta"]["category"] == "NEUTRAL"
    assert minted[0]["meta"]["justification"] == GOOD_JUSTIFICATION


def test_issuance_bad_password_401(monkeypatch):
    _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY], password_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        _mint()
    assert ei.value.status_code == 401
    assert srv._no_strike_overrides == {}


def test_issuance_trivial_justification_400(monkeypatch):
    _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        _mint(justification="n/a")
    assert ei.value.status_code == 400
    assert srv._no_strike_overrides == {}


def test_issuance_not_matched_422(monkeypatch):
    _stub(monkeypatch, detection=_wifi(ssid="NOTLISTED-1"), entries=[NEUTRAL_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        _mint()
    assert ei.value.status_code == 422


def test_issuance_bad_effect_400(monkeypatch):
    _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        _mint(effect="not-a-real-effect")
    assert ei.value.status_code == 400


# ===========================================================================
# 3. Fire-time TIER A — CIVILIAN stays a HARD 403; a planted override is NOT
#    consulted or burned.
# ===========================================================================
def test_fire_time_civilian_hard_403_even_with_planted_override(monkeypatch):
    civ = _wifi(ssid="TESTCIV-AP01")
    events, broadcasts = _stub(monkeypatch, detection=civ, entries=[CIV_ENTRY])
    # Plant a (civilian-category) override token for this exact target+effect.
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "CIVILIAN_INFRASTRUCTURE", GOOD_JUSTIFICATION, "cmdr")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _wifi_body(no_strike_override=out["token"]), user=USER))
    assert ei.value.status_code == 403
    assert "no override token" in ei.value.detail.lower()
    # HARD FLOOR consulted NO token — it is untouched.
    assert out["token"] in srv._no_strike_overrides
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    assert any(e["kind"] == "NO_STRIKE_FIRE_REFUSED" for e in events)


# ===========================================================================
# 4. Fire-time TIER B — NEUTRAL: 403 without token, ALLOW with a valid token.
# ===========================================================================
def test_fire_time_neutral_no_token_403(monkeypatch):
    events, broadcasts = _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_wifi_body(), user=USER))
    assert ei.value.status_code == 403
    assert any(e["kind"] == "NO_STRIKE_OVERRIDE_REFUSED" for e in events)
    assert not _forwarded(broadcasts, "wifi_defeat_request")


def test_fire_time_neutral_valid_token_allows_and_audits(monkeypatch):
    events, broadcasts = _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    resp = asyncio.run(srv.deploy_wifi_defeat(
        _wifi_body(no_strike_override=out["token"]), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert len(_forwarded(broadcasts, "wifi_defeat_request")) == 1
    # The override was burned exactly once.
    assert out["token"] not in srv._no_strike_overrides
    ov = [e for e in events if e["kind"] == "NO_STRIKE_CIVILIAN_OVERRIDE"]
    assert ov and ov[0]["meta"]["category"] == "NEUTRAL"


def test_fire_time_neutral_token_bound_to_other_effect_refused(monkeypatch):
    # A token minted for arsdk_inject cannot fire a wifi_deauth on the same target.
    events, broadcasts = _stub(monkeypatch, detection=_wifi(), entries=[NEUTRAL_ENTRY])
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "arsdk_inject",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _wifi_body(mode="deauth", no_strike_override=out["token"]), user=USER))
    assert ei.value.status_code == 403
    assert not _forwarded(broadcasts, "wifi_defeat_request")


# ===========================================================================
# 5. FRIENDLY_OWN_FORCE (no beacon) — accepts EITHER token, NEVER both.
# ===========================================================================
def test_friendly_accepts_override_token(monkeypatch):
    frd = _wifi(ssid="TESTOWN-AP01")
    events, _ = _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "FRIENDLY_OWN_FORCE", GOOD_JUSTIFICATION, "cmdr")
    # Drive the fire-time floor directly (isolate its logic).
    asyncio.run(srv._enforce_fire_time_no_strike(
        frd, USER, context="unit", effect="wifi_deauth", no_strike_override=out["token"]))
    assert out["token"] not in srv._no_strike_overrides  # burned
    assert any(e["kind"] == "NO_STRIKE_FRIENDLY_FIRE_OVERRIDE" for e in events)


def test_friendly_override_and_ack_never_double_burned(monkeypatch):
    frd = _wifi(ssid="TESTOWN-AP01")
    _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    ov = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                       "FRIENDLY_OWN_FORCE", GOOD_JUSTIFICATION, "cmdr")
    ack = srv._issue_iff_ff_ack("det-x", "cmdr")["iff_friendly_fire_ack"]
    # Present BOTH: the override is tried first and wins; the ack is NOT consumed.
    asyncio.run(srv._enforce_fire_time_no_strike(
        frd, USER, context="unit", effect="wifi_deauth",
        friendly_fire_ack=ack, no_strike_override=ov["token"]))
    assert ov["token"] not in srv._no_strike_overrides  # override burned
    assert ack in srv._iff_ff_acks, "the friendly-fire ack must NOT be burned when the override wins"


def test_friendly_falls_back_to_ack_when_no_override(monkeypatch):
    frd = _wifi(ssid="TESTOWN-AP01")
    _stub(monkeypatch, detection=frd, entries=[FRIENDLY_ENTRY])
    ack = srv._issue_iff_ff_ack("det-x", "cmdr")["iff_friendly_fire_ack"]
    asyncio.run(srv._enforce_fire_time_no_strike(
        frd, USER, context="unit", effect="wifi_deauth", friendly_fire_ack=ack))
    assert ack not in srv._iff_ff_acks  # ack burned via the fallback path


# ===========================================================================
# 6. ONE-TAP /api/engage EXCLUSION — a NEUTRAL no_strike contact is refused at
#    the ROE floor EVEN with a valid override token in _no_strike_overrides.
# ===========================================================================
def _arm_posture(effects=("wifi_deauth", "mavlink_sdr_inject")):
    now = srv.datetime.now(srv.timezone.utc)
    srv._weapons_posture.update({
        "state": "TIGHT", "ao": "AO-1", "permitted_effects": list(effects),
        "expires_at": now + srv.timedelta(seconds=1800), "armed_by": "cmdr",
        "armed_at": now, "safety_ack": {}, "iff_registry_loaded": True,
    })


def test_one_tap_neutral_refused_even_with_valid_override_token(monkeypatch):
    neu = _wifi(no_strike={"matched": True, "category": "NEUTRAL",
                           "entry_id": "ns-neu", "label": "TEST-Org neutral AP"})
    events, broadcasts = _stub(monkeypatch, detection=neu, entries=[NEUTRAL_ENTRY])
    _arm_posture()
    # A perfectly valid override token exists for this target+effect...
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.one_tap_engage(
            srv.EngageBody(target_detection_id="det-x", engage_confirm=True), user=USER))
    assert ei.value.status_code == 403
    assert "roe floor" in ei.value.detail.lower()
    # ...but one-tap NEVER consults it: the token is untouched and nothing fired.
    assert out["token"] in srv._no_strike_overrides
    assert srv._arm_tokens == {}
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    assert any(e["kind"] == "ENGAGE_REFUSED" for e in events)


def test_engage_body_has_no_override_field():
    # Static invariant: /api/engage never even accepts an override field.
    assert "no_strike_override" not in srv.EngageBody.model_fields


# ===========================================================================
# 7. FIX 1 — CIVILIAN-WINS-OVER-SPECIFICITY: a more-specific NEUTRAL/FRIENDLY
#    entry (or a drone spoofing a civilian BSSID) can NEVER shadow an enabled
#    CIVILIAN_INFRASTRUCTURE entry out of the single aggregate no_strike.match()
#    hit. The civilian hard floor holds regardless of what the aggregate returned.
# ===========================================================================
# A civilian OUI entry (24-bit, LESS specific) and a NEUTRAL bssid entry (48-bit,
# MORE specific) that BOTH match the same AP (bssid 90:3A:E6:00:11:22 -> oui
# 90:3A:E6). no_strike.match() returns the NEUTRAL bssid hit (most-specific-first),
# shadowing the civilian OUI entry out of the aggregate verdict.
CIV_OUI_ENTRY = {"id": "ns-civ-oui", "category": "CIVILIAN_INFRASTRUCTURE", "hard": True,
                 "label": "TEST-Org civilian OUI", "enabled": True,
                 "match": {"oui": "90:3A:E6"}}
NEU_BSSID_ENTRY = {"id": "ns-neu-bssid", "category": "NEUTRAL", "hard": False,
                   "label": "TEST-Org neutral BSSID", "enabled": True,
                   "match": {"bssid": "90:3A:E6:00:11:22"}}


def test_matches_any_civilian_beats_shadowing_neutral():
    # Unit-level: the aggregate hit is NEUTRAL, but the per-entry sweep still sees
    # the civilian entry.
    identity = srv._detection_identity(_wifi())
    entries = [NEU_BSSID_ENTRY, CIV_OUI_ENTRY]
    assert no_strike.match(identity, entries).get("category") == "NEUTRAL"
    assert srv._matches_any_civilian(identity, entries) is True


def test_fire_time_civilian_wins_over_shadowing_neutral_bssid(monkeypatch):
    # The core FIX-1 attack: an enabled civilian OUI entry + a more-specific NEUTRAL
    # bssid entry for the SAME AP + a valid NEUTRAL override token planted -> the
    # fire-time floor STILL hard-403s and the token is NOT burned.
    det = _wifi()  # bssid 90:3A:E6:00:11:22
    events, broadcasts = _stub(monkeypatch, detection=det,
                               entries=[NEU_BSSID_ENTRY, CIV_OUI_ENTRY])
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _wifi_body(no_strike_override=out["token"]), user=USER))
    assert ei.value.status_code == 403
    assert "no override token" in ei.value.detail.lower()
    # The civilian hard floor consulted NO token — the planted NEUTRAL token is untouched.
    assert out["token"] in srv._no_strike_overrides
    assert not _forwarded(broadcasts, "wifi_defeat_request")
    refused = [e for e in events if e["kind"] == "NO_STRIKE_FIRE_REFUSED"]
    assert refused, "the shadowed civilian match must still be a hard NO_STRIKE_FIRE_REFUSED"
    # The refusal names the ACTUAL civilian entry, not the shadowing NEUTRAL one.
    assert refused[0]["meta"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert refused[0]["meta"]["entry_id"] == "ns-civ-oui"


def test_mint_refused_when_any_civilian_entry_matches(monkeypatch):
    # FIX-1 on the mint side: even though the aggregate hit is NEUTRAL, a per-entry
    # civilian match REFUSES the mint (403) — no token that the fire-time floor would
    # otherwise honour is ever issued.
    events, _ = _stub(monkeypatch, detection=_wifi(),
                      entries=[NEU_BSSID_ENTRY, CIV_OUI_ENTRY])
    with pytest.raises(srv.HTTPException) as ei:
        _mint()
    assert ei.value.status_code == 403
    assert "civilian infrastructure" in ei.value.detail.lower()
    assert srv._no_strike_overrides == {}
    failed = [e for e in events if e["kind"] == "NO_STRIKE_OVERRIDE_MINT_FAILED"]
    assert failed and failed[0]["meta"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert failed[0]["meta"]["entry_id"] == "ns-civ-oui"


# ===========================================================================
# 8. FIX 4 — token EXPIRY is fail-closed: an override whose expiry is in the past
#    is refused by _consume (returns None) and the record is gone (burned).
# ===========================================================================
def test_override_expiry_refused():
    out = srv._issue_no_strike_override("det-x", "90:3A:E6:00:11:22", "wifi_deauth",
                                        "NEUTRAL", GOOD_JUSTIFICATION, "cmdr")
    token = out["token"]
    # Plant an expiry in the past directly on the record (fail-closed on expiry).
    srv._no_strike_overrides[token]["expiry"] = (
        srv.datetime.now(srv.timezone.utc) - srv.timedelta(seconds=1))
    assert srv._consume_no_strike_override(token, "det-x", "wifi_deauth") is None
    # The expired token is removed (single-use pop happens before the expiry check).
    assert token not in srv._no_strike_overrides


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
