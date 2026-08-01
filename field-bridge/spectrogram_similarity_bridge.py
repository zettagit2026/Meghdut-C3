#!/usr/bin/env python3
"""Spectrogram-similarity "second opinion" classifier INFRASTRUCTURE
(backlog C13), modeled on Classidyne (~/Desktop/Zettawise/PMO Suraj/tool/
Classidyne, CC-BY-NC-SA, catalogued for internal defense use per this
project's licensing override) -- alongside gamutrf_infer.py's ResNet18
softmax classifier.

STATUS AS OF 2026-07-23: INFRASTRUCTURE ONLY. NOT WIRED IN. NO REFERENCE
LIBRARY. NO SIMILARITY SCORE HAS EVER BEEN COMPUTED BY THIS MODULE.
=============================================================================
Same discipline as `rf_features.py` (backlog C13's other "second opinion"
staging pass) and `drone_rf_kb/README.md` (RFUAV staging): a real,
working code path exists, but there is no real reference data to run it
against yet, so it is NOT called from `ml_classify_bridge.py` or
`hackrf_rx.py`. Do not wire this in until a real, multi-class reference
library exists (see "Why this is not wired in yet" below) and has been
validated on held-out real captures.

What Classidyne actually does (confirmed by reading its source, not
assumed from the name)
----------------------------------------------------------------------
Read `Classidyne/README.md` and `Classidyne/app.py` directly. Confirmed:

1. It is genuinely a spectrogram-embedding + k-NN vector-similarity
   classifier, NOT a pixel-diff or template-match tool. Pipeline
   (`app.py` `RadioNetExtractor` + `classify()`):
     - A ResNet34 backbone (`timm.create_model("resnet34", num_classes=0,
       global_pool="avg")`) with a real, git-LFS-tracked checkpoint
       (`RadioNet/RadioNet.pth`, confirmed present locally, 255,806,354
       bytes, a real zip-format torch checkpoint -- not a placeholder)
       strips the classification head and is used purely as a 512-dim
       feature extractor (L2-normalized).
     - Reference images (from a user-populated `datasets/<waterfall|fft>/
       <signal_class>/*.png` tree) are embedded once and stored in a
       Chroma vector DB (`classidyne_db/`, persistent, one collection per
       image type: "waterfall", "fft").
     - At classify time, the query image is embedded the same way, and
       Chroma does a k-NN query (`n_results=20`) returning cosine
       distances to the 20 nearest reference embeddings.
     - Distances are converted to similarity (`1 - distance`), filtered
       against a caller-supplied `similarity_threshold` (default 0.5),
       and the surviving neighbors' class labels are majority-voted:
       `confidence = count_of_class / total_matches_above_threshold * 100`.
   So "confidence" here is a vote share among real nearest neighbors
   above a threshold, not a calibrated probability -- an important
   distinction to carry into any `confidence_type` this eventually feeds.

2. Reference-library requirement, confirmed from `app.py` and
   `README.md`: Classidyne ships NO reference data of its own.
   `Classidyne/classidyne_db/` does not exist locally (unembedded), and
   `Classidyne/test_images/` contains exactly 2 files (`test1.png`,
   `test2.png` -- example query images, not a labeled reference set).
   The README explicitly tells the operator to download the
   ~halcy0nic Kaggle "RF Signal Image Classification Dataset" (waterfall/
   FFT PNGs organized by signal class) and/or capture their own labeled
   images before the tool is usable. There is no bundled reference
   library at all -- Classidyne is reference-library infrastructure
   itself, exactly like GamutRF's classifier needs a trained checkpoint.

Input-format compatibility with gamutrf_infer.py
----------------------------------------------------------------------
NOT the same format out of the box -- needs one real, cheap conversion
step, which this module provides (`iq_window_to_classidyne_png()`):

  - `gamutrf_infer.py`'s `make_spectrogram_image()` returns a 256x256,
    3-channel, jet-colormap **torch.Tensor in [0,1] float**, built for
    direct feeding into a ResNet forward pass (`ToTensor()` + `Resize()`
    composed transform, no file ever touches disk).
  - Classidyne's `RadioNetExtractor.__call__` takes a **PIL Image or a
    filepath string**, converts to grayscale then back to RGB
    (`.convert("L").convert("RGB")`) -- i.e. it deliberately DISCARDS the
    jet colormap and works on luma only, then applies its own
    `timm`-resolved resnet34 preprocessing (ImageNet mean/std normalize,
    likely different input resolution than 256x256 -- resolved
    dynamically via `resolve_data_config({}, model="resnet34")`).

  So the SAME captured spectrogram can feed both classifiers, but not via
  the same in-memory tensor: gamutrf_infer.py's jet-colormap RGB tensor
  must be converted back to a PIL Image and handed to Classidyne's
  extractor as a PIL Image (or saved to PNG and passed as a path) --
  Classidyne's own grayscale conversion then makes the jet colormap
  moot for its internal features, which is fine since Classidyne was
  designed to accept colored waterfall/FFT screenshots of many kinds,
  not exclusively jet-colormap ones. `iq_window_to_classidyne_png()`
  below does exactly this: reuses `gamutrf_infer.make_spectrogram_image()`
  as the single source of truth for the spectrogram render (no
  reimplementation, no drift risk between the two classifiers'
  spectrograms), then converts the tensor to a PIL Image the way
  Classidyne's own `classify()` endpoint receives an uploaded file.

Reference/comparison data assessment (the feasibility-blocking part)
----------------------------------------------------------------------
Checked `field-bridge/drone_rf_kb/` (this project's only staged real RF
sample set) against what a genuine k-NN similarity classifier needs:

  - Real data present: exactly 2 real IQ captures, `mavic_air_2` and
    `mini2_sm` (DJI OcuSync 2.0 DroneID beacons, both from
    `DroneSecurity/samples/`, both the SAME modulation family/signal
    class). No WiFi, no Bluetooth, no other drone type, no
    background/noise-floor captures. No spectrogram PNGs have ever been
    rendered from them (`drone_rf_kb/` has a conversion SCRIPT,
    `convert_iq_to_spectrogram.py`, but it has not been run to produce
    any image files -- confirmed: no `.png`/`.sigmf*` files exist
    anywhere under this repo).
  - A k-NN similarity vote is only meaningful when it can discriminate
    BETWEEN classes. With only one real signal class and 2 source
    captures (which, being near-duplicate DroneID beacons from the same
    hardware family, would almost certainly embed close together), a
    "reference library" built from what's on hand today would ALWAYS
    vote 100% for that one class regardless of query image -- every
    unknown signal would come back "drone" at 100% "confidence" simply
    because there is nothing else in the database to compete with. That
    would be a fabricated-looking result wearing a real pipeline's
    clothes: the code is real, but the signal would be meaningless, and
    this project's standing rule is no fabricated reference libraries or
    fake similarity scores.
  - This is the same shape of blocker as the RFUAV staging situation
    documented in `drone_rf_kb/README.md`: a real, working algorithm,
    genuinely buildable, but blocked on real, multi-class reference data
    we have not pulled/captured yet (e.g. real WiFi 2.4/5GHz waterfall
    captures, real Bluetooth captures, and additional real drone-signal
    families beyond DJI OcuSync -- ideally the same Kaggle dataset
    Classidyne's own README points to, or our own multi-band HackRF
    captures across enough sessions to get several real samples per
    class).

VERDICT: NOT genuinely feasible to wire in today. Feasible in the future
once a real, multi-class, multi-sample-per-class reference library
exists. Building a working `confidence_type="spectrogram_similarity"`
signal against today's 1-class, 2-sample reference set would produce
technically-real-code but practically-fake output (always-100%-drone),
which is exactly what the standing rule prohibits. So: infrastructure
staged, nothing wired, no reference library fabricated, no similarity
score ever emitted below.

Future wiring (not done yet)
----------------------------------------------------------------------
Once a real, multi-class reference library exists and
`build_reference_library()` below has actually been run against it (and
ideally spot-checked the way `drone_rf_kb/`'s conversion script was
tested against real local data), `classify_window()` below is ready to
be called from `ml_classify_bridge.py` as a second signal alongside the
ResNet18 softmax, tagged with a NEW `confidence_type` enum value,
`"spectrogram_similarity"`, following the same closed-enum convention as
`heuristic_binary` / `ml_probability` / `protocol_verified` /
`advisory_only` / (staged-future) `spectral_features_ml` documented in
`backend/CONFIDENCE_MODEL.md`. That wiring step is explicitly OUT OF
SCOPE for this pass -- there is no reference library to wire.
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Tuple

import numpy as np

# Reuse gamutrf_infer.py's spectrogram renderer as the single source of
# truth -- no reimplementation, no drift between the two classifiers'
# view of "what does this capture look like".
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gamutrf_infer import make_spectrogram_image  # noqa: E402

try:
    import torch
    from torchvision import transforms as tv_transforms
    from PIL import Image
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False

try:
    import timm
    import chromadb
    from sklearn.preprocessing import normalize
    _CLASSIDYNE_DEPS_AVAILABLE = True
except ImportError:
    _CLASSIDYNE_DEPS_AVAILABLE = False


CONFIDENCE_TYPE = "spectrogram_similarity"  # reserved enum value; NOT yet
                                             # added to backend/CONFIDENCE_MODEL.md
                                             # or emitted anywhere, pending a
                                             # real reference library (see
                                             # module docstring).

MIN_CLASSES_REQUIRED = 2       # a k-NN vote needs >=2 classes to mean anything
MIN_SAMPLES_PER_CLASS = 5      # a single-digit reference set per class is not
                                # a serious basis for a similarity vote either;
                                # this is a floor, not a claim of sufficiency


def iq_window_to_classidyne_png(samples: np.ndarray, sample_rate_hz: float,
                                 nfft: int, out_path: str) -> str:
    """Render a real IQ window into a PNG file, via the SAME spectrogram
    code gamutrf_infer.py's ResNet18 classifier uses, so both classifiers
    see the same capture rendered the same way. Requires torch/torchvision/
    Pillow (already project dependencies for gamutrf_infer.py).

    Returns out_path on success. Raises if torch/PIL are unavailable --
    this never silently produces a fabricated image."""
    if not _TORCH_AVAILABLE:
        raise RuntimeError(
            "iq_window_to_classidyne_png: torch/torchvision/Pillow not "
            "installed in this environment -- see field-bridge/requirements.txt"
        )
    tensor = make_spectrogram_image(samples, sample_rate_hz, nfft)  # CHW, [0,1]
    img = tv_transforms.ToPILImage()(tensor)
    img.save(out_path)
    return out_path


class ReferenceLibraryNotReadyError(RuntimeError):
    """Raised whenever code below would otherwise have to fabricate a
    similarity score from an absent/too-small/single-class reference
    library. Never caught-and-guessed -- callers must fix the underlying
    data gap, not paper over it."""


def _check_reference_library(chroma_path: str, collection_name: str) -> None:
    """Real check against a real Chroma collection (if one exists at
    chroma_path) -- refuses to proceed unless there are genuinely enough
    classes and samples-per-class to make a k-NN vote meaningful. This is
    the concrete enforcement of MIN_CLASSES_REQUIRED / MIN_SAMPLES_PER_CLASS
    described in the module docstring; it does not know in advance whether
    the library is ready -- it inspects whatever real Chroma DB is on disk."""
    if not _CLASSIDYNE_DEPS_AVAILABLE:
        raise RuntimeError(
            "_check_reference_library: timm/chromadb/scikit-learn not "
            "installed in this environment. This module has never been run "
            "end-to-end because no real reference library exists yet to "
            "run it against (see module docstring) -- installing these "
            "without real reference data would not make this feature usable."
        )
    if not os.path.isdir(chroma_path):
        raise ReferenceLibraryNotReadyError(
            f"No Chroma reference database found at '{chroma_path}'. As of "
            f"2026-07-23 this project has NOT built one -- field-bridge/"
            f"drone_rf_kb/ contains exactly 2 real IQ captures, both the "
            f"same DJI OcuSync signal class, and zero rendered spectrogram "
            f"images. That is not a usable reference library (see module "
            f"docstring 'Reference/comparison data assessment'). Build a "
            f"real, multi-class, multi-sample-per-class library first."
        )
    client = chromadb.PersistentClient(path=chroma_path)
    try:
        collection = client.get_collection(collection_name)
    except Exception as e:
        raise ReferenceLibraryNotReadyError(
            f"Chroma collection '{collection_name}' not found/readable at "
            f"'{chroma_path}': {e}"
        ) from e
    all_meta = collection.get(include=["metadatas"])["metadatas"]
    classes: Dict[str, int] = {}
    for m in all_meta:
        cls = m.get("class", "unknown")
        classes[cls] = classes.get(cls, 0) + 1
    if len(classes) < MIN_CLASSES_REQUIRED:
        raise ReferenceLibraryNotReadyError(
            f"Reference library at '{chroma_path}' has only {len(classes)} "
            f"class(es) ({list(classes.keys())}) -- need >= "
            f"{MIN_CLASSES_REQUIRED} real, distinct signal classes for a "
            f"k-NN vote to mean anything (otherwise every query trivially "
            f"'wins' the only class present)."
        )
    thin = {c: n for c, n in classes.items() if n < MIN_SAMPLES_PER_CLASS}
    if thin:
        raise ReferenceLibraryNotReadyError(
            f"Reference library at '{chroma_path}' has classes with fewer "
            f"than {MIN_SAMPLES_PER_CLASS} real reference samples: {thin}. "
            f"Add more real, labeled captures for these classes before "
            f"trusting a similarity vote against them."
        )


def classify_window_via_classidyne(
    samples: np.ndarray, sample_rate_hz: float, nfft: int,
    chroma_path: str, collection_name: str,
    radionet_checkpoint_path: str,
    similarity_threshold: float = 0.5,
    tmp_png_path: str = os.environ.get("CEMA_CLASSIDYNE_QUERY_PNG",
                                        "/tmp/classidyne_query.png"),
) -> Tuple[str, float, Dict[str, float]]:
    """Real, end-to-end spectrogram-similarity second opinion, reimplementing
    Classidyne's RadioNetExtractor + Chroma k-NN vote (see module docstring)
    against a REAL capture. Returns (top_label, confidence_0_to_100,
    all_class_vote_shares).

    Deliberately raises ReferenceLibraryNotReadyError rather than returning
    any score if there is no real, adequately-populated reference library at
    chroma_path -- this function has never produced output in this project
    because that library does not exist yet (see module docstring)."""
    _check_reference_library(chroma_path, collection_name)  # raises if not ready

    if not _TORCH_AVAILABLE:
        raise RuntimeError("torch/torchvision/Pillow not installed")

    png_path = iq_window_to_classidyne_png(samples, sample_rate_hz, nfft, tmp_png_path)

    device = torch.device(
        "cuda" if torch.cuda.is_available()
        else "mps" if torch.backends.mps.is_available()
        else "cpu"
    )
    model = timm.create_model("resnet34", pretrained=False, num_classes=0, global_pool="avg")
    checkpoint = torch.load(radionet_checkpoint_path, map_location=device)
    state_dict = {k: v for k, v in checkpoint["model_state_dict"].items() if "fc" not in k}
    model.load_state_dict(state_dict, strict=False)
    model.eval().to(device)

    from timm.data.config import resolve_data_config
    from timm.data.transforms_factory import create_transform
    config = resolve_data_config({}, model="resnet34")
    preprocess = create_transform(**config)

    query_img = Image.open(png_path).convert("L").convert("RGB")
    input_tensor = preprocess(query_img)
    if not isinstance(input_tensor, torch.Tensor):
        input_tensor = tv_transforms.ToTensor()(input_tensor)
    input_tensor = input_tensor.unsqueeze(0).to(device)

    with torch.no_grad():
        feature_vector = model(input_tensor).squeeze().cpu().numpy()
    query_embedding = normalize(feature_vector.reshape(1, -1), norm="l2").flatten().tolist()

    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_collection(collection_name)
    results = collection.query(
        query_embeddings=[query_embedding], n_results=20,
        include=["metadatas", "distances"],
    )
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]

    votes: Dict[str, int] = {}
    for meta, distance in zip(metadatas, distances):
        similarity = 1.0 - distance
        if similarity >= similarity_threshold:
            cls = meta["class"]
            votes[cls] = votes.get(cls, 0) + 1

    total = sum(votes.values())
    if total == 0:
        return "no_match_above_threshold", 0.0, {}

    shares = {cls: (count / total) * 100.0 for cls, count in votes.items()}
    top_label = max(shares, key=shares.get)
    return top_label, shares[top_label], shares


def build_reference_library(*args, **kwargs):
    raise NotImplementedError(
        "build_reference_library() is intentionally not implemented in this "
        "pass. Populating a Chroma reference DB with only field-bridge/"
        "drone_rf_kb/'s 2 real single-class captures would produce a "
        "library that always votes 100% for that one class -- a technically "
        "real pipeline producing a practically fabricated-looking result. "
        "Assemble a real, multi-class, multi-sample-per-class labeled "
        "spectrogram set first (e.g. the Kaggle RF Signal Image "
        "Classification dataset Classidyne's own README recommends, or "
        "additional real multi-band HackRF captures), then implement this "
        "as a straightforward loop over "
        "Classidyne/app.py's embed_dataset()-equivalent logic."
    )


if __name__ == "__main__":
    print(__doc__)
    print(f"\ntorch/torchvision/Pillow available: {_TORCH_AVAILABLE}")
    print(f"timm/chromadb/scikit-learn available: {_CLASSIDYNE_DEPS_AVAILABLE}")
    print(
        "\nThis module is infrastructure-only as of 2026-07-23. Run "
        "classify_window_via_classidyne() against a real Chroma reference "
        "DB once one exists; it will raise ReferenceLibraryNotReadyError "
        "otherwise, by design."
    )
