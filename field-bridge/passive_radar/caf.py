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

`caf_fft_batched_gpu` is a GPU port of the SAME math (OB-06 Phase-1
acceleration, roadmap task 1.6): it precomputes the reference-channel
(matched-filter) FFT ONCE, builds the per-Doppler demodulation matrix,
does one BATCHED forward FFT of the demodulated surveillance channel, a
broadcast multiply by the precomputed reference spectrum, one batched
inverse FFT, magnitude, then the same ±max_lag slice -- replacing the
Python `for fd` loop over scipy.signal.fftconvolve (which recomputes the
reference FFT every iteration) with a single batched torch.fft call.

HONESTY / SCOPE: this accelerates the CAF *arithmetic* on synthetic or
recorded IQ and makes the software real-time-ready; it does NOT by itself
produce a live passive-radar detection. A real detection still requires a
2nd coherent SDR channel + a shared GPSDO/10 MHz clock + a real
illuminator-of-opportunity (hardware not present here). The GPU port only
changes WHERE (and in what precision) the same numbers are computed.

PRECISION: the CPU paths run in complex128 (double). The GPU port runs the
FFT stage in complex64 (single) for cuFFT throughput; the per-Doppler
demodulation phase is still evaluated in float64 (matching the reference)
before being cast to complex64, so the ONLY precision change is the
single-precision FFT/multiply/iFFT. That double->single change is the gate
and is validated against caf_bruteforce in test_caf.py (exact peak-bin
match + tight peak-relative magnitude tolerance).

DEVICE SWITCH: env CEMA_CAF_DEVICE (mirrors the ML bridge's
CEMA_ML_DEVICE) = auto | cuda | cpu selects the compute_caf() backend, with
a HARD CPU fallback if CUDA is unavailable OR the GPU path raises at
runtime. The default (env unset) stays "cpu" -- i.e. the numpy/scipy path
remains the default and the fallback -- so this not-yet-deploy-signed-off
acceleration is opt-in until an independent bit-accuracy verifier signs off
(unlike the ML bridge, whose default is "auto"; documented divergence).
"""
from __future__ import annotations

import math
import os
import sys
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


def _next_pow2(m: int) -> int:
    """Smallest power of two >= m (used to size the linear-convolution FFT
    so cuFFT/torch.fft get a smooth transform length instead of the prime-ish
    2*n-1)."""
    if m <= 1:
        return 1
    return 1 << (m - 1).bit_length()


def caf_fft_batched_gpu(
    ref: np.ndarray,
    surv: np.ndarray,
    sample_rate_hz: float,
    doppler_hz: np.ndarray,
    max_lag: int,
    *,
    device: Optional["object"] = None,
    doppler_chunk: Optional[int] = None,
) -> CafResult:
    """GPU-batched CAF: numerically the same map as caf_bruteforce /
    caf_fft_batched, computed with torch.fft.

    Pipeline (see module docstring):
      1. kernel = conj(ref[::-1]); K = FFT(kernel, L) -- the reference
         (matched-filter) spectrum, computed ONCE (not per Doppler bin).
      2. For each (chunked) block of Doppler bins fd: build the demod matrix
         demod[m] = exp(1j*2*pi*fd[m]*t) (phase in float64, then cast to
         complex64), mesdop = surv * demod.
      3. M = FFT(mesdop, L) along the sample axis -- one BATCHED forward FFT.
      4. conv = iFFT(M * K) -- broadcast-multiply by the precomputed
         reference spectrum + one batched inverse FFT (== linear
         cross-correlation, matching np.correlate(surv, ref, 'full')).
      5. abs(), slice to lags [-max_lag, +max_lag].

    The FFT stage runs in complex64 (single precision) for throughput; the
    demod phase is evaluated in float64 to match the reference. Validated
    bit-for-tolerance against caf_bruteforce in test_caf.py.

    Raises RuntimeError-family / torch exceptions on CUDA failure; callers
    that need the hard CPU fallback should go through compute_caf().
    """
    import torch  # lazy: keeps torch off the import path for CPU-only callers

    if device is None:
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    elif not isinstance(device, torch.device):
        device = torch.device(device)

    ref = np.asarray(ref, dtype=np.complex128)
    surv = np.asarray(surv, dtype=np.complex128)
    n = min(len(ref), len(surv))
    ref = ref[:n]
    surv = surv[:n]
    if max_lag >= n:
        raise ValueError("max_lag exceeds available correlation range for given block length")

    doppler_hz = np.asarray(doppler_hz, dtype=np.float64)
    n_doppler = int(doppler_hz.shape[0])
    n_lags = 2 * max_lag + 1

    # Linear-convolution length: conv(mesdop, kernel) has length 2*n-1.
    # Pad to the next power of two so torch.fft gets a smooth length.
    conv_len = 2 * n - 1
    fft_len = _next_pow2(conv_len)
    zero_lag_idx = n - 1
    start = zero_lag_idx - max_lag
    end = zero_lag_idx + max_lag + 1

    # (1) Reference / matched-filter spectrum, computed ONCE. conj(ref[::-1])
    # is the FFT-conv equivalent of np.correlate(surv, ref, 'full') used by
    # the CPU paths (see _xcorr_maxlag's argument-order note above).
    kernel = np.conj(ref[::-1])
    kernel_t = torch.as_tensor(kernel, dtype=torch.complex64, device=device)
    K = torch.fft.fft(kernel_t, n=fft_len)  # (fft_len,)

    surv_t = torch.as_tensor(surv, dtype=torch.complex64, device=device)  # (n,)
    tim = torch.arange(n, dtype=torch.float64, device=device) / float(sample_rate_hz)
    fd_all = torch.as_tensor(doppler_hz, dtype=torch.float64, device=device)  # (n_doppler,)
    two_pi = 2.0 * math.pi

    if doppler_chunk is None:
        try:
            doppler_chunk = int(os.environ.get("CEMA_CAF_DOPPLER_CHUNK", "32") or "32")
        except ValueError:
            doppler_chunk = 32
    doppler_chunk = max(1, min(int(doppler_chunk), max(n_doppler, 1)))

    col_blocks = []
    for i in range(0, n_doppler, doppler_chunk):
        fd_c = fd_all[i:i + doppler_chunk]  # (dc,)
        # Demod phase in float64 (reference precision), then cast to complex64.
        phase = (two_pi * fd_c).unsqueeze(1) * tim.unsqueeze(0)  # (dc, n) float64
        demod = torch.complex(
            torch.cos(phase).to(torch.float32),
            torch.sin(phase).to(torch.float32),
        )  # (dc, n) complex64
        mesdop = surv_t.unsqueeze(0) * demod  # (dc, n) complex64
        M = torch.fft.fft(mesdop, n=fft_len, dim=1)  # (dc, fft_len) -- batched forward
        conv = torch.fft.ifft(M * K.unsqueeze(0), dim=1)  # (dc, fft_len) -- batched inverse
        win = conv[:, start:end].abs().to(torch.float32)  # (dc, n_lags)
        col_blocks.append(win.transpose(0, 1).contiguous().cpu())  # (n_lags, dc)

    rangedop = torch.cat(col_blocks, dim=1).numpy().astype(np.float64)  # (n_lags, n_doppler)
    lags = np.arange(-max_lag, max_lag + 1)
    return CafResult(rangedop, lags, doppler_hz, sample_rate_hz)


def _resolve_caf_backend() -> str:
    """Resolve the compute_caf() backend from env CEMA_CAF_DEVICE
    (auto|cuda|cpu), mirroring gamutrf_infer.resolve_device()'s CEMA_ML_DEVICE
    contract but defaulting to 'cpu' (the numpy/scipy path stays the default
    and the fallback -- this GPU acceleration is opt-in until an independent
    bit-accuracy verifier signs off). Returns 'cuda' or 'cpu'."""
    choice = (os.environ.get("CEMA_CAF_DEVICE", "cpu") or "cpu").strip().lower()
    if choice in ("cuda", "gpu", "auto"):
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
        except Exception:
            pass
        if choice in ("cuda", "gpu"):
            print("[caf] WARNING: CEMA_CAF_DEVICE=%s requested but CUDA is "
                  "unavailable -- falling back to cpu." % choice, file=sys.stderr)
        return "cpu"
    if choice not in ("cpu", ""):
        print("[caf] WARNING: unrecognized CEMA_CAF_DEVICE=%r -- treating as "
              "'cpu'." % choice, file=sys.stderr)
    return "cpu"


def compute_caf(
    ref: np.ndarray,
    surv: np.ndarray,
    sample_rate_hz: float,
    doppler_hz: np.ndarray,
    max_lag: int,
) -> CafResult:
    """Single swappable CAF entry point used by the rest of the pipeline
    (detector.py, passive_radar_bridge.py).

    Backend selected by env CEMA_CAF_DEVICE (auto|cuda|cpu); defaults to the
    complex128 numpy/scipy FFT path (caf_fft_batched). When the GPU backend is
    selected but CUDA is unavailable, or the GPU path raises at runtime, this
    falls back HARD to caf_fft_batched so a GPU problem can never break the
    pipeline. All backends compute the same range-Doppler map (validated
    against caf_bruteforce)."""
    if _resolve_caf_backend() == "cuda":
        try:
            return caf_fft_batched_gpu(ref, surv, sample_rate_hz, doppler_hz, max_lag)
        except Exception as exc:  # hard CPU fallback -- never let the GPU break CEMA
            print("[caf] WARNING: GPU CAF path failed (%s) -- falling back to "
                  "CPU caf_fft_batched." % exc, file=sys.stderr)
    return caf_fft_batched(ref, surv, sample_rate_hz, doppler_hz, max_lag)
