"""Unit tests for alignment.py's inter-channel delay estimation (task #43, C10).

Run: pytest field-bridge/passive_radar/test_alignment.py -v
"""
import numpy as np

from passive_radar.alignment import align_channels, estimate_delay_samples


def _noise(n, seed=1):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)


def test_estimate_delay_zero_when_aligned():
    ref = _noise(4096)
    assert estimate_delay_samples(ref, ref) == 0


def test_estimate_delay_positive_lag():
    ref = _noise(8192)
    shift = 37
    surv = np.roll(ref, shift)  # surv lags ref by `shift` samples
    delay = estimate_delay_samples(ref, surv)
    assert delay == shift


def test_estimate_delay_negative_lag():
    ref = _noise(8192)
    shift = 21
    surv = np.roll(ref, -shift)  # surv leads ref
    delay = estimate_delay_samples(ref, surv)
    assert delay == -shift


def test_align_channels_recovers_common_origin():
    ref = _noise(8192)
    shift = 50
    surv = np.roll(ref, shift)
    ref_a, surv_a, delay = align_channels(ref, surv)
    assert delay == shift
    # After alignment, the two aligned buffers should match closely at
    # their overlap (zero remaining lag).
    n = min(len(ref_a), len(surv_a))
    assert n > 0
    corr = np.correlate(ref_a[:n], surv_a[:n], mode="valid")
    # correlation at zero lag should be near-maximal relative to a shifted check
    shifted_corr = np.correlate(ref_a[:n], np.roll(surv_a[:n], 5), mode="valid")
    assert abs(corr[0]) > abs(shifted_corr[0])
