# Drone RF Knowledge Base (staging)

Status as of 2026-07-23: **dataset provenance documented, one real conversion
tool built and tested against real local data, no fine-tuning run yet.**
This is a staging pass (backlog B2), not a model-training pass.

## What this is

A structured home for real, licensed drone-RF datasets that could feed a
future fine-tuning pass of the ResNet18 classifier already wired into
`field-bridge/gamutrf_infer.py` (`resnet18_leesburg_split_0.02_1_current.pt`,
3-class closed-world: `drone` / `wifi_2_4` / `wifi_5`). Nothing here changes
that checkpoint or its behavior — this is dataset/tooling staging only.

## Sources evaluated

### 1. RFUAV (`~/Desktop/Zettawise/PMO Suraj/tool/RFUAV`, Apache 2.0)

- **What's actually in the local checkout**: code only — `train.py`,
  `inference.py`, `graphic/RawDataProcessor.py` (raw-IQ → spectrogram
  conversion pipeline), 34 experiment configs under `configs/`, a few
  illustrative PNGs under `abstract/` (paper figures, not dataset samples),
  and `example/classify/*.yaml` sample configs.
- **Actual drone recordings present locally: 0 of 35.** The README (section
  4, "Dataset Download") states the raw IQ, spectrograms, and model weights
  are hosted on Hugging Face (`kitofrank/RFUAV`) and a detection subset on
  Roboflow — not shipped in the git checkout, and not downloaded as part of
  this pass (multi-GB corpus; out of scope for a staging-only step without
  an explicit go-ahead to pull it down).
- **Capture hardware / sample rate**: confirmed from README + code —
  collected with **USRP**, and `RawDataProcessor.ShowSpectrogram` /
  `TransRawDataintoSpectrogram` examples pass `sample_rate=100e6` explicitly.
  So ~100 Msps captures, consistent with the ~100MHz recalled from memory —
  this is now source-confirmed, not just recalled.
- **Labels/taxonomy**: `configs/sample.yaml` (a real 23-class subset actually
  used in one of their experiment configs; the full set is 35 per the
  README) lists drone/controller model names as class labels, e.g.
  `Phantom4Pro`, `Mini2`, `Mavic3`, `Matrice300`, `Inspire2`, `AVATA`,
  `FutabaT61Z`, etc. — i.e. specific commercial airframe/controller models,
  not modulation-family labels.
- **Format available**: both raw IQ (binary, USRP-captured) *and*
  pre-rendered spectrogram PNGs, per README section 2.1 — but again, only
  as hosted downloads, not present in this checkout.

### 2. DroneSecurity samples (`~/Desktop/Zettawise/PMO Suraj/tool/DroneSecurity/samples/`, AGPLv3)

- **Actually present locally and real**: `mavic_air_2` (1,802,240 bytes) and
  `mini2_sm` (5,820,000 bytes) — real DJI Mini 2 / Mavic Air 2 OcuSync 2.0
  DroneID captures, already used this session for the DroneID decode
  bridge (`field-bridge/droneid_decode_bridge.py`).
- **Format** (confirmed from `src/droneid_receiver_offline.py` line 14):
  raw interleaved `float32` I/Q (`np.memmap(..., dtype="<f").view(np.complex64)`)
  — i.e. **raw complex64 IQ**, no container/header, no SigMF sidecar.
  Captured at **50 Msps** (default `--sample-rate 50e6`, confirmed via
  `inspectrum -r 50e6` usage in the repo's own README and the argparse
  default in `droneid_receiver_offline.py`).
- **Modulation family**: DJI OcuSync 2.0 (DroneID beacon frames, QPSK/ZC
  sequences) — genuinely different from RFUAV's presumed WiFi-band FHSS/2.4-
  5.8GHz analog video link signatures, so this **broadens** rather than
  duplicates RFUAV's coverage, as expected.

## HackRF compatibility reality (~20MHz / ~20Msps cap)

Neither source's *raw IQ* was captured at a rate our HackRF can replay
1:1 — RFUAV at 100Msps, DroneSecurity samples at 50Msps, both well above
HackRF's ~20Msps instantaneous capture/replay ceiling. Concretely:

- **RFUAV raw IQ**: not locally present anyway (hosted on HF), so moot for
  this pass. If pulled down later, it would need resampling (100→≤20Msps)
  before any raw-IQ-domain use against our HackRF captures, and that
  resampling would discard real bandwidth/frequency-hopping detail RFUAV's
  own analysis (FHSBW/FHSDT/etc., see README) depends on — a lossy,
  disclosed transformation, not an equivalence. Flagging this rather than
  claiming it's "fine."
- **RFUAV pre-rendered spectrogram PNGs** (if pulled from HF): these ARE
  potentially usable directly for spectrogram-domain fine-tuning, since a
  spectrogram is a fixed-resolution image already abstracted from the raw
  signal, independent of the original capture rate — the same principle
  `gamutrf_infer.py`'s classifier already exploits (it was trained on
  GamutRF-domain spectrograms, not on HackRF-only raw IQ). BUT — important
  caveat: `gamutrf_infer.py`'s `make_spectrogram_image()` builds its own
  spectrogram from raw IQ using a specific scipy pipeline (Hann window,
  `nfft`-sized STFT, two-sided FFT-shifted, dB-scaled, min-max normalized,
  `jet` colormap, resized to 256×256 — see below). RFUAV's own
  `RawDataProcessor` almost certainly uses different STFT parameters
  (window, nperseg, normalization, colormap defaults unconfirmed without
  reading their generation code in depth). Pre-rendered RFUAV PNGs are
  **not guaranteed pixel/statistic-equivalent** to what our classifier
  expects unless regenerated from RFUAV's raw IQ through OUR pipeline. So
  the genuinely sound path, if we get real RFUAV data, is: get RFUAV's
  **raw IQ** (not their pre-rendered PNGs) and run it through our own
  `make_spectrogram_image()` — this makes the domain gap purely about
  drone-signal content, not about incompatible image-generation conventions.
- **DroneSecurity raw IQ**: same reasoning — 50Msps raw complex64 IQ can be
  converted through our own `make_spectrogram_image()` pipeline right now
  without downloading anything, since the files are already local. This is
  the one source we can genuinely stage end-to-end this pass.

## What was actually built this pass

- `convert_iq_to_spectrogram.py` — reuses `gamutrf_infer.make_spectrogram_image()`
  verbatim (same Hann-windowed STFT, two-sided FFT-shift, dB scaling,
  min-max normalization, jet colormap, 256×256 resize) to convert a raw
  IQ file into the exact tensor/image format the ResNet18 classifier
  consumes. Tested against both real local DroneSecurity sample files
  (`mavic_air_2`, `mini2_sm`) at their real 50Msps capture rate — this is
  real signal-processing on real captured RF, not synthetic data.
- Output: PNG spectrogram images under `field-bridge/drone_rf_kb/staged/<source>/`,
  labeled by drone model, ready to be assembled into a class-labeled
  training set — but **no ResNet18 fine-tuning has been run**. Doing so
  requires labeled ground truth beyond "this file = this drone model" (the
  existing checkpoint's classes are `drone`/`wifi_2_4`/`wifi_5`, not
  per-airframe — a real fine-tune to add airframe-level classes is a
  separate, larger effort matching the backlog's own sizing for B2, and is
  not attempted here).

## Verification status of `convert_iq_to_spectrogram.py`

- **IQ-loading path verified against real data on the Mac**: loading
  `mavic_air_2` and `mini2_sm` with `dtype="<f4"` → `complex64` (matching
  `droneid_receiver_offline.py`'s own loader) produces plausible small-
  amplitude complex samples (e.g. `mini2_sm`: 727,500 samples =
  5,820,000 bytes / 8 bytes-per-sample, 14.55ms at 50Msps) — confirms the
  loader and windowing-fallback logic are correct against the real files.
- **Full image-generation path (scipy STFT → jet colormap → 256×256 resize
  → PNG) NOT executed on the Mac** — `torch`/`scipy`/`matplotlib` aren't
  installed in this checkout's environment on the Mac, and per this
  project's standing rule, real build/verify work runs on the deploy VM
  (172.16.16.196), not locally. So: the tool exists and its I/O logic is
  proven against real capture files, but nobody should treat "spectrograms
  were generated" as true until it's actually run end-to-end on the deploy
  VM with the field-bridge Python env installed.

## Honest next step (separate from this pass)

1. Decide whether to actually pull RFUAV's raw IQ from Hugging Face
   (`kitofrank/RFUAV`) — multi-GB, needs disk/bandwidth sign-off — and if
   so, regenerate spectrograms through `make_spectrogram_image()` (not
   their PNGs) for pipeline-consistency, per above.
   Note: per this project's DVC/tooling rule, if a Hugging Face download is
   greenlit it should run on the deploy VM (172.16.16.196), not the Mac.
2. Decide the target taxonomy for a fine-tuned model — the existing
   checkpoint's 3 closed-world classes cannot represent RFUAV's 23-35
   airframe-level classes or DJI OcuSync signatures without adding output
   classes and retraining the final FC layer at minimum (likely more, given
   domain shift from GamutRF-style captures).
3. Only after (1) and (2) are resolved: run an actual fine-tuning pass,
   evaluate on a held-out split, and report real accuracy/confusion-matrix
   numbers — not before.
