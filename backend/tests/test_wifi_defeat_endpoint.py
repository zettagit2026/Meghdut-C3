"""Unit tests for the governed Active Wi-Fi Defeat endpoint (deploy_wifi_defeat)
— the FRATRICIDE-CRITICAL surface that mints the wifi_defeat confirm token,
enforces the full arm + confirm + IFF + range-auth + tx-halt spine, and applies
the PMF / unencrypted-Parrot-Tello HONESTY gates before forwarding a targeted
softAP defeat (802.11 deauth link-drop OR ARSDK/Tello UDP land/emergency) to the
wifi-defeat field bridge.

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_mavlink_sdr_inject.py: importing backend/server.py only needs the
env vars SET (motor is lazy). The endpoint is driven directly with its async
dependencies (token consumption, range-auth, Mongo, WS broadcast) monkeypatched,
so nothing transmits and no Mongo is touched. The token/IFF/range-auth spine
stays REAL — those are what these tests exercise.

Run: pytest backend/tests/test_wifi_defeat_endpoint.py -v
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
OPERATOR = {"email": "op@unused.local", "role": "operator"}


def _confirm() -> str:
    return srv._issue_wifi_defeat_confirm_token()["wifi_defeat_confirm_token"]


def _arm(effect="wifi_deauth", target="det-1") -> str:
    return srv._issue_arm_token(effect, target)["arm_token"]


def _body(**overrides):
    mode = overrides.get("mode", "deauth")
    effect = srv._wifi_defeat_effect_for_mode(mode)
    target = overrides.get("target_detection_id", "det-1")
    base = dict(
        target_detection_id=target,
        mode=mode,
        arm_token=overrides.pop("arm_token", None) or _arm(effect, target),
        wifi_defeat_confirm_token=overrides.pop("wifi_defeat_confirm_token", None) or _confirm(),
    )
    base.update(overrides)
    return srv.WifiDefeatBody(**base)


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


# A routine (non-friendly), authorized, IDENTIFIED-unencrypted-Parrot target with
# a concrete softAP BSSID and NO PMF — passes every gate except whatever a given
# test deliberately breaks. Wi-Fi/ARSDK => the arsdk inject modes are applicable;
# no pmf flag => deauth is applicable.
_PARROT_TARGET = {
    "id": "det-1", "callsign": "ANAFI-AB12", "make": "Parrot", "model": "ANAFI",
    "control_link_family": "Wi-Fi/ARSDK", "ssid": "ANAFI-AB12",
    "protocol": "Wi-Fi 802.11 a/n (ARSDK3)", "encrypted": False,
    "bssid": "90:3A:E6:00:11:22", "channel": 6,
    "authorized_target": True, "iff_verified": False, "threat_level": "MEDIUM",
}

# An identified unencrypted Tello (SEPARATE plaintext UDP SDK, not ARSDK3).
_TELLO_TARGET = {
    "id": "det-1", "callsign": "TELLO-99", "make": "Ryze", "model": "Tello",
    "control_link_family": "Wi-Fi/Tello", "ssid": "TELLO-99",
    "protocol": "Wi-Fi 802.11 (Tello UDP)", "encrypted": False,
    "bssid": "60:60:1F:00:33:44", "channel": 1,
    "softap": "192.168.10.1",
    "authorized_target": True, "iff_verified": False, "threat_level": "MEDIUM",
}


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


# ---------------------------------------------------------------------
# Model validation
# ---------------------------------------------------------------------
def test_mode_pattern_rejects_bogus():
    with pytest.raises(Exception):
        srv.WifiDefeatBody(target_detection_id="det-1", mode="takeover",
                           arm_token="a" * 36, wifi_defeat_confirm_token="b" * 36)


def test_target_detection_id_is_required():
    with pytest.raises(Exception):
        srv.WifiDefeatBody(mode="deauth", arm_token="a" * 36,
                           wifi_defeat_confirm_token="b" * 36)


def test_count_floor_and_ceiling():
    ok = srv.WifiDefeatBody(target_detection_id="det-1", mode="deauth", count=5,
                            arm_token="a" * 36, wifi_defeat_confirm_token="b" * 36)
    assert ok.count == 5
    with pytest.raises(Exception):
        srv.WifiDefeatBody(target_detection_id="det-1", mode="deauth", count=0,
                           arm_token="a" * 36, wifi_defeat_confirm_token="b" * 36)
    with pytest.raises(Exception):
        srv.WifiDefeatBody(target_detection_id="det-1", mode="deauth", count=10_000_000,
                           arm_token="a" * 36, wifi_defeat_confirm_token="b" * 36)


# ---------------------------------------------------------------------
# Distinct effect wiring + effect-per-mode mapping
# ---------------------------------------------------------------------
def test_effects_registered_in_arm_and_range_tuples():
    for eff in ("wifi_deauth", "arsdk_inject"):
        assert eff in srv.ARM_TOKEN_EFFECTS
        assert eff in srv.RANGE_AUTH_EFFECTS
        assert eff in srv._range_authorization  # lease dict initialized


def test_effect_for_mode_mapping():
    assert srv._wifi_defeat_effect_for_mode("deauth") == "wifi_deauth"
    for m in ("arsdk_land", "arsdk_emergency", "tello_land", "tello_emergency"):
        assert srv._wifi_defeat_effect_for_mode(m) == "arsdk_inject"


def test_confirm_token_type_is_separate_from_jam_gnss_and_sdr_inject():
    tok = srv._issue_wifi_defeat_confirm_token()["wifi_defeat_confirm_token"]
    # It lives ONLY in its own dict — not in any other confirm-token dict.
    assert tok in srv._wifi_defeat_confirm_tokens
    assert tok not in srv._jam_confirm_tokens
    assert tok not in srv._gnss_spoof_confirm_tokens
    assert tok not in srv._mavlink_sdr_inject_confirm_tokens
    # Length clears the bridge's MIN_CONFIRM_TOKEN_LEN (20).
    assert len(tok) >= 20


def test_cross_effect_confirm_token_not_accepted_by_wifi_and_vice_versa(monkeypatch):
    """A jam/sdr-inject confirm token is NOT accepted by the wifi_defeat consume,
    and a wifi_defeat confirm token is NOT accepted by the jam/sdr-inject consume."""
    jam_tok = srv._issue_jam_confirm_token()["jam_confirm_token"]
    sdr_tok = srv._issue_mavlink_sdr_inject_confirm_token()["mavlink_sdr_inject_confirm_token"]
    wifi_tok = srv._issue_wifi_defeat_confirm_token()["wifi_defeat_confirm_token"]

    # wifi consume rejects a jam/sdr token (they are not in its dict) -> 422.
    for foreign in (jam_tok, sdr_tok):
        with pytest.raises(srv.HTTPException) as ei:
            srv._consume_wifi_defeat_confirm_token(foreign)
        assert ei.value.status_code == 422

    # jam / sdr consume reject the wifi token -> their own 403.
    with pytest.raises(srv.HTTPException) as ei:
        srv._consume_jam_confirm_token(wifi_tok)
    assert ei.value.status_code == 403
    with pytest.raises(srv.HTTPException) as ei:
        srv._consume_mavlink_sdr_inject_confirm_token(wifi_tok)
    assert ei.value.status_code == 403
    # The wifi token was NOT burned by the foreign consumes above — still valid.
    assert wifi_tok in srv._wifi_defeat_confirm_tokens


# ---------------------------------------------------------------------
# RBAC: operator -> 403
# ---------------------------------------------------------------------
def test_operator_forbidden_403():
    # require_commander is the FastAPI dependency; call it directly to prove an
    # operator is rejected before the handler body ever runs.
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(OPERATOR))
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------
# Spine: tx_halt -> 409 (FIRST gate)
# ---------------------------------------------------------------------
def test_tx_halt_refused_409(monkeypatch):
    _stub_spine(monkeypatch)
    monkeypatch.setattr(srv, "_tx_halted", True)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------
# Spine: arm token — missing / cross-effect / wrong-target rejected (403)
# ---------------------------------------------------------------------
def test_missing_arm_token_refused_403(monkeypatch):
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(arm_token="   ", wifi_defeat_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403


def test_cross_effect_arm_token_refused_403(monkeypatch):
    _stub_spine(monkeypatch)
    jam_tok = srv._issue_arm_token("jam")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(arm_token=jam_tok, wifi_defeat_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403
    assert "effect" in ei.value.detail.lower()


def test_deauth_arm_token_not_accepted_for_inject_mode(monkeypatch):
    """A wifi_deauth arm token cannot fire an arsdk_inject mode (separate effects)."""
    _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    deauth_tok = srv._issue_arm_token("wifi_deauth", "det-1")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(mode="arsdk_land", arm_token=deauth_tok,
                  wifi_defeat_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403
    assert "effect" in ei.value.detail.lower()


def test_wrong_target_arm_token_refused_403(monkeypatch):
    _stub_spine(monkeypatch)
    tok = srv._issue_arm_token("wifi_deauth", "other-target")["arm_token"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(arm_token=tok, wifi_defeat_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 403
    assert "target" in ei.value.detail.lower()


# ---------------------------------------------------------------------
# Spine: confirm token — missing/bad -> 422; single-use
# ---------------------------------------------------------------------
def test_missing_confirm_token_refused_422(monkeypatch):
    _stub_spine(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(wifi_defeat_confirm_token="   "), user=USER))
    assert ei.value.status_code == 422
    assert "confirmation token" in ei.value.detail.lower()


def test_confirm_token_is_single_use(monkeypatch):
    _stub_spine(monkeypatch)
    tok = _confirm()
    asyncio.run(srv.deploy_wifi_defeat(
        _body(arm_token=_arm(), wifi_defeat_confirm_token=tok), user=USER))
    # Re-presenting the same (now-burned) confirm token with a fresh arm token -> 422.
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(arm_token=_arm(), wifi_defeat_confirm_token=tok), user=USER))
    assert ei.value.status_code == 422


# ---------------------------------------------------------------------
# Spine: range-authorization off -> 409
# ---------------------------------------------------------------------
def test_range_auth_off_refused_409(monkeypatch):
    _stub_spine(monkeypatch, range_ok=False)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert ei.value.status_code == 409


# ---------------------------------------------------------------------
# IFF fratricide interlock — a friendly is HARD-BLOCKED without the ack
# ---------------------------------------------------------------------
def test_confirmed_friendly_hard_blocked_without_ack(monkeypatch):
    friendly = {**_PARROT_TARGET, "callsign": "FRND-AP", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    _stub_spine(monkeypatch, detection=friendly)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()


def test_confirmed_friendly_allowed_with_single_use_ack_and_loud_audit(monkeypatch):
    friendly = {**_PARROT_TARGET, "callsign": "FRND-AP", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    events, broadcasts = _stub_spine(monkeypatch, detection=friendly)
    ack = srv._issue_iff_ff_ack("det-1", "cmdr@unused.local")["iff_friendly_fire_ack"]
    resp = asyncio.run(srv.deploy_wifi_defeat(_body(iff_friendly_fire_ack=ack), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert any(e["kind"] == "IFF_FRIENDLY_FIRE_OVERRIDE" for e in events)


def test_friendly_fire_ack_bound_to_target(monkeypatch):
    friendly = {**_PARROT_TARGET, "callsign": "FRND-AP", "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    _stub_spine(monkeypatch, detection=friendly)
    ack_other = srv._issue_iff_ff_ack("some-other-det", "cmdr@unused.local")["iff_friendly_fire_ack"]
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(iff_friendly_fire_ack=ack_other), user=USER))
    assert ei.value.status_code == 403
    assert "fratricide" in ei.value.detail.lower()


# ---------------------------------------------------------------------
# HONESTY gate: deauth against a PMF/802.11w target -> 422 (no-op, no TX)
# ---------------------------------------------------------------------
@pytest.mark.parametrize("pmf_field", ["pmf", "pmf_present", "ieee80211w"])
def test_deauth_against_pmf_target_refused_422(monkeypatch, pmf_field):
    det = {**_PARROT_TARGET, pmf_field: True}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    assert ei.value.status_code == 422
    assert "pmf" in ei.value.detail.lower() or "not applicable" in ei.value.detail.lower()
    # No wifi_defeat_request may be forwarded when the honesty gate refuses.
    assert not [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]


def test_deauth_against_pmf_in_wifi_security_dict_refused_422(monkeypatch):
    det = {**_PARROT_TARGET, "wifi_security": {"required": True}}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    assert ei.value.status_code == 422


def test_deauth_allowed_when_pmf_absent(monkeypatch):
    """Honesty invariant: deauth is a best-effort link-drop and IS allowed when
    PMF is not positively indicated (absence != PMF-present)."""
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]


# ---------------------------------------------------------------------
# HONESTY gate: inject against an encrypted / non-Parrot-Tello target -> 422
# ---------------------------------------------------------------------
def test_arsdk_inject_against_encrypted_target_refused_422(monkeypatch):
    det = {**_PARROT_TARGET, "encrypted": True}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="arsdk_land"), user=USER))
    assert ei.value.status_code == 422
    assert "encrypted" in ei.value.detail.lower() or "not applicable" in ei.value.detail.lower()
    assert not [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]


def test_arsdk_inject_against_non_parrot_target_refused_422(monkeypatch):
    """A generic / DJI OcuSync Wi-Fi contact with no Parrot/ARSDK identity fails
    closed for an ARSDK inject."""
    det = {"id": "det-1", "callsign": "UNK-1", "protocol": "DJI OcuSync",
           "control_link_family": "DJI OcuSync", "encrypted": False,
           "bssid": "AA:BB:CC:00:11:22", "channel": 6,
           "authorized_target": True, "iff_verified": False, "threat_level": "MEDIUM"}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="arsdk_land"), user=USER))
    assert ei.value.status_code == 422
    assert "cannot confirm" in ei.value.detail.lower() or "not applicable" in ei.value.detail.lower()


def test_arsdk_token_and_mode_against_tello_only_target_refused_422(monkeypatch):
    """An ARSDK inject mode requires an ARSDK marker — a Tello-only target (Tello
    is NOT ARSDK3) fails the arsdk identity check, fail-closed."""
    _stub_spine(monkeypatch, detection=_TELLO_TARGET)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(
            _body(mode="arsdk_land",
                  arm_token=_arm("arsdk_inject", "det-1"),
                  wifi_defeat_confirm_token=_confirm()), user=USER))
    assert ei.value.status_code == 422


def test_tello_inject_against_identified_tello_accepted(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch, detection=_TELLO_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(
        _body(mode="tello_land",
              arm_token=_arm("arsdk_inject", "det-1"),
              wifi_defeat_confirm_token=_confirm()), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    reqs = [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]
    assert reqs and reqs[0]["mode"] == "tello_land"


# ---------------------------------------------------------------------
# FRATRICIDE-CRITICAL: absent / broadcast BSSID -> 422 (fail-closed, no TX)
# ---------------------------------------------------------------------
def test_absent_bssid_refused_422(monkeypatch):
    det = {k: v for k, v in _PARROT_TARGET.items() if k != "bssid"}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    assert ei.value.status_code == 422
    assert "bssid" in ei.value.detail.lower()
    assert not [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]


def test_broadcast_bssid_refused_422(monkeypatch):
    det = {**_PARROT_TARGET, "bssid": "FF:FF:FF:FF:FF:FF"}
    events, broadcasts = _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    assert ei.value.status_code == 422
    assert not [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]


# ---------------------------------------------------------------------
# Non-friendly unauthorized target / unknown detection
# ---------------------------------------------------------------------
def test_non_friendly_unauthorized_target_refused_403(monkeypatch):
    det = {**_PARROT_TARGET, "authorized_target": False}
    _stub_spine(monkeypatch, detection=det)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert ei.value.status_code == 403
    assert "not authorized" in ei.value.detail.lower()


def test_unknown_target_detection_404(monkeypatch):
    _stub_spine(monkeypatch, detection={"id": "someone-else"})
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert ei.value.status_code == 404


# ---------------------------------------------------------------------
# Happy path: full valid chain -> accepted + forwarded to the bridge consumer
# ---------------------------------------------------------------------
def test_happy_path_deauth_audits_distinctly_and_forwards(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))

    assert resp["status"] == "AWAITING_ACK"
    assert resp["mode"] == "deauth"
    assert resp["effect"] == "wifi_deauth"
    assert resp["target_bssid"] == "90:3A:E6:00:11:22"
    assert resp["continuous"] is True  # deauth with no count is continuous

    # Audited DISTINCTLY: a WIFI_DEFEAT event whose meta.mode == "deauth".
    df = [e for e in events if e["kind"] == "WIFI_DEFEAT" and e["meta"].get("mode") == "deauth"
          and not e["meta"].get("not_applicable") and not e["meta"].get("refused")]
    assert df, "expected a WIFI_DEFEAT fire-request audit event carrying meta.mode"
    assert df[0]["meta"]["target_bssid"] == "90:3A:E6:00:11:22"

    # Forwarded to the wifi-defeat bridge consumer with the target scope + confirm.
    reqs = [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]
    assert len(reqs) == 1
    assert reqs[0]["mode"] == "deauth"
    assert reqs[0]["target_bssid"] == "90:3A:E6:00:11:22"
    assert reqs[0]["channel"] == 6
    assert reqs[0]["target_bssid"] != "FF:FF:FF:FF:FF:FF"
    # confirm token is forwarded (already consumed) as the bridge's evidence and
    # clears the bridge's MIN_CONFIRM_TOKEN_LEN.
    assert len(reqs[0]["wifi_defeat_confirm_token"]) >= 20


def test_happy_path_arsdk_inject_forwards(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(
        _body(mode="arsdk_emergency",
              arm_token=_arm("arsdk_inject", "det-1"),
              wifi_defeat_confirm_token=_confirm()), user=USER))
    assert resp["status"] == "AWAITING_ACK"
    assert resp["effect"] == "arsdk_inject"
    reqs = [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]
    assert reqs and reqs[0]["mode"] == "arsdk_emergency"


# ---------------------------------------------------------------------
# SAFETY: no path fires (forwards a wifi_defeat_request) without the FULL
# arm + confirm + IFF + range-auth + tx-halt chain intact.
# ---------------------------------------------------------------------
def test_no_ungated_fire_path(monkeypatch):
    """Each single broken gate must prevent the wifi_defeat_request forward; only
    the fully-armed chain forwards. Proves there is no un-gated TX path."""

    def _forwarded(broadcasts):
        return [b for b in broadcasts if b.get("type") == "wifi_defeat_request"]

    # 1. tx_halted -> no forward
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    monkeypatch.setattr(srv, "_tx_halted", True)
    with pytest.raises(srv.HTTPException):
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert not _forwarded(broadcasts)
    monkeypatch.setattr(srv, "_tx_halted", False)

    # 2. missing arm token -> no forward
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    with pytest.raises(srv.HTTPException):
        asyncio.run(srv.deploy_wifi_defeat(
            _body(arm_token="   ", wifi_defeat_confirm_token=_confirm()), user=USER))
    assert not _forwarded(broadcasts)

    # 3. missing confirm token -> no forward
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    with pytest.raises(srv.HTTPException):
        asyncio.run(srv.deploy_wifi_defeat(
            _body(wifi_defeat_confirm_token="   "), user=USER))
    assert not _forwarded(broadcasts)

    # 4. range-auth off -> no forward
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET, range_ok=False)
    with pytest.raises(srv.HTTPException):
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert not _forwarded(broadcasts)

    # 5. confirmed-friendly, no ff-ack -> no forward
    friendly = {**_PARROT_TARGET, "iff_verified": True,
                "threat_level": "FRIENDLY (IFF verified)"}
    events, broadcasts = _stub_spine(monkeypatch, detection=friendly)
    with pytest.raises(srv.HTTPException):
        asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert not _forwarded(broadcasts)

    # 6. full valid chain -> exactly one forward
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    asyncio.run(srv.deploy_wifi_defeat(_body(), user=USER))
    assert len(_forwarded(broadcasts)) == 1


# ---------------------------------------------------------------------
# The ack handler transitions pending state (closes the false-green gap)
# ---------------------------------------------------------------------
def test_wifi_defeat_ack_transitions_pending(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch, detection=_PARROT_TARGET)
    resp = asyncio.run(srv.deploy_wifi_defeat(_body(mode="deauth"), user=USER))
    rid = resp["request_id"]
    assert srv._pending_wifi_defeat[rid]["status"] == "AWAITING_ACK"
    asyncio.run(srv._handle_wifi_defeat_ack({"type": "wifi_defeat_ack",
                                             "request_id": rid, "phase": "started", "ok": True}))
    assert srv._pending_wifi_defeat[rid]["status"] == "WIFI_DEFEAT_ACTIVE"
    asyncio.run(srv._handle_wifi_defeat_ack({"type": "wifi_defeat_ack",
                                             "request_id": rid, "phase": "failed", "ok": False,
                                             "error": "pin fail-closed"}))
    assert srv._pending_wifi_defeat[rid]["status"] == "TX_FAILED"
    assert srv._pending_wifi_defeat[rid]["terminal"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
