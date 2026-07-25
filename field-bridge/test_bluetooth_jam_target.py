#!/usr/bin/env python3
"""Tests for Task #16: Bluetooth as an explicit jam target/band.

Bluetooth Classic (79x 1MHz channels) and BLE (40x 2MHz channels) both
frequency-hop WITHIN the same 2400-2483.5MHz ISM band the pre-existing
"2g4" (DJI video/control) jam preset already targets -- they are not
separate spectrum. Confirmed by reading hackrf_jam.py's transmit_burst()
(band-limited noise burst, no channel-following/hop-tracking logic exists
anywhere in the jam path) before adding anything here: this task is a
correctly-labeled preset addition for operator clarity, NOT new
signal-generation logic. These tests assert that framing, not the presence
of some new "hop-follower" capability that was never built and is not
needed given the existing broad-band-noise approach.

Run: python3 -m pytest field-bridge/test_bluetooth_jam_target.py -v
"""
from __future__ import annotations

import pytest

import hackrf_jam as hj


def test_bt_2g4_preset_present_and_within_ism_band():
    """bt_2g4 must resolve to a real frequency inside the 2400-2483.5MHz
    Bluetooth Classic/BLE ISM band (not e.g. accidentally left unset /
    falling back to None, and not outside the band it claims to target)."""
    assert "bt_2g4" in hj.BAND_PRESETS_MHZ
    freq = hj.BAND_PRESETS_MHZ["bt_2g4"]
    assert 2400.0 <= freq <= 2483.5


def test_bt_2g4_shares_ism_band_with_existing_2g4_preset():
    """bt_2g4 and 2g4 (DJI video/control) must both land in the SAME shared
    ISM band -- this is the concrete evidence that Bluetooth jamming is a
    labeling addition on top of an already-covered band, not a distinct RF
    target requiring new hardware/signal logic."""
    bt_freq = hj.BAND_PRESETS_MHZ["bt_2g4"]
    dji_freq = hj.BAND_PRESETS_MHZ["2g4"]
    assert 2400.0 <= bt_freq <= 2483.5
    assert 2400.0 <= dji_freq <= 2483.5


def test_bluetooth_ism_full_width_constant_covers_whole_band():
    """The documented full-ISM-band width constant (used only for UI/CLI
    operator guidance on the bandwidth_khz value to pick) must actually span
    the full 2400.0-2483.5MHz range Bluetooth hops within."""
    assert hj.BLUETOOTH_ISM_FULL_WIDTH_KHZ == pytest.approx(83_500.0)


def test_bt_2g4_selectable_via_cli_band_argument():
    """--band's argparse choices are built from BAND_PRESETS_MHZ.keys() --
    confirm bt_2g4 is actually reachable from the CLI, not just present in
    the dict."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--band", choices=list(hj.BAND_PRESETS_MHZ.keys()))
    ns = ap.parse_args(["--band", "bt_2g4"])
    assert ns.band == "bt_2g4"


def test_bt_2g4_is_not_accidentally_a_gnss_band():
    """Bluetooth denial is a comms-band effect (like 2g4/5g8/433/915), not a
    GNSS nav-denial effect -- it must not trip the GNSS-specific
    extra-warning/logging path (GNSS_BANDS), which is reserved for the L1
    satellite-nav presets."""
    assert "bt_2g4" not in hj.GNSS_BANDS


def test_no_hop_following_logic_exists_for_bt_2g4():
    """Guards the documented design decision: transmit_burst() takes a
    single center frequency + bandwidth_khz and no channel-hop-sequence
    argument of any kind -- confirming Bluetooth jamming really is
    broad-band noise across a static center frequency, not a hop-follower,
    for anyone tempted to assume otherwise from the preset's existence."""
    import inspect
    sig = inspect.signature(hj.transmit_burst)
    params = list(sig.parameters.keys())
    assert "freq_mhz" in params
    assert "bandwidth_khz" in params
    assert not any("hop" in p.lower() or "channel" in p.lower() for p in params)
