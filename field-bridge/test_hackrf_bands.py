#!/usr/bin/env python3
"""Unit tests for hackrf_rx.py's band-configuration logic (task #51/C18,
Army directive 2026-07-25 priority (a): expand spectrum sweep coverage).

Covers load_bands_config()'s three configuration paths (built-in default+
extra, HACKRF_EXTRA_BANDS opt-out, HACKRF_BANDS_JSON full override) and the
per-band detection-metadata table (BAND_DETECTION_META) that replaced the
old `"DJI" in name` inline-ternary labeling. These are pure config/logic
tests -- no real HackRF hardware or subprocess calls are exercised here,
consistent with this being unit-level coverage of the new band-list logic,
not an integration test of the sweep itself.

Run: pytest field-bridge/test_hackrf_bands.py -v
"""
import json

import pytest

from hackrf_rx import (
    BAND_DETECTION_META,
    DEFAULT_BANDS_MHZ,
    DEFAULT_DETECTION_META,
    EXTRA_BANDS_MHZ,
    load_bands_config,
)


def test_default_bands_unchanged():
    """The original 3 hardcoded bands must be byte-for-byte unchanged --
    this is additive, not a rewrite."""
    assert DEFAULT_BANDS_MHZ == [
        ("SiK-915", 902, 928, "SiK/ISM 915MHz"),
        ("DJI-2G4", 2400, 2483, "OcuSync/Wi-Fi 2.4GHz"),
        ("DJI-5G8", 5725, 5850, "OcuSync 5.8GHz"),
    ]


def test_default_env_enables_extra_bands():
    """With no env vars set at all, the new bands are ON by default
    (opt-out, not opt-in) per the directive's priority on expanding
    coverage now."""
    bands = load_bands_config(env={})
    names = [b[0] for b in bands]
    assert names == ["SiK-915", "DJI-2G4", "DJI-5G8", "LRS-433", "SRD-868", "FPV-1G3"]
    # existing bands' (low, high, label) fields must be untouched
    for b in DEFAULT_BANDS_MHZ:
        assert b in bands


@pytest.mark.parametrize("off_value", ["0", "false", "False", "no", "off", "OFF"])
def test_extra_bands_can_be_disabled(off_value):
    bands = load_bands_config(env={"HACKRF_EXTRA_BANDS": off_value})
    assert bands == DEFAULT_BANDS_MHZ


def test_extra_bands_explicit_on_values_still_enable():
    for on_value in ["1", "true", "yes", "anything-else"]:
        bands = load_bands_config(env={"HACKRF_EXTRA_BANDS": on_value})
        assert len(bands) == len(DEFAULT_BANDS_MHZ) + len(EXTRA_BANDS_MHZ)


def test_bands_json_full_override():
    custom = [["ONLY-BAND", 100, 200, "custom test band"]]
    bands = load_bands_config(env={"HACKRF_BANDS_JSON": json.dumps(custom)})
    assert bands == [("ONLY-BAND", 100, 200, "custom test band")]


def test_bands_json_takes_precedence_over_extra_flag():
    """HACKRF_BANDS_JSON is a full override -- HACKRF_EXTRA_BANDS should be
    irrelevant when it's set, since load_bands_config() returns immediately
    from the override branch."""
    custom = [["X", 1, 2, "x"]]
    bands = load_bands_config(env={
        "HACKRF_BANDS_JSON": json.dumps(custom),
        "HACKRF_EXTRA_BANDS": "1",
    })
    assert bands == [("X", 1, 2, "x")]


@pytest.mark.parametrize("bad_json", [
    "not json at all",
    "[]",  # valid JSON, but empty -- must not silently sweep zero bands
    "[[1,2,3]]",  # wrong shape (missing 4th element)
    '{"not": "a list"}',
])
def test_bands_json_invalid_falls_back_to_default(bad_json, capsys):
    bands = load_bands_config(env={"HACKRF_BANDS_JSON": bad_json})
    # falls back to the normal default+extra path (HACKRF_EXTRA_BANDS unset)
    assert bands == DEFAULT_BANDS_MHZ + EXTRA_BANDS_MHZ
    captured = capsys.readouterr()
    assert "invalid HACKRF_BANDS_JSON" in captured.err


def test_every_configured_band_has_detection_metadata():
    """Every band in DEFAULT_BANDS_MHZ + EXTRA_BANDS_MHZ must resolve to a
    real (non-fallback) entry in BAND_DETECTION_META, so a configured band
    never silently degrades to the generic 'Unidentified emitter' label."""
    for name, *_ in DEFAULT_BANDS_MHZ + EXTRA_BANDS_MHZ:
        assert name in BAND_DETECTION_META, f"{name} missing from BAND_DETECTION_META"


def test_original_band_labels_unchanged():
    """The original DJI/SiK label strings the old inline ternaries produced
    must be byte-for-byte preserved after generalizing to a table."""
    assert BAND_DETECTION_META["SiK-915"] == {
        "model": "MAVLink craft (candidate)",
        "protocol": "SiK/MAVLink",
        "source": "SIK_RF_HEURISTIC",
    }
    assert BAND_DETECTION_META["DJI-2G4"]["model"] == "DJI Mini (candidate)"
    assert BAND_DETECTION_META["DJI-2G4"]["protocol"] == "OcuSync/Wi-Fi"
    assert BAND_DETECTION_META["DJI-2G4"]["source"] == "HACKRF"
    assert BAND_DETECTION_META["DJI-5G8"]["model"] == "DJI Mini (candidate)"
    assert BAND_DETECTION_META["DJI-5G8"]["protocol"] == "OcuSync/Wi-Fi"


def test_unknown_band_name_falls_back_to_default_metadata():
    assert BAND_DETECTION_META.get("NOT-A-REAL-BAND", DEFAULT_DETECTION_META) is DEFAULT_DETECTION_META
