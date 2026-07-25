"""Tests for IFF per-asset revocation endpoints (hardening on top of task
#60): POST /iff/assets/{id}/revoke, POST /iff/assets/{id}/unrevoke, GET
/iff/assets/revoked. See backend/server.py's iff_revoke_asset()/
iff_unrevoke_asset()/iff_list_revoked() and field-bridge/
iff_beacon_bridge.py's AssetRegistry.revoked_asset_ids for the full
rationale: a single captured/compromised asset can be locked out without
rotating the whole mission's master secret.

Requires a live backend (same convention as test_new_endpoints.py) --
REACT_APP_BACKEND_URL env var or /app/frontend/.env.
"""
from __future__ import annotations

import os
import random
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
# The seeded admin account is provisioned with role="commander" (see
# backend/server.py startup()) -- same account test_new_endpoints.py uses.
ADMIN_EMAIL = "operator@cema.mil"
ADMIN_PASSWORD = "cema@2026"


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


@pytest.fixture
def test_asset_id() -> int:
    # Random high asset_id per test to avoid collisions across test runs /
    # parallel test workers touching the same revocation record.
    return random.randint(10_000_000, 99_999_999)


class TestIFFRevocation:
    def test_revoke_then_appears_in_revoked_list(self, auth_headers, test_asset_id):
        r = requests.post(f"{API}/iff/assets/{test_asset_id}/revoke",
                           headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["asset_id"] == test_asset_id
        assert body["revoked"] is True

        r2 = requests.get(f"{API}/iff/assets/revoked", headers=auth_headers, timeout=10)
        assert r2.status_code == 200, r2.text
        assert test_asset_id in r2.json()["revoked_asset_ids"]

    def test_revoke_is_idempotent(self, auth_headers, test_asset_id):
        r1 = requests.post(f"{API}/iff/assets/{test_asset_id}/revoke",
                            headers=auth_headers, timeout=10)
        r2 = requests.post(f"{API}/iff/assets/{test_asset_id}/revoke",
                            headers=auth_headers, timeout=10)
        assert r1.status_code == 200 and r2.status_code == 200

    def test_unrevoke_removes_from_list(self, auth_headers, test_asset_id):
        requests.post(f"{API}/iff/assets/{test_asset_id}/revoke",
                       headers=auth_headers, timeout=10)
        r = requests.post(f"{API}/iff/assets/{test_asset_id}/unrevoke",
                           headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["revoked"] is False

        r2 = requests.get(f"{API}/iff/assets/revoked", headers=auth_headers, timeout=10)
        assert test_asset_id not in r2.json()["revoked_asset_ids"]

    def test_revoke_requires_auth(self, test_asset_id):
        r = requests.post(f"{API}/iff/assets/{test_asset_id}/revoke", timeout=10)
        assert r.status_code in (401, 403)

    def test_revoked_asset_excluded_from_fresh_friendlies(self, auth_headers, test_asset_id):
        """Ingesting a beacon for an asset, then revoking it, must remove it
        from GET /iff/friendlies immediately -- backend-side defense-in-
        depth on top of the bridge's own AssetRegistry-level revocation
        check (see _fresh_friendlies() docstring in backend/server.py)."""
        ingest_body = {
            "asset_id": test_asset_id,
            "callsign": "Test-Revocation-Asset",
            "mission_id": 1,
            "geocell": 0,
            "geocell_known": False,
            "bearing_deg": None,
            "distance_m": None,
        }
        r = requests.post(f"{API}/iff/beacons/ingest", json=ingest_body,
                           headers=auth_headers, timeout=10)
        assert r.status_code == 200, r.text

        before = requests.get(f"{API}/iff/friendlies", headers=auth_headers, timeout=10)
        assert any(f["asset_id"] == test_asset_id for f in before.json()["friendlies"])

        requests.post(f"{API}/iff/assets/{test_asset_id}/revoke",
                      headers=auth_headers, timeout=10)
        after = requests.get(f"{API}/iff/friendlies", headers=auth_headers, timeout=10)
        assert not any(f["asset_id"] == test_asset_id for f in after.json()["friendlies"])

        # cleanup
        requests.post(f"{API}/iff/assets/{test_asset_id}/unrevoke",
                      headers=auth_headers, timeout=10)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
