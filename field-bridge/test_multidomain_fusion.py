#!/usr/bin/env python3
"""Tests for multidomain_fusion.py (task #123). All inputs are synthetic --
no camera/thermal/acoustic hardware exists in this project (see
CAMERA_THERMAL_ACOUSTIC_SCOPE.md); this validates the fusion MATH, not any
real sensor's calibration."""
import math

import pytest

from multidomain_fusion import (
    CONFIDENCE_TYPE_MULTIDOMAIN_FUSED,
    ModalityEvidence,
    fuse_confidences,
)


def test_no_evidence_returns_neutral_prior_and_no_fabricated_signal():
    result = fuse_confidences([])
    assert result.fused_confidence == 0.5
    assert result.contributing_modalities == []
    assert result.conflict_detected is False
    assert "no evidence" in result.note.lower() or "fabricat" in result.note.lower()


def test_absent_modalities_are_excluded_not_treated_as_zero():
    evidences = [
        ModalityEvidence("rf", confidence=0.9, present=True),
        ModalityEvidence("thermal", confidence=None, present=False),
        ModalityEvidence("acoustic", confidence=0.8, present=False),
    ]
    result = fuse_confidences(evidences)
    assert result.contributing_modalities == ["rf"]
    # single usable modality with weight 1.0 -> identity
    assert result.fused_confidence == pytest.approx(0.9, abs=1e-6)


def test_single_modality_only_graceful_degradation():
    result = fuse_confidences([ModalityEvidence("rf", confidence=0.73)])
    assert result.contributing_modalities == ["rf"]
    assert result.fused_confidence == pytest.approx(0.73, abs=1e-6)
    assert result.conflict_detected is False
    assert "single-modality" in result.note.lower()


def test_agreeing_modalities_increase_fused_confidence_above_either_input():
    evidences = [
        ModalityEvidence("rf", confidence=0.7),
        ModalityEvidence("thermal", confidence=0.7),
    ]
    result = fuse_confidences(evidences)
    assert result.fused_confidence > 0.7
    assert result.conflict_detected is False
    assert set(result.contributing_modalities) == {"rf", "thermal"}


def test_three_agreeing_modalities_more_confident_than_two():
    two = fuse_confidences(
        [ModalityEvidence("rf", confidence=0.7), ModalityEvidence("thermal", confidence=0.7)]
    )
    three = fuse_confidences(
        [
            ModalityEvidence("rf", confidence=0.7),
            ModalityEvidence("thermal", confidence=0.7),
            ModalityEvidence("acoustic", confidence=0.7),
        ]
    )
    assert three.fused_confidence > two.fused_confidence


def test_conflicting_modalities_do_not_silently_average_to_false_confidence():
    evidences = [
        ModalityEvidence("rf", confidence=0.95),
        ModalityEvidence("thermal", confidence=0.05),
    ]
    result = fuse_confidences(evidences)
    # A plain average would report 0.5 as an ordinary medium-confidence
    # reading; this module must additionally flag the disagreement.
    assert result.conflict_detected is True
    assert result.fused_confidence == pytest.approx(0.5, abs=1e-6)
    assert "conflict" in result.note.lower()


def test_conflicting_modalities_of_different_magnitude_partially_cancel_and_flag():
    evidences = [
        ModalityEvidence("rf", confidence=0.97),
        ModalityEvidence("acoustic", confidence=0.03),
    ]
    result = fuse_confidences(evidences)
    assert result.conflict_detected is True
    # rf weight 1.0 vs acoustic weight 0.6 (DEFAULT_MODALITY_WEIGHTS) means
    # rf's evidence dominates but does not fully cancel acoustic's.
    assert result.fused_confidence > 0.5


def test_mild_disagreement_below_threshold_not_flagged_as_conflict():
    # Both readings are close to neutral (0.55 vs 0.45); this is noise, not
    # a confident disagreement, and should not trip the conflict flag.
    evidences = [
        ModalityEvidence("rf", confidence=0.55),
        ModalityEvidence("thermal", confidence=0.45),
    ]
    result = fuse_confidences(evidences)
    assert result.conflict_detected is False


def test_custom_weights_are_respected():
    evidences = [
        ModalityEvidence("rf", confidence=0.8),
        ModalityEvidence("acoustic", confidence=0.8),
    ]
    default_result = fuse_confidences(evidences)
    zero_weight_acoustic = fuse_confidences(evidences, weights={"rf": 1.0, "acoustic": 0.0})
    # Zero-weighting acoustic should reduce its contribution to the fused
    # result relative to default weighting (acoustic still counted at 0.6).
    assert zero_weight_acoustic.fused_confidence < default_result.fused_confidence
    assert zero_weight_acoustic.fused_confidence == pytest.approx(0.8, abs=1e-6)


def test_extreme_confidence_values_do_not_produce_inf_or_nan():
    evidences = [
        ModalityEvidence("rf", confidence=1.0),
        ModalityEvidence("thermal", confidence=0.0),
    ]
    result = fuse_confidences(evidences)
    assert math.isfinite(result.fused_confidence)
    assert 0.0 <= result.fused_confidence <= 1.0


def test_fused_confidence_always_in_valid_probability_range():
    for rf in (0.01, 0.3, 0.5, 0.7, 0.99):
        for thermal in (0.01, 0.3, 0.5, 0.7, 0.99):
            result = fuse_confidences(
                [ModalityEvidence("rf", confidence=rf), ModalityEvidence("thermal", confidence=thermal)]
            )
            assert 0.0 <= result.fused_confidence <= 1.0


def test_confidence_type_is_multidomain_fused():
    result = fuse_confidences([ModalityEvidence("rf", confidence=0.6)])
    assert result.confidence_type == CONFIDENCE_TYPE_MULTIDOMAIN_FUSED
    assert result.confidence_type == "multidomain_fused"


def test_per_modality_llr_reported_for_debugging():
    result = fuse_confidences(
        [ModalityEvidence("rf", confidence=0.8), ModalityEvidence("thermal", confidence=0.6)]
    )
    assert set(result.per_modality_llr.keys()) == {"rf", "thermal"}
    # RF's higher confidence should produce a larger positive log-odds value.
    assert result.per_modality_llr["rf"] > result.per_modality_llr["thermal"]
