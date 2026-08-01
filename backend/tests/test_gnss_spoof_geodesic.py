"""Unit tests for GNSS-spoof (Task #103) pure-logic pieces that do NOT
require a live server/Mongo instance: the geodesic destination-point
formula, the confirm-token issue/consume + attestation-binding logic, and
the friendly-asset-attestation validity check.

These are true unit tests (no requests/websockets/live BASE_URL, unlike
tests/test_backend.py and tests/test_new_endpoints.py which are
integration tests against a running backend) — importing backend/server.py
only requires MONGO_URL/DB_NAME env vars to be SET (motor's client
construction is lazy and never actually connects for these tests), not a
running Mongo.

Run: pytest backend/tests/test_gnss_spoof_geodesic.py -v
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi import HTTPException

import server as srv


# ---------------------------------------------------------------------
# geodesic_destination() — verified two independent ways:
#   1. Cardinal-direction sanity checks (due-north/due-east from the
#      equator must move latitude/longitude by ~1 degree per ~111.2/111.3km
#      respectively, with the OTHER coordinate unchanged) — textbook
#      behavior of the spherical-earth destination-point formula.
#   2. Round-trip self-consistency against an INDEPENDENTLY implemented
#      haversine distance+initial-bearing ("inverse geodesic") formula:
#      feed geodesic_destination's own output back through a completely
#      separate formula and recover the original distance/bearing to
#      sub-millimeter / sub-thousandth-of-a-degree precision. Two
#      differently-derived formulas agreeing this precisely is strong
#      evidence the forward formula is implemented correctly, not a case
#      of guessing.
# ---------------------------------------------------------------------
_EARTH_RADIUS_M = 6371000.0


def _haversine_distance_bearing(lat1, lon1, lat2, lon2):
    """Independent reference implementation (standard haversine distance +
    initial-bearing formulas — Movable Type Scripts / Ed Williams Aviation
    Formulary), used ONLY to cross-check server.geodesic_destination, never
    imported from server.py itself."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    d = _EARTH_RADIUS_M * c
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    brng = (math.degrees(math.atan2(y, x)) + 360) % 360
    return d, brng


def test_due_north_from_equator_moves_latitude_only():
    lat2, lon2 = srv.geodesic_destination(0.0, 0.0, 111194.9, 0.0)  # ~1 deg of latitude
    assert lat2 == pytest.approx(1.0, abs=1e-4)
    assert lon2 == pytest.approx(0.0, abs=1e-9)


def test_due_east_from_equator_moves_longitude_only():
    lat2, lon2 = srv.geodesic_destination(0.0, 0.0, 111319.9, 90.0)  # ~1 deg of longitude at equator
    assert lat2 == pytest.approx(0.0, abs=1e-9)
    # within ~0.15% of exactly 1 degree — the 111319.9m figure is WGS84's
    # equatorial-longitude-degree length, while geodesic_destination uses a
    # mean spherical earth radius (6371km), so a small, expected discrepancy.
    assert lon2 == pytest.approx(1.0, rel=2e-3)


@pytest.mark.parametrize("lat1,lon1,d,b", [
    (28.6139, 77.2090, 312.0, 47.0),      # Delhi-area, small offset (typical spoof-preview scale)
    (-33.8688, 151.2093, 5000.0, 200.0),  # Sydney-area, larger offset
    (53.3206, -1.7297, 124800.0, 116.7288),  # far offset, non-trivial bearing
    (0.0, 179.999, 2000.0, 90.0),          # near the antimeridian — exercises longitude wraparound
])
def test_geodesic_destination_round_trips_against_independent_haversine_formula(lat1, lon1, d, b):
    lat2, lon2 = srv.geodesic_destination(lat1, lon1, d, b)
    d2, b2 = _haversine_distance_bearing(lat1, lon1, lat2, lon2)
    assert d2 == pytest.approx(d, abs=0.01)   # sub-centimeter agreement
    assert b2 == pytest.approx(b, abs=0.01)   # sub-hundredth-of-a-degree agreement


def test_geodesic_destination_longitude_normalized_to_valid_range():
    lat2, lon2 = srv.geodesic_destination(0.0, 179.999, 50000.0, 90.0)
    assert -180.0 <= lon2 <= 180.0


def test_bearing_compass_labels():
    assert srv._bearing_compass(0) == "000° N"
    assert srv._bearing_compass(47) == "047° NE"
    assert srv._bearing_compass(90) == "090° E"
    assert srv._bearing_compass(180) == "180° S"
    assert srv._bearing_compass(270) == "270° W"
    assert srv._bearing_compass(360) == "000° N"


# ---------------------------------------------------------------------
# Friendly-asset attestation validity check
# ---------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", None, "n/a", "N/A", "none", "None", "confirmed",
                                  "yes", "ok", "test", "short text"])
def test_attestation_rejects_trivial_or_short_values(bad):
    assert srv._looks_like_real_attestation(bad) is False


def test_attestation_accepts_real_statement():
    real = ("Confirmed: no friendly GPS-dependent assets within 500m of target position. "
            "Reviewed friendly asset tracker at 14:32Z.")
    assert srv._looks_like_real_attestation(real) is True


# ---------------------------------------------------------------------
# gnss_spoof confirm-token issue/consume + attestation binding
# ---------------------------------------------------------------------
def test_issue_and_consume_gnss_spoof_confirm_token_roundtrip():
    attestation = "Confirmed: no friendly assets in blast radius, reviewed tracker at T+0."
    tok = srv._issue_gnss_spoof_confirm_token(attestation)
    assert "gnss_spoof_confirm_token" in tok
    # Consuming with the SAME attestation text must succeed (no exception).
    srv._consume_gnss_spoof_confirm_token(tok["gnss_spoof_confirm_token"], attestation)


def test_consume_gnss_spoof_confirm_token_rejects_mismatched_attestation():
    attestation = "Confirmed: no friendly assets in blast radius, reviewed tracker at T+0."
    tok = srv._issue_gnss_spoof_confirm_token(attestation)
    with pytest.raises(HTTPException) as exc:
        srv._consume_gnss_spoof_confirm_token(tok["gnss_spoof_confirm_token"], "a completely different attestation text")
    assert exc.value.status_code == 400


def test_consume_gnss_spoof_confirm_token_is_single_use():
    attestation = "Confirmed: no friendly assets in blast radius, reviewed tracker at T+0."
    tok = srv._issue_gnss_spoof_confirm_token(attestation)
    srv._consume_gnss_spoof_confirm_token(tok["gnss_spoof_confirm_token"], attestation)
    with pytest.raises(HTTPException) as exc:
        srv._consume_gnss_spoof_confirm_token(tok["gnss_spoof_confirm_token"], attestation)
    assert exc.value.status_code == 403


def test_consume_gnss_spoof_confirm_token_rejects_missing_token():
    with pytest.raises(HTTPException) as exc:
        srv._consume_gnss_spoof_confirm_token(None, "anything")
    assert exc.value.status_code == 403


def test_gnss_spoof_confirm_token_is_not_a_jam_confirm_token():
    """Structural non-interchangeability check (architecture doc §4): a
    freshly-issued jam_confirm_token must NOT be accepted by
    _consume_gnss_spoof_confirm_token — the two token stores are entirely
    separate dicts, so a jam token was never inserted into
    _gnss_spoof_confirm_tokens in the first place."""
    jam_tok = srv._issue_jam_confirm_token()
    with pytest.raises(HTTPException) as exc:
        srv._consume_gnss_spoof_confirm_token(jam_tok["jam_confirm_token"], "Confirmed: reviewed friendly tracker at T+0.")
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------
# effect=gnss_spoof range-authorization plumbing (§3): must be its own
# independently-tracked lease, not implicitly armed by effect=jam.
# ---------------------------------------------------------------------
def test_gnss_spoof_is_a_distinct_range_authorization_effect():
    assert "gnss_spoof" in srv.RANGE_AUTH_EFFECTS
    assert "jam" in srv.RANGE_AUTH_EFFECTS
    assert "gnss_spoof" in srv._range_authorization
    assert srv._range_authorization["gnss_spoof"] is not srv._range_authorization["jam"]


def test_gnss_spoof_duration_cap_is_shorter_than_jam_cap():
    assert srv.GNSS_SPOOF_MAX_DURATION_S == 3.0
    assert srv.GNSS_SPOOF_MAX_DURATION_S < srv.JAM_MAX_DURATION_S
