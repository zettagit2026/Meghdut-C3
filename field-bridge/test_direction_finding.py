#!/usr/bin/env python3
"""Tests for direction_finding.py -- amplitude-comparison (RSSI-ratio) DF.

All inputs are SYNTHETIC. No multi-antenna array hardware exists in this
project yet (see DIRECTION_FINDING_NOTES.md and the module docstring's HARDWARE
DEPENDENCY section). This validates the bearing MATH and the honesty contract
(single-antenna -> explicitly unavailable, never a fake 0 deg), not any real
antenna's calibration.

Feed synthetic RSSI vectors for known emitter bearings through the estimator
and assert it recovers the bearing within tolerance; check the uncertainty/
quality behaviour (high at beam-overlap crossover, low/ambiguous at edges);
and check the degenerate single-antenna case returns "unavailable".
"""
import math
import random

import pytest

from direction_finding import (
    AntennaMeasurement,
    DEFAULT_BEAMWIDTH_DEG,
    STATUS_OK,
    STATUS_UNAVAILABLE_SINGLE,
    STATUS_DEGENERATE_GEOMETRY,
    estimate_bearing,
    gaussian_beam_gain_db,
    synthesize_measurements,
)


# --- honesty contract: degenerate / single-antenna cases --------------------

def test_zero_antennas_unavailable_not_fake_zero():
    r = estimate_bearing([])
    assert r.available is False
    assert r.bearing_deg is None          # NOT 0.0
    assert r.quality == 0.0
    assert "multi-antenna" in r.status


def test_single_antenna_unavailable_not_fake_zero():
    r = estimate_bearing([AntennaMeasurement(boresight_deg=0.0, rssi_dbm=-50.0)])
    assert r.available is False
    assert r.bearing_deg is None          # the whole point: no fake 0 deg North
    assert r.status == STATUS_UNAVAILABLE_SINGLE


def test_single_antenna_ingest_fields_are_honest():
    fields = estimate_bearing(
        [AntennaMeasurement(0.0, -50.0)]).to_ingest_fields()
    assert fields["bearing_deg"] is None
    assert fields["bearing_available"] is False
    assert fields["bearing_estimated"] is False
    assert "unavailable" in fields["bearing_status"].lower()


def test_coincident_boresights_unavailable():
    # Two antennas pointed the same way -> no angular baseline.
    ms = [AntennaMeasurement(90.0, -40.0), AntennaMeasurement(90.0, -42.0)]
    r = estimate_bearing(ms)
    assert r.available is False
    assert r.status == STATUS_DEGENERATE_GEOMETRY


# --- bearing recovery: two antennas -----------------------------------------

@pytest.mark.parametrize("true_bearing", [0.0, 15.0, 45.0, 60.0, 75.0, 90.0])
def test_two_antenna_recovers_known_bearing(true_bearing):
    # Antennas squinted +/- around 45 deg; overlap covers ~0..90.
    boresights = [15.0, 75.0]
    ms = synthesize_measurements(true_bearing, boresights)
    r = estimate_bearing(ms)
    assert r.available is True
    assert r.bearing_deg == pytest.approx(true_bearing, abs=1.0)


def test_crossover_is_boresight_midpoint():
    # Emitter exactly between the two boresights -> equal RSSI -> midpoint,
    # best-conditioned (quality ~1).
    boresights = [30.0, 90.0]
    ms = synthesize_measurements(60.0, boresights)
    r = estimate_bearing(ms)
    assert r.bearing_deg == pytest.approx(60.0, abs=1e-6)
    assert r.delta_db == pytest.approx(0.0, abs=1e-9)
    assert r.quality == pytest.approx(1.0, abs=1e-3)
    assert r.ambiguous is False


def test_bearing_recovery_across_north_wraparound():
    # Boresights straddling 0/360 (350 and 50 -> midpoint 20).
    boresights = [350.0, 50.0]
    ms = synthesize_measurements(10.0, boresights)
    r = estimate_bearing(ms)
    assert r.available is True
    # 10 deg should come back as ~10, not ~ -350 or 370.
    assert 0.0 <= r.bearing_deg < 360.0
    assert r.bearing_deg == pytest.approx(10.0, abs=1.0)


# --- uncertainty / quality behaviour ----------------------------------------

def test_quality_higher_at_center_than_edge():
    boresights = [0.0, 90.0]
    center = estimate_bearing(synthesize_measurements(45.0, boresights))   # crossover
    near_edge = estimate_bearing(synthesize_measurements(80.0, boresights))  # toward a beam edge
    assert center.quality > near_edge.quality
    # Uncertainty should widen off-center.
    assert center.uncertainty_deg < near_edge.uncertainty_deg


def test_outside_overlap_flagged_ambiguous_low_quality():
    # Emitter well outside the two boresights (both antennas point away) ->
    # extrapolation past a beam edge -> ambiguous, low quality, NOT a crisp value.
    boresights = [40.0, 80.0]
    r = estimate_bearing(synthesize_measurements(160.0, boresights))
    assert r.available is True          # we still return a best-effort estimate
    assert r.ambiguous is True
    assert r.quality < 0.2
    fields = r.to_ingest_fields()
    assert "ambiguous" in fields["bearing_status"].lower()


# --- N > 2 antennas: strongest-pair selection -------------------------------

def test_four_antenna_compass_array_recovers_bearing():
    # Four antennas around the compass; estimator must pick the right sector.
    boresights = [0.0, 90.0, 180.0, 270.0]
    for true_bearing in (45.0, 135.0, 225.0, 315.0):
        ms = synthesize_measurements(true_bearing, boresights,
                                     beamwidth_deg=90.0)
        r = estimate_bearing(ms)
        assert r.available is True
        assert r.bearing_deg == pytest.approx(true_bearing, abs=2.0), true_bearing


# --- robustness under RSSI noise --------------------------------------------

def test_recovery_is_robust_to_small_rssi_noise():
    rng = random.Random(1234)
    boresights = [20.0, 80.0]
    errors = []
    for _ in range(200):
        true_bearing = rng.uniform(30.0, 70.0)
        ms = synthesize_measurements(true_bearing, boresights,
                                     noise_db=1.0, rng=rng)
        r = estimate_bearing(ms)
        errors.append(abs(r.bearing_deg - true_bearing))
    mean_err = sum(errors) / len(errors)
    # Coarse method under +/-1 dB jitter should still average within a few deg.
    assert mean_err < 5.0


# --- pattern model sanity ---------------------------------------------------

def test_pattern_model_peak_and_symmetry():
    assert gaussian_beam_gain_db(0.0, 0.0) == pytest.approx(0.0)      # peak at boresight
    left = gaussian_beam_gain_db(-30.0, 0.0)
    right = gaussian_beam_gain_db(30.0, 0.0)
    assert left == pytest.approx(right)                              # symmetric
    assert gaussian_beam_gain_db(0.0, 0.0) > gaussian_beam_gain_db(40.0, 0.0)  # falls off


def test_pattern_model_minus_3db_at_half_beamwidth():
    # By construction 12*((BW/2)/BW)^2 = 12*0.25 = 3 dB at half-beamwidth offset.
    g = gaussian_beam_gain_db(DEFAULT_BEAMWIDTH_DEG / 2.0, 0.0)
    assert g == pytest.approx(-3.0, abs=1e-6)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
