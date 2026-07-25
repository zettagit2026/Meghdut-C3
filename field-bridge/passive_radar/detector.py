"""CFAR / peak-picking over the range-Doppler map -> discrete detections.

NEW relative to the reference repo (PASSIVE_RADAR_ARCHITECTURE.md §2.3,
§5 step 5): goship.m stops at "here is a range-Doppler image" -- a human
looks at the PNG. This project needs discrete (range, doppler, snr)
detections to feed detection_ingest, so peak detection is designed fresh
here, not ported from anywhere.

Two detectors are provided:
  - `topk_peaks`: simple top-K peak-picking with an absolute or
    relative-to-noise-floor SNR threshold. Simple, fast, good default.
  - `cfar_ca_detect`: cell-averaging CFAR (CA-CFAR) over the range-Doppler
    map, the standard classical radar detector, for callers wanting a
    more principled false-alarm-rate-controlled threshold.

Kept deliberately simple per the design doc's guidance: no tracker, no
track association across frames here -- that's a reasonable future
extension, out of scope for this handoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from .caf import CafResult


@dataclass
class Detection:
    range_lag_samples: int
    range_m: float
    doppler_hz: float
    snr_db: float
    magnitude: float


def _snr_db(magnitude: float, noise_floor: float) -> float:
    if noise_floor <= 0:
        return float("inf")
    return 20.0 * np.log10(magnitude / noise_floor)


def topk_peaks(
    caf_result: CafResult,
    k: int = 5,
    min_snr_db: float = 10.0,
    exclude_zero_doppler_bins: int = 0,
) -> List[Detection]:
    """Top-K peak-picking over the range-Doppler map with a minimum-SNR
    gate. Noise floor is estimated as the median magnitude across the
    whole map (robust to a handful of strong peaks), matching common
    practice for a first-pass detector over an image dominated by
    near-zero clutter.

    `exclude_zero_doppler_bins` optionally masks out the N Doppler bins
    nearest zero Doppler (post-DSI-suppression, near-zero-Doppler is
    still typically dominated by residual static clutter/multipath, not
    moving targets) -- 0 (no exclusion) by default so tests can exercise
    the raw map.
    """
    rd = caf_result.range_doppler.copy()
    noise_floor = float(np.median(rd))

    if exclude_zero_doppler_bins > 0:
        zero_idx = int(np.argmin(np.abs(caf_result.doppler_hz)))
        lo = max(0, zero_idx - exclude_zero_doppler_bins)
        hi = min(rd.shape[1], zero_idx + exclude_zero_doppler_bins + 1)
        rd[:, lo:hi] = -np.inf

    flat_idx = np.argsort(rd, axis=None)[::-1]
    detections: List[Detection] = []
    seen: List[Tuple[int, int]] = []
    for idx in flat_idx:
        if len(detections) >= k:
            break
        lag_i, dop_i = np.unravel_index(idx, rd.shape)
        magnitude = rd[lag_i, dop_i]
        if not np.isfinite(magnitude):
            continue
        snr = _snr_db(magnitude, noise_floor)
        if snr < min_snr_db:
            break  # sorted descending -- everything after is weaker
        # Simple non-max suppression: skip candidates immediately adjacent
        # to an already-accepted peak (same physical target's mainlobe).
        too_close = any(abs(lag_i - pl) <= 1 and abs(dop_i - pd) <= 1 for pl, pd in seen)
        if too_close:
            continue
        seen.append((lag_i, dop_i))
        lag = int(caf_result.lags[lag_i])
        range_m = float(caf_result.range_m()[lag_i])
        doppler = float(caf_result.doppler_hz[dop_i])
        detections.append(Detection(lag, range_m, doppler, snr, float(magnitude)))
    return detections


def cfar_ca_detect(
    caf_result: CafResult,
    guard_cells: int = 2,
    training_cells: int = 8,
    pfa_threshold_db: float = 13.0,
) -> List[Detection]:
    """Cell-averaging CFAR (CA-CFAR) over the range-Doppler map.

    For each cell, estimates the local noise floor from a ring of
    training cells (excluding a guard band around the cell under test to
    avoid the target's own mainlobe polluting the noise estimate), and
    flags the cell as a detection if it exceeds the local noise floor by
    `pfa_threshold_db`. Classical 2D CA-CFAR, applied along the range
    (lag) axis per Doppler column since range resolution (lag bins) is
    where the DSI/multipath clutter structure varies fastest; Doppler-axis
    smoothing is coarser (4 Hz bins in the reference config) and less
    critical to guard here.
    """
    rd = caf_result.range_doppler
    n_lags, n_dop = rd.shape
    detections: List[Detection] = []
    window = guard_cells + training_cells

    for dop_i in range(n_dop):
        col = rd[:, dop_i]
        for lag_i in range(n_lags):
            lo = max(0, lag_i - window)
            hi = min(n_lags, lag_i + window + 1)
            guard_lo = max(0, lag_i - guard_cells)
            guard_hi = min(n_lags, lag_i + guard_cells + 1)
            training_mask = np.ones(hi - lo, dtype=bool)
            training_mask[guard_lo - lo : guard_hi - lo] = False
            training_vals = col[lo:hi][training_mask]
            if training_vals.size == 0:
                continue
            noise_floor = float(np.mean(training_vals))
            if noise_floor <= 0:
                continue
            cell = col[lag_i]
            snr = _snr_db(cell, noise_floor)
            if snr >= pfa_threshold_db:
                lag = int(caf_result.lags[lag_i])
                range_m = float(caf_result.range_m()[lag_i])
                doppler = float(caf_result.doppler_hz[dop_i])
                detections.append(Detection(lag, range_m, doppler, snr, float(cell)))
    return detections
