#!/usr/bin/env python3
"""Unit tests for droneid_cued_capture.py -- the CUED, device-lock-guarded,
non-contending DroneID capture. No hardware, no network, no DroneSecurity deps.

The most important test here is test_capture_yields_when_radio_busy: it proves
the cued capture NEVER seizes the HackRF while the detection sweep holds the
device lock -- it skips the cue instead of starving detection.
"""
import ast
import os
import sys
import types

import pytest

import droneid_cued_capture as dc
from hackrf_device_lock import hackrf_device_lock


# --------------------------------------------------------------------------
# Cue selection logic.
# --------------------------------------------------------------------------
def test_in_band_drone_hint_is_a_candidate():
    det = {"status": "ACTIVE", "center_freq_ghz": 2.4405, "model": "DJI Mini (candidate)"}
    assert dc.is_dji_ocusync_candidate(det) is True


def test_in_band_without_drone_hint_is_not_a_candidate():
    # An in-band Wi-Fi AP must NOT trigger a capture (no ambient-2.4GHz cueing).
    det = {"status": "ACTIVE", "center_freq_ghz": 2.44, "model": "WiFi AP", "protocol": "802.11"}
    assert dc.is_dji_ocusync_candidate(det) is False


def test_ml_drone_label_is_a_candidate():
    det = {"status": "ACTIVE", "center_freq_ghz": 5.8, "ml_label": "drone"}
    assert dc.is_dji_ocusync_candidate(det) is True


def test_out_of_band_is_not_a_candidate():
    det = {"status": "ACTIVE", "center_freq_ghz": 1.2, "model": "DJI something"}
    assert dc.is_dji_ocusync_candidate(det) is False


def test_inactive_detection_is_not_a_candidate():
    det = {"status": "NEUTRALIZED", "center_freq_ghz": 2.44, "model": "DJI"}
    assert dc.is_dji_ocusync_candidate(det) is False


def test_select_cue_targets_strongest_candidate():
    dets = [
        {"status": "ACTIVE", "center_freq_ghz": 2.4405, "model": "DJI Mini", "rssi_dbm": -50},
        {"status": "ACTIVE", "center_freq_ghz": 5.8, "ml_label": "drone", "rssi_dbm": -70},
    ]
    cue = dc.select_cue_frequency_mhz(dets)
    assert cue == 2444.5  # nearest OcuSync channel to the strongest (-50 dBm) candidate


def test_select_cue_none_when_no_candidate():
    assert dc.select_cue_frequency_mhz(
        [{"status": "ACTIVE", "center_freq_ghz": 1.2, "model": "x"}]) is None


def test_nearest_candidate_channel():
    assert dc.nearest_candidate_mhz(5.802) == 5801.5


# --------------------------------------------------------------------------
# Candidate list stays in sync with the reused decoder's source list.
# --------------------------------------------------------------------------
def test_candidate_list_matches_droneid_decode_bridge_source():
    """The local CANDIDATE_FREQS_MHZ mirrors droneid_decode_bridge's list; assert
    equality by parsing that file's source (no heavy import of the decoder)."""
    src_path = os.path.join(os.path.dirname(__file__), "droneid_decode_bridge.py")
    with open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "CANDIDATE_FREQS_MHZ":
                    found = ast.literal_eval(node.value)
    assert found is not None, "CANDIDATE_FREQS_MHZ not found in droneid_decode_bridge.py"
    assert dc.CANDIDATE_FREQS_MHZ == found


# --------------------------------------------------------------------------
# NON-CONTENTION: the cued capture yields to the detection sweep.
# --------------------------------------------------------------------------
def _install_fake_droneid_module(serial):
    """Inject a fake droneid_decode_bridge whose capture_iq FAILS the test if
    ever called -- so we can prove the capture was never attempted while the
    radio lock was held."""
    fake = types.ModuleType("droneid_decode_bridge")
    fake.HACKRF_SERIAL = serial
    fake.DEFAULT_SRC_DIR = "/nonexistent"

    def _capture_iq(*a, **k):
        raise AssertionError("capture_iq must NOT be called while the HackRF lock is held")

    fake.capture_iq = _capture_iq
    fake.load_sigmf = lambda *a, **k: (None, None, None, None)
    fake.decode_capture = lambda *a, **k: []
    sys.modules["droneid_decode_bridge"] = fake
    return fake


def test_capture_yields_when_radio_busy(monkeypatch):
    serial = "CUEDTESTSERIAL"
    _install_fake_droneid_module(serial)
    try:
        # Simulate the detection sweep holding the device lock for this serial.
        with hackrf_device_lock(serial=serial):
            result = dc.cued_capture_once(
                2444.5, console_url="http://x", headers={}, email="e", password="p",
                sample_rate_hz=16e6, capture_s=1.0, modules=None, tmp_dir="/tmp",
                lock_timeout_s=0.2,  # short -> bail fast, never block detection
            )
        # Busy radio -> honest skip, no decode, no fabricated detection.
        assert result is False
    finally:
        sys.modules.pop("droneid_decode_bridge", None)
