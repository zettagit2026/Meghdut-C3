#!/usr/bin/env python3
"""Real unit tests for the 401-retry-once auth-recovery helpers added to
every field-bridge script that authenticates against the backend.

Background: hackrf_rx.py, ml_classify_bridge.py, fpv_video_bridge.py,
mavlink_sniffer.py, droneid_decode_bridge.py, sik_mavlink_bridge.py,
crsf_parser.py, dronecan_parser.py, canopen_parser.py,
graupner_hott_parser.py, and ltm_parser.py each log in once at startup and
cache the resulting bearer token forever (task #150 retrofitted the last
five of these). The
backend's JWT TTL is 12h (create_access_token() in backend/server.py), and
these are systemd Restart=always services meant to run indefinitely, so a
token cached at startup silently and PERMANENTLY 401s once the process has
been up longer than the TTL -- this exact bug hit hackrf_rx.py,
ml_classify_bridge.py, and fpv_video_bridge.py in production, with real RF
detections computed but never ingested.

These tests mock `requests.post`/`requests.get` (and the login() HTTP call
itself) to simulate: (1) a 401 followed by a successful retry after
re-login, and (2) a 401 that persists even after re-login (a real auth
problem, e.g. wrong credentials -- must NOT retry forever or crash). This
exercises the actual control-flow logic in the shipped code, not fabricated
data -- no RF hardware or live backend is required.
"""
from __future__ import annotations

import sys
import unittest
from unittest import mock

import hackrf_rx
import mavlink_sniffer
import sik_mavlink_bridge
import crsf_parser
import dronecan_parser
import canopen_parser
import graupner_hott_parser
import ltm_parser


def _resp(status_code: int, json_body: dict | None = None):
    r = mock.Mock()
    r.status_code = status_code
    r.json.return_value = json_body or {}
    r.raise_for_status.side_effect = (
        None if status_code < 400
        else Exception(f"HTTP {status_code}")
    )
    return r


class ReauthOnceTestsBase:
    """Shared test bodies, parameterized by which module's
    `_post_with_reauth` is under test (hackrf_rx.py's canonical copy,
    mavlink_sniffer.py's duplicate, sik_mavlink_bridge.py's duplicate)."""

    module = None  # set by subclasses

    def test_success_no_401_makes_one_call(self):
        headers = {"Authorization": "Bearer stale-but-still-valid"}
        with mock.patch.object(self.module.requests, "post",
                                return_value=_resp(200, {"ok": True})) as post:
            r = self.module._post_with_reauth(
                "http://console", "/api/detections/ingest", {"x": 1},
                headers, "op@example.com", "pw", timeout=5,
            )
        self.assertEqual(post.call_count, 1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(headers["Authorization"], "Bearer stale-but-still-valid")

    def test_expired_token_reauths_and_retries_once(self):
        """First call gets 401 (expired 12h JWT). Helper must re-login
        exactly once and retry the SAME request exactly once, succeeding
        the second time -- and it must update `headers` in place so the
        caller's cached token is refreshed for all subsequent calls too."""
        headers = {"Authorization": "Bearer expired-token"}
        post_responses = [_resp(401), _resp(200, {"ok": True})]

        def fake_post(url, json=None, headers=None, timeout=None):
            return post_responses.pop(0)

        with mock.patch.object(self.module.requests, "post", side_effect=fake_post) as post, \
             mock.patch.object(self.module, "login", return_value="fresh-token") as login:
            r = self.module._post_with_reauth(
                "http://console", "/api/detections/ingest", {"x": 1},
                headers, "op@example.com", "pw", timeout=5,
            )

        self.assertEqual(post.call_count, 2)          # original attempt + exactly one retry
        login.assert_called_once_with("http://console", "op@example.com", "pw")
        self.assertEqual(headers["Authorization"], "Bearer fresh-token")  # refreshed in place
        self.assertEqual(r.status_code, 200)

    def test_persistent_401_after_reauth_does_not_loop_forever(self):
        """If the retry ALSO 401s, that's a real auth problem (e.g. wrong
        credentials), not just an expired token. The helper must make
        exactly 2 POST attempts total (not retry indefinitely) and return
        the failing response so the caller can log it and move on."""
        headers = {"Authorization": "Bearer expired-token"}
        with mock.patch.object(self.module.requests, "post",
                                return_value=_resp(401)) as post, \
             mock.patch.object(self.module, "login", return_value="still-bad-token") as login:
            r = self.module._post_with_reauth(
                "http://console", "/api/detections/ingest", {"x": 1},
                headers, "wrong@example.com", "wrongpw", timeout=5,
            )

        self.assertEqual(post.call_count, 2)   # exactly one retry, never more
        login.assert_called_once()
        self.assertEqual(r.status_code, 401)   # surfaced, not swallowed/crashed

    def test_relogin_failure_returns_original_401_without_crashing(self):
        """If re-login itself raises (backend unreachable, bad creds format,
        etc.), the helper must not propagate/crash -- it should hand back
        the original 401 response so the caller's existing try/except
        RequestException path (present at every call site) handles it."""
        headers = {"Authorization": "Bearer expired-token"}
        with mock.patch.object(self.module.requests, "post",
                                return_value=_resp(401)) as post, \
             mock.patch.object(self.module, "login",
                                side_effect=self.module.requests.RequestException("network down")):
            r = self.module._post_with_reauth(
                "http://console", "/api/detections/ingest", {"x": 1},
                headers, "op@example.com", "pw", timeout=5,
            )
        self.assertEqual(post.call_count, 1)   # no retry attempted -- re-login itself failed
        self.assertEqual(r.status_code, 401)


class HackrfRxReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = hackrf_rx


class MavlinkSnifferReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = mavlink_sniffer


class SikMavlinkBridgeReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = sik_mavlink_bridge


class CrsfParserReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = crsf_parser


class DroneCANParserReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = dronecan_parser


class CanopenParserReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = canopen_parser


class GraupnerHottParserReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = graupner_hott_parser


class LtmParserReauthTests(ReauthOnceTestsBase, unittest.TestCase):
    module = ltm_parser


class FpvVideoBridgeReauthTests(unittest.TestCase):
    """fpv_video_bridge.py's helper (_reauth_once) has a different shape --
    it only flips a bool and refreshes `headers`, leaving the actual
    GET/POST retry to each call site -- so it gets its own focused tests."""

    def setUp(self):
        import fpv_video_bridge
        self.mod = fpv_video_bridge

    def test_reauths_when_credentials_available(self):
        headers = {"Authorization": "Bearer expired"}
        with mock.patch.object(self.mod, "login", return_value="fresh-token") as login:
            should_retry = self.mod._reauth_once("http://console", headers, "op@example.com", "pw")
        self.assertTrue(should_retry)
        login.assert_called_once_with("http://console", "op@example.com", "pw")
        self.assertEqual(headers["Authorization"], "Bearer fresh-token")

    def test_no_retry_without_credentials(self):
        """A static --token/CEMA_TOKEN override with no --email/--password
        behind it cannot self-refresh -- must not retry, must not crash."""
        headers = {"Authorization": "Bearer static-token"}
        with mock.patch.object(self.mod, "login") as login:
            should_retry = self.mod._reauth_once("http://console", headers, None, None)
        self.assertFalse(should_retry)
        login.assert_not_called()
        self.assertEqual(headers["Authorization"], "Bearer static-token")  # untouched

    def test_no_retry_when_relogin_itself_fails(self):
        headers = {"Authorization": "Bearer expired"}
        with mock.patch.object(self.mod, "login",
                                side_effect=self.mod.requests.RequestException("down")):
            should_retry = self.mod._reauth_once("http://console", headers, "op@example.com", "pw")
        self.assertFalse(should_retry)


if __name__ == "__main__":
    unittest.main()
