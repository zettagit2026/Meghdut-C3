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
from typing import Dict, Tuple

import numpy as np
import torch
from torchvision import models, transforms
from scipy import signal
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


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


def make_spectrogram_image(samples: np.ndarray, sample_rate: float, nfft: int):
    """Exact reimplementation of GamutRFDataset.__getitem__ feat='spec' path."""
    f, t, S = signal.spectrogram(
        samples, sample_rate,
        window=signal.windows.hann(nfft, sym=False),
        nperseg=nfft,
        detrend="constant",
        return_onesided=False,
    )
    S = np.fft.fftshift(S, axes=0)
    S = 10 * np.log10(S + 1e-20)  # dB scale (epsilon guards log(0) on real low-energy bins)

    S_norm = (S - np.min(S)) / (np.max(S) - np.min(S))

    cmap = plt.get_cmap("jet")
    rgba_img = cmap(S_norm)
    rgb_img = np.delete(rgba_img, 3, 2)

    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Resize((256, 256)),
    ])
    return transform(np.float32(rgb_img))


class GamutRFClassifier:
    """Loads a GamutRF-style ResNet18 checkpoint ONCE and exposes repeated
    classify_window() calls against it -- the whole point of factoring this
    out of the one-shot gamutrf_sigmf_infer.py script, since a long-running
    bridge process must not reload a ~45MB checkpoint + rebuild the model
    every gated-in cycle."""

    def __init__(self, checkpoint_path: str):
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        self.checkpoint = safe_load_checkpoint(checkpoint_path)
        self.sample_secs = self.checkpoint["sample_secs"]
        self.nfft = self.checkpoint["nfft"]
        self.idx_to_class: Dict = self.checkpoint["dataset_idx_to_class"]
        self.model = build_model(self.checkpoint, self.device)
        self.checkpoint_path = checkpoint_path

    def min_window_samples(self, sample_rate_hz: float) -> int:
        return int(sample_rate_hz * self.sample_secs)

    def classify_window(self, samples: np.ndarray, sample_rate_hz: float
                         ) -> Tuple[str, float, Dict[str, float]]:
        """Run real inference on a window of REAL IQ samples.

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
        data = make_spectrogram_image(window, sample_rate_hz, self.nfft)
        data = data.unsqueeze(0).to(self.device)

        with torch.no_grad():
            out = self.model(data)
            probs = torch.softmax(out, dim=1).cpu().numpy()[0]
        pred_idx = int(np.argmax(probs))

        all_probs = {}
        for i, p in enumerate(probs):
            cls = self.idx_to_class.get(i, self.idx_to_class.get(str(i), f"class_{i}"))
            all_probs[cls] = float(p)

        pred_cls = self.idx_to_class.get(pred_idx, self.idx_to_class.get(str(pred_idx), f"class_{pred_idx}"))
        return pred_cls, float(probs[pred_idx]), all_probs
