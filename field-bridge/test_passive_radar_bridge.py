"""Integration test for passive_radar_bridge.py (task #43, C10): CLI wiring
of DualChannelSource -> alignment -> dsi_suppression -> caf -> detector ->
geometry -> /api/detections/ingest field mapping.

No real HackRF/RTL-SDR hardware, no live backend -- requests.post is
monkeypatched, matching test_gnss_spoof_bridge.py's/test_reauth_on_401.py's
existing convention for testing bridge classes without a live server. Per
this project's standing rule to never write test detections into a
real/demo tenant, this test never touches a live backend at all.

Run: pytest field-bridge/test_passive_radar_bridge.py -v
"""
from __future__ import annotations

import os

os.environ.setdefault("CEMA_API_URL", "http://backend.invalid")
os.environ.setdefault("CEMA_EMAIL", "test@unused.local")
os.environ.setdefault("CEMA_PASSWORD", "unused")

import numpy as np
import pytest

import passive_radar_bridge as prb
from passive_radar.channel_source import SyntheticDualChannelSource
from passive_radar.illuminator_profile import FM_BROADCAST_PLACEHOLDER
from passive_radar.geometry import ReceiverGeometry


def test_process_block_detects_injected_target():
    source = SyntheticDualChannelSource(
        sample_rate_hz=2.048e6,
        targets=[(35, 56.0, 1.0)],
        seed=13,
    )
    ref, surv = source.read_block(6000)
    detections = prb.process_block(
        ref, surv, source.sample_rate_hz,
        max_lag=128, doppler_hz=np.arange(-100, 101, 4), min_snr_db=5.0,
    )
    assert len(detections) >= 1
    best = detections[0]
    assert abs(best.range_lag_samples - 35) <= 1
    # CAF's doppler_hz axis reports -Dtrue -- see caf.py's SIGN CONVENTION note.
    assert abs(best.doppler_hz - (-56.0)) <= 4


def test_detection_to_ingest_body_field_mapping():
    source = SyntheticDualChannelSource(sample_rate_hz=2.048e6, targets=[(30, 50.0, 1.0)], seed=4)
    ref, surv = source.read_block(6000)
    detections = prb.process_block(ref, surv, source.sample_rate_hz, max_lag=128, min_snr_db=5.0)
    assert detections
    receiver = ReceiverGeometry(lat=1.0, lon=2.0, alt_m=3.0, surveillance_antenna_boresight_deg=90.0)
    body = prb.detection_to_ingest_body(detections[0], FM_BROADCAST_PLACEHOLDER, receiver)

    assert body["source"] == "PASSIVE_RADAR"
    assert body["distance_estimated"] is False  # genuine time-of-flight range, not RSSI guess
    assert body["confidence_type"] == "bistatic_radar_detection"
    assert body["protocol_confirmed"] is False
    assert body["bearing_deg"] == 90.0
    assert body["model"] == "passive-bistatic-radar-caf-v1"
    assert body["distance_m"] >= 0
    assert isinstance(body["snr_db"], float)


def test_run_once_synthetic_end_to_end_posts_to_monkeypatched_backend(monkeypatch):
    posted = []

    class FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"token": "fake-token"}

        def raise_for_status(self):
            return None

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/api/auth/login"):
            return FakeResponse()
        posted.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(prb.requests, "post", fake_post)
    monkeypatch.setattr("hackrf_rx.requests.post", fake_post, raising=False)

    class Args:
        source = "synthetic"
        sample_rate_hz = 2.048e6
        block_samples = 6000
        max_lag = 128
        min_snr_db = 5.0
        no_dsi = False
        illuminator = "fm"
        receiver_lat = 0.0
        receiver_lon = 0.0
        receiver_alt_m = 0.0
        antenna_boresight_deg = 45.0
        console_url = "http://backend.invalid"
        email = "test@unused.local"
        password = "unused"
        once = True
        ref_file = None
        surv_file = None
        dtype = "int8"
        skip_samples = 0

    headers = {"Authorization": "Bearer fake-token"}
    rc = prb.run_once(Args(), headers)
    assert rc == 0
    # A synthetic source with default target params should produce at
    # least one posted detection within a single block.
    assert len(posted) >= 1
    _, body = posted[0]
    assert body["source"] == "PASSIVE_RADAR"


def test_dual_rtlsdr_source_stub_returns_error_not_crash():
    class Args:
        source = "rtlsdr-dual"
        sample_rate_hz = 2.048e6
        block_samples = 1000
        max_lag = 64
        min_snr_db = 5.0
        no_dsi = False
        illuminator = "fm"
        receiver_lat = 0.0
        receiver_lon = 0.0
        receiver_alt_m = 0.0
        antenna_boresight_deg = 0.0
        console_url = None
        email = None
        password = None
        once = True
        ref_file = None
        surv_file = None
        dtype = "int8"
        skip_samples = 0

    rc = prb.run_once(Args(), {})
    assert rc == 1
