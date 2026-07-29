"""Tests for iteration-4 endpoints: /health, /emergency/abort, /report/mission.pdf,
plus dual-registered WS at /api/ws/mavlink (auth gate + hello frame).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

import pytest
import requests
import websockets

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
ADMIN_EMAIL = "operator@cema.mil"
# Task #127: never hardcode a real password here -- server.py's
# _PLACEHOLDER_SECRETS blocklist refuses to boot with known placeholder
# values (the old "cema@2026" literal that used to live here included), so a
# fixed test constant can never authenticate against a correctly-configured
# backend. Reuse whatever ADMIN_PASSWORD the backend was actually booted
# with (same env var, set once per test session by the harness/docker-compose
# invocation) and only fall back to a random session-only value -- generated
# fresh each run, never written to disk -- if the harness didn't export one.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)


@pytest.fixture(scope="module")
def token() -> str:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ------------- System health -------------
class TestHealth:
    def test_health_shape(self, auth_headers):
        r = requests.get(f"{API}/health", headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ["backend", "mongo", "hackrf", "sik_radio", "ws_clients",
                  "active_targets", "total_packets_tx", "server_time",
                  "tx_halted"]:
            assert k in d, f"missing field {k} in health response"
        assert d["backend"] is True
        assert d["mongo"] is True
        # In sandbox no hardware
        assert isinstance(d["hackrf"], bool)
        assert isinstance(d["sik_radio"], bool)
        assert isinstance(d["ws_clients"], int)
        assert isinstance(d["active_targets"], int)
        assert isinstance(d["total_packets_tx"], int)
        assert isinstance(d["tx_halted"], bool)

    def test_tx_halted_fail_closed_default(self, auth_headers):
        # Task #136: a freshly-started backend must default to TX-HALTED
        # (fail-closed) — see backend/TX_HALT_PERSISTENCE_SCOPE.md. This test
        # is ordered before TestEmergencyAbort/TestEmergencyResume in this
        # module so it observes the boot-time default rather than a value
        # left over from another test's abort/resume call.
        r = requests.get(f"{API}/health", headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["tx_halted"] is True

        logs = requests.get(f"{API}/logs", headers=auth_headers, timeout=10)
        assert logs.status_code == 200, logs.text
        kinds = [entry.get("kind") for entry in logs.json()]
        assert "TX_HALT_STARTUP" in kinds, (
            "expected a TX_HALT_STARTUP audit entry logged at process start"
        )

    def test_health_requires_auth(self):
        r = requests.get(f"{API}/health", timeout=10)
        assert r.status_code == 401


# ------------- Emergency abort -------------
class TestEmergencyAbort:
    def test_abort_returns_ok_and_creates_log(self, auth_headers):
        r = requests.post(f"{API}/emergency/abort", headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d.get("ok") is True
        assert isinstance(d.get("ts"), str) and "T" in d["ts"]

        # Verify a mission log entry with kind='ABORT' was created
        r2 = requests.get(f"{API}/logs?limit=50", headers=auth_headers, timeout=10)
        assert r2.status_code == 200
        kinds = [e["kind"] for e in r2.json()]
        assert "ABORT" in kinds, f"ABORT not found in recent kinds={kinds[:10]}"

    def test_abort_requires_auth(self):
        r = requests.post(f"{API}/emergency/abort", timeout=10)
        assert r.status_code == 401


# ------------- Mission PDF -------------
class TestMissionPDF:
    def test_pdf_returns_valid_file(self, auth_headers):
        r = requests.get(f"{API}/report/mission.pdf", headers=auth_headers, timeout=30)
        assert r.status_code == 200, r.text[:400]
        ctype = r.headers.get("content-type", "")
        assert "application/pdf" in ctype, f"unexpected content-type: {ctype}"
        cdisp = r.headers.get("content-disposition", "")
        assert "attachment" in cdisp.lower()
        assert r.content[:4] == b"%PDF", f"body does not start with %PDF: {r.content[:10]!r}"
        assert len(r.content) >= 5 * 1024, f"pdf too small ({len(r.content)} bytes)"

    def test_pdf_requires_auth(self):
        r = requests.get(f"{API}/report/mission.pdf", timeout=15)
        assert r.status_code == 401


# ------------- WebSocket handshake (regression) -------------
def _ws_url(token: str | None = None) -> str:
    u = urlparse(BASE_URL)
    scheme = "wss" if u.scheme == "https" else "ws"
    base = f"{scheme}://{u.netloc}/api/ws/mavlink"
    return f"{base}?token={token}" if token else base


class TestWebSocketRegression:
    @pytest.mark.asyncio
    async def test_ws_hello_frame(self, token):
        url = _ws_url(token)
        async with websockets.connect(url) as ws:
            hello_raw = await asyncio.wait_for(ws.recv(), timeout=8)
            hello = json.loads(hello_raw)
            assert hello.get("type") == "hello", f"unexpected first frame: {hello}"

    @pytest.mark.asyncio
    async def test_ws_no_token_rejected(self):
        url = _ws_url(None)
        rejected = False
        try:
            async with websockets.connect(url) as ws:
                # Should be closed by server (1008); recv raises
                await asyncio.wait_for(ws.recv(), timeout=4)
        except Exception:
            rejected = True
        assert rejected, "server should reject unauth WS handshake"
