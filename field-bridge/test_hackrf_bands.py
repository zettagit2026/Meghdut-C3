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
    ELRS_BT_LIKE_MIN_HZ,
    ELRS_HOP_RATE_RANGE_HZ,
    EXTRA_BANDS_MHZ,
    HOP_CONFIRM_CYCLES,
    classify_hop_interval,
    load_bands_config,
    update_hop_track,
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


# --- ELRS/Crossfire-class hop-interval-consistency heuristic (task #88) -----
# Covers classify_hop_interval() (pure function) and update_hop_track()
# (stateful, same shape as wifi_persist/bt_track/sik_hit_window) against
# synthetic reappearance-interval sequences -- no real HackRF/subprocess
# involved, same convention as the rest of this file.


def test_hop_range_matches_source_derived_values():
    """Guard against an accidental edit to the constants -- these are derived
    directly from ExpressLRS's common.h/common.cpp (see hackrf_rx.py comment
    block), not arbitrary."""
    assert ELRS_HOP_RATE_RANGE_HZ == (12.5, 125.0)
    assert ELRS_BT_LIKE_MIN_HZ > ELRS_HOP_RATE_RANGE_HZ[1]


def test_classify_hop_interval_within_elrs_range():
    for hz in (12.5, 25.0, 50.0, 62.5, 100.0, 125.0):
        assert classify_hop_interval(1.0 / hz) == "elrs_consistent"


def test_classify_hop_interval_bt_like_speed():
    # Real BT/BLE hops ~1600/sec -- far faster than any ELRS air rate.
    assert classify_hop_interval(1.0 / 1600.0) == "bt_like"
    assert classify_hop_interval(1.0 / 800.0) == "bt_like"


def test_classify_hop_interval_stationary_or_no_prior_sample():
    assert classify_hop_interval(None) == "insufficient"
    assert classify_hop_interval(0) == "insufficient"
    assert classify_hop_interval(-1.0) == "insufficient"
    # A signal that reappears only once every several seconds (this bridge's
    # own sweep cadence, not real hopping) must NOT be misclassified as ELRS.
    assert classify_hop_interval(3.0) == "no_match"
    assert classify_hop_interval(30.0) == "no_match"


def _fresh_track():
    return {"last_time": None, "last_power_dbm": None, "consistent_cycles": 0,
            "last_rate_hz": None}


def test_update_hop_track_synthetic_elrs_sequence_flags():
    """A synthetic sequence of same-power hits spaced at a 50Hz-consistent
    interval (0.02s) must flag hop_consistent once HOP_CONFIRM_CYCLES
    consecutive reappearances have been observed, and not before."""
    track = _fresh_track()
    now = 1000.0
    peak_dbm = -40.0
    consistent_flags = []
    # first hit only establishes the baseline (no prior sample yet)
    consistent, _ = update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
    consistent_flags.append(consistent)
    for _ in range(HOP_CONFIRM_CYCLES + 2):
        now += 1.0 / 50.0  # 50Hz-consistent reappearance interval
        consistent, rate = update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
        consistent_flags.append(consistent)
    assert consistent_flags[-1] is True
    assert not all(consistent_flags[:HOP_CONFIRM_CYCLES - 1])  # not flagged prematurely
    assert rate is not None and abs(rate - 50.0) < 1.0


def test_update_hop_track_bluetooth_speed_sequence_does_not_flag_elrs():
    """A sequence matching Bluetooth-speed non-persistence (implied rate far
    above ELRS_HOP_RATE_RANGE_HZ) must never flag hop_consistent -- it should
    route away from an ELRS conclusion instead of double-flagging."""
    track = _fresh_track()
    now = 2000.0
    peak_dbm = -45.0
    update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
    for _ in range(10):
        now += 1.0 / 1600.0  # real Bluetooth-class hop rate
        consistent, rate = update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
        assert consistent is False
        assert rate is None


def test_update_hop_track_stationary_signal_does_not_false_positive():
    """A stationary/non-hopping signal (reappears every cycle at this
    bridge's own several-second sweep cadence) must never be misclassified
    as ELRS-consistent."""
    track = _fresh_track()
    now = 3000.0
    peak_dbm = -42.0
    update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
    for _ in range(10):
        now += 3.0  # this bridge's own sweep cadence, not real hopping
        consistent, rate = update_hop_track(track, now, peak_dbm, is_hit=True, is_real_data=True)
        assert consistent is False
        assert rate is None


def test_update_hop_track_resets_on_genuine_miss():
    track = _fresh_track()
    now = 4000.0
    update_hop_track(track, now, -40.0, is_hit=True, is_real_data=True)
    now += 0.02
    update_hop_track(track, now, -40.0, is_hit=True, is_real_data=True)
    # a genuine (real-data) miss must reset the pattern
    now += 0.02
    consistent, rate = update_hop_track(track, now, -40.0, is_hit=False, is_real_data=True)
    assert consistent is False
    assert rate is None
    assert track["consistent_cycles"] == 0
    assert track["last_time"] is None


def test_update_hop_track_ignores_wedge_filler_cycles():
    """A wedge/fallback filler cycle (is_real_data=False) must leave the
    tracker untouched, same convention as wifi_persist/bt_track/sik_hit_window."""
    track = _fresh_track()
    now = 5000.0
    update_hop_track(track, now, -40.0, is_hit=True, is_real_data=True)
    before = dict(track)
    # a wedged cycle should not reset or advance anything
    update_hop_track(track, now + 100.0, -999.0, is_hit=False, is_real_data=False)
    assert track == before


def test_update_hop_track_large_power_jump_treated_as_different_emitter():
    """A reappearance far outside HOP_POWER_TOL_DB of the previous hit is
    treated as a different/unrelated emitter, not a continuing hop pattern."""
    track = _fresh_track()
    now = 6000.0
    update_hop_track(track, now, -40.0, is_hit=True, is_real_data=True)
    now += 0.02
    consistent, rate = update_hop_track(track, now, -10.0, is_hit=True, is_real_data=True)
    assert consistent is False
    assert rate is None
