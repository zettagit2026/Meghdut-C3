"""Unit tests for Task #16 (Bluetooth jam target) on the backend side:
JAM_BAND_PRESETS_MHZ / JamRequestBody.band pattern / JAM_GNSS_BANDS.

True unit tests (no requests/websockets/live BASE_URL, no Mongo connection
required) -- same pattern as test_gnss_spoof_geodesic.py: importing
backend/server.py only requires MONGO_URL/DB_NAME/etc env vars to be SET
(motor's client construction is lazy), not a running Mongo.

Run: pytest backend/tests/test_jam_bluetooth_band.py -v
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import server as srv


def test_bt_2g4_present_in_jam_band_presets():
    assert "bt_2g4" in srv.JAM_BAND_PRESETS_MHZ
    freq = srv.JAM_BAND_PRESETS_MHZ["bt_2g4"]
    assert 2400.0 <= freq <= 2483.5


def test_bt_2g4_matches_field_bridge_hackrf_jam_preset():
    """Backend's duplicated preset dict must stay in sync with
    field-bridge/hackrf_jam.py's BAND_PRESETS_MHZ (same convention already
    enforced informally by comments for the other bands)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "field-bridge"))
    import hackrf_jam as hj
    assert srv.JAM_BAND_PRESETS_MHZ["bt_2g4"] == hj.BAND_PRESETS_MHZ["bt_2g4"]


def test_bt_2g4_not_a_gnss_band():
    assert "bt_2g4" not in srv.JAM_GNSS_BANDS


def test_jam_request_body_band_pattern_accepts_bt_2g4():
    field = srv.JamRequestBody.model_fields["band"]
    pattern = field.metadata[0].pattern if field.metadata else None
    assert pattern is not None
    assert re.match(pattern, "bt_2g4")


def _dummy_tokens() -> dict:
    # arm_token/jam_confirm_token are required fields on JamRequestBody but
    # carry no meaning for these pure band/pattern-validation tests -- their
    # real single-use/shape validation is exercised elsewhere
    # (test_gnss_spoof_geodesic.py's jam-confirm-token tests, and the
    # endpoint's own _consume_arm_token/_consume_jam_confirm_token calls).
    return {"arm_token": "dummy-arm-token", "jam_confirm_token": "dummy-jam-confirm-token-00000000"}


def test_jam_request_body_accepts_bt_2g4_band_value():
    body = srv.JamRequestBody(band="bt_2g4", **_dummy_tokens())
    assert body.band == "bt_2g4"


def test_jam_request_body_rejects_bogus_band_value():
    with pytest.raises(Exception):
        srv.JamRequestBody(band="not_a_real_band", **_dummy_tokens())


def test_all_jam_band_presets_have_valid_regex_entries():
    """Every key actually present in JAM_BAND_PRESETS_MHZ must be accepted by
    JamRequestBody's band pattern -- guards against the dict and the pattern
    string silently drifting apart again in the future."""
    for band in srv.JAM_BAND_PRESETS_MHZ:
        body = srv.JamRequestBody(band=band, **_dummy_tokens())
        assert body.band == band
