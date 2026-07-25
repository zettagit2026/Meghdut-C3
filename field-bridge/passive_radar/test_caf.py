"""Unit tests for caf.py's CAF/range-Doppler computation (task #43, C10).

Verifies against synthetic data with a known injected (delay, Doppler)
target -- per PASSIVE_RADAR_ARCHITECTURE.md §5 step 4's explicit test plan
("assert the CAF peak lands in the corresponding (range-lag, Doppler) bin
within tolerance") -- and cross-checks caf_fft_batched against
caf_bruteforce (the goship.m-equivalent correctness baseline) for
numerical equivalence.

Run: pytest field-bridge/passive_radar/test_caf.py -v
"""
import numpy as np
import pytest

from passive_radar.caf import caf_bruteforce, caf_fft_batched, compute_caf
from passive_radar.channel_source import SyntheticDualChannelSource
from passive_radar.alignment import align_channels
from passive_radar.dsi_suppression import suppress_dsi


def _make_synthetic(delay_samples, doppler_hz, fs=2.048e6, n=4000, seed=5, attenuation=1.0):
    source = SyntheticDualChannelSource(
        sample_rate_hz=fs,
        targets=[(delay_samples, doppler_hz, attenuation)],
        direct_path_gain=1.0,
        seed=seed,
    )
    ref, surv = source.read_block(n)
    return ref, surv, fs


def test_caf_bruteforce_and_fft_batched_agree():
    ref, surv, fs = _make_synthetic(delay_samples=15, doppler_hz=40.0, n=3000)
    doppler_hz = np.arange(-100, 101, 10)
    max_lag = 64
    bf = caf_bruteforce(ref, surv, fs, doppler_hz, max_lag)
    fb = caf_fft_batched(ref, surv, fs, doppler_hz, max_lag)
    np.testing.assert_allclose(bf.range_doppler, fb.range_doppler, rtol=1e-6, atol=1e-6)


def test_caf_recovers_known_delay_and_doppler():
    delay_samples = 25
    doppler_hz = 48.0
    ref, surv, fs = _make_synthetic(delay_samples, doppler_hz, n=6000, seed=42)

    doppler_scan = np.arange(-100, 101, 4)  # matches goship.m's 4Hz step convention
    max_lag = 128
    result = compute_caf(ref, surv, fs, doppler_scan, max_lag)

    # Direct-path breakthrough (attenuation 1.0, delay 0, doppler 0) always
    # dominates the raw (pre-DSI-suppression) CAF at (lag=0, doppler=0);
    # the injected target (delay=25, doppler=48) creates its own distinct
    # peak elsewhere in the map. Verify that peak is a strong local maximum
    # near the known ground truth.
    # NOTE: CAF's doppler_hz axis reports the negated trial-demodulation
    # frequency relative to the true target Doppler -- see caf.py's
    # "SIGN CONVENTION" docstring note (verified against goship.m's own
    # algebra: mesdop=mes.*exp(j*2*pi*fd*tim) peaks when fd=-Dtrue).
    doppler_col = np.argmin(np.abs(result.doppler_hz - (-doppler_hz)))
    lag_row = np.argmin(np.abs(result.lags - delay_samples))
    target_region = result.range_doppler[
        max(lag_row - 2, 0): lag_row + 3, max(doppler_col - 1, 0): doppler_col + 2
    ]
    assert target_region.size > 0
    # The target's own peak should be a strong local maximum, i.e. among
    # the largest values within a small neighborhood of the injected truth.
    neighborhood_max = target_region.max()
    assert neighborhood_max > np.median(result.range_doppler) * 3

    # And a dedicated DSI-suppressed run should put the GLOBAL peak at the
    # correct (delay, doppler) bin, since the direct-path breakthrough is
    # removed and can no longer dominate.
    ref_a, surv_a, _ = align_channels(ref, surv)
    surv_ds = suppress_dsi(ref_a, surv_a)
    result_ds = compute_caf(ref_a, surv_ds, fs, doppler_scan, max_lag)
    lag_idx2, dop_idx2 = result_ds.peak_bin()
    assert abs(result_ds.lags[lag_idx2] - delay_samples) <= 1
    assert abs(result_ds.doppler_hz[dop_idx2] - (-doppler_hz)) <= 4  # one Doppler-bin-step tolerance


def test_range_m_conversion_matches_goship_convention():
    ref, surv, fs = _make_synthetic(delay_samples=10, doppler_hz=0.0, n=2000)
    result = compute_caf(ref, surv, fs, np.array([0.0]), max_lag=16)
    # goship.m: range_km axis = lag * 3e8/fs/2/1000 -> range_m = lag*c/fs/2
    expected = result.lags * 299792458.0 / fs / 2.0
    np.testing.assert_allclose(result.range_m(), expected)
