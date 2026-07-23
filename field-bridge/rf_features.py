#!/usr/bin/env python3
"""Spectral-feature classifier INFRASTRUCTURE for CEMA cUAS (backlog C13,
"second opinion" alongside gamutrf_infer.py's ResNet18 classifier).

STATUS AS OF 2026-07-23: INFRASTRUCTURE ONLY. NOT TRAINED. NO ACCURACY CLAIM.
=============================================================================
This module implements a real, honestly-scoped feature-extraction +
classifier pipeline (`extract_rf_features()`, `train_classifier()`,
`classify()`), but there is NO trained model shipped with it and NO
labeled real-world training set has been assembled yet. Nothing in this
file has ever seen real training data. Do not wire this into the live
detection-ingest pipeline (hackrf_rx.py / ml_classify_bridge.py) until a
model has actually been trained on real, operator-confirmed labeled
captures and evaluated on a held-out split -- that is future work, tracked
separately from this pass.

Why this exists
----------------
A user-supplied reference snippet proposed a lightweight RandomForestClassifier
over hand-engineered spectral features (mean/std power, PAPR, spectral
flatness, bandwidth, hop rate) as a fast "second opinion" next to the
ResNet18 spectrogram classifier. The FEATURE-ENGINEERING IDEAS are sound
and worth adopting -- PAPR and spectral flatness in particular are
legitimate, well-known signal-classification features, and the reference
code's spectral-flatness formula (geometric mean / arithmetic mean of
linear power) is mathematically correct.

What was REJECTED from the reference code, and why
----------------------------------------------------
1. The reference code trained on 100% `np.random.normal(...)`-generated
   SYNTHETIC signatures for "wifi"/"bluetooth"/"drone" classes. This
   project's standing rule is real data only, never fabricated/simulated
   training data, and never a fabricated accuracy claim -- see
   `field-bridge/drone_rf_kb/README.md` for the same discipline applied to
   the ResNet18 fine-tuning effort (real IQ captures staged, but explicitly
   "no fine-tuning run yet" rather than claiming a trained model exists).
   This module follows the identical discipline: it is staged, not trained.
2. The reference code has a real bug: `train_test_split(X, y,
   test_weight=0.2, ...)`. scikit-learn's actual keyword is `test_size`,
   not `test_weight` -- `test_weight` is not a valid argument to
   `train_test_split` and this call would raise `TypeError` if executed.
   `train_classifier()` below uses the correct `test_size` parameter.

What real feature-extraction infrastructure this reuses (not reinvented)
--------------------------------------------------------------------------
- `hackrf_rx.py` already computes, per real sweep cycle: peak dBm, that
  band's calibrated floor dBm (`BAND_NOISE_FLOOR_DBM`), a crude occupied
  bandwidth in MHz (contiguous run of bins within `DETECT_THRESHOLD_DB/2`
  of the peak -- see its Bluetooth-exclusion block, `occupied_bw_mhz`), and
  persistence/duty-cycle-adjacent counters (`consecutive_hits`,
  `bt_track["moving_cycles"]`, `sik_hit_window`). `compute_bandwidth_mhz()`
  below reimplements the SAME contiguous-run-above-half-threshold
  algorithm as a standalone, reusable function (hackrf_rx.py's version is
  inlined in its main loop and not currently exported) so callers who
  already have a raw power-bin array can get an occupied-bandwidth feature
  without re-deriving it.
- `backend/server.py`'s `/detections/{id}/cadence` endpoint (`_interval_stats()`)
  computes REAL inter-arrival-interval statistics (mean/min/max/stddev/CV)
  from actual re-confirmation timestamps -- never a fabricated on/off duty
  cycle. `compute_cadence_features()` below reimplements the same
  interval-statistics logic (mean interval, coefficient of variation) as a
  standalone function with no FastAPI/Motor dependency, so it can be reused
  here to turn a real detection's `reconfirm_events` timestamps into
  hop-rate/cadence-style ML features without importing the whole backend
  app. The math is identical to `_interval_stats()`; this is a deliberate,
  documented duplication to avoid pulling FastAPI/Motor into a standalone
  ML module, not a fork of the logic with different behavior.

Feature vector (in FEATURE_NAMES order)
----------------------------------------
    mean_power_dbm, std_power_dbm, max_power_dbm, papr_db,
    spectral_flatness, bandwidth_mhz, mean_interval_s,
    coefficient_of_variation

The last two (cadence features) are optional -- pass `timestamps_iso=None`
(or too few real timestamps) and they are reported as `None`/omitted from
the numeric feature vector rather than fabricated as 0, matching
`_interval_stats()`'s own "never fabricate, report None" convention.

Training path (once real labeled data exists)
------------------------------------------------
`train_classifier(X, y, test_size=0.2)` takes REAL feature rows (each row
built by `extract_rf_features()` from a REAL sweep/capture) and REAL labels
(operator-confirmed ground truth, e.g. "this was a confirmed DJI OcuSync
detection", "this was confirmed ordinary Wi-Fi AP traffic", "this was
confirmed Bluetooth"). It does NOT generate its own data. Running
`python rf_features.py train --data <real_labeled.csv>` with no such file
will fail loudly rather than silently falling back to synthetic data.

Future wiring (not done yet)
-----------------------------
If/when a real model is trained and validated on a held-out split, this
classifier could be wired into `hackrf_rx.py` or `ml_classify_bridge.py`'s
ingest as a second, independent signal alongside the ResNet18 softmax,
using a NEW `confidence_type` enum value, `"spectral_features_ml"`,
following the closed-enum convention documented in
`backend/CONFIDENCE_MODEL.md` (alongside `heuristic_binary`,
`ml_probability`, `protocol_verified`, `advisory_only`). That wiring is
explicitly OUT OF SCOPE for this pass -- there is no trained model to wire.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Sequence

import numpy as np

FEATURE_NAMES: List[str] = [
    "mean_power_dbm",
    "std_power_dbm",
    "max_power_dbm",
    "papr_db",
    "spectral_flatness",
    "bandwidth_mhz",
    "mean_interval_s",
    "coefficient_of_variation",
]

# Sentinel used for the two optional cadence features when no/insufficient
# real timestamp data is available. NaN (not 0.0) so a downstream
# imputer/model can tell "genuinely absent" apart from "measured as zero" --
# scikit-learn's RandomForestClassifier does not accept NaN directly, so
# train_classifier() below performs an explicit, documented median-impute
# rather than silently passing NaN through or defaulting to 0.
MISSING = float("nan")


def compute_bandwidth_mhz(power_dbm_bins: Sequence[float], floor_dbm: float,
                           detect_threshold_db: float, bin_width_mhz: float = 1.0) -> float:
    """Standalone reimplementation of hackrf_rx.py's inline occupied-bandwidth
    estimate (see its Bluetooth-exclusion block): width, in MHz, of the
    contiguous run of bins within `detect_threshold_db / 2` of the peak,
    centered on the peak bin. Same coarse-by-design heuristic, same caveat:
    this is NOT a real spectral-mask analysis.

    Returns 0.0 if `power_dbm_bins` is empty (nothing to measure).
    """
    powers = list(power_dbm_bins)
    if not powers:
        return 0.0
    peak_idx = int(np.argmax(powers))
    half_thresh = floor_dbm + detect_threshold_db / 2.0
    lo_idx = peak_idx
    while lo_idx > 0 and powers[lo_idx - 1] >= half_thresh:
        lo_idx -= 1
    hi_idx = peak_idx
    while hi_idx < len(powers) - 1 and powers[hi_idx + 1] >= half_thresh:
        hi_idx += 1
    return (hi_idx - lo_idx + 1) * bin_width_mhz


def compute_cadence_features(timestamps_iso: Optional[List[str]]) -> Dict[str, Optional[float]]:
    """Reimplements backend/server.py's `_interval_stats()` math (mean
    inter-event interval, coefficient of variation) as a standalone
    function with no FastAPI/Motor dependency, so ML feature extraction can
    reuse the SAME real-timestamp-derived statistics `/detections/{id}/cadence`
    already exposes, without importing the whole backend app.

    Returns {"mean_interval_s": None, "coefficient_of_variation": None} if
    there are fewer than 2 timestamps (not enough to form even one
    interval) -- never fabricates a value, matching `_interval_stats()`.
    """
    from datetime import datetime

    if not timestamps_iso or len(timestamps_iso) < 2:
        return {"mean_interval_s": None, "coefficient_of_variation": None}
    times = sorted(datetime.fromisoformat(t) for t in timestamps_iso)
    deltas_s = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    mean_s = float(np.mean(deltas_s))
    result: Dict[str, Optional[float]] = {
        "mean_interval_s": round(mean_s, 2),
        "coefficient_of_variation": None,
    }
    if len(deltas_s) >= 2 and mean_s > 0:
        stdev_s = float(np.std(deltas_s))  # population stddev, matches statistics.pstdev used server-side
        result["coefficient_of_variation"] = round(stdev_s / mean_s, 3)
    return result


def extract_rf_features(power_dbm_bins: Sequence[float], floor_dbm: float,
                         detect_threshold_db: float = 15.0, bin_width_mhz: float = 1.0,
                         timestamps_iso: Optional[List[str]] = None) -> Dict[str, float]:
    """Compute a real spectral feature vector from a real power-bin array
    (e.g. one `sweep_band()` cycle's held bins from hackrf_rx.py, or bins
    derived from a real IQ capture's PSD).

    mean/std/max power: plain descriptive stats over the dBm bins.

    PAPR (peak-to-average power ratio, dB): computed correctly in LINEAR
    power (not dBm) -- convert each dBm bin to linear watts-relative units
    first, then PAPR_db = 10*log10(peak_linear / mean_linear). This matches
    the reference code's formula, which was mathematically sound even
    though its training data was not real.

    Spectral flatness: geometric_mean(linear_power) / arithmetic_mean(linear_power),
    in [0, 1]. Near 1.0 = noise-like/flat spectrum (e.g. wideband noise or a
    very wide OFDM-like signal); near 0 = a few dominant narrow tones
    (e.g. a Bluetooth-like narrowband hop, or a strong single carrier).
    Uses a small epsilon floor to avoid log(0) from a true-zero bin.

    bandwidth_mhz: `compute_bandwidth_mhz()` (see above).

    mean_interval_s / coefficient_of_variation: `compute_cadence_features()`
    (see above) -- None if `timestamps_iso` is omitted or has <2 entries.

    Returns a plain dict keyed by FEATURE_NAMES (cadence keys may hold
    None). Use `feature_dict_to_vector()` to get an ordered, NaN-imputed
    numpy row for feeding a scikit-learn model.
    """
    powers = np.asarray(list(power_dbm_bins), dtype=np.float64)
    if powers.size == 0:
        raise ValueError("extract_rf_features requires at least one power bin")

    mean_power_dbm = float(np.mean(powers))
    std_power_dbm = float(np.std(powers))
    max_power_dbm = float(np.max(powers))

    # dBm -> linear (relative) power for PAPR / spectral flatness. The
    # absolute reference (dBm vs dBW vs dBFS) cancels out in both ratios, so
    # treating dBm numerically as dB-relative-to-1-unit is fine here.
    linear = np.power(10.0, powers / 10.0)
    eps = 1e-12
    peak_linear = float(np.max(linear))
    mean_linear = float(np.mean(linear))
    papr_db = 10.0 * np.log10(max(peak_linear, eps) / max(mean_linear, eps))

    geo_mean = float(np.exp(np.mean(np.log(linear + eps))))
    arith_mean = max(mean_linear, eps)
    spectral_flatness = geo_mean / arith_mean

    bandwidth_mhz = compute_bandwidth_mhz(powers, floor_dbm, detect_threshold_db, bin_width_mhz)

    cadence = compute_cadence_features(timestamps_iso)

    return {
        "mean_power_dbm": mean_power_dbm,
        "std_power_dbm": std_power_dbm,
        "max_power_dbm": max_power_dbm,
        "papr_db": float(papr_db),
        "spectral_flatness": spectral_flatness,
        "bandwidth_mhz": bandwidth_mhz,
        "mean_interval_s": cadence["mean_interval_s"],
        "coefficient_of_variation": cadence["coefficient_of_variation"],
    }


def feature_dict_to_vector(features: Dict[str, Optional[float]]) -> np.ndarray:
    """Order a feature dict per FEATURE_NAMES, mapping missing/None cadence
    values to MISSING (NaN) rather than 0.0 -- see MISSING's docstring."""
    return np.array([
        MISSING if features.get(name) is None else float(features[name])
        for name in FEATURE_NAMES
    ], dtype=np.float64)


# --- Training path -----------------------------------------------------------
# Requires scikit-learn, which is not necessarily installed everywhere this
# repo's Python runs (e.g. the lightweight field-bridge sweep host). Imported
# lazily inside functions that need it so importing rf_features.py for
# extract_rf_features()/compute_* alone never requires scikit-learn.

def train_classifier(X: np.ndarray, y: Sequence[str], test_size: float = 0.2,
                      random_state: int = 42, n_estimators: int = 100):
    """Train a RandomForestClassifier on REAL feature rows `X` (shape
    [n_samples, len(FEATURE_NAMES)]) and REAL labels `y`.

    Fixes the reference code's real bug: scikit-learn's train_test_split
    keyword is `test_size`, NOT `test_weight` (`test_weight` is not a valid
    argument and the reference call would raise TypeError if run as-is).

    Does NOT generate, augment, or otherwise fabricate any data -- X and y
    must already be real, operator-labeled samples. Raises ValueError if
    fewer than 2 samples per class are provided (can't hold out a test
    split from a singleton class) rather than silently proceeding.

    Returns (model, metrics) where metrics is computed ONLY on the real
    held-out test split -- never a training-set score reported as if it
    were held-out accuracy, and never a number reported when there wasn't
    enough real data to compute one honestly.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.metrics import accuracy_score, classification_report
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import Pipeline

    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y)
    if X.shape[0] != len(y):
        raise ValueError(f"X has {X.shape[0]} rows but y has {len(y)} labels")

    counts = {label: int(np.sum(y == label)) for label in np.unique(y)}
    too_small = [label for label, c in counts.items() if c < 2]
    if too_small:
        raise ValueError(
            f"Cannot honestly train/evaluate: class(es) {too_small} have <2 real "
            f"samples, so no held-out split is possible without fabricating data. "
            f"Collect more real labeled captures for these classes first."
        )

    # median-impute missing cadence features (NaN) rather than pass NaN into
    # RandomForestClassifier (which cannot handle it) or silently zero-fill.
    pipeline = Pipeline([
        ("impute", SimpleImputer(strategy="median")),
        ("clf", RandomForestClassifier(n_estimators=n_estimators, random_state=random_state)),
    ])

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=random_state, stratify=y,
    )
    pipeline.fit(X_train, y_train)
    y_pred = pipeline.predict(X_test)
    metrics = {
        "n_train": int(len(y_train)),
        "n_test": int(len(y_test)),
        "test_accuracy": float(accuracy_score(y_test, y_pred)),
        "classification_report": classification_report(y_test, y_pred, output_dict=True, zero_division=0),
    }
    return pipeline, metrics


def classify(model, features: Dict[str, Optional[float]]):
    """Run inference for one real feature dict against a trained pipeline.

    Returns (label, class_probabilities_dict). Callers wiring this into the
    live ingest pipeline (future work, NOT done in this pass) should set
    `confidence_type="spectral_features_ml"` per the module docstring's
    CONFIDENCE_MODEL.md convention -- not `ml_probability`, which is already
    reserved for the ResNet18 spectrogram classifier's softmax output.
    """
    vector = feature_dict_to_vector(features).reshape(1, -1)
    label = model.predict(vector)[0]
    proba = model.predict_proba(vector)[0]
    classes = model.named_steps["clf"].classes_ if hasattr(model, "named_steps") else model.classes_
    proba_dict = {str(c): float(p) for c, p in zip(classes, proba)}
    return label, proba_dict


def save_model(model, path: str) -> None:
    import joblib
    joblib.dump(model, path)


def load_model(path: str):
    import joblib
    return joblib.load(path)


# --- CLI ----------------------------------------------------------------------

def _load_real_labeled_csv(path: str):
    """Load a real labeled feature CSV: one row per real sample, columns
    matching FEATURE_NAMES plus a `label` column. Does NOT generate rows --
    fails loudly if the file is missing or malformed rather than falling
    back to synthetic data."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"No real labeled dataset found at {path!r}. This module ships "
            f"infrastructure only -- it does not fabricate training data. "
            f"Build a real labeled CSV first (columns: {', '.join(FEATURE_NAMES)}, label), "
            f"e.g. from operator-confirmed hackrf_rx.py detections or real "
            f"field-bridge/drone_rf_kb captures run through extract_rf_features()."
        )
    rows, labels = [], []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        missing_cols = [n for n in FEATURE_NAMES if n not in (reader.fieldnames or [])]
        if missing_cols or "label" not in (reader.fieldnames or []):
            raise ValueError(
                f"{path} is missing required column(s): "
                f"{missing_cols + (['label'] if 'label' not in (reader.fieldnames or []) else [])}"
            )
        for row in reader:
            feat = {name: (float(row[name]) if row[name] not in ("", None) else None)
                    for name in FEATURE_NAMES}
            rows.append(feature_dict_to_vector(feat))
            labels.append(row["label"])
    if not rows:
        raise ValueError(f"{path} contains a header but zero real data rows.")
    return np.vstack(rows), labels


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    train_ap = sub.add_parser("train", help="Train on a REAL labeled feature CSV (no synthetic fallback)")
    train_ap.add_argument("--data", required=True,
                           help="Path to a real labeled CSV (columns: " + ", ".join(FEATURE_NAMES) + ", label)")
    train_ap.add_argument("--test-size", type=float, default=0.2)
    train_ap.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "rf_features_model.joblib"))

    args = ap.parse_args()

    if args.cmd == "train":
        X, y = _load_real_labeled_csv(args.data)
        model, metrics = train_classifier(X, y, test_size=args.test_size)
        save_model(model, args.out)
        print(json.dumps({
            "status": "trained_on_real_data",
            "model_path": args.out,
            "metrics_on_real_held_out_split": metrics,
        }, indent=2, default=str))


if __name__ == "__main__":
    main()
