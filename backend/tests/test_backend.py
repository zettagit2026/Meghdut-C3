"""End-to-end backend tests for CEMA cUAS operator console.

Covers: auth, detections (CRUD via real ingest), CEMA/killchain advance,
spectrum, MAVLink craft/broadcast/list, payloads (deploy target + broadcast),
mission logs, and the WebSocket packet stream.

Task #141: this file was stale against server.py's actual current API
surface (found during 2026-07-29/30 QA of task #136). In particular:
  - /detections/simulate and /detections/upload were removed at some point;
    the real, current mechanism for creating a detection is POST
    /detections/ingest (used by the field-bridge scripts, e.g.
    hackrf_rx.py / ml_classify_bridge.py / mavlink_sniffer.py). Tests that
    used to call /detections/simulate now call _ingest_detection() below,
    which POSTs a unique (random center_freq_ghz + model) detection via
    /detections/ingest so it never merges with another test's contact
    (see DETECTION_MERGE_WINDOW_S / match_model+match_protocol matching in
    server.py's detection_ingest()).
  - /detections/upload has no current replacement at all (no upload_meta/
    upload_filename/upload_size_bytes fields exist anywhere in server.py) --
    that test is now explicitly skipped rather than silently deleted.
  - The seeded admin's role is "commander", not "admin" (server.py ~845).
  - No detections are seeded at boot (real detections only ever come from
    real ingest -- confirmed intentional, honest behavior, not a bug).
  - /spectrum/waterfall serves whatever was last POSTed to /spectrum/ingest
    within the last 30s (and otherwise honestly reports an empty spectrum);
    it does NOT synthesize rows from the bins/rows query params. The
    waterfall test now seeds real spectrum data first.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from urllib.parse import urlparse

import pytest
import requests
import websockets

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if "REACT_APP_BACKEND_URL" in os.environ else None
if BASE_URL is None:
    # Fallback: read from frontend .env
    from pathlib import Path
    env_txt = Path("/app/frontend/.env").read_text()
    for line in env_txt.splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL not resolvable"

API = f"{BASE_URL}/api"

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@meghaduta.mil")
# Task #127: never hardcode a real password here -- server.py's
# _PLACEHOLDER_SECRETS blocklist refuses to boot with known placeholder
# values (the old "cema@2026" literal that used to live here included), so a
# fixed test constant can never authenticate against a correctly-configured
# backend. Reuse whatever ADMIN_PASSWORD the backend was actually booted
# with (same env var, set once per test session by the harness/docker-compose
# invocation) and only fall back to a random session-only value -- generated
# fresh each run, never written to disk -- if the harness didn't export one.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)


# ------------------------- Fixtures -------------------------
@pytest.fixture(scope="session")
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="session")
def token(session: requests.Session) -> str:
    r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    data = r.json()
    assert "token" in data
    tok = data["token"]
    # Task #136: backend now boots TX-HALTED by default (fail-closed --
    # see backend/TX_HALT_PERSISTENCE_SCOPE.md). This module exercises real
    # /mavlink/broadcast and /payloads/deploy TX, which is gated by that
    # flag, so a commander-level resume is required before any TX-gated
    # call in this file will work.
    r2 = session.post(f"{API}/emergency/resume",
                       headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    assert r2.status_code == 200, f"emergency/resume failed: {r2.status_code} {r2.text}"
    return tok


@pytest.fixture(scope="session")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ingest_detection(auth_headers: dict) -> dict:
    """Create a fresh, uniquely-identified ACTIVE detection via the real
    current ingest mechanism (POST /detections/ingest), replacing the old
    (removed) POST /detections/simulate helper. A random center_freq_ghz and
    model per call guarantees this never merges into another test's/worker's
    in-flight contact -- see match_model/match_protocol matching in
    server.py's detection_ingest()."""
    body = {
        "callsign": f"TEST-{uuid.uuid4().hex[:8]}",
        "model": f"Test UAV {uuid.uuid4().hex[:8]}",
        "protocol": "Test-Protocol",
        "threat_level": "MEDIUM",
        "center_freq_ghz": round(2.400 + secrets.randbelow(400) / 1000, 3),
        "source": "HACKRF",
    }
    r = requests.post(f"{API}/detections/ingest", headers=auth_headers, json=body)
    assert r.status_code == 200, f"detections/ingest failed: {r.status_code} {r.text}"
    return r.json()


def _ws_url(token: str) -> str:
    u = urlparse(BASE_URL)
    scheme = "wss" if u.scheme == "https" else "ws"
    return f"{scheme}://{u.netloc}/api/ws/mavlink?token={token}"


def _send_tx_ack(token: str, request_id: str, ok: bool = True) -> None:
    """Simulate the real bridge's (rf-bridge/mavlink_bridge.py) tx_ack
    reply over the mavlink WS after a real serial write attempt -- this is
    the ONLY path that transitions a detection out of AWAITING_ACK into
    NEUTRALIZED/TX_FAILED (see server.py's _handle_tx_ack()). Since task
    #136's TX-halt/ack architecture, /payloads/deploy itself only ever
    parks a detection in AWAITING_ACK and never marks it NEUTRALIZED
    synchronously -- tests that need a deploy to actually resolve must
    supply this ack themselves, same as a real connected bridge would."""

    async def _do() -> None:
        async with websockets.connect(_ws_url(token)) as ws:
            await asyncio.wait_for(ws.recv(), timeout=5)  # hello frame
            await ws.send(json.dumps({"type": "tx_ack", "request_id": request_id, "ok": ok}))
            # Give the server a moment to process the ack before the caller
            # re-reads detection state over HTTP.
            await asyncio.sleep(0.2)

    asyncio.run(_do())


# ------------------------- Auth -------------------------
class TestAuth:
    def test_login_success(self, session):
        r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data.get("token"), str) and len(data["token"]) > 20
        u = data.get("user", {})
        assert u.get("email") == ADMIN_EMAIL
        # Task #141: the seeded admin's actual role is "commander" (see
        # server.py ~845), not "admin" -- fixed to match reality.
        assert u.get("role") == "commander"
        assert u.get("clearance") == "RESTRICTED"

    def test_login_wrong_password(self, session):
        r = session.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
        assert r.status_code == 401

    def test_me_with_token(self, auth_headers):
        r = requests.get(f"{API}/auth/me", headers=auth_headers)
        assert r.status_code == 200
        u = r.json()
        assert u.get("email") == ADMIN_EMAIL
        assert "password_hash" not in u

    def test_me_without_token(self):
        r = requests.get(f"{API}/auth/me")
        assert r.status_code == 401


# ------------------------- Detections -------------------------
class TestDetections:
    def test_list_seeded(self, auth_headers):
        # Task #141: this project's established convention is that NO
        # synthetic/seeded detections are ever inserted at boot (see
        # server.py ~870-873) -- real detections only ever come from real
        # ingest. That is confirmed honest, intentional behavior, not a
        # bug, so a freshly-booted stack legitimately has zero detections
        # here. Seed one via the real ingest mechanism ourselves so we can
        # still assert on the shape of a real detection document.
        det = _ingest_detection(auth_headers)
        r = requests.get(f"{API}/detections", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) >= 1, f"expected >=1 detection after seeding one via ingest, got {len(data)}"
        assert any(d["id"] == det["id"] for d in data)
        for k in ["id", "callsign", "model", "protocol", "threat_level",
                  "center_freq_ghz", "rssi_dbm", "cema_stage", "kill_chain_stage", "status"]:
            assert k in det, f"detection missing field {k}"

    def test_ingest_increases_count(self, auth_headers):
        r1 = requests.get(f"{API}/detections", headers=auth_headers)
        c0 = len(r1.json())
        det = _ingest_detection(auth_headers)
        assert "id" in det and "callsign" in det
        r3 = requests.get(f"{API}/detections", headers=auth_headers)
        assert len(r3.json()) == c0 + 1

    @pytest.mark.skip(
        reason="Task #141: POST /detections/upload was removed from server.py "
               "with no replacement -- no upload_meta/upload_filename/"
               "upload_size_bytes fields exist anywhere in the current backend. "
               "Detection creation from an uploaded capture file is not "
               "currently a supported feature; skipping rather than testing "
               "removed functionality."
    )
    def test_upload_returns_detection_with_meta(self, auth_headers):
        pass

    def test_cema_advance(self, auth_headers):
        # Create a fresh detection first (via real ingest, not the removed
        # /detections/simulate)
        det = _ingest_detection(auth_headers)
        idx0 = det["cema_stage_index"]
        r = requests.post(f"{API}/detections/{det['id']}/cema-advance", headers=auth_headers)
        assert r.status_code == 200
        d2 = r.json()
        assert d2["cema_stage_index"] == idx0 + 1
        # unknown id 404
        r404 = requests.post(f"{API}/detections/{uuid.uuid4()}/cema-advance", headers=auth_headers)
        assert r404.status_code == 404

    def test_killchain_advance(self, auth_headers):
        det = _ingest_detection(auth_headers)
        idx0 = det["kill_chain_index"]
        r = requests.post(f"{API}/detections/{det['id']}/killchain-advance", headers=auth_headers)
        assert r.status_code == 200
        d2 = r.json()
        assert d2["kill_chain_index"] == idx0 + 1


# ------------------------- Spectrum -------------------------
class TestSpectrum:
    def test_waterfall_shape(self, auth_headers):
        # Task #141: /spectrum/waterfall does NOT synthesize rows from the
        # bins/rows query params -- it only ever serves whatever was last
        # POSTed to /spectrum/ingest within the last 30s (server.py's
        # spectrum_waterfall(), _last_spectrum_ingest), and otherwise
        # honestly reports an empty spectrum (bins from the query param,
        # rows=[], source="NONE"). Seed real spectrum data first, the same
        # way the real RF bridge (hackrf_sweep bridge) does.
        bins = 64
        rows = [[float(v) for v in range(bins)] for _ in range(8)]
        ingest_body = {"bins": bins, "rows": rows, "center_freq_ghz": 2.44, "span_mhz": 40.0}
        r_ingest = requests.post(f"{API}/spectrum/ingest", headers=auth_headers, json=ingest_body)
        assert r_ingest.status_code == 200, r_ingest.text

        r = requests.get(f"{API}/spectrum/waterfall?bins=64&rows=8", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "HACKRF"
        assert data["bins"] == 64
        assert isinstance(data["rows"], list)
        assert len(data["rows"]) == 8
        for row in data["rows"]:
            assert isinstance(row, list) and len(row) == 64
            assert all(isinstance(v, (int, float)) for v in row)


# ------------------------- MAVLink -------------------------
class TestMavlink:
    def test_craft_v2_command_long(self, auth_headers):
        body = {"version": "v2", "message_id": 76, "command": 21, "target_system": 1}
        r = requests.post(f"{API}/mavlink/craft", headers=auth_headers, json=body)
        assert r.status_code == 200
        data = r.json()
        hex_ = data["hex"]
        assert hex_.upper().startswith("FD"), f"v2 STX expected, got {hex_[:2]}"
        assert data["length"] >= 12
        assert data["decoded"]["version"] == "v2"
        assert data["decoded"]["message_id"] == 76

    def test_craft_v1(self, auth_headers):
        body = {"version": "v1", "message_id": 76, "command": 21, "target_system": 1}
        r = requests.post(f"{API}/mavlink/craft", headers=auth_headers, json=body)
        assert r.status_code == 200
        data = r.json()
        assert data["hex"].upper().startswith("FE")
        assert data["decoded"]["version"] == "v1"

    def test_broadcast_persists_and_listed(self, auth_headers):
        body = {"version": "v2", "message_id": 76, "command": 21, "target_system": 1}
        r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers, json=body)
        assert r.status_code == 200
        pkt = r.json()
        assert "id" in pkt and "hex" in pkt
        r2 = requests.get(f"{API}/mavlink/packets?limit=50", headers=auth_headers)
        assert r2.status_code == 200
        ids = [p["id"] for p in r2.json()]
        assert pkt["id"] in ids


# ------------------------- Payloads -------------------------
class TestPayloads:
    def test_list_payloads(self, auth_headers):
        r = requests.get(f"{API}/payloads", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 10
        ids = {p["id"] for p in data}
        for k in [f"PL-{i:03d}" for i in range(1, 11)]:
            assert k in ids, f"missing payload {k}"
        for p in data:
            for f in ["id", "name", "category", "severity", "mav_cmd"]:
                assert f in p

    def test_deploy_target_pl005(self, auth_headers, token):
        # create a fresh active detection to target (via real ingest, not
        # the removed /detections/simulate)
        det = _ingest_detection(auth_headers)
        # Friendly-fire interlock: a kinetic payload cannot target a
        # detection until it has been explicitly authorized -- see
        # server.py's authorize_target()/deploy_payload().
        auth_r = requests.post(f"{API}/detections/{det['id']}/authorize-target",
                                headers=auth_headers, json={"authorized": True})
        assert auth_r.status_code == 200, auth_r.text
        # PL-005 (PROPELLER_STOP) is CRITICAL severity, so /payloads/deploy
        # requires a fresh single-use arm token (POST /arm) as a second
        # factor -- see _consume_arm_token()/spec.severity == "CRITICAL" in
        # server.py's deploy_payload().
        arm = requests.post(f"{API}/arm", headers=auth_headers)
        assert arm.status_code == 200, arm.text
        arm_token = arm.json()["arm_token"]
        r = requests.post(f"{API}/payloads/deploy",
                          headers=auth_headers,
                          json={"payload_id": "PL-005", "target_detection_id": det["id"],
                                "arm_token": arm_token})
        assert r.status_code == 200, r.text
        pkt = r.json()
        assert pkt["payload_id"] == "PL-005"
        assert pkt["status"] == "AWAITING_ACK"
        # Since task #136's TX-halt/ack architecture, deploy only parks the
        # detection in AWAITING_ACK -- it does not synchronously neutralize
        # it. Simulate the real bridge's tx_ack over the mavlink WS to
        # resolve it, exactly as rf-bridge/mavlink_bridge.py would after a
        # real successful serial write.
        _send_tx_ack(token, pkt["request_id"], ok=True)
        # Verify detection became NEUTRALIZED/DEFEAT
        r2 = requests.get(f"{API}/detections/{det['id']}", headers=auth_headers)
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["status"] == "NEUTRALIZED"
        assert d2["kill_chain_stage"] == "DEFEAT"

    def test_deploy_unknown_payload_id(self, auth_headers):
        r = requests.post(f"{API}/payloads/deploy",
                          headers=auth_headers,
                          json={"payload_id": "PL-999", "target_detection_id": None, "broadcast": True})
        assert r.status_code == 404

    def test_deploy_missing_target_no_broadcast(self, auth_headers):
        r = requests.post(f"{API}/payloads/deploy",
                          headers=auth_headers,
                          json={"payload_id": "PL-001", "broadcast": False})
        assert r.status_code == 400

    def test_deploy_broadcast_pl010_neutralizes_all_active(self, auth_headers, token):
        # Ensure at least a couple active targets (via real ingest, not the
        # removed /detections/simulate)
        for _ in range(3):
            _ingest_detection(auth_headers)
        active_before = [d for d in requests.get(f"{API}/detections", headers=auth_headers).json()
                         if d["status"] == "ACTIVE"]
        assert len(active_before) >= 1
        before_ids = {d["id"] for d in active_before}
        # Any broadcast (target_system=0) needs a fresh single-use arm
        # token too, regardless of payload severity -- see body.broadcast
        # check in server.py's deploy_payload().
        arm = requests.post(f"{API}/arm", headers=auth_headers)
        assert arm.status_code == 200, arm.text
        arm_token = arm.json()["arm_token"]
        r = requests.post(f"{API}/payloads/deploy",
                          headers=auth_headers,
                          json={"payload_id": "PL-010", "broadcast": True, "arm_token": arm_token})
        assert r.status_code == 200
        pkt = r.json()
        # Same as test_deploy_target_pl005: broadcast deploy only parks
        # detections in AWAITING_ACK until a real bridge tx_ack resolves
        # them (task #136 TX-halt/ack architecture) -- simulate that ack.
        _send_tx_ack(token, pkt["request_id"], ok=True)
        # Verify every previously-active id is now NEUTRALIZED (other workers may have created
        # new ACTIVE detections after our broadcast — that's fine).
        after = {d["id"]: d for d in requests.get(f"{API}/detections", headers=auth_headers).json()}
        for did in before_ids:
            assert did in after, f"detection {did} disappeared"
            assert after[did]["status"] == "NEUTRALIZED", (
                f"expected {did} NEUTRALIZED, got {after[did]['status']}"
            )


# ------------------------- Mission log -------------------------
class TestLogs:
    def test_logs_have_prior_actions(self, auth_headers):
        r = requests.get(f"{API}/logs?limit=200", headers=auth_headers)
        assert r.status_code == 200
        entries = r.json()
        assert isinstance(entries, list) and len(entries) > 0
        kinds = {e["kind"] for e in entries}
        # AUTH from login is guaranteed; others depend on prior tests. Check presence loosely.
        assert "AUTH" in kinds
        # At least one of the following should be present as we exercised them
        assert kinds & {"DETECTION", "MAVLINK", "PAYLOAD", "CEMA", "KILLCHAIN"}


# ------------------------- WebSocket -------------------------
class TestWebSocket:
    def _ws_url(self, token: str | None = None) -> str:
        u = urlparse(BASE_URL)
        scheme = "wss" if u.scheme == "https" else "ws"
        base = f"{scheme}://{u.netloc}/api/ws/mavlink"
        return f"{base}?token={token}" if token else base

    @pytest.mark.asyncio
    async def test_ws_no_token_rejected(self):
        url = self._ws_url(None)
        try:
            async with websockets.connect(url) as ws:
                # If we somehow got in, wait for a close.
                await asyncio.wait_for(ws.recv(), timeout=3)
                pytest.fail("expected ws to be rejected without token")
        except Exception:
            # connection closed / rejected is expected
            assert True

    @pytest.mark.asyncio
    async def test_ws_receives_broadcast_packet(self, token, auth_headers):
        url = self._ws_url(token)
        async with websockets.connect(url) as ws:
            # Consume hello frame
            hello = await asyncio.wait_for(ws.recv(), timeout=5)
            assert "hello" in hello

            # Trigger a broadcast via HTTP
            body = {"version": "v2", "message_id": 76, "command": 21, "target_system": 1}
            r = requests.post(f"{API}/mavlink/broadcast", headers=auth_headers, json=body)
            assert r.status_code == 200

            # Expect a "packet" message
            got_packet = False
            for _ in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                try:
                    obj = json.loads(msg)
                except Exception:
                    continue
                if obj.get("type") == "packet":
                    got_packet = True
                    break
            assert got_packet, "did not receive packet frame on ws"
