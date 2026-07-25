"""Unit tests for detector.py's CFAR/peak-picking (task #43, C10).

New relative to the reference repo (which stops at "here's an image") --
tested against synthetic CAF output with known injected targets, per
PASSIVE_RADAR_ARCHITECTURE.md §5 step 5.

Run: pytest field-bridge/passive_radar/test_detector.py -v
"""
import numpy as np

from passive_radar.caf import compute_caf
from passive_radar.detector import topk_peaks, cfar_ca_detect
from passive_radar.channel_source import SyntheticDualChannelSource
from passive_radar.alignment import align_channels
from passive_radar.dsi_suppression import suppress_dsi


def _caf_with_target(delay_samples=30, doppler_hz=52.0, n=6000, seed=9, dsi=True):
    source = SyntheticDualChannelSource(
        sample_rate_hz=2.048e6,
        targets=[(delay_samples, doppler_hz, 1.0)],
        seed=seed,
    )
    ref, surv = source.read_block(n)
    ref_a, surv_a, _ = align_channels(ref, surv)
    if dsi:
        surv_a = suppress_dsi(ref_a, surv_a)
    doppler_scan = np.arange(-100, 101, 4)
    return compute_caf(ref_a, surv_a, source.sample_rate_hz, doppler_scan, max_lag=128)


def test_topk_peaks_finds_injected_target():
    caf_result = _caf_with_target(delay_samples=30, doppler_hz=52.0)
    detections = topk_peaks(caf_result, k=3, min_snr_db=5.0)
    assert len(detections) >= 1
    best = detections[0]
    assert abs(best.range_lag_samples - 30) <= 1
    # CAF's doppler_hz axis reports -Dtrue -- see caf.py's SIGN CONVENTION note.
    assert abs(best.doppler_hz - (-52.0)) <= 4
    assert best.snr_db >= 5.0


def test_topk_peaks_respects_min_snr_gate():
    caf_result = _caf_with_target(delay_samples=30, doppler_hz=52.0)
    # An absurdly high SNR gate should yield no detections.
    detections = topk_peaks(caf_result, k=5, min_snr_db=1000.0)
    assert detections == []


def test_cfar_ca_detect_finds_injected_target():
    caf_result = _caf_with_target(delay_samples=40, doppler_hz=-60.0)
    detections = cfar_ca_detect(caf_result, pfa_threshold_db=8.0)
    assert len(detections) >= 1
    # CAF's doppler_hz axis reports -Dtrue -- see caf.py's SIGN CONVENTION note.
    hits = [d for d in detections if abs(d.range_lag_samples - 40) <= 1 and abs(d.doppler_hz - 60.0) <= 4]
    assert hits, f"expected a CFAR hit near (lag=40, doppler=60), got {[(d.range_lag_samples, d.doppler_hz) for d in detections]}"
