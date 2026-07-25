"""Tests for OWASP gap #84: rate limiting / lockout on POST /auth/login.

Mirrors the existing e2e style used across this test suite (live server,
REACT_APP_BACKEND_URL-resolved BASE_URL) and mirrors the existing
range-authorization lockout test pattern: hammer the endpoint with bad
credentials until the in-memory throttle (see `_login_locked_out` /
LOGIN_MAX_FAILURES / LOGIN_LOCKOUT_WINDOW_S in backend/server.py) trips a 429,
then confirm a valid login still succeeds once the window rolls off is NOT
asserted here (that would require sleeping past LOGIN_LOCKOUT_WINDOW_S in
real time) -- we only assert the lockout itself engages and that it is keyed
per-account (a different, always-valid account is unaffected by another
account's failures).
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

# Must match LOGIN_MAX_FAILURES in backend/server.py.
LOGIN_MAX_FAILURES = 5


class TestLoginLockout:
    def test_repeated_bad_password_locks_out_account(self):
        # Use a throwaway, non-existent email so we don't interfere with
        # ADMIN_EMAIL used by the rest of the suite (the throttle is keyed
        # by attempted email, same convention as the range-auth throttle
        # being keyed by the authenticated commander's email).
        target_email = f"lockout-test-{uuid.uuid4()}@cema.mil"

        last_status = None
        for _ in range(LOGIN_MAX_FAILURES):
            r = requests.post(f"{API}/auth/login",
                               json={"email": target_email, "password": "wrong"},
                               timeout=15)
            last_status = r.status_code
            assert last_status == 401, r.text

        # One more failed attempt beyond the threshold must now be refused
        # outright with 429, without even checking credentials.
        r = requests.post(f"{API}/auth/login",
                           json={"email": target_email, "password": "wrong"},
                           timeout=15)
        assert r.status_code == 429, r.text

        # And it stays locked even if the (still-wrong) password changes --
        # the lockout is on attempt count, not on a specific guessed value.
        r = requests.post(f"{API}/auth/login",
                           json={"email": target_email, "password": "also-wrong"},
                           timeout=15)
        assert r.status_code == 429, r.text

    def test_lockout_is_per_account_not_global(self):
        # Lock out a throwaway account...
        target_email = f"lockout-test-{uuid.uuid4()}@cema.mil"
        for _ in range(LOGIN_MAX_FAILURES + 1):
            requests.post(f"{API}/auth/login",
                          json={"email": target_email, "password": "wrong"},
                          timeout=15)

        # ...and confirm the real admin account is unaffected.
        r = requests.post(f"{API}/auth/login",
                          json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
                          timeout=15)
        assert r.status_code == 200, r.text
