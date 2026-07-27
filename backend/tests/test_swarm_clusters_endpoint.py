"""Integration tests for the /swarm/clusters split (follow-up to Task #122's
Reality Checker QA finding, 2026-07-26): the original GET /swarm/clusters
performed update_many writes + an audit-log entry despite being a GET,
violating HTTP safe-method semantics.

Fix under test: GET /swarm/clusters is now genuinely read-only (computes
clusters, persists nothing, logs nothing). POST /swarm/clusters/recompute
carries the original persist-swarm_id-onto-detections + audit-log
side effect.

Requires a live backend (same pattern as test_new_endpoints.py): set
REACT_APP_BACKEND_URL or provide /app/frontend/.env.
"""
from __future__ import annotations

import os
import secrets
import time
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
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@cema.mil")
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


def _ingest(auth_headers: dict, *, source: str, model: str, protocol: str) -> dict:
    body = {
        "model": model,
        "protocol": protocol,
        "center_freq_ghz": 2.44,
        "source": source,
    }
    r = requests.post(f"{API}/detections/ingest", json=body, headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _get_logs_count(auth_headers: dict) -> int:
    r = requests.get(f"{API}/logs?limit=500", headers=auth_headers, timeout=15)
    assert r.status_code == 200, r.text
    return len(r.json())


class TestSwarmClustersIsReadOnly:
    def test_get_does_not_persist_swarm_id(self, auth_headers):
        # Two concurrent detections from distinct sources -> should cluster.
        d1 = _ingest(auth_headers, source=f"TEST-A-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")
        d2 = _ingest(auth_headers, source=f"TEST-B-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")

        # Sanity: freshly ingested detections have no swarm_id yet.
        assert d1.get("swarm_id") is None
        assert d2.get("swarm_id") is None

        logs_before = _get_logs_count(auth_headers)

        r = requests.get(f"{API}/swarm/clusters", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        # GET must not write an audit log entry.
        assert _get_logs_count(auth_headers) == logs_before

        # GET must not persist swarm_id onto the underlying detections,
        # regardless of what clusters it computed and returned.
        det1 = requests.get(f"{API}/detections/{d1['id']}", headers=auth_headers, timeout=15)
        det2 = requests.get(f"{API}/detections/{d2['id']}", headers=auth_headers, timeout=15)
        assert det1.status_code == 200 and det2.status_code == 200
        assert det1.json().get("swarm_id") is None
        assert det2.json().get("swarm_id") is None

    def test_post_recompute_persists_swarm_id_and_logs(self, auth_headers):
        d1 = _ingest(auth_headers, source=f"TEST-C-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")
        d2 = _ingest(auth_headers, source=f"TEST-D-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")

        logs_before = _get_logs_count(auth_headers)

        r = requests.post(f"{API}/swarm/clusters/recompute", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        clusters = r.json()["clusters"]
        assert any(
            {d1["id"], d2["id"]} <= set(c["member_ids"]) for c in clusters
        ), f"expected d1/d2 to cluster together, got {clusters}"

        # POST must persist swarm_id onto the member detections.
        det1 = requests.get(f"{API}/detections/{d1['id']}", headers=auth_headers, timeout=15)
        det2 = requests.get(f"{API}/detections/{d2['id']}", headers=auth_headers, timeout=15)
        assert det1.json().get("swarm_id") is not None
        assert det1.json()["swarm_id"] == det2.json()["swarm_id"]

        # POST must write an audit log entry (SWARM_CLASSIFY).
        assert _get_logs_count(auth_headers) > logs_before

    def test_get_still_returns_clusters_shape(self, auth_headers):
        r = requests.get(f"{API}/swarm/clusters", headers=auth_headers, timeout=15)
        assert r.status_code == 200, r.text
        assert "clusters" in r.json()
        assert isinstance(r.json()["clusters"], list)

    def test_old_verb_no_longer_persists_via_get_repeatedly(self, auth_headers):
        """Regression guard for the original bug: hammering GET must never
        cause swarm_id to appear on a fresh, never-recomputed detection."""
        d1 = _ingest(auth_headers, source=f"TEST-E-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")
        d2 = _ingest(auth_headers, source=f"TEST-F-{secrets.token_hex(4)}",
                     model="FPV Racer", protocol="ExpressLRS")
        for _ in range(3):
            r = requests.get(f"{API}/swarm/clusters", headers=auth_headers, timeout=15)
            assert r.status_code == 200
        det1 = requests.get(f"{API}/detections/{d1['id']}", headers=auth_headers, timeout=15)
        det2 = requests.get(f"{API}/detections/{d2['id']}", headers=auth_headers, timeout=15)
        assert det1.json().get("swarm_id") is None
        assert det2.json().get("swarm_id") is None
