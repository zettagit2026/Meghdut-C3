#!/usr/bin/env python3
"""Thermal-camera drone-detection bridge -- SOFTWARE SCAFFOLDING ONLY
(task #83 AI Engineer follow-up, 2026-07-25).

=============================================================================
STATUS: NOT DEPLOYABLE. NO TRAINED MODEL. NO CAMERA HARDWARE.
=============================================================================
This module is a buildable-now INTERFACE scaffold, the same "software
scaffolding now, hardware/data-dependent parts later" pattern already used
for passive radar (task #43/#57) and the CAN/DroneCAN protocol parsers
(HARDWARE-BLOCKED sections in canopen_parser.py / dronecan_parser.py). It
defines the shape of the inference pipeline and the ingest-posting contract
so that once (a) a real thermal camera exists and (b) a real model has been
trained on real labeled thermal-drone imagery and validated on a held-out
split, this file's TODOs can be filled in without redesigning the pipeline
or the wire format.

It deliberately does NOT:
  - fabricate or download a "drone-vs-not-drone" thermal dataset
  - ship a randomly-initialized or synthetic-data-trained model and call it
    a classifier
  - claim any accuracy/precision/recall number (none exist -- no real model
    has been trained in this session)
  - talk to any camera device (none is attached to this project)

=============================================================================
WHAT'S REAL VS. WHAT'S BLOCKED (read before touching this file)
=============================================================================
REAL / verified this session:
  - torch + torchvision are already a de facto dependency of this repo
    (field-bridge/gamutrf_infer.py imports both for the RF ML classifier;
    field-bridge/spectrogram_similarity_bridge.py's requirements.txt entry
    for timm/chromadb explicitly notes "torch/torchvision already required
    by gamutrf_infer.py"). They are NOT pinned as an explicit line in
    requirements.txt today -- that gap predates this file and is worth
    fixing but is out of scope for this scaffold-only pass.
  - torchvision's built-in detection models (fasterrcnn_resnet50_fpn,
    retinanet_resnet50_fpn, fasterrcnn_mobilenet_v3_large_fpn, etc.) are
    BSD-3-Clause licensed (torchvision itself is BSD-3-Clause; verify at
    https://github.com/pytorch/vision/blob/main/LICENSE at integration
    time) -- OSI-permissive, satisfies this project's sovereignty policy.
    This is the RECOMMENDED architecture family for this task specifically
    BECAUSE the obvious YOLO-family alternatives are copyleft-encumbered:
    Ultralytics YOLOv8/v11 = AGPL-3.0 (commercial license required
    otherwise), WongKinYiu YOLOv7 = GPL-3.0. Both rejected per this
    project's OSI-permissive-only policy (see CAMERA_THERMAL_ACOUSTIC_SCOPE.md
    Sec.1). RT-DETR (Apache-2.0 via HuggingFace transformers) and YOLO-NAS
    (weights carry usage restrictions despite an "open" narrative -- verify
    the actual license text before use, it is NOT a clean Apache/BSD grant)
    were also considered; plain torchvision is the safest, most clearly
    permissive choice and needs no extra dependency beyond what this repo
    already has.
  - A real, correctly-targeted candidate dataset exists in the published
    literature: **Anti-UAV** (Jiang et al., "Anti-UAV: A Large Multi-Modal
    Benchmark for UAV Tracking", and the CVPR/ICCV Anti-UAV challenge
    series; repo https://github.com/ZhaoJ9014/Anti-UAV, MIT-licensed
    *code*). This is the right modality match: RGB+thermal-IR video of a
    ground/aerial camera tracking a UAV target -- i.e. a sensor looking AT
    a drone, which is exactly this project's use case. (NOTE: HIT-UAV and
    DroneVehicle, also found during this search, are the OPPOSITE modality
    -- a thermal camera MOUNTED ON a drone looking down at ground objects
    -- and are not useful for a ground-based counter-UAS thermal detector;
    flagged here so a future pass doesn't waste time on them.)

BLOCKED / not verified, do not assume otherwise:
  - Anti-UAV's repository code license (MIT) is NOT the same thing as the
    actual video/annotation DATASET's redistribution license. Benchmark
    datasets of this kind commonly gate the actual data behind a signed
    data-use agreement restricting use to non-commercial research. That
    agreement's exact terms were NOT read or verified in this session (no
    dataset download was performed) -- this is a real, open action item,
    not a confirmed "go", before any training run against it. Verify (a)
    the actual dataset license/EULA text at the download source and (b)
    whether it satisfies this project's OSI-permissive-only policy or
    would need the project's already-approved non-commercial exception,
    before downloading or training on it.
  - No thermal camera hardware exists in this project (see
    CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.3 BOM -- nothing purchased yet).
  - No training run has been performed. There is no checkpoint file. Do not
    add one to this repo without an accompanying honest note on what
    dataset/split/metrics produced it.
  - torch/torchvision are NOT installed in this Mac dev copy (verified via
    `pip3 show torch` -- not found). Per this project's standing rule, real
    installs/runs happen on the deploy VM (172.16.16.185), not the Mac.
    This file's torch-dependent code paths have NOT been executed anywhere
    in this session; only the pure-Python interface/shape has been
    reasoned about. Interface tests in test_thermal_bridge.py are written
    to skip gracefully when torch is unavailable, and must actually be run
    on the deploy VM (where torch is installed) before this scaffold is
    trusted even at the "imports and runs" level.

=============================================================================
PIPELINE SHAPE (mirrors ml_classify_bridge.py / gamutrf_infer.py's pattern)
=============================================================================
  1. Acquire one thermal frame (camera driver -- NOT IMPLEMENTED, no camera
     exists; would be a USB-UVC read for a FLIR Lepton/PureThermal-class
     module per the scoping doc's BOM).
  2. Preprocess: resize/normalize per the trained model's expected input
     (mirrors torchvision's standard detection-model transform contract).
  3. Run inference: torchvision detection model returns per-instance
     boxes + labels + scores (this is the SAME "real softmax-style
     probability from a trained model" epistemic category as the RF ML
     classifier -- see CONFIDENCE_MODEL.md's reasoning for why this reuses
     confidence_type="ml_probability" rather than inventing a new enum
     value).
  4. Post one DetectionIngestBody-shaped record per accepted detection to
     POST /api/detections/ingest, source="THERMAL_CAM",
     confidence_type="ml_probability", exactly following
     CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4's data-model integration plan.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

# torch/torchvision are imported lazily inside functions that need them
# (not at module import time) so this file can be imported, and its
# non-model logic (dataclasses, ingest payload construction, CLI parsing)
# can be unit-tested, even on a machine without torch installed -- e.g.
# this Mac dev copy, where torch is genuinely not installed (verified via
# `pip3 show torch`). Real inference always requires torch/torchvision to
# actually be present; NO_TORCH_MSG below is what callers see if they try
# to run real inference without it.
NO_TORCH_MSG = (
    "torch/torchvision are not installed in this environment. Per this "
    "project's standing rule, install and run real inference on the deploy "
    "VM (172.16.16.185), not the Mac dev copy. `pip install torch "
    "torchvision` (CPU or CUDA build matching the Jetson/edge-compute "
    "target) is required before ThermalDetector.load()/infer() can run."
)

# New source value for thermal-camera detections, per
# CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4 ("New source values").
SOURCE_THERMAL_CAM = "THERMAL_CAM"

# Reuse the existing epistemic-category enum value, NOT a new
# "visual_confirmed" value -- see CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4 and
# backend/CONFIDENCE_MODEL.md for why a trained detector's score is the same
# epistemic category as the RF ML classifier's softmax output.
CONFIDENCE_TYPE_ML_PROBABILITY = "ml_probability"

# Drone-class label this scaffold expects a trained checkpoint to use.
# torchvision detection models are class-index-based; the actual
# index<->name mapping must come from whatever label map the (not yet
# existing) training run produces -- placeholder name only, not a
# commitment to a specific index.
DRONE_CLASS_NAME = "drone"

# Below this score, a raw detector output is NOT posted at all (mirrors
# hackrf_rx.py/ml_classify_bridge.py's "only report real signal" gating
# philosophy -- do not manufacture a false-positive-heavy feed). This is a
# STARTING placeholder, not a tuned value -- no real precision/recall curve
# exists yet to derive it from. Must be re-derived once a real validation
# set exists (mirrors this project's existing UNCLASSIFIED_MAX_CONFIDENCE /
# ML_RECLASSIFY_MIN_CONFIDENCE coherence discipline in CONFIDENCE_MODEL.md).
MIN_REPORT_SCORE = 0.5


@dataclass
class ThermalDetection:
    """One accepted detection instance from a single thermal frame.

    Mirrors the per-instance output shape of a torchvision detection model
    (boxes/labels/scores) filtered down to the drone class and the score
    gate above.
    """
    score: float
    box_xyxy: Sequence[float]  # (x1, y1, x2, y2) in pixel coordinates
    frame_width_px: int
    frame_height_px: int

    def bearing_deg(self) -> None:
        """NO real bearing estimate is available from a single uncalibrated
        thermal camera. A real implementation needs the camera's known
        field-of-view and mounting azimuth to convert a bounding-box
        horizontal center into a bearing (camera calibration data that does
        not exist yet -- no camera is physically mounted), OR the
        multi-antenna RF amplitude-comparison DF in
        field-bridge/direction_finding.py (also hardware-gated, needs >=2
        directional antennas -- task #20).

        Returns None -- an EXPLICIT "bearing unknown", never a fabricated
        0.0 that the map/console would render as a confident "0 deg North".
        This is the project's no-fake-data rule: an unavailable measurement
        is surfaced as unavailable, not as a plausible-looking number.
        Formerly returned a hardcoded 0.0 (removed)."""
        return None


def _require_torch():
    try:
        import torch  # noqa: F401
        import torchvision  # noqa: F401
    except ImportError as e:
        raise RuntimeError(NO_TORCH_MSG) from e
    return torch, torchvision


def build_model(num_classes: int = 2, pretrained_backbone: bool = True):
    """Construct a torchvision detection model architecture (untrained
    task head) suitable for fine-tuning on a real thermal-drone dataset.

    num_classes: background + drone = 2 for a single-class detector (the
    scope doc recommends starting single-class: drone vs. not-drone).

    Uses torchvision's fasterrcnn_resnet50_fpn_v2 as the default choice:
    BSD-3-Clause licensed (permissive, satisfies this project's
    sovereignty policy), well-documented fine-tuning recipe (replace the
    box predictor head), and does not require any new dependency beyond
    what field-bridge/gamutrf_infer.py already uses (torch + torchvision).
    RetinaNet (torchvision.models.detection.retinanet_resnet50_fpn_v2) is
    an equally permissive alternative if single-stage/anchor-free tradeoffs
    are preferred later -- not chosen here only to keep this scaffold to
    one concrete architecture.

    NOT CALLED ANYWHERE YET outside tests -- this is the architecture
    scaffold, not a trained model. `pretrained_backbone=True` would load
    ImageNet/COCO-pretrained backbone weights (transfer learning starting
    point), which is standard practice and does not constitute "training
    on drone data" -- the task-specific head is still randomly initialized
    until a real fine-tuning run against real labeled thermal-drone data
    happens.
    """
    torch, torchvision = _require_torch()
    from torchvision.models.detection import fasterrcnn_resnet50_fpn_v2
    from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

    model = fasterrcnn_resnet50_fpn_v2(
        weights="DEFAULT" if pretrained_backbone else None,
    )
    in_features = model.roi_heads.box_predictor.cls_score.in_features
    model.roi_heads.box_predictor = FastRCNNPredictor(in_features, num_classes)
    return model


class ThermalDetector:
    """Inference-time wrapper. Mirrors gamutrf_infer.py's GamutRFClassifier
    shape (load-once, repeated infer()), but for a torchvision detection
    model over thermal image frames instead of a ResNet18 classifier over
    IQ-derived spectrograms.

    NOT FUNCTIONAL END-TO-END: load() requires a real checkpoint path that
    does not exist (no training run has produced one). Calling load()
    without a real, honestly-trained checkpoint will raise -- this class
    deliberately does not fall back to a randomly-initialized model and
    silently call that "loaded", which would be indistinguishable from a
    real, validated model to a caller and is exactly the kind of dishonest
    validation this project's standing rule forbids.
    """

    def __init__(self, device: Optional[str] = None):
        self._model = None
        self._device_str = device
        self._device = None

    def load(self, checkpoint_path: str, num_classes: int = 2) -> None:
        """Load real trained weights. Raises FileNotFoundError if
        checkpoint_path does not exist -- there is intentionally no
        "demo mode" fallback to random weights."""
        import os
        torch, _torchvision = _require_torch()
        if not os.path.exists(checkpoint_path):
            raise FileNotFoundError(
                f"No thermal-detector checkpoint at {checkpoint_path}. "
                "None has been trained in this project yet -- see this "
                "module's docstring (BLOCKED section) and "
                "train_thermal_detector.py for what a real training run "
                "needs before one can exist.")
        self._device = torch.device(
            self._device_str or ("cuda:0" if torch.cuda.is_available() else "cpu"))
        model = build_model(num_classes=num_classes, pretrained_backbone=False)
        # SAFETY: same weights_only=True discipline as gamutrf_infer.py --
        # never fall back to unsafe unpickling.
        state = torch.load(checkpoint_path, map_location=self._device, weights_only=True)
        model.load_state_dict(state)
        model.eval()
        model.to(self._device)
        self._model = model

    def infer(self, frame_chw_float01) -> List[ThermalDetection]:
        """frame_chw_float01: a (3, H, W) float tensor in [0, 1] (torchvision
        detection models' standard input contract -- a single-channel
        thermal frame should be replicated across 3 channels or the model's
        first conv adapted at training time; that choice belongs to
        train_thermal_detector.py, not here).

        Returns accepted ThermalDetection instances (score >=
        MIN_REPORT_SCORE, class == drone). Raises RuntimeError via
        _require_torch() if torch isn't installed, or RuntimeError if
        load() hasn't been called yet.
        """
        torch, _torchvision = _require_torch()
        if self._model is None:
            raise RuntimeError("ThermalDetector.load() must be called with a real checkpoint before infer().")
        with torch.no_grad():
            output = self._model([frame_chw_float01.to(self._device)])[0]
        h, w = frame_chw_float01.shape[-2], frame_chw_float01.shape[-1]
        results: List[ThermalDetection] = []
        for box, label, score in zip(output["boxes"], output["labels"], output["scores"]):
            score_f = float(score)
            if score_f < MIN_REPORT_SCORE:
                continue
            # label==1 is the (only) foreground class for a single-class
            # (drone) detector built via build_model(num_classes=2); this
            # mapping must be re-verified against whatever label map the
            # real training run actually used.
            if int(label) != 1:
                continue
            results.append(ThermalDetection(
                score=score_f,
                box_xyxy=tuple(float(v) for v in box.tolist()),
                frame_width_px=int(w),
                frame_height_px=int(h),
            ))
        return results


def detection_to_ingest_body(det: ThermalDetection, model_name: str = "thermal-detector (candidate)") -> dict:
    """Build a DetectionIngestBody-compatible dict for one accepted
    ThermalDetection, following CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4 and
    the exact field-naming pattern ml_classify_bridge.py already uses for
    /api/detections/ingest (see that file's `det = {...}` block).

    Does NOT set distance_m/altitude_m/speed_ms to any real value (no
    ranging capability exists for a monocular thermal camera without
    stereo/known-object-size assumptions) -- left at ingest defaults.
    Sets bearing_deg to None (EXPLICITLY unavailable), not a fake 0.0 -- see
    ThermalDetection.bearing_deg's docstring. bearing_available=False mirrors
    the existing distance_estimated honesty-flag pattern so the backend and
    map render "bearing unknown", never "0 deg North".
    """
    return {
        "model": model_name,
        "protocol": "Thermal-IR",
        "threat_level": "MEDIUM",
        "center_freq_ghz": 0.0,  # not applicable to a non-RF sensor; kept
                                 # for backward-compat with DetectionIngestBody's
                                 # required field until/unless that model is
                                 # made source-conditional (out of scope here)
        "bandwidth_mhz": 0.0,
        "rssi_dbm": 0.0,
        "snr_db": 0.0,
        "bearing_deg": det.bearing_deg(),      # None -- honest "unknown"
        "bearing_available": False,
        "distance_m": 0.0,
        "distance_estimated": False,
        "source": SOURCE_THERMAL_CAM,
        "ml_label": DRONE_CLASS_NAME,
        "ml_confidence": round(det.score, 4),
        "ml_gated": True,
        "confidence_type": CONFIDENCE_TYPE_ML_PROBABILITY,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--console-url", required=True)
    ap.add_argument("--checkpoint", required=True,
                     help="Path to a real trained thermal-detector checkpoint. "
                          "None exists yet in this project -- see module docstring.")
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    print("thermal_bridge.py is a SCAFFOLD, not a runnable field bridge yet.",
          file=sys.stderr)
    print("Blocked on: (1) no thermal camera hardware, (2) no trained "
          "checkpoint (no real labeled thermal-drone dataset has been "
          "downloaded/verified/trained against in this project). See "
          "CAMERA_THERMAL_ACOUSTIC_SCOPE.md and this file's docstring.",
          file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
