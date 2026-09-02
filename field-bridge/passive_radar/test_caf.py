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

from passive_radar.caf import (
    caf_bruteforce,
    caf_fft_batched,
    caf_fft_batched_gpu,
    compute_caf,
)
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


def _require_cuda():
    """Skip gracefully on runners without a CUDA torch build (e.g. the Mac
    dev box); the GPU path is exercised on .186."""
    torch = pytest.importorskip("torch")
    if not torch.cuda.is_available():
        pytest.skip("CUDA not available on this runner (GPU CAF runs on .186)")
    return torch


def test_caf_gpu_matches_bruteforce_bit_accuracy():
    """BIT-ACCURACY GATE (OB-06 task 1.6): the GPU port runs the FFT stage in
    complex64 (single) while caf_bruteforce is complex128 (double). Assert the
    single-precision change is safe: the global peak bin must match EXACTLY and
    the whole map must agree within a tight peak-relative tolerance."""
    _require_cuda()
    delay_samples = 25
    doppler_hz = 48.0
    ref, surv, fs = _make_synthetic(delay_samples, doppler_hz, n=6000, seed=42)
    doppler_scan = np.arange(-100, 101, 4)
    max_lag = 128

    bf = caf_bruteforce(ref, surv, fs, doppler_scan, max_lag)
    gpu = caf_fft_batched_gpu(ref, surv, fs, doppler_scan, max_lag)

    # 1) Exact peak-bin agreement (range-lag AND Doppler index).
    assert gpu.peak_bin() == bf.peak_bin()

    # 2) Whole-map agreement, normalized by the peak magnitude (relative error
    #    on near-zero sidelobe bins is meaningless; peak-relative is the honest
    #    single-precision metric). complex64 FFT over n=6000 -> ~1e-5 or better.
    peak = bf.range_doppler.max()
    max_abs_err = np.max(np.abs(gpu.range_doppler - bf.range_doppler))
    assert max_abs_err / peak < 1e-4, f"peak-relative error {max_abs_err/peak:.2e} too large"


def test_caf_gpu_matches_cpu_fft_batched():
    """The GPU port and the CPU FFT path implement the identical algorithm;
    they should agree even more tightly than either does with the brute-force
    oracle."""
    _require_cuda()
    ref, surv, fs = _make_synthetic(delay_samples=15, doppler_hz=40.0, n=3000)
    doppler_hz = np.arange(-100, 101, 10)
    max_lag = 64
    cpu = caf_fft_batched(ref, surv, fs, doppler_hz, max_lag)
    gpu = caf_fft_batched_gpu(ref, surv, fs, doppler_hz, max_lag)
    assert gpu.peak_bin() == cpu.peak_bin()
    peak = cpu.range_doppler.max()
    assert np.max(np.abs(gpu.range_doppler - cpu.range_doppler)) / peak < 1e-4


def test_caf_gpu_doppler_chunking_is_invariant():
    """Chunking the Doppler axis (memory control) must not change the result:
    a single-chunk run and a multi-chunk run must be identical."""
    _require_cuda()
    ref, surv, fs = _make_synthetic(delay_samples=15, doppler_hz=40.0, n=3000)
    doppler_hz = np.arange(-100, 101, 10)  # 21 bins
    max_lag = 64
    whole = caf_fft_batched_gpu(ref, surv, fs, doppler_hz, max_lag, doppler_chunk=999)
    chunked = caf_fft_batched_gpu(ref, surv, fs, doppler_hz, max_lag, doppler_chunk=4)
    # cuFFT selects a different batch-plan by batch size, so different Doppler
    # chunkings are NOT bit-identical in complex64 at production bin counts — they
    # agree to single-precision tolerance with an INVARIANT peak bin (measured on
    # the RTX 3060: ~2.4e-7 peak-relative at 101 bins; 0.0 at the 21 bins here).
    # Assert the physically-meaningful invariant, not literal bit-equality, so this
    # does not false-fail at production scale.
    assert whole.peak_bin() == chunked.peak_bin()
    peak = whole.range_doppler.max()
    assert np.max(np.abs(whole.range_doppler - chunked.range_doppler)) / peak < 1e-6


def test_compute_caf_cpu_default_and_hard_fallback(monkeypatch):
    """compute_caf defaults to the CPU path (env unset), and forcing
    CEMA_CAF_DEVICE=cuda on a runner without CUDA must fall back HARD to the
    CPU path rather than raise."""
    ref, surv, fs = _make_synthetic(delay_samples=15, doppler_hz=40.0, n=3000)
    doppler_hz = np.arange(-100, 101, 10)
    max_lag = 64
    cpu_ref = caf_fft_batched(ref, surv, fs, doppler_hz, max_lag)

    monkeypatch.delenv("CEMA_CAF_DEVICE", raising=False)
    default = compute_caf(ref, surv, fs, doppler_hz, max_lag)
    np.testing.assert_array_equal(default.range_doppler, cpu_ref.range_doppler)

    # Force cuda; on a CUDA-less runner this must still return the CPU result.
    monkeypatch.setenv("CEMA_CAF_DEVICE", "cuda")
    forced = compute_caf(ref, surv, fs, doppler_hz, max_lag)
    try:
        import torch
        has_cuda = torch.cuda.is_available()
    except Exception:
        has_cuda = False
    if not has_cuda:
        np.testing.assert_array_equal(forced.range_doppler, cpu_ref.range_doppler)
    else:
        # On the GPU host, the forced-cuda result must still match the oracle
        # within single-precision tolerance.
        assert forced.peak_bin() == cpu_ref.peak_bin()
        peak = cpu_ref.range_doppler.max()
        assert np.max(np.abs(forced.range_doppler - cpu_ref.range_doppler)) / peak < 1e-4


def test_range_m_conversion_matches_goship_convention():
    ref, surv, fs = _make_synthetic(delay_samples=10, doppler_hz=0.0, n=2000)
    result = compute_caf(ref, surv, fs, np.array([0.0]), max_lag=16)
    # goship.m: range_km axis = lag * 3e8/fs/2/1000 -> range_m = lag*c/fs/2
    expected = result.lags * 299792458.0 / fs / 2.0
    np.testing.assert_allclose(result.range_m(), expected)
