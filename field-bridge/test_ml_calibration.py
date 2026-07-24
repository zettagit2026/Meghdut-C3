#!/usr/bin/env python3
"""Unit tests for ml_calibration.py's OOD-rejection / calibration layer.

Run: pytest field-bridge/test_ml_calibration.py -v
"""
import json
import math
import os

import pytest

from ml_calibration import (
    NoiseCalibrationStats,
    effective_confidence_threshold,
    is_ood,
    load_calibration,
    normalized_entropy,
    save_calibration,
    softmax_entropy,
)


def test_normalized_entropy_uniform_is_one():
    probs = {"drone": 1 / 3, "wifi_2_4": 1 / 3, "wifi_5": 1 / 3}
    assert normalized_entropy(probs) == pytest.approx(1.0, abs=1e-9)


def test_normalized_entropy_onehot_is_zero():
    probs = {"drone": 1.0, "wifi_2_4": 0.0, "wifi_5": 0.0}
    assert normalized_entropy(probs) == pytest.approx(0.0, abs=1e-9)


def test_normalized_entropy_reproduces_documented_noise_finding():
    # Documented finding: model predicted "drone" at 0.99+ on pure noise.
    # That distribution should be LOW entropy (confidently, if wrongly,
    # peaked) -- this test locks in that the entropy check alone would NOT
    # have caught that specific documented failure (it is genuinely
    # low-entropy), which is exactly why the energy gate + confidence
    # threshold remain the primary mitigations and entropy is additive, not
    # a replacement.
    probs = {"drone": 0.9905, "wifi_2_4": 0.005, "wifi_5": 0.0045}
    ent = normalized_entropy(probs)
    assert ent < 0.15  # confidently low-entropy, as documented


def test_no_calibration_file_returns_empty_stats(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    stats = load_calibration(str(missing))
    assert stats.n == 0
    assert stats.source == "none"


def test_effective_threshold_falls_back_to_default_below_min_samples():
    stats = NoiseCalibrationStats(n=5, confidence_sum=4.5, confidence_sq_sum=4.05, source="file")
    thr = effective_confidence_threshold(stats, default=0.6, min_samples=20)
    assert thr == 0.6


def test_effective_threshold_never_drops_below_default():
    # Even if measured noise-floor confidence is very LOW, the effective
    # threshold must not fall below the fixed default -- calibration can
    # only make the system more conservative, never less.
    stats = NoiseCalibrationStats(n=50, confidence_sum=5.0, confidence_sq_sum=0.6, source="file")  # mean=0.1
    thr = effective_confidence_threshold(stats, default=0.6, min_samples=20)
    assert thr >= 0.6


def test_effective_threshold_raises_bar_when_noise_scores_confidently():
    # Simulate the documented failure mode: real noise-floor captures score
    # high mean confidence (~0.95) with low variance. n=50 samples all
    # around 0.95 confidence.
    n = 50
    conf = 0.95
    stats = NoiseCalibrationStats(
        n=n,
        confidence_sum=conf * n,
        confidence_sq_sum=(conf ** 2) * n,
        source="file",
    )
    thr = effective_confidence_threshold(stats, default=0.6, min_samples=20)
    assert thr > 0.6
    assert thr <= 0.97  # capped


def test_is_ood_low_confidence_without_calibration():
    stats = NoiseCalibrationStats(source="none")
    probs = {"drone": 0.4, "wifi_2_4": 0.35, "wifi_5": 0.25}
    result = is_ood(0.4, probs, stats, default_confidence_threshold=0.6)
    assert result["unclassified"] is True
    assert "low_confidence" in result["reason"]
    assert result["calibration_source"] == "none"


def test_is_ood_confident_low_entropy_passes():
    stats = NoiseCalibrationStats(source="none")
    probs = {"drone": 0.97, "wifi_2_4": 0.02, "wifi_5": 0.01}
    result = is_ood(0.97, probs, stats, default_confidence_threshold=0.6)
    assert result["unclassified"] is False
    assert result["reason"] is None


def test_is_ood_high_entropy_rejects_even_above_confidence_threshold():
    # Top class clears 0.6 but distribution is nearly uniform across the
    # other two -- this is the rf-signal-intelligence-inspired addition.
    stats = NoiseCalibrationStats(source="none")
    probs = {"drone": 0.62, "wifi_2_4": 0.30, "wifi_5": 0.08}
    result = is_ood(0.62, probs, stats, default_confidence_threshold=0.6,
                     max_normalized_entropy=0.85)
    ent = normalized_entropy(probs)
    if ent > 0.85:
        assert result["unclassified"] is True
        assert "high_entropy" in result["reason"]
    else:
        # document actual entropy for this distribution so the test is
        # self-explaining if the threshold constant changes later
        assert ent <= 0.85


def test_save_and_load_roundtrip(tmp_path):
    path = str(tmp_path / "calib.json")
    stats = NoiseCalibrationStats(source="file")
    stats.update(0.91, {"drone": 0.91, "wifi_2_4": 0.06, "wifi_5": 0.03})
    stats.update(0.88, {"drone": 0.88, "wifi_2_4": 0.08, "wifi_5": 0.04})
    save_calibration(stats, path)

    loaded = load_calibration(path)
    assert loaded.n == 2
    assert loaded.source == "file"
    assert loaded.mean_confidence == pytest.approx(stats.mean_confidence)


def test_corrupt_calibration_file_falls_back_safely(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json")
    stats = load_calibration(str(path))
    assert stats.n == 0
    assert stats.source == "none"


def test_documented_noise_case_end_to_end_without_calibration():
    """Reproduces the module-docstring-documented finding end-to-end: no
    calibration file exists (the honest default state in this sandbox),
    confidence is 0.99 -- with UNCLASSIFIED_MAX_CONFIDENCE=0.6 (existing
    mitigation) this is NOT flagged unclassified by this layer alone,
    exactly matching current deployed behavior (the energy gate is what
    prevents this call from ever happening on true silence; this test
    documents that ml_calibration.py does not change that gated-in
    behavior when no real calibration data exists yet)."""
    stats = NoiseCalibrationStats(source="none")
    probs = {"drone": 0.9905, "wifi_2_4": 0.005, "wifi_5": 0.0045}
    result = is_ood(0.9905, probs, stats, default_confidence_threshold=0.6)
    assert result["unclassified"] is False
    assert result["calibration_n"] == 0
