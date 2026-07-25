#!/usr/bin/env python3
"""Training pipeline SCAFFOLD for a permissively-licensed thermal-drone
detector (task #83 AI Engineer follow-up, 2026-07-25). See
thermal_bridge.py's module docstring first -- this file is the training-time
counterpart to that inference-time scaffold.

=============================================================================
THIS FILE HAS NEVER BEEN RUN. NO MODEL HAS BEEN TRAINED. NO DATASET HAS BEEN
DOWNLOADED. Nothing in this file should be read as a claim that training has
happened or that any checkpoint/metric exists.
=============================================================================

Why this exists anyway: per this project's "software scaffolding now,
hardware/data-dependent parts later" pattern, the training LOOP shape
(dataset contract, model construction, optimizer/schedule, checkpointing)
is not blocked on hardware or a specific dataset -- only on ACTUALLY
RUNNING it is. Writing the loop now means that once a real dataset is
acquired and license-cleared (see thermal_bridge.py's BLOCKED section on
Anti-UAV), a training run is "plug in the real DataLoader and go," not a
from-scratch design exercise.

=============================================================================
DATASET CONTRACT (what train_one_epoch/ThermalDroneDataset expect)
=============================================================================
A real dataset plugged in here must supply, per sample:
  - a thermal image tensor (H, W) or (3, H, W) -- see note in
    thermal_bridge.py about single-channel-to-3-channel handling
  - a target dict: {"boxes": FloatTensor[N, 4] in (x1,y1,x2,y2) pixel
    coords, "labels": Int64Tensor[N] (1 == drone, since num_classes=2 is a
    single foreground class + background)}
This is torchvision's standard detection-model target contract (same shape
torchvision.datasets.CocoDetection-style wrappers already use) -- chosen
deliberately so a COCO-format-exported Anti-UAV (or any other) annotation
set needs only a format-converting Dataset subclass, not a redesigned
training loop.

CANDIDATE REAL DATASET (see thermal_bridge.py for full citation/caveats):
Anti-UAV (Jiang et al.; https://github.com/ZhaoJ9014/Anti-UAV). Right
modality (ground/aerial sensor -> UAV target, RGB+thermal-IR video with
per-frame bounding boxes). Repo CODE is MIT; the DATASET's own
redistribution/use terms were NOT verified in this session (no download was
attempted) -- confirm the actual data license/EULA satisfies this project's
OSI-permissive-only policy, or invoke the project's already-approved
non-commercial exception if it does not, BEFORE downloading or training
against it.

If/when Anti-UAV (or another verified real dataset) is available locally,
implement ThermalDroneDataset.__getitem__ to read its per-frame IR images +
bounding-box ground truth and yield samples per the contract above. Do
NOT implement it against synthetic/randomly-generated boxes -- that would
produce a model "trained" on fabricated data, exactly what this project's
standing rule prohibits.
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from typing import Optional


def _require_torch():
    try:
        import torch  # noqa: F401
        import torchvision  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "torch/torchvision not installed -- run this on the deploy VM, "
            "not the Mac dev copy (see thermal_bridge.py's NO_TORCH_MSG)."
        ) from e
    return torch, torchvision


@dataclass
class TrainConfig:
    dataset_root: str
    epochs: int = 10
    batch_size: int = 4
    learning_rate: float = 0.005
    momentum: float = 0.9
    weight_decay: float = 0.0005
    num_classes: int = 2  # background + drone
    checkpoint_out: str = "thermal_detector_checkpoint.pt"
    device: Optional[str] = None


class ThermalDroneDataset:
    """torch.utils.data.Dataset-shaped stub. NOT IMPLEMENTED: reading real
    thermal-drone imagery + bounding-box annotations requires a real,
    license-cleared dataset on disk, which does not exist in this project.

    Deliberately raises rather than returning placeholder/synthetic
    samples -- see this module's docstring for why silently substituting
    fake data would violate this project's data-honesty rule.
    """

    def __init__(self, root: str, split: str = "train"):
        self.root = root
        self.split = split
        if not os.path.isdir(root):
            raise FileNotFoundError(
                f"No dataset at {root}. No real, license-cleared "
                "thermal-drone dataset has been acquired for this project "
                "yet -- see this module's docstring and "
                "CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.5 (\"Labeled training "
                "data\"). Do not point this at synthetic/fabricated data.")
        # A real implementation would index the dataset's real
        # image/annotation files here (e.g. Anti-UAV's per-sequence frame
        # + JSON bounding-box files, once license-cleared and downloaded).
        raise NotImplementedError(
            "ThermalDroneDataset reading logic is not implemented -- no "
            "real dataset format has been chosen/downloaded yet. This "
            "class only defines the directory-existence guard above; fill "
            "in real annotation parsing once a real, verified dataset is "
            "available on disk.")

    def __len__(self):  # pragma: no cover - unreachable until implemented
        raise NotImplementedError

    def __getitem__(self, idx):  # pragma: no cover - unreachable until implemented
        raise NotImplementedError


def build_optimizer(model, cfg: TrainConfig):
    torch, _ = _require_torch()
    params = [p for p in model.parameters() if p.requires_grad]
    return torch.optim.SGD(params, lr=cfg.learning_rate,
                            momentum=cfg.momentum, weight_decay=cfg.weight_decay)


def train_one_epoch(model, optimizer, data_loader, device) -> float:
    """Standard torchvision detection training step (sum of the model's
    built-in loss dict). Returns mean loss for the epoch. Not runnable
    until ThermalDroneDataset is implemented against a real dataset."""
    torch, _ = _require_torch()
    model.train()
    total_loss = 0.0
    n = 0
    for images, targets in data_loader:
        images = [img.to(device) for img in images]
        targets = [{k: v.to(device) for k, v in t.items()} for t in targets]
        loss_dict = model(images, targets)
        loss = sum(loss_dict.values())
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        total_loss += float(loss)
        n += 1
    return total_loss / max(n, 1)


def main() -> None:
    from thermal_bridge import build_model  # local import: keeps this file
                                             # importable without torch too

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset-root", required=True)
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--checkpoint-out", default="thermal_detector_checkpoint.pt")
    args = ap.parse_args()

    cfg = TrainConfig(dataset_root=args.dataset_root, epochs=args.epochs,
                       checkpoint_out=args.checkpoint_out)

    # This will raise FileNotFoundError/NotImplementedError right now --
    # intentionally. See ThermalDroneDataset's docstring.
    dataset = ThermalDroneDataset(cfg.dataset_root, split="train")
    torch, _ = _require_torch()
    from torch.utils.data import DataLoader
    data_loader = DataLoader(dataset, batch_size=cfg.batch_size, shuffle=True,
                              collate_fn=lambda batch: tuple(zip(*batch)))

    device = torch.device(cfg.device or ("cuda:0" if torch.cuda.is_available() else "cpu"))
    model = build_model(num_classes=cfg.num_classes, pretrained_backbone=True)
    model.to(device)
    optimizer = build_optimizer(model, cfg)

    for epoch in range(cfg.epochs):
        mean_loss = train_one_epoch(model, optimizer, data_loader, device)
        print(f"epoch {epoch}: mean_loss={mean_loss:.4f}")

    torch.save(model.state_dict(), cfg.checkpoint_out)
    print(f"saved {cfg.checkpoint_out}")


if __name__ == "__main__":
    main()
