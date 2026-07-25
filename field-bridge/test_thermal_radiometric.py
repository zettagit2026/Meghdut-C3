#!/usr/bin/env python3
"""Tests for thermal_radiometric.py (task #64/#83 close-out, AI Engineer,
2026-07-25).

MATH-LEVEL TESTS ONLY. No FLIR-calibrated R-JPEG sample file exists in this
project (no thermal camera hardware -- see thermal_radiometric.py's module
docstring), so extract_flir_calibration_constants / extract_raw_thermal_array
/ radiometric_temperature_array cannot be exercised end-to-end against real
camera output here. What IS tested: (1) the Planck-inversion formula itself
against independently hand-computed expected values, (2) exiftool-missing
error handling, (3) input validation. Do not read a pass here as "this
extracts correct temperatures from a real FLIR file."

Run: pytest field-bridge/test_thermal_radiometric.py -v
"""
import math

import numpy as np
import pytest

from thermal_radiometric import (
    EXIFTOOL_MISSING_MSG,
    FLIR_PLANCK_TAGS,
    FlirPlanckConstants,
    extract_flir_calibration_constants,
    extract_raw_thermal_array,
    raw_array_to_temperature_celsius,
    raw_to_temperature_celsius,
)


# Representative FLIR Planck constants -- these are the kind of values
# actually embedded by FLIR Tau2/Boson-class sensors (order-of-magnitude
# plausible, not read from any specific real file since none exists in
# this project). Used only to exercise the formula's arithmetic, not as a
# claim about any specific camera model's true calibration.
SAMPLE_CONSTANTS = FlirPlanckConstants(
    r1=21106.77, r2=0.012545258, b=1501.0, f=1.0, o=-7340.0,
)


def _hand_computed_celsius(raw: float, c: FlirPlanckConstants) -> float:
    """Independently re-derives the formula here (rather than importing
    the module's own implementation) so the test is a genuine cross-check,
    not a tautology. This is the SAME public FLIR single-band Planck-law
    inversion documented in thermal_radiometric.py's module docstring:
    T_kelvin = B / ln(R1 / (R2*(raw+O)) + F); T_celsius = T_kelvin - 273.15
    """
    kelvin = c.b / math.log(c.r1 / (c.r2 * (raw + c.o)) + c.f)
    return kelvin - 273.15


@pytest.mark.parametrize("raw", [8000, 12000, 15000, 20000])
def test_raw_to_temperature_matches_hand_computed_formula(raw):
    expected = _hand_computed_celsius(raw, SAMPLE_CONSTANTS)
    actual = raw_to_temperature_celsius(raw, SAMPLE_CONSTANTS)
    assert actual == pytest.approx(expected, rel=1e-9)


def test_raw_to_temperature_plausible_range():
    """Sanity bound: with these representative constants, a raw count in
    the sensor's typical operating range should produce a temperature in
    a physically plausible range for a scene the camera would actually be
    pointed at (not literally validated against a real scene -- just a
    sanity check that the formula isn't producing nonsense like -50000C)."""
    t = raw_to_temperature_celsius(14000, SAMPLE_CONSTANTS)
    assert -40.0 < t < 200.0


def test_raw_array_vectorized_matches_scalar_elementwise():
    raw_values = [8000, 9500, 11000, 13500, 17000]
    array_result = raw_array_to_temperature_celsius(
        np.array(raw_values, dtype=np.float64), SAMPLE_CONSTANTS
    )
    for raw, expected_array_val in zip(raw_values, array_result):
        scalar_result = raw_to_temperature_celsius(raw, SAMPLE_CONSTANTS)
        assert float(expected_array_val) == pytest.approx(scalar_result, rel=1e-9)


def test_raw_array_vectorized_shape_2d():
    raw_frame = np.full((4, 6), 12000, dtype=np.float64)
    temps = raw_array_to_temperature_celsius(raw_frame, SAMPLE_CONSTANTS)
    assert temps.shape == (4, 6)
    expected_scalar = raw_to_temperature_celsius(12000, SAMPLE_CONSTANTS)
    assert np.allclose(temps, expected_scalar)


def test_monotonic_increasing_raw_gives_increasing_temperature():
    """Higher raw digital count -> higher scene temperature is the whole
    physical point of a radiometric sensor; a formula that didn't preserve
    this monotonic relationship over its normal operating range would be
    wrong regardless of any single hand-computed match."""
    raws = [8000, 9000, 12000, 15000, 18000]
    temps = [raw_to_temperature_celsius(r, SAMPLE_CONSTANTS) for r in raws]
    assert temps == sorted(temps)


def test_flir_planck_tags_are_the_five_documented_fields():
    """Pin the exact tag set this module reads so a future edit can't
    silently drift from the documented FLIR field names (PlanckR1/R2/B/F/O)
    without a visible test failure."""
    assert set(FLIR_PLANCK_TAGS) == {"PlanckR1", "PlanckR2", "PlanckB", "PlanckF", "PlanckO"}


def test_extract_calibration_constants_raises_without_exiftool(monkeypatch):
    """Whether or not exiftool happens to be installed on the machine
    running this test, force the not-installed path so the honest
    error-message behavior is verified deterministically."""
    import shutil as shutil_mod
    monkeypatch.setattr(shutil_mod, "which", lambda name: None)
    import thermal_radiometric
    monkeypatch.setattr(thermal_radiometric.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc_info:
        extract_flir_calibration_constants("/nonexistent/does_not_matter.jpg")
    assert "exiftool" in str(exc_info.value).lower()


def test_extract_raw_thermal_array_raises_without_exiftool(monkeypatch):
    import thermal_radiometric
    monkeypatch.setattr(thermal_radiometric.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError) as exc_info:
        extract_raw_thermal_array("/nonexistent/does_not_matter.jpg")
    assert "exiftool" in str(exc_info.value).lower()


def test_exiftool_missing_msg_mentions_install_guidance():
    assert "exiftool" in EXIFTOOL_MISSING_MSG.lower()
    assert "install" in EXIFTOOL_MISSING_MSG.lower()
