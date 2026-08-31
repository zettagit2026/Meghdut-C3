#!/usr/bin/env python3
"""Reusable GamutRF-style ResNet18 inference helpers, factored out of the
proven /tmp/gamutrf_sigmf_infer.py one-shot script so ml_classify_bridge.py
can load the checkpoint ONCE and run repeated real inferences against it,
instead of re-loading a ~45MB checkpoint + rebuilding the model every cycle.

Pipeline is byte-for-byte the same as the verified gamutrf_sigmf_infer.py:
mirrors gamutRF/gamutrf_dataset.py (GamutRFDataset.__getitem__, feat='spec')
and gamutRF/gamutrf_model.py (GamutRFModel), with no gamutrf/gamutRF package
dependency.

SAFETY: checkpoints are loaded with torch.load(..., weights_only=True) only.
If that fails, this module raises rather than silently falling back to
unsafe unpickling -- same standing rule as gamutrf_sigmf_infer.py.

KNOWN LIMITATION (documented here and threaded through to callers/consumers):
The pretrained checkpoint this project has (resnet18_leesburg_split_0.02_1_current.pt)
is a CLOSED-WORLD 3-class model: {drone, wifi_2_4, wifi_5}. There is no
idle/noise/background/"none of the above" class. Phase 1 testing (real
noise-floor IQ capture at 3.6GHz, well outside any swept CEMA band) showed
the model confidently (>99% softmax probability) predicts "drone" on pure
noise-floor energy -- it cannot say "I don't know". This is exactly why
ml_classify_bridge.py NEVER calls this module's classify_window() unless the
caller has already confirmed real signal energy above that band's
established noise floor + detection threshold (see hackrf_rx.py's
BAND_NOISE_FLOOR_DBM / DETECT_THRESHOLD_DB) -- the energy gate is the
mitigation, not a change to this model's behavior.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Dict, Tuple

import numpy as np
import torch
from torchvision import models, transforms
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from event_gating import gate_spectrogram_freq_bins

# EVENT/BURST GATING (2026-07-24): see event_gating.py's module docstring for
# full provenance (ported technique from MULT-25-607-ML-for-RF-Sprectrum-
# sensing) and how this is a DIFFERENT, complementary layer from
# ml_calibration.py's post-classification OOD rejection. This gates the
# spectrogram ITSELF (input hygiene) before the ResNet18 forward pass, rather
# than gating the classifier's output. Default ON; settable via env var so a
# Reality Checker A/B pass can compare gated vs. ungated behavior before this
# is signed off for deployment.
EVENT_GATE_ENABLED = os.environ.get("CEMA_ML_EVENT_GATE", "1") not in ("0", "false", "False")


SIGMF_DTYPE_MAP = {
    "ci8": np.dtype([("i", "i1"), ("q", "i1")]),
    "ci16_le": np.dtype([("i", "<i2"), ("q", "<i2")]),
    "ci16": np.dtype([("i", "<i2"), ("q", "<i2")]),
    "cf32_le": np.dtype([("i", "<f4"), ("q", "<f4")]),
    "cf32": np.dtype([("i", "<f4"), ("q", "<f4")]),
}


def load_sigmf(meta_path: str, data_path: str):
    """Load a real SigMF capture (meta + data) into a complex numpy array.
    Returns (samples, sample_rate_hz, frequency_hz, datatype)."""
    with open(meta_path, "r") as f:
        meta = json.load(f)

    global_info = meta["global"]
    capture_info = meta["captures"][0]

    datatype = global_info["core:datatype"]
    sample_rate = float(global_info["core:sample_rate"])
    frequency = float(capture_info.get("core:frequency", 0.0))

    if datatype not in SIGMF_DTYPE_MAP:
        raise ValueError(f"Unsupported SigMF datatype for this module: {datatype}")
    dtype = SIGMF_DTYPE_MAP[datatype]

    raw = np.fromfile(data_path, dtype=dtype)
    samples = raw["i"].astype(np.csingle) + np.csingle(1j) * raw["q"].astype(np.csingle)

    return samples, sample_rate, frequency, datatype


def safe_load_checkpoint(path: str) -> dict:
    """Load a .pt checkpoint using ONLY the safe weights_only=True path
    (widening the allow-list with known-benign numpy globals if needed).
    Never falls back to torch.load(weights_only=False)."""
    try:
        return torch.load(path, map_location="cpu", weights_only=True)
    except Exception as e:
        try:
            candidates = []
            try:
                candidates.append(np.core.multiarray._reconstruct)
            except Exception:
                pass
            try:
                candidates.append(np.ndarray)
            except Exception:
                pass
            try:
                candidates.append(np.dtype)
            except Exception:
                pass
            if candidates and hasattr(torch.serialization, "add_safe_globals"):
                torch.serialization.add_safe_globals(candidates)
                return torch.load(path, map_location="cpu", weights_only=True)
        except Exception as e2:
            raise RuntimeError(
                f"safe_load_checkpoint: weights_only=True load failed even after "
                f"widening allow-list with benign numpy globals: {e2}"
            ) from e2
        raise RuntimeError(
            f"safe_load_checkpoint: weights_only=True load failed and no "
            f"allow-list widening was attempted: {e}"
        ) from e


def build_model(checkpoint: dict, device: "torch.device"):
    model_weights = checkpoint["model_state_dict"]
    n_classes = len(checkpoint["dataset_idx_to_class"])
    model = models.resnet18()
    model.fc = torch.nn.Linear(model.fc.in_features, n_classes)
    model.load_state_dict(model_weights)
    model = model.to(device)
    model.eval()
    return model


def make_spectrogram_image(samples: np.ndarray, sample_rate: float, nfft: int,
                            event_gate: bool = None):
    """Exact reimplementation of GamutRFDataset.__getitem__ feat='spec' path,
    plus an optional event/burst-gating pass (see event_gating.py) applied to
    the dB spectrogram BEFORE normalization/colorization -- i.e. before the
    ResNet18 classifier ever sees it. `event_gate=None` (default) defers to
    the module-level EVENT_GATE_ENABLED toggle (env CEMA_ML_EVENT_GATE)."""
    f, t, S = signal.spectrogram(
        samples, sample_rate,
        window=signal.windows.hann(nfft, sym=False),
        nperseg=nfft,
        detrend="constant",
        return_onesided=False,
    )
    S = np.fft.fftshift(S, axes=0)
    S = 10 * np.log10(S + 1e-20)  # dB scale (epsilon guards log(0) on real low-energy bins)

    if event_gate is None:
        event_gate = EVENT_GATE_ENABLED
    if event_gate:
        # Replace frequency bins that never carry burst/event energy
        # anywhere in this capture window with their own measured noise
        # floor, BEFORE normalization -- see event_gating.py module
        # docstring for the MULT-25-607-ported technique this implements.
        S, _events, _floor = gate_spectrogram_freq_bins(S)

    S_norm = (S - np.min(S)) / (np.max(S) - np.min(S))

    cmap = plt.get_cmap("jet")
    rgba_img = cmap(S_norm)
    rgb_img = np.delete(rgba_img, 3, 2)

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Resize((256, 256)),
    ])
    return transform(np.float32(rgb_img))


def resolve_device() -> "torch.device":
    """Resolve the inference device from env CEMA_ML_DEVICE (auto|cuda|cpu).

    DEVICE-AUTO WITH HARD CPU FALLBACK (2026-08-31): this project's GPU host
    (.186, RTX 3060 / Ampere sm_86) runs a CUDA torch build so the ResNet18
    forward can run on the GPU, but the ML pass is the live CEMA detection
    path and MUST NEVER be broken by a GPU problem. This selector only
    chooses WHERE the tensor math runs; the spectrogram pipeline, the class
    labels, the softmax/confidence and the energy gate are all unchanged.

      "auto" (default): cuda:0 iff torch.cuda.is_available(), else cpu.
      "cuda"/"gpu":     force cuda:0 if available, else warn + fall back to cpu.
      "cpu":            force cpu (used to prove the fallback path / demo-safe).

    A CUDA *runtime* failure at inference time is handled separately, and even
    more defensively, in GamutRFClassifier (see _infer_probs) -- resolving to
    cuda here never commits the process to the GPU; it can still fall back."""
    choice = (os.environ.get("CEMA_ML_DEVICE", "auto") or "auto").strip().lower()
    if choice == "cpu":
        return torch.device("cpu")
    if choice in ("cuda", "gpu"):
        if torch.cuda.is_available():
            return torch.device("cuda:0")
        print("[gamutrf_infer] WARNING: CEMA_ML_DEVICE=cuda requested but "
              "torch.cuda.is_available() is False -- falling back to cpu.",
              file=sys.stderr)
        return torch.device("cpu")
    if choice not in ("auto", ""):
        print(f"[gamutrf_infer] WARNING: unrecognized CEMA_ML_DEVICE={choice!r} "
              "-- treating as 'auto'.", file=sys.stderr)
    return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class GamutRFClassifier:
    """Loads a GamutRF-style ResNet18 checkpoint ONCE and exposes repeated
    classify_window() calls against it -- the whole point of factoring this
    out of the one-shot gamutrf_sigmf_infer.py script, since a long-running
    bridge process must not reload a ~45MB checkpoint + rebuild the model
    every gated-in cycle."""

    def __init__(self, checkpoint_path: str):
        self.device = resolve_device()  # env CEMA_ML_DEVICE (auto|cuda|cpu)
        self.checkpoint = safe_load_checkpoint(checkpoint_path)
        self.sample_secs = self.checkpoint["sample_secs"]
        self.nfft = self.checkpoint["nfft"]
        self.idx_to_class: Dict = self.checkpoint["dataset_idx_to_class"]
        # LOAD-TIME CUDA FALLBACK: if building/moving the model onto the GPU
        # throws (driver/runtime fault, out-of-memory at load), never crash the
        # bridge -- log it and permanently drop this process to cpu so real
        # detection still runs.
        try:
            self.model = build_model(self.checkpoint, self.device)
        except Exception as e:
            if self.device.type == "cuda":
                print(f"[gamutrf_infer] WARNING: failed to load model on "
                      f"{self.device} ({e}) -- falling back to cpu for this "
                      "process; detection continues.", file=sys.stderr)
                self.device = torch.device("cpu")
                self.model = build_model(self.checkpoint, self.device)
            else:
                raise
        self.checkpoint_path = checkpoint_path
        self._log_resolved_device()

    def _log_resolved_device(self) -> None:
        """Log the resolved inference device at startup (task requirement)."""
        if self.device.type == "cuda":
            try:
                name = torch.cuda.get_device_name(self.device.index or 0)
            except Exception:
                name = "CUDA device"
            print(f"[gamutrf_infer] ML inference device = {self.device} ({name})")
        else:
            print("[gamutrf_infer] ML inference device = cpu (fallback)"
                  if os.environ.get("CEMA_ML_DEVICE", "auto").strip().lower()
                  not in ("cpu",)
                  else "[gamutrf_infer] ML inference device = cpu")

    def min_window_samples(self, sample_rate_hz: float) -> int:
        return int(sample_rate_hz * self.sample_secs)

    def classify_window(self, samples: np.ndarray, sample_rate_hz: float,
                         event_gate: bool = None
                         ) -> Tuple[str, float, Dict[str, float]]:
        """Run real inference on a window of REAL IQ samples.

        `event_gate` (default None -> module-level EVENT_GATE_ENABLED /
        CEMA_ML_EVENT_GATE env toggle): whether to apply the burst/event
        gating pass (event_gating.py) to the spectrogram before it is fed to
        the model. This is upstream INPUT hygiene, separate from and
        complementary to ml_calibration.py's post-classification OOD
        rejection -- see event_gating.py's module docstring.

        Returns (predicted_label, confidence, all_class_probs). Caller is
        responsible for having already energy-gated this call (see module
        docstring) -- this function will happily produce a confident label
        on pure noise, since the underlying model has no "none of the
        above" class."""
        window_n = self.min_window_samples(sample_rate_hz)
        if len(samples) < window_n:
            raise ValueError(
                f"classify_window: capture has {len(samples)} samples, need at "
                f"least {window_n} ({self.sample_secs}s @ {sample_rate_hz} Hz)"
            )
        window = samples[:window_n]
        data = make_spectrogram_image(window, sample_rate_hz, self.nfft, event_gate=event_gate)
        probs = self._infer_probs(data)
        pred_idx = int(np.argmax(probs))

        all_probs = {}
        for i, p in enumerate(probs):
            cls = self.idx_to_class.get(i, self.idx_to_class.get(str(i), f"class_{i}"))
            all_probs[cls] = float(p)

        pred_cls = self.idx_to_class.get(pred_idx, self.idx_to_class.get(str(pred_idx), f"class_{pred_idx}"))
        return pred_cls, float(probs[pred_idx]), all_probs

    def _forward_softmax(self, data: "torch.Tensor",
                         device: "torch.device") -> np.ndarray:
        """Run the ResNet18 forward + softmax on `device` and return the class
        probability vector as a host (cpu) numpy array. The math here is
        byte-for-byte the original CPU path -- only the tensor device changed:
        the input is moved to `device`, the softmax output is moved back to cpu
        for the existing numpy argmax/labeling path."""
        x = data.unsqueeze(0).to(device)
        with torch.no_grad():
            out = self.model(x)
            return torch.softmax(out, dim=1).cpu().numpy()[0]

    def _infer_probs(self, data: "torch.Tensor") -> np.ndarray:
        """Device-aware inference with a HARD CUDA->CPU runtime fallback.

        CRITICAL SAFETY PROPERTY (the whole point of the 2026-08-31 GPU change):
        if self.device is CUDA and ANY part of the GPU forward raises -- OOM,
        driver/runtime fault, device lost, a bad-cast edge case -- this logs a
        warning, PERMANENTLY drops this process to cpu (moves the model to cpu
        so every subsequent classify_window() also runs on cpu), and re-runs
        the SAME inference on cpu. The classification result, class labels,
        softmax and confidence are identical either way (modulo GPU/CPU
        float-rounding well under the calibration thresholds); only WHERE the
        tensors live changes. A GPU problem can therefore degrade performance
        but can NEVER break detection -- the live demo path stays up."""
        try:
            return self._forward_softmax(data, self.device)
        except Exception as e:  # noqa: BLE001 -- deliberate: never let GPU break detection
            if self.device.type == "cuda":
                print(f"[gamutrf_infer] WARNING: CUDA inference failed on "
                      f"{self.device} ({e}) -- permanently falling back to cpu "
                      "for the rest of this process; detection continues.",
                      file=sys.stderr)
                self.device = torch.device("cpu")
                try:
                    self.model = self.model.to(self.device)
                except Exception as move_err:  # noqa: BLE001
                    print(f"[gamutrf_infer] WARNING: could not move model to cpu "
                          f"after CUDA failure ({move_err}); retrying on cpu "
                          "anyway.", file=sys.stderr)
                return self._forward_softmax(data, self.device)
            raise
