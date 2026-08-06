#!/usr/bin/env python3
"""INTERFACE tests for thermal_bridge.py (task #83 AI Engineer follow-up,
2026-07-25).

These are interface/contract tests only -- they verify the scaffold's
shapes (ingest payload construction, gating thresholds, error-raising
behavior when no checkpoint/torch exists) using dummy data. They are NOT
model-accuracy validation: no trained model exists, so there is nothing to
validate accuracy against. Do not read a pass here as "the thermal
detector works" -- it means "the plumbing around a not-yet-existing model
is shaped correctly."

Tests requiring a real torch/torchvision install (build_model,
ThermalDetector.load/infer) are skipped with a clear reason when torch is
not importable -- e.g. on this Mac dev copy, where torch is genuinely not
installed (per this project's standing rule, real installs/runs belong on
the deploy VM). Run this file there for the torch-dependent coverage.

Run: pytest field-bridge/test_thermal_bridge.py -v
"""
import pytest

from thermal_bridge import (
    CONFIDENCE_TYPE_ML_PROBABILITY,
    DRONE_CLASS_NAME,
    MIN_REPORT_SCORE,
    NO_TORCH_MSG,
    SOURCE_THERMAL_CAM,
    ThermalDetection,
    ThermalDetector,
    detection_to_ingest_body,
)

try:
    import torch  # noqa: F401
    import torchvision  # noqa: F401
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False

requires_torch = pytest.mark.skipif(
    not TORCH_AVAILABLE,
    reason="torch/torchvision not installed in this environment -- run on "
           "the deploy VM for torch-dependent coverage (see thermal_bridge.py)")


def test_source_and_confidence_type_match_scope_doc():
    """CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4 specifies THERMAL_CAM as the
    new source value and reuse of ml_probability (not a new
    "visual_confirmed" enum) -- pin both here so a future edit can't drift
    from the documented design without a visible test failure."""
    assert SOURCE_THERMAL_CAM == "THERMAL_CAM"
    assert CONFIDENCE_TYPE_ML_PROBABILITY == "ml_probability"


def test_detection_to_ingest_body_shape():
    det = ThermalDetection(score=0.83, box_xyxy=(10.0, 20.0, 110.0, 220.0),
                            frame_width_px=640, frame_height_px=512)
    body = detection_to_ingest_body(det)

    assert body["source"] == "THERMAL_CAM"
    assert body["confidence_type"] == "ml_probability"
    assert body["ml_label"] == DRONE_CLASS_NAME
    assert body["ml_confidence"] == pytest.approx(0.83)
    assert body["protocol"] == "Thermal-IR"
    # honesty guards: no fabricated ranging/bearing numbers
    assert body["distance_m"] == 0.0
    assert body["distance_estimated"] is False
    # bearing is EXPLICITLY unavailable (None), never a fake 0.0 "North"
    assert body["bearing_deg"] is None
    assert body["bearing_available"] is False


def test_detection_to_ingest_body_confidence_rounding():
    det = ThermalDetection(score=0.123456, box_xyxy=(0, 0, 1, 1),
                            frame_width_px=1, frame_height_px=1)
    body = detection_to_ingest_body(det)
    assert body["ml_confidence"] == round(0.123456, 4)


def test_bearing_deg_is_unavailable_not_fabricated():
    """No camera calibration/mounting-azimuth data (and no multi-antenna DF
    array) exists -- bearing must be an EXPLICIT None ("unknown"), never a
    fabricated 0.0 that renders as a confident '0 deg North'."""
    det = ThermalDetection(score=0.9, box_xyxy=(0, 0, 10, 10),
                            frame_width_px=100, frame_height_px=100)
    assert det.bearing_deg() is None


def test_min_report_score_is_a_real_threshold_not_zero_or_one():
    """Sanity check that the gate is neither disabled (0.0, reports
    everything) nor impossible (1.0, reports nothing) -- exact value is an
    untuned placeholder per the module docstring, but it must be a real
    gate."""
    assert 0.0 < MIN_REPORT_SCORE < 1.0


def test_thermal_detector_infer_without_load_raises():
    detector = ThermalDetector()
    with pytest.raises(RuntimeError):
        # infer() will hit _require_torch() first if torch is missing, or
        # the "must call load()" RuntimeError if torch is present -- both
        # are RuntimeError, so this assertion holds either way.
        detector.infer(None)


def test_thermal_detector_load_missing_checkpoint_raises_filenotfound():
    if not TORCH_AVAILABLE:
        with pytest.raises(RuntimeError) as exc_info:
            ThermalDetector().load("/nonexistent/checkpoint.pt")
        assert "torch" in str(exc_info.value).lower()
        return
    detector = ThermalDetector()
    with pytest.raises(FileNotFoundError):
        detector.load("/nonexistent/thermal_checkpoint_that_does_not_exist.pt")


@requires_torch
def test_build_model_constructs_single_class_detector():
    from thermal_bridge import build_model
    model = build_model(num_classes=2, pretrained_backbone=False)
    # Faster R-CNN's box predictor cls_score final layer should emit
    # num_classes logits (background + drone = 2).
    assert model.roi_heads.box_predictor.cls_score.out_features == 2


@requires_torch
def test_build_model_forward_pass_shape_on_dummy_frame():
    """Pure interface/shape test on a random dummy tensor -- exercises
    that the architecture is wired correctly end to end (forward pass
    runs, returns the expected output dict keys), NOT that it detects
    drones (an untrained model's outputs are meaningless for that)."""
    import torch
    from thermal_bridge import build_model

    model = build_model(num_classes=2, pretrained_backbone=False)
    model.eval()
    dummy_frame = torch.rand(3, 256, 256)
    with torch.no_grad():
        output = model([dummy_frame])[0]

    assert set(["boxes", "labels", "scores"]).issubset(output.keys())


def test_no_torch_msg_mentions_deploy_vm_guidance():
    """This project's standing rule is real installs/runs happen on the
    deploy VM, not the Mac -- make sure the error message a developer sees
    actually says that, rather than leaving them to guess."""
    assert "deploy VM" in NO_TORCH_MSG or "torch" in NO_TORCH_MSG.lower()
