#!/usr/bin/env python3
"""Unit tests for event_gating.py's burst/event gating (ported technique
from MULT-25-607-ML-for-RF-Sprectrum-sensing's classifier.py/preprocessing.py
-- see event_gating.py's module docstring for full provenance).

These tests verify the SIGNAL-PROCESSING MATH is correct (peak/burst
detection and noise-floor replacement land where analytically expected on a
known synthetic tone injected at a known bin/time). They do NOT claim to
prove real-world drone-detection accuracy -- no fabricated "this is what a
real drone looks like" ground truth is used anywhere here, consistent with
this project's standing rule against fabricated test data. Real validation
of the end-to-end effect on the deployed model happens via
collect_noise_calibration.py-style real hardware captures in the field.

Run: pytest field-bridge/test_event_gating.py -v
"""
import numpy as np
import pytest

from event_gating import (
    detect_burst_bins,
    gate_psd_to_noise_floor,
    gate_spectrogram_freq_bins,
    measured_noise_floor_db,
)


NOISE_FLOOR_DB = -100.0
BURST_DB = -40.0
N_BINS = 512


def _synthetic_psd_with_burst(burst_start=200, burst_width=20, rng=None):
    """A PSD (dB) that is flat measured noise floor everywhere except a
    known injected burst region -- pure signal-processing test fixture, not
    a claim about real RF/drone signatures."""
    rng = rng or np.random.default_rng(42)
    psd = NOISE_FLOOR_DB + rng.normal(0, 0.5, size=N_BINS)  # tiny real measurement jitter
    psd[burst_start:burst_start + burst_width] = BURST_DB
    return psd


def test_measured_noise_floor_matches_known_distribution():
    rng = np.random.default_rng(1)
    psd = NOISE_FLOOR_DB + rng.normal(0, 1.0, size=2000)
    floor = measured_noise_floor_db(psd, floor_percentile=10.0)
    # 10th percentile of a tight normal around -100 should land close to -100
    assert floor == pytest.approx(NOISE_FLOOR_DB, abs=3.0)


def test_measured_noise_floor_robust_to_burst_outliers():
    # A handful of high-power burst bins should not drag the LOW-percentile
    # floor estimate upward.
    psd = _synthetic_psd_with_burst()
    floor = measured_noise_floor_db(psd, floor_percentile=10.0)
    assert floor < BURST_DB - 10  # nowhere near the burst level
    assert floor == pytest.approx(NOISE_FLOOR_DB, abs=2.0)


def test_detect_burst_bins_finds_known_injected_tone():
    psd = _synthetic_psd_with_burst(burst_start=200, burst_width=20)
    events = detect_burst_bins(psd, noise_floor_db=NOISE_FLOOR_DB, min_snr_db=6.0)
    assert len(events) == 1
    start, end = events[0]
    # peak_widths at rel_height=0.9 should land within a few bins of the
    # true injected [200, 219] range.
    assert abs(start - 200) <= 5
    assert abs(end - 219) <= 5


def test_detect_burst_bins_empty_on_pure_noise():
    rng = np.random.default_rng(7)
    psd = NOISE_FLOOR_DB + rng.normal(0, 0.5, size=N_BINS)
    events = detect_burst_bins(psd, noise_floor_db=NOISE_FLOOR_DB, min_snr_db=6.0)
    assert events == []


def test_detect_burst_bins_two_separated_bursts():
    rng = np.random.default_rng(3)
    psd = NOISE_FLOOR_DB + rng.normal(0, 0.5, size=N_BINS)
    psd[50:70] = BURST_DB
    psd[400:415] = BURST_DB
    events = detect_burst_bins(psd, noise_floor_db=NOISE_FLOOR_DB, min_snr_db=6.0)
    assert len(events) == 2
    events.sort()
    assert abs(events[0][0] - 50) <= 5
    assert abs(events[1][0] - 400) <= 5


def test_gate_psd_to_noise_floor_preserves_event_suppresses_rest():
    psd = _synthetic_psd_with_burst(burst_start=300, burst_width=15)
    gated, events, floor, total_event_bins = gate_psd_to_noise_floor(
        psd, noise_floor_db=NOISE_FLOOR_DB, min_snr_db=6.0
    )
    assert len(events) == 1
    assert total_event_bins > 0
    start, end = events[0]
    # Inside the detected event range, the real (burst) values must survive.
    assert np.allclose(gated[start:end + 1], psd[start:end + 1])
    # Far outside the event, everything must be forced to the noise floor,
    # even where the original (real, jittery) PSD had some higher/lower bins.
    far_outside = np.r_[0:100, 450:512]
    assert np.all(gated[far_outside] == pytest.approx(NOISE_FLOOR_DB))


def test_gate_psd_to_noise_floor_derives_floor_when_not_supplied():
    psd = _synthetic_psd_with_burst()
    gated, events, floor, _ = gate_psd_to_noise_floor(psd, noise_floor_db=None, min_snr_db=6.0)
    assert floor == pytest.approx(NOISE_FLOOR_DB, abs=2.0)
    assert len(events) == 1


def test_gate_spectrogram_freq_bins_suppresses_never_bursting_rows():
    rng = np.random.default_rng(11)
    n_freq, n_time = 256, 40
    S_db = NOISE_FLOOR_DB + rng.normal(0, 0.5, size=(n_freq, n_time))

    # Inject a burst confined to frequency rows [100:120), present in only a
    # few time columns (a short transient) -- this is the case a MEAN-based
    # gating PSD would risk washing out, which is why gate_spectrogram_freq_bins
    # uses per-bin MAX across time for detection.
    burst_rows = slice(100, 120)
    burst_cols = slice(15, 18)
    S_db[burst_rows, burst_cols] = BURST_DB

    gated, events, floor = gate_spectrogram_freq_bins(S_db, min_snr_db=6.0)

    assert len(events) == 1
    start, end = events[0]
    assert abs(start - 100) <= 5
    assert abs(end - 119) <= 5

    # The burst columns within the detected event rows must be preserved.
    assert np.allclose(gated[burst_rows, burst_cols], BURST_DB)

    # Frequency rows well outside the event must be forced to their own
    # per-bin (per-row) measured noise floor for EVERY time column -- not
    # left as the original per-sample noisy values.
    far_rows = np.r_[0:50, 200:256]
    row_medians = np.median(S_db[far_rows, :], axis=1)
    assert np.allclose(gated[far_rows, :], row_medians[:, np.newaxis])


def test_gate_spectrogram_freq_bins_no_burst_everything_flattened_to_own_median():
    rng = np.random.default_rng(5)
    S_db = NOISE_FLOOR_DB + rng.normal(0, 0.5, size=(64, 10))
    gated, events, floor = gate_spectrogram_freq_bins(S_db, min_snr_db=6.0)
    assert events == []
    row_medians = np.median(S_db, axis=1)
    assert np.allclose(gated, row_medians[:, np.newaxis])


def test_gate_spectrogram_freq_bins_rejects_non_2d_input():
    with pytest.raises(ValueError):
        gate_spectrogram_freq_bins(np.zeros(10))
