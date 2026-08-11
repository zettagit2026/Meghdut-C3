"""E2E tests for per-operator identity management (audit non-repudiation).

Context: previously ONE seeded shared commander account (ADMIN_EMAIL) existed,
so every jam/spoof/strike authorization, IFF override and deploy was attributed
to that single actor and the hash-chained audit log could not say WHO authorized
a kinetic/EW action. server.py now exposes commander-only user management:
  POST /api/users  (create operator|commander, bcrypt-hashed, duplicate-rejected)
  GET  /api/users  (list — never leaks password hashes)
so distinct accounts exist and `actor` in the mission log attributes real people.

Mirrors the existing e2e style used across this suite (live server,
REACT_APP_BACKEND_URL-resolved BASE_URL, ADMIN_EMAIL/ADMIN_PASSWORD env creds,
and the _ingest_detection convention of seeding your own data per test).

Run (needs a live backend + Mongo): pytest backend/tests/test_user_management.py -v
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
# Same rationale as the rest of the suite (task #127): reuse the real
# ADMIN_PASSWORD the backend was booted with; never hardcode a placeholder.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)


@pytest.fixture(scope="module")
def commander_headers() -> dict:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                      timeout=15)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _unique_email(role: str) -> str:
    return f"test-{role}-{uuid.uuid4().hex[:12]}@meghaduta.mil"


def test_commander_can_create_operator(commander_headers):
    email = _unique_email("op")
    pw = secrets.token_urlsafe(16)
    r = requests.post(f"{API}/users", headers=commander_headers,
                      json={"email": email, "password": pw, "role": "operator"},
                      timeout=15)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["email"] == email
    assert body["role"] == "operator"
    assert "password_hash" not in body and "password" not in body


def test_duplicate_email_rejected(commander_headers):
    email = _unique_email("dup")
    pw = secrets.token_urlsafe(16)
    r1 = requests.post(f"{API}/users", headers=commander_headers,
                       json={"email": email, "password": pw, "role": "operator"},
                       timeout=15)
    assert r1.status_code in (200, 201), r1.text
    r2 = requests.post(f"{API}/users", headers=commander_headers,
                       json={"email": email, "password": pw, "role": "operator"},
                       timeout=15)
    assert r2.status_code == 409, r2.text


def test_created_user_can_login_and_is_audit_actor(commander_headers):
    email = _unique_email("actor")
    pw = secrets.token_urlsafe(16)
    r = requests.post(f"{API}/users", headers=commander_headers,
                      json={"email": email, "password": pw, "role": "operator"},
                      timeout=15)
    assert r.status_code in (200, 201), r.text

    # The created user can log in with their own credentials...
    lr = requests.post(f"{API}/auth/login",
                       json={"email": email, "password": pw}, timeout=15)
    assert lr.status_code == 200, lr.text
    new_headers = {"Authorization": f"Bearer {lr.json()['token']}"}

    # ...and an action they take (logout is audited) is attributed to THEM.
    lo = requests.post(f"{API}/auth/logout", headers=new_headers, timeout=15)
    assert lo.status_code == 200, lo.text

    logs = requests.get(f"{API}/logs?limit=200", headers=commander_headers,
                        timeout=15).json()
    actors = {e.get("actor") for e in logs}
    assert email in actors, f"created user {email} not attributed as an audit actor"


def test_operator_cannot_create_user(commander_headers):
    # Mint an operator, then prove that operator's token cannot mint anyone —
    # and in particular cannot escalate by minting a commander.
    op_email = _unique_email("noesc")
    pw = secrets.token_urlsafe(16)
    r = requests.post(f"{API}/users", headers=commander_headers,
                      json={"email": op_email, "password": pw, "role": "operator"},
                      timeout=15)
    assert r.status_code in (200, 201), r.text
    lr = requests.post(f"{API}/auth/login",
                       json={"email": op_email, "password": pw}, timeout=15)
    assert lr.status_code == 200, lr.text
    op_headers = {"Authorization": f"Bearer {lr.json()['token']}"}

    forbidden = requests.post(
        f"{API}/users", headers=op_headers,
        json={"email": _unique_email("mint"), "password": secrets.token_urlsafe(16),
              "role": "commander"}, timeout=15)
    assert forbidden.status_code == 403, forbidden.text


def test_list_users_requires_commander_and_hides_hashes(commander_headers):
    # Commander can list, and no password hash is ever leaked.
    r = requests.get(f"{API}/users", headers=commander_headers, timeout=15)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert "users" in payload and isinstance(payload["users"], list)
    for u in payload["users"]:
        assert "password_hash" not in u
        assert "password" not in u

    # An operator cannot list users.
    op_email = _unique_email("nolist")
    pw = secrets.token_urlsafe(16)
    requests.post(f"{API}/users", headers=commander_headers,
                  json={"email": op_email, "password": pw, "role": "operator"},
                  timeout=15)
    lr = requests.post(f"{API}/auth/login",
                       json={"email": op_email, "password": pw}, timeout=15)
    op_headers = {"Authorization": f"Bearer {lr.json()['token']}"}
    forbidden = requests.get(f"{API}/users", headers=op_headers, timeout=15)
    assert forbidden.status_code == 403, forbidden.text


def test_invalid_role_rejected(commander_headers):
    # Pydantic pattern constrains role to operator|commander — anything else 422.
    r = requests.post(f"{API}/users", headers=commander_headers,
                      json={"email": _unique_email("badrole"),
                            "password": secrets.token_urlsafe(16),
                            "role": "superuser"}, timeout=15)
    assert r.status_code == 422, r.text
