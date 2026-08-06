"""Integration tests for the multi-target track manager endpoints (OB-04):
GET /api/tracks (auth gate + shape) and the /api/health track summary fields.

Requires a live backend (same pattern as test_new_endpoints.py /
test_swarm_clusters_endpoint.py): set REACT_APP_BACKEND_URL or provide
/app/frontend/.env. These are the endpoint/wiring tests; the lifecycle state
machine itself is covered by the pure-logic test_track_manager.py.
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


def _ingest(auth_headers: dict, *, source: str, freq: float) -> dict:
    body = {
        "model": "Test UAV",
        "protocol": "Test-Protocol",
        "threat_level": "MEDIUM",
        "center_freq_ghz": freq,
        "source": source,
    }
    r = requests.post(f"{API}/detections/ingest", json=body,
                      headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


class TestTracksAuth:
    def test_tracks_requires_auth(self):
        r = requests.get(f"{API}/tracks", timeout=15)
        assert r.status_code in (401, 403)


class TestTracksShape:
    def test_tracks_shape_and_summary(self, auth_headers):
        r = requests.get(f"{API}/tracks", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body.get("tracks"), list)
        # Summary counts present.
        for k in ("active_tracks", "tracks_confirmed", "tracks_budget_max",
                  "tracks_at_capacity"):
            assert k in body, f"missing summary field {k}"
        assert isinstance(body["tracks_at_capacity"], bool)

    def test_ingest_births_a_track(self, auth_headers):
        src = f"TRK-{uuid.uuid4().hex[:8]}"
        _ingest(auth_headers, source=src, freq=2.437)
        r = requests.get(f"{API}/tracks", headers=auth_headers, timeout=15)
        assert r.status_code == 200
        tracks = r.json()["tracks"]
        mine = [t for t in tracks if t.get("source") == src]
        assert mine, "expected a track born from the just-ingested detection"
        t = mine[0]
        # A brand-new track is honestly TENTATIVE, not presented as confirmed.
        assert t["state"] in ("TENTATIVE", "CONFIRMED")
        assert "track_id" in t and "stale" in t and "hits" in t

    def test_repeated_ingest_associates_not_duplicates(self, auth_headers):
        src = f"TRK-{uuid.uuid4().hex[:8]}"
        # Same source + classification + frequency -> one track, multiple hits.
        for _ in range(3):
            _ingest(auth_headers, source=src, freq=2.412)
        r = requests.get(f"{API}/tracks", headers=auth_headers, timeout=15)
        mine = [t for t in r.json()["tracks"] if t.get("source") == src]
        assert len(mine) == 1, f"expected exactly one track for {src}, got {len(mine)}"
        assert mine[0]["hits"] >= 3


class TestHealthTrackFields:
    def test_health_exposes_track_summary(self, auth_headers):
        r = requests.get(f"{API}/health", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        for k in ("active_tracks", "tracks_confirmed", "tracks_at_capacity"):
            assert k in body, f"/health missing {k}"
