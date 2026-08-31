# RTX 3060 Utilization Roadmap — MEGHDUT C3 Counter-UAS

_Host: `172.16.16.186` (meghdut-srv02). GPU: NVIDIA GeForce RTX 3060 (GA106,
Ampere sm_86, ~3584 CUDA cores, ~13 TFLOPS FP32, 3rd-gen tensor cores, NVENC/
NVDEC, ~12 GB GDDR6). Driver + CUDA already working (`nvidia-smi` runs)._

_Derived from a read-only, file:line-grounded analysis of `field-bridge/` across
five domains (ML classifier, passive-radar CAF, multi-domain EO/thermal/acoustic,
RF fingerprinting/SEI, general GPU DSP/DF). Cross-references
[`field-bridge/FPGA_ACCELERATION_SCOPE.md`](field-bridge/FPGA_ACCELERATION_SCOPE.md),
[`CAPABILITY_ROADMAP.md`](CAPABILITY_ROADMAP.md),
[`field-bridge/PASSIVE_RADAR_ARCHITECTURE.md`](field-bridge/PASSIVE_RADAR_ARCHITECTURE.md),
[`field-bridge/CAMERA_THERMAL_ACOUSTIC_SCOPE.md`](field-bridge/CAMERA_THERMAL_ACOUSTIC_SCOPE.md).
This is a planning ledger, not a claim of delivered capability — read §4 before
any evaluator-facing statement._

---

## The one fact under everything

Three torch models already select the GPU in source — `gamutrf_infer.py:181`,
`spectrogram_similarity_bridge.py:300`, `thermal_bridge.py:272` — but
`field-bridge/requirements.txt` pinned the `+cpu` wheels, so
`torch.cuda.is_available()` returned False and the 3060 sat idle. The CUDA repin
(shipped, commit landing this work) is the master enabler: it lights up every
GPU inference path at once.

**Status of item #1 (CUDA repin): DELIVERED + third-eye verified** on .186 —
`torch 2.13.0+cu126`, live bridge on `device=cuda:0`, argmax parity vs CPU
(softmax max-abs-diff ~8e-07), ~7× forward latency, hard CPU fallback at load-
and inference-time via `CEMA_ML_DEVICE`, service active `NRestarts=0`.

---

## 1. Ranked opportunities

| # | Capability | Effort | Value | Demo-relevant? | Real gate beyond the GPU |
|---|-----------|--------|-------|----------------|--------------------------|
| 1 | **CUDA torch repin** (enabler) — DELIVERED | S | high | **yes** | cu12x wheel; `.pt` checkpoint ships separately |
| 2 | **GPU-batched CAF** (`caf_fft_batched` → `torch.fft`/cupy) — **OB-06 Phase-1** | M | high | slide only | verifier bit-accuracy vs `caf_bruteforce`; Army OB-06 intent |
| 3 | **Retrain RF classifier with a reject/noise class** | L | high | no | write training script (none exists); labeled set incl. reject class |
| 4 | GPU DSI suppression (`pinv` → `torch.linalg.pinv`) | S (in #2) | med-high | no | bundled with #2 |
| 5 | GPU-native spectrogram preproc (`torch.stft`) | M | med | no | numeric parity with `make_spectrogram_image` |
| 6 | Batched multi-emitter inference | M | med | no | single-HackRF serial sweep caps concurrency |
| 7 | RF SEI — open-set emitter embedding on raw IQ | L→XL | high | no | **labeled per-emitter IQ campaign** (the real gate), IQ ingest wiring, an SEI architecture |
| 8 | Thermal/EO detector training + real-time inference | L | med | no | thermal + EO cameras (none procured); license-cleared dataset |
| 9 | Wire the ResNet34 "second-opinion" embedder | M | med | no | multi-class labeled reference library (folds into #7) |
| 10 | NVENC/NVDEC hardware video codec | spec. | low | no | a real digital/RTSP/UVC camera (analog FPV path has no compressed stream) |

---

## 2. Demo-relevant vs post-demo

**Demo-relevant — one item, honestly scoped:** #1 CUDA repin (delivered).
Truthful talking point: _"the live ML classifier now runs on the RTX 3060,
freeing the CPU for real-time sweep + passive-radar DSP."_ Two honesty guards:
(a) it is **not** a large classification speedup — for the gated single-window
workload the CNN forward is not the bottleneck (scipy STFT + matplotlib colormap
+ USB IQ capture dominate, per `FPGA_ACCELERATION_SCOPE.md`); (b) it does **not**
fix the confident-on-noise flaw — that needs #3. Safety net: `CEMA_ML_DEVICE=cpu`
(or the automatic runtime fallback) reverts to the known-good CPU path with one
env var.

**Everything else is post-demo roadmap** — hardware- or data-gated and/or needs
independent verifier sign-off; none should be rushed into the demo window.

---

## 3. Single highest-value unlock for the Army program

**#2 — the GPU-batched CAF port** (`caf_fft_batched` → `torch.fft`/cupy in
`field-bridge/passive_radar/caf.py`).

- It is the documented interim bridge to **OB-06 (Army-CRITICAL FPGA
  acceleration)** — `FPGA_ACCELERATION_SCOPE.md` names the CAF chain as the
  single latency-sensitive heavy-DSP workload in the repo and names GPU
  cupy/torch of `caf_fft_batched` as the explicit Phase-1 answer.
- It is a named, already-scoped roadmap task (1.6) that was explicitly
  GPU-gated — `CAPABILITY_ROADMAP.md:209` blocked it on _"ST550 has no GPU."_
  The 3060 lifts that gate.
- The code is unusually ready: `compute_caf` is one swappable line
  (`caf.py:169`), and `caf_bruteforce` (`caf.py:97-122`) is a ready-made
  bit-accuracy oracle already used in `test_caf.py`. Estimate ~10-50× over the
  scipy path, ~5-15× real-time headroom at production scale (N≈1.05M, 101
  Doppler bins, ±512 lag). Runs on synthetic/recorded IQ today → a before/after
  benchmark is a legitimate hardware-free artifact (only if verifier-signed;
  not to be rushed).

**Runner-up: #3** — retrain with a reject/noise class, the direct cure for the
documented "99% confident drone on pure noise" flaw that every energy-gate/OOD
layer in `ml_calibration.py` currently only mitigates.

---

## 4. What the GPU does NOT solve — do not overclaim

- **No live passive radar.** GPU accelerates the CAF/DSI math, not the missing
  front end — still needs a 2nd SDR + GPSDO shared clock + illuminator-of-
  opportunity confirmation (`DualRTLSDRSource` is a `NotImplementedError` stub,
  `channel_source.py:227-247`; task #57).
- **OB-06 only partially closed.** GPU satisfies OB-06 **only under the
  throughput reading**. If it means deterministic/bounded latency, SWaP-C, or
  certification, a **true FPGA is still required** — Phase-1 GPU reduces none of
  that scope. **Confirm Army intent before claiming closure.**
- **No GPU direction-finding.** `direction_finding.py` is a scalar amplitude-
  comparison estimator (stdlib `math` only). MUSIC/beamforming needs the OB-05
  phase-coherent array — fully hardware-gated.
- **No wider instantaneous bandwidth.** The HackRF sweep is USB/cadence-limited;
  GPU does not widen it. A wideband SDR/RFSoC is a separate buy.
- **No multi-domain sensing.** No thermal core, EO camera, or mic array procured
  (`HARDWARE_PROCUREMENT.md:78-80`). A same-day GPU install produces no EO/
  thermal/acoustic detection. **Do not stage a webcam + stock COCO model as
  "drone detection"** — a COCO net labels a quadcopter as airplane/bird; that
  would violate the standing no-fake-capability rule.
- **Data, not compute, gates SEI and thermal.** SEI needs a labeled per-emitter
  IQ campaign; thermal needs a license-cleared labeled dataset. The 3060 removes
  only the compute blocker.

---

## 5. Pragmatic sequence

**Around the demo (beyond the delivered CUDA switch):**
1. Keep the CPU pipeline available as the safe live-demo fallback; the GPU path
   is verified but the fallback is the belt-and-braces.
2. Demo narrative: "GPU now runs the live ML classifier" (true) + "GPU CAF is
   the OB-06 Phase-1 acceleration backbone, now unblocked" (forward-looking). No
   SEI/thermal "working" claims.

**Post-demo Phase-1 (the CAF workstream):**
3. First do roadmap task 1.5 — validate the CPU CAF chain against the real
   recorded IQ (`171210ship`) so the GPU port is checked against real data, not
   only synthetic.
4. Port `caf_fft_batched` to `torch.fft`/cupy complex64 (#2) **with verifier
   bit-accuracy sign-off vs `caf_bruteforce`** (double→single precision must be
   checked) + before/after benchmark. Bundle GPU DSI `pinv` (#4).
5. Follow with GPU-native spectrogram preprocessing (#5) and batched multi-peak
   inference (#6) — noting #6's payoff is capped until more SDRs arrive.

**Flagship builds (multi-week, AI-Engineer lane, independent verifier sign-off):**
6. Retrain the RF classifier with a reject/noise class (#3).
7. RF SEI (#7): disciplined per-unit labeled-IQ campaign with `iq_capture.py`,
   then an open-set contrastive/siamese embedding (data-efficient, matches the
   "track this emitter" mission) reusing the `spectrogram_similarity_bridge.py`
   pattern (#9).
8. Thermal/EO (#8): procure a Lepton/PureThermal-class core first, license-audit
   + acquire the dataset, implement `ThermalDroneDataset.__getitem__`, train FP16
   on the 3060, then feed a real thermal confidence into the already-built
   `multidomain_fusion.py`.

**Bottom line:** the RTX 3060 delivers one honest, low-risk demo win now
(GPU-accelerated live ML classifier) and one genuinely flagship, already-scoped
build next (the GPU CAF port that unblocks OB-06 Phase-1). It does not by itself
produce live passive radar, direction finding, or multi-domain sensing — those
remain gated on the 2nd SDR/GPSDO, the OB-05 array, and EO/thermal/acoustic
sensors respectively — and it substitutes for a true FPGA only under the
throughput reading of OB-06, which the Army should be asked to confirm.
