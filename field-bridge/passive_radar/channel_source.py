"""DualChannelSource: the hardware-agnostic seam of the passive radar pipeline.

Everything downstream of this module (alignment.py, dsi_suppression.py,
caf.py, detector.py) operates purely on "two complex baseband streams at a
common known sample rate" and must never care whether those streams came
from synthetic data, a recorded file, or (once task #57 lands) two real
RTL-SDRs. See PASSIVE_RADAR_ARCHITECTURE.md §2.2.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional, Tuple

import numpy as np


class DualChannelSource(ABC):
    """Yields aligned, complex-baseband reference and surveillance streams
    at a common, known sample rate. Implementations may be synthetic,
    file-replay, or live dual-SDR hardware -- callers downstream of this
    interface must not care which."""

    @property
    @abstractmethod
    def sample_rate_hz(self) -> float:
        ...

    @abstractmethod
    def read_block(self, n_samples: int) -> Tuple[np.ndarray, np.ndarray]:
        """Returns (ref_iq, surv_iq), both complex64, length n_samples.

        Raises StopIteration when exhausted (file sources) or blocks until
        available (live sources).
        """
        ...

    def close(self) -> None:
        """Optional cleanup hook. Default no-op."""
        return None

    # Context-manager convenience, matching iq_capture.py's style elsewhere
    # in field-bridge.
    def __enter__(self) -> "DualChannelSource":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


class SyntheticDualChannelSource(DualChannelSource):
    """Direct Python port of the reference repo's simulation.m.

    simulation.m (verified against ~/Desktop/zettagit/passive_radar/171210ship):
        ref = randn(4*fs,1)                       # band-limited-ish noise stand-in
        lo  = exp(j*2*pi*50*tim)                   # fixed 50 Hz Doppler shift
        sur = ref + [ref(101:end); ref(1:100)] .* lo   # ref + delayed+doppler-shifted copy

    i.e. the surveillance channel is the direct-path reference PLUS a
    delayed (by a fixed sample count, wrapped circularly in the Octave
    script) and Doppler-shifted copy of that same reference, standing in
    for "direct signal + one reflected target." This class generalizes
    that to an arbitrary (or multi-target) list of (delay_samples,
    doppler_hz, attenuation) tuples so tests can assert the CAF/detector
    recover known ground truth.
    """

    def __init__(
        self,
        sample_rate_hz: float = 2.048e6,
        targets: Optional[list] = None,
        direct_path_gain: float = 1.0,
        noise_std: float = 0.0,
        seed: Optional[int] = 0,
        total_samples: Optional[int] = None,
    ) -> None:
        self._fs = float(sample_rate_hz)
        # Default: single target reproducing simulation.m's own numbers
        # (100-sample delay, 50 Hz Doppler), 0 dB relative to direct path.
        self._targets = targets if targets is not None else [(100, 50.0, 1.0)]
        self._direct_path_gain = direct_path_gain
        self._noise_std = noise_std
        self._seed = seed
        self._total_samples = total_samples
        self._pos = 0
        self._ref_buf: Optional[np.ndarray] = None
        self._surv_buf: Optional[np.ndarray] = None
        self._buf_len = 0

    @property
    def sample_rate_hz(self) -> float:
        return self._fs

    def _ensure_buffer(self, upto: int) -> None:
        """Synthetic source generates lazily but deterministically (seeded
        RNG) in growing chunks so read_block() can be called repeatedly
        with different block sizes without needing to know the total
        length up front. Regenerated from scratch (same seed) on growth so
        previously-issued samples are stable -- correctness over speed,
        this is a test-fixture source, not a hot path."""
        if self._ref_buf is not None and self._buf_len >= upto:
            return
        max_delay = max((abs(d) for d, _fd, _a in self._targets), default=0)
        n = max(upto + max_delay + 1, 4096)
        rng = np.random.default_rng(self._seed)
        ref_real = rng.standard_normal(n)
        ref_imag = rng.standard_normal(n)
        ref = (ref_real + 1j * ref_imag).astype(np.complex64)
        tim = np.arange(n) / self._fs

        surv = self._direct_path_gain * ref.copy()
        for delay_samples, doppler_hz, attenuation in self._targets:
            delayed = np.roll(ref, delay_samples)
            lo = np.exp(1j * 2 * np.pi * doppler_hz * tim)
            surv = surv + attenuation * delayed * lo
        if self._noise_std > 0:
            surv = surv + self._noise_std * (
                rng.standard_normal(n) + 1j * rng.standard_normal(n)
            )
            ref = ref + self._noise_std * (
                rng.standard_normal(n) + 1j * rng.standard_normal(n)
            )
        self._ref_buf = ref.astype(np.complex64)
        self._surv_buf = surv.astype(np.complex64)
        self._buf_len = n

    def read_block(self, n_samples: int) -> Tuple[np.ndarray, np.ndarray]:
        if self._total_samples is not None and self._pos >= self._total_samples:
            raise StopIteration("SyntheticDualChannelSource exhausted")
        self._ensure_buffer(self._pos + n_samples)
        ref = self._ref_buf[self._pos : self._pos + n_samples]
        surv = self._surv_buf[self._pos : self._pos + n_samples]
        self._pos += n_samples
        return ref.astype(np.complex64), surv.astype(np.complex64)


class RecordedFileDualChannelSource(DualChannelSource):
    """Replays a recorded dual-channel I/Q capture from disk.

    Supports both layouts documented in goship.m:
      - "interleaved": one file, samples alternate ref/surv I/Q quads
        (not used by the 171210ship dataset but supported for the GRC
        blocks_interleave + blocks_file_sink single-file capture path
        described in dual_rtl_sdr.grc).
      - "split": two files (ref_path, surv_path), each interleaved I,Q,I,Q...
        for its own channel -- this is the 171210ship_ch1.sigmf-data /
        171210ship_ch2.sigmf-data layout goship.m actually reads.

    dtype follows goship.m's `datatype` parameter: 'int8', 'int16', 'int32',
    or 'float' (float32).
    """

    _DTYPE_MAP = {
        "int8": np.int8,
        "int16": np.int16,
        "int32": np.int32,
        "float": np.float32,
    }

    def __init__(
        self,
        sample_rate_hz: float,
        ref_path: Optional[str] = None,
        surv_path: Optional[str] = None,
        interleaved_path: Optional[str] = None,
        dtype: str = "int8",
        skip_samples: int = 0,
    ) -> None:
        if dtype not in self._DTYPE_MAP:
            raise ValueError(f"Unsupported dtype {dtype!r}; expected one of {list(self._DTYPE_MAP)}")
        self._fs = float(sample_rate_hz)
        self._np_dtype = self._DTYPE_MAP[dtype]
        self._interleaved = interleaved_path is not None
        if self._interleaved:
            if ref_path is not None or surv_path is not None:
                raise ValueError("Provide either interleaved_path OR (ref_path, surv_path), not both")
            self._f = open(interleaved_path, "rb")
        else:
            if ref_path is None or surv_path is None:
                raise ValueError("Split-file mode requires both ref_path and surv_path")
            self._f_ref = open(ref_path, "rb")
            self._f_surv = open(surv_path, "rb")
        if skip_samples:
            self._skip(skip_samples)

    @property
    def sample_rate_hz(self) -> float:
        return self._fs

    def _skip(self, n_samples: int) -> None:
        # Each complex sample = 2 scalars (I, Q) of dtype size, per-channel.
        itemsize = np.dtype(self._np_dtype).itemsize
        if self._interleaved:
            # interleaved: ref_I,ref_Q,surv_I,surv_Q per sample
            self._f.seek(n_samples * 4 * itemsize, 1)
        else:
            self._f_ref.seek(n_samples * 2 * itemsize, 1)
            self._f_surv.seek(n_samples * 2 * itemsize, 1)

    def _read_channel(self, f, n_samples: int) -> np.ndarray:
        raw = np.fromfile(f, dtype=self._np_dtype, count=n_samples * 2)
        if raw.size < n_samples * 2:
            raise StopIteration("RecordedFileDualChannelSource exhausted")
        iq = raw.astype(np.float32)
        return (iq[0::2] + 1j * iq[1::2]).astype(np.complex64)

    def read_block(self, n_samples: int) -> Tuple[np.ndarray, np.ndarray]:
        if self._interleaved:
            raw = np.fromfile(self._f, dtype=self._np_dtype, count=n_samples * 4)
            if raw.size < n_samples * 4:
                raise StopIteration("RecordedFileDualChannelSource exhausted")
            raw = raw.astype(np.float32)
            ref = (raw[0::4] + 1j * raw[1::4]).astype(np.complex64)
            surv = (raw[2::4] + 1j * raw[3::4]).astype(np.complex64)
            return ref, surv
        ref = self._read_channel(self._f_ref, n_samples)
        surv = self._read_channel(self._f_surv, n_samples)
        return ref, surv

    def close(self) -> None:
        if self._interleaved:
            self._f.close()
        else:
            self._f_ref.close()
            self._f_surv.close()


class DualRTLSDRSource(DualChannelSource):
    """HARDWARE-BLOCKED STUB -- task #57's implementation target.

    Real dual-RTL-SDR acquisition needs: two RTL-SDRs, a shared reference
    clock/GPSDO (or an accepted post-hoc alignment fallback via
    alignment.py), and whatever OS-level access gr-osmosdr/pyrtlsdr
    requires. None of that exists on this deployment yet. This class
    exists only so the CLI surface (passive_radar_bridge.py's
    --source rtlsdr-dual) is visible today; it must never silently
    pretend to produce real samples.
    """

    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError("blocked on task #57 hardware")

    @property
    def sample_rate_hz(self) -> float:  # pragma: no cover - unreachable
        raise NotImplementedError("blocked on task #57 hardware")

    def read_block(self, n_samples: int):  # pragma: no cover - unreachable
        raise NotImplementedError("blocked on task #57 hardware")
