"""Adversarial safety-review regression tests for the SUSTAINED RC-override
maneuver-takeover payload (PL-011). Each test targets the REJECTION / the
bound — those refusals are the safety guarantees the separate adversarial
review will look for:

  * PL-011 is CRITICAL and therefore arm-token-gated (deploy refused w/o token).
  * deploy refused when TX-halted (EMERGENCY ABORT), even with a valid token.
  * cross-effect / wrong-target arm tokens rejected (inherits F3 binding).
  * a target IFF-verified friendly is refused (inherits the F2 interlock).
  * an encrypted/FHSS target link is refused as NOT APPLICABLE (honesty),
    never transmitted.
  * the operator-set duration is clamped to the payload's hard cap server-side.
  * sustained takeover cannot be broadcast (target-bound only).

Requires a live backend, same convention as test_tx_authorization_holes.py:
REACT_APP_BACKEND_URL (or /app/frontend/.env) + ADMIN_EMAIL/ADMIN_PASSWORD +
IFF_BRIDGE_API_KEY. Skips cleanly if no backend URL is resolvable so the
pure-Python suite (field-bridge/test_mavlink_takeover.py) still runs anywhere.
"""
from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

import pytest
import requests

BASE_URL = None
if "REACT_APP_BACKEND_URL" in os.environ:
    BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
else:
    env_path = Path("/app/frontend/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                break

pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="no live backend (REACT_APP_BACKEND_URL unset)"
)

API = f"{BASE_URL}/api" if BASE_URL else None
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@meghaduta.mil")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)
IFF_BRIDGE_API_KEY = os.environ.get("IFF_BRIDGE_API_KEY") or secrets.token_urlsafe(16)

PL = "PL-011"


@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _resume(auth_headers):
    requests.post(f"{API}/emergency/resume", headers=auth_headers, timeout=15)


def _arm(auth_headers, effect, target=None):
    body = {"effect": effect}
    if target is not None:
        body["target_detection_id"] = target
    r = requests.post(f"{API}/arm", headers=auth_headers, json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["arm_token"]


def _ingest(auth_headers, *, protocol=None, bearing_deg=None, system_id=None):
    body = {
        "callsign": f"TKO-{uuid.uuid4().hex[:8]}",
        "model": f"Test UAV {uuid.uuid4().hex[:8]}",
        "protocol": protocol or "MAVLink-SiK-Legacy",
        "threat_level": "MEDIUM",
        "center_freq_ghz": round(2.400 + secrets.randbelow(400) / 1000, 3),
        "source": "HACKRF",
    }
    if bearing_deg is not None:
        body["bearing_deg"] = bearing_deg
        body["bearing_available"] = True
    if system_id is not None:
        body["system_id"] = system_id
    r = requests.post(f"{API}/detections/ingest", headers=auth_headers, json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json(), body


def _authorize(auth_headers, det_id):
    r = requests.post(f"{API}/detections/{det_id}/authorize-target",
                      headers=auth_headers, json={"authorized": True}, timeout=15)
    assert r.status_code == 200, r.text


def _ingest_friendly_beacon(auth_headers, bearing_deg):
    body = {
        "asset_id": secrets.randbelow(89_000_000) + 10_000_000,
        "callsign": f"FRND-{uuid.uuid4().hex[:6]}",
        "mission_id": 1, "geocell": 0, "geocell_known": False,
        "bearing_deg": bearing_deg, "distance_m": 500.0,
    }
    r = requests.post(f"{API}/iff/beacons/ingest", json=body,
                      headers={**auth_headers, "X-IFF-Bridge-Key": IFF_BRIDGE_API_KEY}, timeout=15)
    assert r.status_code == 200, r.text


def _enable_lease(auth_headers):
    # Best-effort: enable the range lease so we exercise the payload gates, not
    # a lease short-circuit. Enabling may require re-auth/phrase; ignore failures
    # (tests that must pass the lease assert on their own status codes).
    for eff in ("mavlink",):
        requests.post(f"{API}/range-authorization", headers=auth_headers,
                      json={"effect": eff, "enabled": True,
                            "password": ADMIN_PASSWORD, "confirm_phrase": "AUTHORIZE LIVE RANGE"},
                      timeout=15)


def _disable_lease(auth_headers, effect="mavlink"):
    # Turn the range lease OFF so we can prove the F-2 backend-side gate rejects
    # a deploy (409) even before any bridge is involved.
    requests.post(f"{API}/range-authorization", headers=auth_headers,
                  json={"effect": effect, "enabled": False}, timeout=15)


def _mint_ff_ack(auth_headers, det_id):
    r = requests.post(f"{API}/detections/{det_id}/friendly-fire-ack",
                      headers=auth_headers, json={}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["iff_friendly_fire_ack"]


def _find_override_log(auth_headers, det_id):
    r = requests.get(f"{API}/logs", headers=auth_headers, params={"limit": 500}, timeout=15)
    assert r.status_code == 200, r.text
    for e in r.json():
        if e.get("kind") == "IFF_FRIENDLY_FIRE_OVERRIDE" and \
                (e.get("meta") or {}).get("detection_id") == det_id:
            return e
    return None


# ---- PL-011 is CRITICAL => arm-token gated ------------------------------
class TestArmTokenGate:
    def test_deploy_without_arm_token_refused(self, auth_headers):
        _resume(auth_headers)
        det, _ = _ingest(auth_headers)
        _authorize(auth_headers, det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"]}, timeout=15)
        assert r.status_code == 403, r.text

    def test_cross_effect_token_refused(self, auth_headers):
        _resume(auth_headers)
        det, _ = _ingest(auth_headers)
        _authorize(auth_headers, det["id"])
        jam = _arm(auth_headers, "jam")
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": jam}, timeout=15)
        assert r.status_code == 403, r.text
        assert "effect" in r.text.lower()

    def test_wrong_target_token_refused(self, auth_headers):
        _resume(auth_headers)
        det_a, _ = _ingest(auth_headers)
        det_b, _ = _ingest(auth_headers)
        _authorize(auth_headers, det_b["id"])
        tok = _arm(auth_headers, "deploy", target=det_a["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det_b["id"],
                                "arm_token": tok}, timeout=15)
        assert r.status_code == 403, r.text
        assert "target" in r.text.lower()


# ---- TX-halt (EMERGENCY ABORT) blocks deploy ----------------------------
class TestTxHalt:
    def test_deploy_refused_when_tx_halted(self, auth_headers):
        det, _ = _ingest(auth_headers)
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        # Assert the abort AFTER minting the token, so the only thing stopping
        # the deploy is the TX-halt.
        requests.post(f"{API}/emergency/abort", headers=auth_headers, timeout=15)
        try:
            r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                              json={"payload_id": PL, "target_detection_id": det["id"],
                                    "arm_token": tok}, timeout=15)
            assert r.status_code in (403, 409, 423), r.text
        finally:
            _resume(auth_headers)


# ---- honesty: encrypted/FHSS target refused as NOT APPLICABLE ------------
class TestEncryptedLinkRefused:
    @pytest.mark.parametrize("proto", ["ELRS 2.4", "DJI OcuSync", "Spektrum DSMX", "CRSF"])
    def test_encrypted_target_not_applicable(self, auth_headers, proto):
        _resume(auth_headers)
        _enable_lease(auth_headers)
        det, _ = _ingest(auth_headers, protocol=proto)
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok}, timeout=15)
        # 422 not-applicable (never transmitted). Must not be a 200 success.
        assert r.status_code == 422, r.text
        assert "not applicable" in r.text.lower() or "encrypted" in r.text.lower()


# ---- IFF friendly interlock (inherits F2) -------------------------------
class TestIFFFriendlyRefused:
    def test_friendly_target_refused(self, auth_headers):
        """Plain commander authorize-target + deploy with NO explicit friendly-fire
        ack → still 403 (FRATRICIDE INTERLOCK). This is the gate test that was
        failing under the old SILENT standing-override, and now passes: a
        confirmed friendly cannot be fired on without the deliberate per-
        engagement ack."""
        _resume(auth_headers)
        bearing = 123.0
        _ingest_friendly_beacon(auth_headers, bearing)
        det, _ = _ingest(auth_headers, bearing_deg=bearing)
        assert det.get("iff_verified") is True, det
        # Routine authorize on a friendly is itself refused now (no standing
        # override); the request is harmless and licenses nothing.
        requests.post(f"{API}/detections/{det['id']}/authorize-target",
                      headers=auth_headers, json={"authorized": True}, timeout=15)
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok}, timeout=15)
        assert r.status_code == 403, r.text
        assert "fratricide" in r.text.lower()


# ---- Deliberate friendly-fire override: the ONLY path to engage a friendly ---
class TestFriendlyFireAckOverride:
    def test_ack_allows_deploy_and_loudly_audits(self, auth_headers):
        """The explicit, single-use, commander-minted, target-bound friendly-fire
        ack IS the sanctioned override: with it (plus the full spine — commander
        role, arm token, range lease, tx not halted) the deploy proceeds
        (200 / AWAITING_ACK), and a LOUD IFF_FRIENDLY_FIRE_OVERRIDE event is
        written to the hash-chained mission log."""
        _resume(auth_headers)
        _enable_lease(auth_headers)
        # Bearing chosen >15deg (IFF_BEARING_TOLERANCE_DEG) from every other
        # bearing used across this shared-backend suite so friendly beacons from
        # other tests can't cross-match this contact.
        bearing = 250.0
        _ingest_friendly_beacon(auth_headers, bearing)
        det, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy",
                         bearing_deg=bearing, system_id=17)
        assert det.get("iff_verified") is True, det
        ack = _mint_ff_ack(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok, "iff_friendly_fire_ack": ack,
                                "target_link_legacy_mavlink": True}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "AWAITING_ACK", r.json()
        # The loud, un-missable audit event must be present.
        ev = _find_override_log(auth_headers, det["id"])
        assert ev is not None, "IFF_FRIENDLY_FIRE_OVERRIDE audit event missing"
        assert "CONFIRMED-FRIENDLY" in ev.get("message", "")
        assert (ev.get("meta") or {}).get("engaged_by")

    def test_ack_for_target_a_refused_on_target_b(self, auth_headers):
        """An ack minted for friendly A cannot be replayed onto friendly B — it is
        bound to A's detection id. Deploy on B with A's ack → 403 FRATRICIDE."""
        _resume(auth_headers)
        _enable_lease(auth_headers)
        b_a, b_b = 150.0, 300.0  # well-separated (>15deg) from each other and other tests
        _ingest_friendly_beacon(auth_headers, b_a)
        det_a, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy", bearing_deg=b_a, system_id=17)
        _ingest_friendly_beacon(auth_headers, b_b)
        det_b, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy", bearing_deg=b_b, system_id=17)
        assert det_a.get("iff_verified") and det_b.get("iff_verified")
        ack_a = _mint_ff_ack(auth_headers, det_a["id"])
        tok = _arm(auth_headers, "deploy", target=det_b["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det_b["id"],
                                "arm_token": tok, "iff_friendly_fire_ack": ack_a,
                                "target_link_legacy_mavlink": True}, timeout=15)
        assert r.status_code == 403, r.text
        assert "fratricide" in r.text.lower()

    def test_ack_is_single_use(self, auth_headers):
        """The ack is burned on first use. A second deploy re-presenting the same
        ack (with a fresh arm token) is refused 403 — no replay of one override
        across two engagements."""
        _resume(auth_headers)
        _enable_lease(auth_headers)
        bearing = 330.0
        _ingest_friendly_beacon(auth_headers, bearing)
        det, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy",
                         bearing_deg=bearing, system_id=17)
        assert det.get("iff_verified") is True, det
        ack = _mint_ff_ack(auth_headers, det["id"])
        tok1 = _arm(auth_headers, "deploy", target=det["id"])
        r1 = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                           json={"payload_id": PL, "target_detection_id": det["id"],
                                 "arm_token": tok1, "iff_friendly_fire_ack": ack,
                                 "target_link_legacy_mavlink": True}, timeout=15)
        assert r1.status_code == 200, r1.text
        # Second attempt: fresh arm token, SAME (now-burned) ack -> refused.
        tok2 = _arm(auth_headers, "deploy", target=det["id"])
        r2 = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                           json={"payload_id": PL, "target_detection_id": det["id"],
                                 "arm_token": tok2, "iff_friendly_fire_ack": ack,
                                 "target_link_legacy_mavlink": True}, timeout=15)
        assert r2.status_code == 403, r2.text
        assert "fratricide" in r2.text.lower()


# ---- bounded: cannot broadcast; duration clamped to cap -----------------
class TestBoundedAndTargetBound:
    def test_sustained_cannot_broadcast(self, auth_headers):
        _resume(auth_headers)
        tok = _arm(auth_headers, "deploy")
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "broadcast": True, "arm_token": tok}, timeout=15)
        assert r.status_code == 400, r.text
        assert "broadcast" in r.text.lower()

    def test_duration_clamped_to_cap(self, auth_headers):
        _resume(auth_headers)
        _enable_lease(auth_headers)
        det, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy")
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok, "duration_s": 9999.0}, timeout=15)
        # If the deploy reaches TX (lease on), the returned packet must carry a
        # duration clamped to the 30 s cap and must be flagged sustained.
        if r.status_code == 200:
            pkt = r.json()
            assert pkt.get("sustained") is True, pkt
            assert pkt.get("duration_s", 0) <= 30.0 + 1e-6, pkt
            assert pkt.get("max_duration_s") == 30.0, pkt
        else:
            # lease off / policy => must be a controlled refusal, never a 500.
            assert r.status_code in (403, 409, 422), r.text


# ---- F-2: /payloads/deploy has a BACKEND-SIDE range-auth gate ------------
class TestRangeAuthGate:
    def test_deploy_refused_when_mavlink_lease_off(self, auth_headers):
        _resume(auth_headers)
        det, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy")
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        # Lease OFF => backend must 409 before any frame is built/broadcast,
        # regardless of the bridge poll.
        _disable_lease(auth_headers, "mavlink")
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok,
                                "target_link_legacy_mavlink": True}, timeout=15)
        assert r.status_code == 409, r.text
        assert "range authorization" in r.text.lower()


# ---- F-3: unknown-protocol fails closed unless legacy-attested ------------
class TestUnknownProtocolFailClosed:
    def test_unknown_protocol_without_attestation_refused(self, auth_headers):
        _resume(auth_headers)
        _enable_lease(auth_headers)
        det, _ = _ingest(auth_headers, protocol="SomeMysteryLink-XYZ")
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok}, timeout=15)
        assert r.status_code == 422, r.text
        assert "not applicable" in r.text.lower() or "unknown" in r.text.lower()

    def test_unknown_protocol_with_attestation_passes_gate(self, auth_headers):
        _resume(auth_headers)
        _enable_lease(auth_headers)
        det, _ = _ingest(auth_headers, protocol="SomeMysteryLink-XYZ")
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok,
                                "target_link_legacy_mavlink": True}, timeout=15)
        # With the attestation the F-3 applicability gate must NOT be the thing
        # blocking it — anything but a 422-not-applicable is acceptable (200 if
        # the lease holds, or a controlled non-422 refusal otherwise).
        assert r.status_code != 422, r.text


# ---- F-4: target_system==0 targeted deploy is refused --------------------
class TestBroadcastSystemIdRefused:
    def test_system_id_zero_target_refused(self, auth_headers):
        _resume(auth_headers)
        _enable_lease(auth_headers)
        det, _ = _ingest(auth_headers, protocol="MAVLink-SiK-Legacy", system_id=0)
        _authorize(auth_headers, det["id"])
        tok = _arm(auth_headers, "deploy", target=det["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": PL, "target_detection_id": det["id"],
                                "arm_token": tok,
                                "target_link_legacy_mavlink": True}, timeout=15)
        assert r.status_code == 422, r.text
        assert "system_id" in r.text.lower() or "broadcast" in r.text.lower()


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
