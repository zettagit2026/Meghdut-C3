"""Adversarial safety-review regression tests for the RF-transmit
authorization chain (2026-08). Each test PROVES one of five previously-open
friendly-fire / gate-bypass holes is now closed. The assertions deliberately
target the REJECTION (the gate holding) — those refusals are the safety
guarantees.

Findings covered:
  F1 — targeted /mavlink/broadcast bypassed arm-token + IFF interlock.
  F2 — fire-time IFF TOCTOU: a target authorized while hostile could be
       fired on after it became IFF-verified friendly.
  F3 — arm token was unbound to effect/target (cross-effect/target reuse).
  F4 — transmit endpoints did not themselves enforce the range-auth lease.
  F5 — DELETE /detections/{id} was gated with get_current_user (any operator).

Requires a live backend (same convention as test_new_endpoints.py /
test_iff_revocation.py): REACT_APP_BACKEND_URL env var or /app/frontend/.env,
plus ADMIN_EMAIL/ADMIN_PASSWORD and IFF_BRIDGE_API_KEY the backend was booted
with. The seeded admin account is role="commander" (see server.py startup()).
"""
from __future__ import annotations

import os
import secrets
import uuid
from pathlib import Path

import pytest
import requests

if "REACT_APP_BACKEND_URL" in os.environ:
    BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
else:
    env_txt = Path("/app/frontend/.env").read_text()
    BASE_URL = None
    for line in env_txt.splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL not resolvable"

API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@meghaduta.mil")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)
IFF_BRIDGE_API_KEY = os.environ.get("IFF_BRIDGE_API_KEY") or secrets.token_urlsafe(16)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module", autouse=True)
def _tx_resumed(auth_headers: dict):
    # Backend boots TX-HALTED (fail-closed). Clear the halt so we reach the
    # gates under test rather than short-circuiting on _check_tx_not_halted.
    requests.post(f"{API}/emergency/resume", headers=auth_headers, timeout=15)


@pytest.fixture(scope="module")
def operator_headers(auth_headers: dict) -> dict:
    """A NON-commander (operator) account, minted by the seeded commander, used
    to prove the friendly-fire ack mint is commander-gated (role refusal)."""
    email = f"op-{uuid.uuid4().hex[:8]}@meghaduta.mil"
    pw = "Operator-Test-Pw-" + uuid.uuid4().hex[:10]
    cr = requests.post(f"{API}/users", headers=auth_headers,
                       json={"email": email, "password": pw, "role": "operator"}, timeout=15)
    assert cr.status_code in (201, 409), cr.text
    lr = requests.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    assert lr.status_code == 200, lr.text
    return {"Authorization": f"Bearer {lr.json()['token']}"}


def _mint_ff_ack(auth_headers: dict, det_id: str):
    """POST the single-use friendly-fire ack mint for det_id; return the Response."""
    return requests.post(f"{API}/detections/{det_id}/friendly-fire-ack",
                         headers=auth_headers, json={}, timeout=15)


def _find_log_kind(auth_headers: dict, kind: str, det_id: str) -> bool:
    r = requests.get(f"{API}/logs", headers=auth_headers, params={"limit": 500}, timeout=15)
    assert r.status_code == 200, r.text
    for e in r.json():
        if e.get("kind") == kind and (e.get("meta") or {}).get("detection_id") == det_id:
            return True
    return False


def _arm(auth_headers: dict, effect: str, target_detection_id: str | None = None) -> str:
    body: dict = {"effect": effect}
    if target_detection_id is not None:
        body["target_detection_id"] = target_detection_id
    r = requests.post(f"{API}/arm", headers=auth_headers, json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["arm_token"]


def _ingest_detection(auth_headers: dict, *, bearing_deg: float | None = None,
                      model: str | None = None, protocol: str | None = None,
                      callsign: str | None = None) -> dict:
    body = {
        "callsign": callsign or f"TXHOLE-{uuid.uuid4().hex[:8]}",
        "model": model or f"Test UAV {uuid.uuid4().hex[:8]}",
        "protocol": protocol or "Test-Protocol",
        "threat_level": "MEDIUM",
        "center_freq_ghz": round(2.400 + secrets.randbelow(400) / 1000, 3),
        "source": "HACKRF",
    }
    if bearing_deg is not None:
        body["bearing_deg"] = bearing_deg
        body["bearing_available"] = True
    r = requests.post(f"{API}/detections/ingest", headers=auth_headers, json=body, timeout=15)
    assert r.status_code == 200, r.text
    return r.json(), body  # return the raw body too so callers can re-ingest (merge)


def _ingest_friendly_beacon(auth_headers: dict, bearing_deg: float) -> int:
    asset_id = secrets.randbelow(89_000_000) + 10_000_000
    body = {
        "asset_id": asset_id,
        "callsign": f"FRND-{uuid.uuid4().hex[:6]}",
        "mission_id": 1,
        "geocell": 0,
        "geocell_known": False,
        "bearing_deg": bearing_deg,
        "distance_m": 500.0,
    }
    r = requests.post(f"{API}/iff/beacons/ingest", json=body,
                      headers={**auth_headers, "X-IFF-Bridge-Key": IFF_BRIDGE_API_KEY}, timeout=15)
    assert r.status_code == 200, r.text
    return asset_id


def _disable_lease(auth_headers: dict, effect: str) -> None:
    # Disabling a range-auth lease needs no password/phrase (§2.3 of the
    # redesign) — makes the "lease off" precondition deterministic.
    r = requests.post(f"{API}/range-authorization",
                      headers=auth_headers, json={"effect": effect, "enabled": False}, timeout=15)
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------------
# F1 — targeted /mavlink/broadcast must not bypass the interlock
# --------------------------------------------------------------------------
class TestF1TargetedBroadcast:
    def test_targeted_broadcast_refused(self, auth_headers):
        """A non-zero target_system inject is refused (must route through
        /payloads/deploy) — no arbitrary targeted COMMAND_LONG escapes."""
        arm = _arm(auth_headers, "mavlink")
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers,
                          json={"target_system": 5, "message_id": 76, "command": 400,
                                "arm_token": arm}, timeout=15)
        assert r.status_code == 403, r.text
        assert "payloads/deploy" in r.text

    def test_broadcast_requires_arm_token_even_targeted(self, auth_headers):
        """Even a targeted call with NO arm token is refused (previously a
        non-zero target_system skipped the token entirely)."""
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers,
                          json={"target_system": 5, "message_id": 76, "command": 400}, timeout=15)
        assert r.status_code == 403, r.text

    def test_broadcast_zero_requires_arm_token(self, auth_headers):
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers,
                          json={"target_system": 0, "message_id": 76, "command": 400}, timeout=15)
        assert r.status_code == 403, r.text
        assert "arm" in r.text.lower()


# --------------------------------------------------------------------------
# F3 — arm token bound to effect (and target)
# --------------------------------------------------------------------------
class TestF3ArmTokenBinding:
    def test_cross_effect_token_rejected(self, auth_headers):
        """A token minted for effect=jam must be rejected on /mavlink/broadcast
        (effect=mavlink). This runs before the range-auth check, so it holds
        regardless of lease state."""
        jam_token = _arm(auth_headers, "jam")
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers,
                          json={"target_system": 0, "message_id": 76, "command": 400,
                                "arm_token": jam_token}, timeout=15)
        assert r.status_code == 403, r.text
        assert "effect" in r.text.lower()

    def test_cross_target_deploy_token_rejected(self, auth_headers):
        """A deploy token bound to detection A must be rejected when spent on
        detection B."""
        det_a, _ = _ingest_detection(auth_headers)
        det_b, _ = _ingest_detection(auth_headers)
        requests.post(f"{API}/detections/{det_b['id']}/authorize-target",
                      headers=auth_headers, json={"authorized": True}, timeout=15)
        tok = _arm(auth_headers, "deploy", target_detection_id=det_a["id"])
        r = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                          json={"payload_id": "PL-005", "target_detection_id": det_b["id"],
                                "arm_token": tok}, timeout=15)
        assert r.status_code == 403, r.text
        assert "target" in r.text.lower()

    def test_arm_requires_effect(self, auth_headers):
        """POST /arm with no effect is a 422 (effect is required + bound)."""
        r = requests.post(f"{API}/arm", headers=auth_headers, json={}, timeout=15)
        assert r.status_code == 422, r.text


# --------------------------------------------------------------------------
# F2 — fire-time IFF TOCTOU / fratricide
# --------------------------------------------------------------------------
class TestF2FireTimeIFF:
    def test_authorized_then_friendly_is_refused(self, auth_headers):
        """Authorize a hostile contact, then flip it to IFF-verified friendly
        via a bearing-consistent friendly beacon + re-ingest. A subsequent
        deploy MUST be refused (403)."""
        bearing = 90.0
        det, raw = _ingest_detection(auth_headers, bearing_deg=bearing)
        # Sanity: created hostile (not friendly), then authorize it.
        assert not det.get("iff_verified")
        ar = requests.post(f"{API}/detections/{det['id']}/authorize-target",
                           headers=auth_headers, json={"authorized": True}, timeout=15)
        assert ar.status_code == 200, ar.text

        # Now a friendly beacon appears at the same bearing; re-ingesting the
        # SAME contact (same model/protocol merge key) flips it to friendly.
        _ingest_friendly_beacon(auth_headers, bearing_deg=bearing)
        r2 = requests.post(f"{API}/detections/ingest", headers=auth_headers, json=raw, timeout=15)
        assert r2.status_code == 200, r2.text
        flipped = r2.json()
        assert flipped.get("iff_verified") is True, flipped
        # F2(b): the stale authorization must have been cleared on the flip.
        assert flipped.get("authorized_target") in (False, None), flipped

        # Fire — must be refused (belt: cleared auth AND/OR fire-time IFF check).
        tok = _arm(auth_headers, "deploy", target_detection_id=det["id"])
        r3 = requests.post(f"{API}/payloads/deploy", headers=auth_headers,
                           json={"payload_id": "PL-005", "target_detection_id": det["id"],
                                 "arm_token": tok}, timeout=15)
        assert r3.status_code == 403, r3.text

    def test_routine_authorize_of_friendly_is_refused_even_for_commander(self, auth_headers):
        """RECONCILED to the deliberate-ack model (was
        test_operator_cannot_authorize_friendly_via_role, which asserted the OLD
        SILENT standing-override behavior). Routine authorize-target can NEVER
        license firing on a confirmed friendly — not even for a commander. The
        old commander-override path (which silently set a standing
        iff_override_authorized=True) is GONE. The refusal points the caller at
        the only sanctioned path: an explicit single-use friendly-fire ack."""
        bearing = 200.0
        _ingest_friendly_beacon(auth_headers, bearing_deg=bearing)
        det, _ = _ingest_detection(auth_headers, bearing_deg=bearing)
        assert det.get("iff_verified") is True, det
        # Even the commander is refused the routine authorize on a friendly.
        r = requests.post(f"{API}/detections/{det['id']}/authorize-target",
                          headers=auth_headers, json={"authorized": True}, timeout=15)
        assert r.status_code == 403, r.text
        assert "friendly-fire ack" in r.text.lower()
        # And the detection was NOT left authorized by that refused call.
        g = requests.get(f"{API}/detections", headers=auth_headers, timeout=15)
        if g.status_code == 200:
            match = [d for d in g.json() if d.get("id") == det["id"]]
            if match:
                assert not match[0].get("authorized_target"), match[0]


# --------------------------------------------------------------------------
# Friendly-fire ack lifecycle: mint -> bind -> single-use -> role-gated
# (the ONLY path to firing on a confirmed friendly; replaces the old silent
# standing iff_override_authorized flag)
# --------------------------------------------------------------------------
class TestFriendlyFireAckLifecycle:
    def test_operator_cannot_mint_ack(self, auth_headers, operator_headers):
        """The single-use friendly-fire ack MUST be commander-minted. An operator
        requesting it is refused with a role error (require_commander => 403)."""
        bearing = 41.0
        _ingest_friendly_beacon(auth_headers, bearing_deg=bearing)
        det, _ = _ingest_detection(auth_headers, bearing_deg=bearing)
        assert det.get("iff_verified") is True, det
        r = _mint_ff_ack(operator_headers, det["id"])
        assert r.status_code == 403, r.text
        assert "commander" in r.text.lower()

    def test_ack_only_for_confirmed_friendly(self, auth_headers):
        """Minting an ack for a NON-friendly contact is refused (400) — the ack
        is meaningful only as a fratricide override on a confirmed friendly."""
        det, _ = _ingest_detection(auth_headers)  # hostile, not iff_verified
        assert not det.get("iff_verified")
        r = _mint_ff_ack(auth_headers, det["id"])
        assert r.status_code == 400, r.text

    def test_ack_minted_by_commander_is_target_bound(self, auth_headers):
        """A commander CAN mint the ack for a confirmed friendly; the mint is
        loudly audited (IFF_FRIENDLY_FIRE_ACK_MINTED) and returns a token bound
        to that exact detection."""
        bearing = 61.0
        _ingest_friendly_beacon(auth_headers, bearing_deg=bearing)
        det, _ = _ingest_detection(auth_headers, bearing_deg=bearing)
        assert det.get("iff_verified") is True, det
        r = _mint_ff_ack(auth_headers, det["id"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("iff_friendly_fire_ack")
        assert body.get("target_detection_id") == det["id"], body
        assert _find_log_kind(auth_headers, "IFF_FRIENDLY_FIRE_ACK_MINTED", det["id"])


# --------------------------------------------------------------------------
# F4 — backend-side range-authorization lease enforcement
# --------------------------------------------------------------------------
class TestF4RangeAuthEnforcement:
    def test_mavlink_broadcast_409_when_lease_off(self, auth_headers):
        _disable_lease(auth_headers, "mavlink")
        arm = _arm(auth_headers, "mavlink")
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers,
                          json={"target_system": 0, "message_id": 76, "command": 400,
                                "arm_token": arm}, timeout=15)
        assert r.status_code == 409, r.text
        assert "range authorization" in r.text.lower()

    def test_jam_409_when_lease_off(self, auth_headers):
        _disable_lease(auth_headers, "jam")
        arm = _arm(auth_headers, "jam")
        confirm = requests.post(f"{API}/jam/confirm", headers=auth_headers, timeout=15)
        assert confirm.status_code == 200, confirm.text
        r = requests.post(f"{API}/payloads/jam", headers=auth_headers,
                          json={"band": "2g4", "duration_s": 1.0, "bandwidth_khz": 500.0,
                                "tx_gain": 20, "arm_token": arm,
                                "jam_confirm_token": confirm.json()["jam_confirm_token"]}, timeout=15)
        assert r.status_code == 409, r.text


# --------------------------------------------------------------------------
# F5 — DELETE /detections/{id} must be commander-gated + audited
# --------------------------------------------------------------------------
class TestF5DeleteGate:
    def test_delete_requires_auth(self, auth_headers):
        det, _ = _ingest_detection(auth_headers)
        r = requests.delete(f"{API}/detections/{det['id']}", timeout=15)
        assert r.status_code in (401, 403), r.text

    def test_commander_delete_succeeds_and_audits(self, auth_headers):
        det, _ = _ingest_detection(auth_headers)
        r = requests.delete(f"{API}/detections/{det['id']}", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        # The deletion is audited to the tamper-evident mission log BEFORE the
        # row is removed (F5) — confirm a DETECTION removal entry exists.
        logs = requests.get(f"{API}/logs", params={"limit": 200}, headers=auth_headers, timeout=15)
        assert logs.status_code == 200
        assert any(det["id"] in (e.get("message") or "")
                   or (e.get("meta") or {}).get("detection_id") == det["id"]
                   for e in logs.json()), "expected an audit entry for the deletion"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
