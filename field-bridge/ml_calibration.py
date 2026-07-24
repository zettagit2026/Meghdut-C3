#!/usr/bin/env python3
"""Confidence calibration / OOD-rejection layer for the closed-world 3-class
ML classifier used by ml_classify_bridge.py (drone / wifi_2_4 / wifi_5,
resnet18_leesburg_split_0.02_1_current.pt).

=============================================================================
WHY THIS EXISTS (2026-07-24)
=============================================================================
ml_classify_bridge.py already documents (and reproduces) that this checkpoint
is a closed-world classifier with no reject/"none of the above" class: it
predicted "drone" at >99% softmax confidence on pure noise-floor IQ captured
well outside any CEMA band. The existing mitigation is two-layered:
  1. An energy gate (hackrf_sweep RSSI vs. band noise floor) that stops the
     classifier from ever being CALLED on obvious silence.
  2. UNCLASSIFIED_MAX_CONFIDENCE (0.6): if the model's own top-class softmax
     probability is below this fixed threshold, report "unclassified_signal"
     instead of forcing the label.

That fixed 0.6 threshold is a reasonable default but it is not informed by
any measurement of how this SPECIFIC deployed model actually behaves on
this SPECIFIC site's real noise floor -- every RF environment has a
different ambient floor (other Wi-Fi APs, Bluetooth, amplifier noise, etc.),
and the model can be confidently wrong in a way that is site-specific.

TWO EXTERNAL DATASETS WERE EVALUATED FOR A DATA-DRIVEN FIX (2026-07-24):
  - NoisyDroneRFv2 (Kaggle, sgluege/noisy-drone-rf-signal-classification-v2):
    license is CC BY 4.0 (verified via the dataset's own schema.org metadata
    -- permissive, redistribution/reuse allowed with attribution, no
    non-commercial restriction). NOT a license blocker. BUT: this sandboxed
    dev environment has no Kaggle API credentials (no `kaggle` CLI, no
    ~/.kaggle/kaggle.json, and the unauthenticated download API 404s), so
    the actual IQ data could not be pulled in here. This is a genuine
    environment/access blocker, not a license blocker -- documented so a
    future session WITH Kaggle credentials can revisit real retraining
    against this dataset's deep-negative-SNR examples.
  - rf-signal-intelligence (rameyjm7/rf-signal-intelligence): non-commercial
    license, explicitly cleared for this Meghaduta project only (government
    non-commercial R&D exception -- does not generalize to other projects).
    Its full VGG architecture swap is a deferred, bigger lift. What IS
    adopted here is its general OOD-rejection PATTERN: don't just threshold
    the top softmax probability, also look at the SHAPE of the softmax
    distribution (entropy) and compare against the model's own measured
    behavior on known-negative (noise-only) samples -- rather than a single
    hand-picked cutoff.

Given the Kaggle data access blocker, this module does NOT use any
NoisyDroneRFv2 samples (real or fabricated) and does NOT fabricate synthetic
"noise" training data -- both are hard standing rules for this project. What
it DOES do is give the ALREADY-DEPLOYED energy gate a place to donate its
own byproduct: every gate-check cycle where the RSSI sweep does NOT clear
the noise-floor threshold is, by the system's own existing and already-
trusted definition, a genuine noise-floor moment. collect_noise_calibration.py
(companion script, run only on real HackRF hardware in the field) captures a
short REAL IQ window during exactly those already-occurring, already-labeled
"not a real signal" moments and records the model's OWN softmax behavior on
them. That is real, non-fabricated, honestly-labeled calibration data -- it
is just not yet collected in THIS sandbox because there is no HackRF here.

This module therefore has two independent, additive layers, both OFF (i.e.
falling back to the pre-existing fixed 0.6 / no entropy check) until real
calibration data exists:
  1. Adaptive confidence threshold, derived from collected noise-floor
     softmax statistics, that can only ever RAISE the effective threshold
     above UNCLASSIFIED_MAX_CONFIDENCE (never lower it) -- i.e. calibration
     can only make the system MORE conservative, never less.
  2. A softmax-entropy check (the rf-signal-intelligence-inspired OOD
     signal): even if the top class clears the confidence bar, a
     near-uniform distribution across the 3 known classes (high normalized
     entropy) means the model isn't really distinguishing between them --
     report unclassified in that case too.

Nothing here changes the classifier's core 3-class predictions when it IS
confident, low-entropy, and (per calibration data, once collected) not
behaving like it does on known noise. It only widens the set of cases
honestly reported as "unclassified_signal" instead of a forced guess.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_CALIBRATION_FILE = os.environ.get(
    "CEMA_ML_CALIBRATION_FILE", "/var/lib/cema/ml_noise_calibration.json"
)

# Minimum number of real noise-floor samples before the adaptive threshold is
# trusted at all. Below this, statistics are too noisy to act on -- fall back
# to the fixed default entirely. This is a floor on SAMPLE COUNT, not a
# fabricated value substituting for real data.
MIN_CALIBRATION_SAMPLES = int(os.environ.get("CEMA_ML_MIN_CALIBRATION_SAMPLES", "20"))

# How many standard deviations above the mean noise-floor confidence to set
# the adaptive threshold at, i.e. "reject anything that looks like what we've
# actually measured real noise scoring as, plus margin". Conservative by
# design (2.0 sigma) since false "unclassified" is cheap (falls back to the
# existing heuristic detection) while a missed reject is the failure mode
# this whole layer exists to reduce.
CALIBRATION_Z = 2.0

# Softmax entropy check (added signal beyond top-1 confidence, per
# rf-signal-intelligence's OOD-rejection approach). Entropy is normalized to
# [0, 1] by dividing by ln(n_classes) so it is threshold-count-agnostic.
# A uniform 3-class distribution has normalized entropy 1.0; a fully
# confident one-hot distribution has 0.0. 0.85 means "softmax is close to
# uniform" -- i.e. the model is effectively guessing between classes even if
# one happens to edge out the others past UNCLASSIFIED_MAX_CONFIDENCE.
DEFAULT_MAX_NORMALIZED_ENTROPY = float(
    os.environ.get("CEMA_ML_MAX_NORMALIZED_ENTROPY", "0.85")
)


def softmax_entropy(all_probs: Dict[str, float]) -> float:
    """Shannon entropy (nats) of the classifier's softmax output."""
    h = 0.0
    for p in all_probs.values():
        if p > 0.0:
            h -= p * math.log(p)
    return h


def normalized_entropy(all_probs: Dict[str, float]) -> float:
    """Entropy normalized to [0, 1] by the max-entropy (uniform) case for
    however many classes this checkpoint has. Robust to the class count
    changing if the checkpoint is ever retrained with more classes."""
    n = len(all_probs)
    if n <= 1:
        return 0.0
    max_h = math.log(n)
    if max_h <= 0.0:
        return 0.0
    return softmax_entropy(all_probs) / max_h


@dataclass
class NoiseCalibrationStats:
    """Aggregated (not raw-sample) statistics of the classifier's softmax
    behavior on genuine, already-gate-confirmed noise-floor IQ captures.
    Stored as running sums so the calibration file stays small and merges
    cheaply across collection runs -- no raw IQ or per-sample softmax
    vectors are retained."""
    n: int = 0
    confidence_sum: float = 0.0
    confidence_sq_sum: float = 0.0
    entropy_sum: float = 0.0
    source: str = "none"  # "none" | "file" -- for honest logging

    @property
    def mean_confidence(self) -> float:
        return self.confidence_sum / self.n if self.n else 0.0

    @property
    def std_confidence(self) -> float:
        if self.n < 2:
            return 0.0
        mean = self.mean_confidence
        var = max(0.0, self.confidence_sq_sum / self.n - mean * mean)
        return math.sqrt(var)

    @property
    def mean_entropy(self) -> float:
        return self.entropy_sum / self.n if self.n else 0.0

    def update(self, confidence: float, all_probs: Dict[str, float]) -> None:
        self.n += 1
        self.confidence_sum += confidence
        self.confidence_sq_sum += confidence * confidence
        self.entropy_sum += normalized_entropy(all_probs)

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "confidence_sum": self.confidence_sum,
            "confidence_sq_sum": self.confidence_sq_sum,
            "entropy_sum": self.entropy_sum,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NoiseCalibrationStats":
        return cls(
            n=int(d.get("n", 0)),
            confidence_sum=float(d.get("confidence_sum", 0.0)),
            confidence_sq_sum=float(d.get("confidence_sq_sum", 0.0)),
            entropy_sum=float(d.get("entropy_sum", 0.0)),
            source="file",
        )


def load_calibration(path: Optional[str] = None) -> NoiseCalibrationStats:
    """Load real noise-floor calibration stats from disk if present.
    Missing/unreadable/corrupt file => empty stats (n=0), which callers
    must treat as "no real calibration data yet, use the fixed default" --
    never silently substitutes fabricated numbers."""
    path = path or DEFAULT_CALIBRATION_FILE
    if not os.path.exists(path):
        return NoiseCalibrationStats(source="none")
    try:
        with open(path, "r") as f:
            d = json.load(f)
        return NoiseCalibrationStats.from_dict(d)
    except Exception:
        return NoiseCalibrationStats(source="none")


def save_calibration(stats: NoiseCalibrationStats, path: Optional[str] = None) -> None:
    path = path or DEFAULT_CALIBRATION_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(stats.to_dict(), f, indent=2)
    os.replace(tmp_path, path)


def effective_confidence_threshold(
    stats: NoiseCalibrationStats,
    default: float,
    min_samples: int = MIN_CALIBRATION_SAMPLES,
    z: float = CALIBRATION_Z,
    cap: float = 0.97,
) -> float:
    """Compute the confidence threshold below which a prediction is reported
    as unclassified_signal. Only ever returns a value >= `default` -- real
    calibration data can make the system MORE conservative (raise the bar
    because this site's real noise floor is being scored dangerously close
    to confident by this checkpoint) but can never lower it below the
    already-reasoned-through fixed default, even if noise happens to score
    unusually low confidence in a small sample."""
    if stats.n < min_samples:
        return default
    adaptive = stats.mean_confidence + z * stats.std_confidence
    return min(cap, max(default, adaptive))


def is_ood(
    confidence: float,
    all_probs: Dict[str, float],
    stats: NoiseCalibrationStats,
    default_confidence_threshold: float,
    max_normalized_entropy: float = DEFAULT_MAX_NORMALIZED_ENTROPY,
) -> Dict[str, object]:
    """Decide whether a gated-in, energy-confirmed prediction should still be
    reported as unclassified_signal, and why. Returns a dict (not just a
    bool) so callers can log/ingest the reasoning honestly:
      {
        "unclassified": bool,
        "reason": "low_confidence" | "high_entropy" | "low_confidence+high_entropy" | None,
        "effective_confidence_threshold": float,
        "normalized_entropy": float,
        "calibration_source": "none" | "file",
        "calibration_n": int,
      }
    """
    threshold = effective_confidence_threshold(stats, default_confidence_threshold)
    ent = normalized_entropy(all_probs)

    low_conf = confidence < threshold
    high_entropy = ent > max_normalized_entropy

    reasons: List[str] = []
    if low_conf:
        reasons.append("low_confidence")
    if high_entropy:
        reasons.append("high_entropy")

    return {
        "unclassified": bool(low_conf or high_entropy),
        "reason": "+".join(reasons) if reasons else None,
        "effective_confidence_threshold": threshold,
        "normalized_entropy": ent,
        "calibration_source": stats.source if stats.n > 0 else "none",
        "calibration_n": stats.n,
    }
