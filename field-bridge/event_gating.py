#!/usr/bin/env python3
"""Burst/event gating for the IQ->spectrogram classifier pipeline.

=============================================================================
WHAT THIS IS AND WHERE IT CAME FROM (2026-07-24)
=============================================================================
Ported from a DIFFERENT project in this workspace,
`MULT-25-607-ML-for-RF-Sprectrum-sensing/src/model/MLRF/` (a WiFi-vs-
Bluetooth LightGBM classifier over PSD data), specifically:

  - `classifier.py`'s `RFClassifier.preprocess_signal()` (and the equivalent
    free function `preprocess_with_event_detection()` in
    `feature_extraction.py`... actually `preprocessing.py`): for every PSD
    row, detect burst/event regions, then build a mask that is the MEASURED
    noise floor everywhere EXCEPT inside those detected event ranges (where
    the real PSD values are kept), and classify/feature-extract on the
    MASKED signal, never the raw one.
  - `classifier.py`'s `_extract_spectral_shape_features_direct()`, which is
    where that codebase actually calls `scipy.signal.find_peaks()` /
    `scipy.signal.peak_widths()` to characterize burst shape (used there for
    feature engineering; adapted HERE to do the burst/no-burst DECISION that
    drives the noise-floor replacement mask, since find_peaks + peak_widths
    is a cleaner, better-documented way to get contiguous event ranges than
    reimplementing MLRF's separate windowed-average `event_detector()` in
    `utils.py`). The end RESULT -- non-event bins forced to the noise floor
    before anything downstream sees them -- is the same technique verified
    by reading MLRF's actual source; this module does not reuse MLRF's code
    verbatim (different domain: 2D time-frequency spectrogram dB image here
    vs. MLRF's 1D PSD-per-sample arrays there) but reimplements the same
    gate-then-mask idea against this project's own IQ->spectrogram pipeline.

=============================================================================
WHY THIS IS A DIFFERENT, COMPLEMENTARY LAYER FROM ml_calibration.py
=============================================================================
ml_calibration.py's OOD-rejection (adaptive confidence threshold + softmax
entropy) runs AFTER the ResNet18 forward pass -- it looks at the model's
OUTPUT and decides whether to trust the label. It cannot fix or clean the
INPUT; if noise-floor energy already looks vaguely signal-shaped to the
model, calibration can only flag the resulting guess as unclassified after
the fact.

This module runs BEFORE the ResNet18 forward pass, on the spectrogram that
becomes the model's input. It explicitly detects which frequency bins carry
real burst/event energy (via find_peaks/peak_widths on a measured PSD) and
overwrites every OTHER bin with that bin's own measured noise floor, so
random noise-floor texture in never-bursting frequency bins cannot
contribute anything that a CNN could latch onto as "signal-like" texture.
This does not replace the entropy/confidence OOD layer -- it is meant to
make the spectrogram FED to the classifier and to that OOD layer cleaner,
so both layers now exist and do different jobs:
  1. event_gating.py (this file): upstream input hygiene -- gate/mask the
     spectrogram itself before classification.
  2. ml_calibration.py: downstream output hygiene -- gate/reject the
     classifier's OWN prediction after classification.

Nothing here changes the energy gate in hackrf_rx.py/ml_classify_bridge.py
(whether to run inference AT ALL) or the entropy/confidence check in
ml_calibration.py (whether to trust the resulting label). It only changes
what the ResNet18 forward pass actually sees as input, once the energy gate
has already decided a capture is worth classifying.

=============================================================================
NO FABRICATED "NOISE" -- ONLY MEASURED
=============================================================================
The noise floor used here is always MEASURED from the actual captured PSD/
spectrogram (a low percentile of its own real distribution), never a
hardcoded or fabricated constant. This mirrors MLRF's classifier.py, which
takes `noise_floor` as an explicit measured parameter (not derived inside
the classifier) -- here we derive it directly from the real capture's own
statistics, since (unlike MLRF's offline dataset pipeline) this project
does not have a separately-supplied per-capture noise floor value threaded
in from elsewhere at this point in the pipeline.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np
from scipy.signal import find_peaks, peak_widths


def measured_noise_floor_db(psd_db: np.ndarray, floor_percentile: float = 10.0) -> float:
    """Estimate the noise floor of a PSD/spectrogram-row (dB) as a low
    percentile of its OWN measured distribution -- never a fabricated or
    hardcoded value. Default 10th percentile: robust to a handful of bins
    already carrying burst energy (which would otherwise pull a simple
    min/mean estimate up or down)."""
    return float(np.percentile(np.asarray(psd_db, dtype=np.float64), floor_percentile))


def detect_burst_bins(
    psd_db: np.ndarray,
    noise_floor_db: float,
    min_snr_db: float = 6.0,
    min_distance_bins: int = 3,
    rel_height: float = 0.9,
) -> List[Tuple[int, int]]:
    """Detect burst/event regions in a 1D PSD (dB) using
    scipy.signal.find_peaks + scipy.signal.peak_widths, the same two SciPy
    primitives MLRF's classifier.py uses for its spectral-shape feature
    extraction. Here they are used to locate contiguous event ranges (peak
    +/- its width at `rel_height`), which is what drives the noise-floor
    replacement mask in gate_psd_to_noise_floor()/gate_spectrogram_freq_bins()
    below.

    `min_snr_db` is a REQUIRED margin above the measured noise floor (not a
    fabricated absolute level) before a peak counts as an event -- mirrors
    hackrf_rx.py/ml_classify_bridge.py's own DETECT_THRESHOLD_DB-above-floor
    convention for the energy gate, so "what counts as real" is defined the
    same way at both the RF-sweep-gate layer and this spectrogram-gate layer.

    Returns a list of merged, bounds-clipped (start_bin, end_bin) INCLUSIVE
    ranges. Empty list if no burst is found (i.e. everything is noise floor).
    """
    psd_db = np.asarray(psd_db, dtype=np.float64)
    height = noise_floor_db + min_snr_db
    peaks, _ = find_peaks(psd_db, height=height, distance=max(1, min_distance_bins))
    if len(peaks) == 0:
        return []

    widths, _width_heights, left_ips, right_ips = peak_widths(psd_db, peaks, rel_height=rel_height)

    n = len(psd_db)
    ranges: List[Tuple[int, int]] = []
    for left, right in zip(left_ips, right_ips):
        start = max(0, int(np.floor(left)))
        end = min(n - 1, int(np.ceil(right)))
        if end >= start:
            ranges.append((start, end))

    # Merge overlapping/adjacent ranges (peak_widths can return overlapping
    # spans for closely-spaced peaks).
    ranges.sort()
    merged: List[Tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def gate_psd_to_noise_floor(
    psd_db: np.ndarray,
    noise_floor_db: float = None,
    min_snr_db: float = 6.0,
    min_distance_bins: int = 3,
    rel_height: float = 0.9,
):
    """Direct 1D port of MLRF's RFClassifier.preprocess_signal(): replace
    every bin OUTSIDE detected burst events with the measured noise floor,
    keeping the real PSD values only inside event ranges. Returns
    (gated_psd_db, events, noise_floor_db, total_event_bins).
    """
    psd_db = np.asarray(psd_db, dtype=np.float64)
    if noise_floor_db is None:
        noise_floor_db = measured_noise_floor_db(psd_db)

    events = detect_burst_bins(psd_db, noise_floor_db, min_snr_db, min_distance_bins, rel_height)

    gated = np.full_like(psd_db, noise_floor_db)
    total_event_bins = 0
    for start, end in events:
        gated[start:end + 1] = psd_db[start:end + 1]
        total_event_bins += end - start + 1

    return gated, events, noise_floor_db, total_event_bins


def gate_spectrogram_freq_bins(
    S_db: np.ndarray,
    min_snr_db: float = 6.0,
    min_distance_bins: int = 3,
    rel_height: float = 0.9,
):
    """Apply burst/event gating to a 2D spectrogram (n_freq x n_time, dB)
    along the FREQUENCY axis, before it is normalized/colorized and fed to
    the ResNet18 classifier.

    Gating PSD used for event DETECTION is the per-frequency-bin MAX across
    time (not the mean), so a short transient burst that only appears in a
    few time columns is not averaged away before detection. Frequency bins
    that never carry event energy anywhere in the capture window get
    replaced, for ALL time samples, with that bin's OWN measured noise floor
    (its per-bin median across time) -- so genuinely noisier vs. genuinely
    quieter never-bursting frequency bins each get their own correct floor
    value rather than one blanket scalar for the whole image.

    Returns (gated_S_db, events, overall_noise_floor_db).
    """
    S_db = np.asarray(S_db, dtype=np.float64)
    if S_db.ndim != 2:
        raise ValueError(f"gate_spectrogram_freq_bins: expected 2D (freq, time) array, got shape {S_db.shape}")

    n_freq, _n_time = S_db.shape
    gating_psd = np.max(S_db, axis=1)
    per_bin_floor = np.median(S_db, axis=1)
    overall_floor = measured_noise_floor_db(gating_psd)

    events = detect_burst_bins(gating_psd, overall_floor, min_snr_db, min_distance_bins, rel_height)

    event_mask = np.zeros(n_freq, dtype=bool)
    for start, end in events:
        event_mask[start:end + 1] = True

    gated = S_db.copy()
    non_event_rows = ~event_mask
    if np.any(non_event_rows):
        gated[non_event_rows, :] = per_bin_floor[non_event_rows, np.newaxis]

    return gated, events, overall_floor
