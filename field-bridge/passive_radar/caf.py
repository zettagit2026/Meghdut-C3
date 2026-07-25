"""Cross-Ambiguity Function (CAF) / range-Doppler map computation.

Direct port of goship.m's Doppler-bin loop (verified against
~/Desktop/zettagit/passive_radar/171210ship/goship.m lines ~118-125), the
actual "CAF/Doppler processing chain":

    for fd=freq
       mesdop=mes.*exp(j*2*pi*fd*tim);
       x=abs(xcorr(ref,mesdop,dN));
       rangedop(:,m)=x(dN-negdist:dN+maxdist);
       m=m+1;
    end

For each candidate Doppler shift fd, demodulate the surveillance channel
by exp(j*2*pi*fd*t), cross-correlate against the reference channel, and
keep a windowed range-lag slice. Stacking these slices over all fd gives
the 2D range-Doppler map. This is the classic passive-radar CAF:

    CAF(tau, fd) = sum_n ref[n] * conj(surv[n] * exp(-j*2*pi*fd*n/fs))
                 (shifted by lag tau)

SIGN CONVENTION (verified against goship.m's own algebra, not a bug):
mesdop = surv * exp(+j*2*pi*fd*t) demodulates the surveillance channel by
a *trial* Doppler `fd`. If the true target's Doppler shift is `Dtrue`
(i.e. surv contains a component `ref_delayed * exp(+j*2*pi*Dtrue*t)`),
coherent correlation gain against the (unmodulated) reference channel
only occurs when the residual phase drift is ~0, i.e. when
`fd + Dtrue ~= 0`, so the CAF/range-Doppler map's peak column lands at
`fd = -Dtrue`. This is exactly goship.m's own convention (it performs the
identical `mesdop=mes.*exp(j*2*pi*fd*tim)` before `xcorr`), so
`doppler_to_speed_mps()`/callers must negate the CAF's reported
`doppler_hz` peak to recover the physical target Doppler sign, OR treat
the CAF's own `fd` axis as already being "the trial demodulation
frequency" rather than "the target's Doppler" -- documented explicitly
here since it is an easy sign error to introduce downstream.

`caf_bruteforce` is a direct, readable port matching goship.m's brute-force
per-Doppler-bin xcorr approach -- this is the correctness baseline.
`caf_fft_batched` is the throughput-oriented reimplementation
PASSIVE_RADAR_ARCHITECTURE.md §2.3/§5 flags as a standard passive-radar
optimization (one FFT-based correlation per Doppler bin via
scipy.signal.fftconvolve, versus Octave's implicit O(n^2)-per-bin
`xcorr`), validated against caf_bruteforce for correctness before being
treated as the default.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.signal import fftconvolve


@dataclass
class CafResult:
    range_doppler: np.ndarray  # shape (n_lags, n_doppler_bins), real-valued magnitude
    lags: np.ndarray           # sample lags corresponding to each row
    doppler_hz: np.ndarray     # Doppler shift (Hz) corresponding to each column
    sample_rate_hz: float

    def range_m(self, speed_of_light_mps: float = 299792458.0) -> np.ndarray:
        """Bistatic range (one-way-equivalent, per goship.m's own
        `*3E8/fs/2` convention -- the /2 accounts for the two-way path
        length implied by a lag measured in a monostatic-like round trip
        approximation) corresponding to each row of range_doppler."""
        return self.lags * speed_of_light_mps / self.sample_rate_hz / 2.0

    def peak_bin(self):
        """Returns (lag_index, doppler_index) of the global maximum."""
        idx = np.unravel_index(np.argmax(self.range_doppler), self.range_doppler.shape)
        return idx


def _xcorr_maxlag(ref: np.ndarray, surv: np.ndarray, max_lag: int) -> np.ndarray:
    """Cross-correlation of ref and surv restricted to lags -max_lag..+max_lag,
    matching Octave's `xcorr(ref, surv, max_lag)` magnitude convention used
    by goship.m (it takes `abs(xcorr(...))` immediately). Returns a real
    (magnitude) array of length 2*max_lag+1, indexed so that index
    max_lag corresponds to zero lag.
    """
    # NOTE on argument order: np.correlate(a, v)'s peak lag convention is
    # such that correlate(surv, ref) peaks at lag=+d when surv lags ref by
    # d samples (surv[n] == ref[n-d]) -- verified empirically against
    # np.roll(ref, d). This matches goship.m's own convention where a
    # positive `pos` means `mes` (surveillance) lags `ref`.
    full = np.correlate(surv, ref, mode="full")  # length len(ref)+len(surv)-1
    n = len(surv)
    zero_lag_idx = n - 1
    start = zero_lag_idx - max_lag
    end = zero_lag_idx + max_lag + 1
    if start < 0 or end > len(full):
        raise ValueError("max_lag exceeds available correlation range for given block length")
    return full[start:end]


def caf_bruteforce(
    ref: np.ndarray,
    surv: np.ndarray,
    sample_rate_hz: float,
    doppler_hz: np.ndarray,
    max_lag: int,
) -> CafResult:
    """Direct port of goship.m's per-Doppler-bin brute-force xcorr loop.
    Correctness baseline -- see module docstring."""
    ref = np.asarray(ref, dtype=np.complex128)
    surv = np.asarray(surv, dtype=np.complex128)
    n = min(len(ref), len(surv))
    ref = ref[:n]
    surv = surv[:n]
    tim = np.arange(n) / sample_rate_hz

    doppler_hz = np.asarray(doppler_hz, dtype=np.float64)
    n_lags = 2 * max_lag + 1
    rangedop = np.zeros((n_lags, len(doppler_hz)), dtype=np.float64)
    for m, fd in enumerate(doppler_hz):
        mesdop = surv * np.exp(1j * 2 * np.pi * fd * tim)
        x = np.abs(_xcorr_maxlag(ref, mesdop, max_lag))
        rangedop[:, m] = x

    lags = np.arange(-max_lag, max_lag + 1)
    return CafResult(rangedop, lags, doppler_hz, sample_rate_hz)


def caf_fft_batched(
    ref: np.ndarray,
    surv: np.ndarray,
    sample_rate_hz: float,
    doppler_hz: np.ndarray,
    max_lag: int,
) -> CafResult:
    """Throughput-oriented reimplementation: same math as caf_bruteforce,
    but each per-Doppler-bin correlation uses FFT convolution
    (scipy.signal.fftconvolve) instead of goship.m's implicit
    O(n^2)-scale direct xcorr. Numerically equivalent to caf_bruteforce
    up to floating point tolerance -- validated in test_caf.py.
    """
    ref = np.asarray(ref, dtype=np.complex128)
    surv = np.asarray(surv, dtype=np.complex128)
    n = min(len(ref), len(surv))
    ref = ref[:n]
    surv = surv[:n]
    tim = np.arange(n) / sample_rate_hz

    doppler_hz = np.asarray(doppler_hz, dtype=np.float64)
    n_lags = 2 * max_lag + 1
    rangedop = np.zeros((n_lags, len(doppler_hz)), dtype=np.float64)

    # See _xcorr_maxlag's note on argument order/sign convention above --
    # correlate(mesdop, ref) (i.e. mesdop as "a", ref as "v") is the FFT
    # equivalent of np.correlate(surv, ref, 'full') used there, giving the
    # same +d-means-surv-lags-ref sign convention.
    zero_lag_idx = n - 1
    for m, fd in enumerate(doppler_hz):
        mesdop = surv * np.exp(1j * 2 * np.pi * fd * tim)
        full = fftconvolve(mesdop, np.conj(ref[::-1]), mode="full")
        start = zero_lag_idx - max_lag
        end = zero_lag_idx + max_lag + 1
        rangedop[:, m] = np.abs(full[start:end])

    lags = np.arange(-max_lag, max_lag + 1)
    return CafResult(rangedop, lags, doppler_hz, sample_rate_hz)


# Default entry point used by the rest of the pipeline (detector.py,
# passive_radar_bridge.py): FFT-batched for throughput, per
# PASSIVE_RADAR_ARCHITECTURE.md §2.3's explicit recommendation, now that
# caf_bruteforce exists as the correctness baseline it's validated against.
compute_caf = caf_fft_batched
