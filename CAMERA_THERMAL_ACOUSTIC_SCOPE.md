# Camera / Thermal / Acoustic Sensing — Scoping Document (Task #83)

## Status
Proposed (architecture/scoping only — no implementation in this pass).

## 0. Framing: what problem this actually solves

Army directive 2026-07-25 priority (d) is RF-passive drone detection, which splits into two
genuinely distinct problems already tracked separately in this project:

- **#43/#57 — Passive bistatic radar**: detects RF-silent drones by illuminating them with
  ambient RF (broadcast TV, cellular, Wi-Fi) and reading the reflection. Still an RF sensor;
  the drone itself emits nothing, but the environment does.
- **#83 (this task)**: EMCON/RF-silent drones flying below passive radar's own reflection
  detection threshold (small airframe -> low radar cross-section, or radar geometry/range
  not favorable) are invisible to *both* RF pipelines in this system. The only way to close
  that gap is a sensing modality that doesn't depend on RF at all: **vision (optical/thermal)
  or acoustic.**

This document is scoping for that non-RF gap only. It does not replace or duplicate the
passive radar design work.

## 1. What already exists locally — repo survey result

The 2026-07-24 GitHub `drone-detection` topic survey evaluated four repos via metadata only
and dismissed them as "different modality" at the time. Re-checked now that this is in-scope:

Checked `~/Desktop/Zettawise/PMO Suraj/tool/` and `~/Desktop/zettagit/` (both directories
fully listed, `find -iname` swept for each repo name and common variants):

| Repo | Cloned locally? | Notes from prior metadata evaluation |
|---|---|---|
| doguilmak/Drone-Detection-YOLOv7 (and v8/v11x variants) | **No** | Camera/thermal + YOLO object detection. Standard YOLOv7/v8/v11x fine-tune on drone imagery datasets. Typically small (~single-author, few-contributor) research/portfolio repos — check license and last-commit recency again at clone time; these tend to be MIT/AGPL depending on which YOLO base they fork (Ultralytics YOLOv8/v11 is AGPL-3.0 unless a commercial license is purchased — **this matters** for the open-source-sovereignty/permissive-license constraint on this project; YOLOv7 (WongKinYiu) is GPL-3.0. Neither is OSI-permissive; both are copyleft and would need a clean-room reimplementation or a permissively-licensed detector (e.g. Ultralytics' older AGPL is out, but truly permissive alternatives like YOLO-NAS (Apache-2.0 weights caveat), RT-DETR (Apache-2.0), or training a plain torchvision Faster-RCNN/RetinaNet from scratch are viable substitutes). |
| Prabhdeep1999/uav-detection | **No** | Infrared video stream detection. Same category as above — verify license before any reuse; not yet cloned to confirm. |
| kbhujbal/SudarshanChakra | **No** | Acoustic CNN signature detection. Acoustic ML is the least mature of the three modalities generically (see §3) and this specific repo was only evaluated via GitHub metadata, not code — depth/quality unverified. |
| batear-io/batear | **No** | Low-cost off-grid acoustic detector — hardware + firmware angle rather than a pure ML repo, potentially useful as a hardware reference (mic array + edge compute BOM) even if the detection model itself isn't reused. |

**None of the four have been cloned or pulled since the 2026-07-24 survey.** Nothing new has
appeared in either directory under these names or obvious variants. This confirms the premise
in the task: there is genuinely nothing built or vendored locally for this modality yet — it
would start from zero, not from an existing partially-integrated library (unlike this
session's RF protocol-parser work, which wrapped real, already-installed libraries).

**Action before any implementation spike**: actually `git clone` (read-only, shallow) all
four to inspect real license files (`LICENSE`, not just GitHub's inferred badge), commit
history/maintenance cadence, and code quality — the table above is still second-hand
metadata, not a verified read. Given the OSI-permissive-only constraint (BSL/SSPL/AGPL/GPL
rejected per standing project policy), expect that the actual reusable fraction of these four
repos is the *dataset and technique documentation*, not the code verbatim, unless a repo
turns out to be MIT/Apache/BSD internally rather than inheriting its base model's copyleft.

## 2. Sensing modality comparison

| Modality | Maturity for drone detection | Day/night | Range (typical field unit) | Environmental robustness | Integration complexity | Relative cost |
|---|---|---|---|---|---|---|
| **Optical camera + YOLO-style detector** | High — this is a well-precedented, heavily-published CV task (COCO-style object detection fine-tuned on drone imagery). Most mature of the three. | Day only (or requires supplemental IR illuminator at night, which itself is detectable) | Detection range highly dependent on lens/sensor — small quadcopter reliably detected in the tens-to-low-hundreds of meters with a standard lens, further with a telephoto/PTZ unit | Degraded by fog, rain, glare, low light, foliage occlusion, camouflage | Moderate — camera + GPU/NPU inference host, well-trodden ML ops path | Low ($50-300 camera; compute is the bigger line item) |
| **Thermal camera + YOLO-style detector** | High — same detection architecture as optical, different sensor input; also well-precedented (all four surveyed camera repos are optical/thermal, none acoustic-only except two) | **Day and night** — this is the key advantage over optical | Shorter typical range than optical at equivalent cost tier; thermal cameras are resolution-limited relative to optical at the same price point | More robust to darkness and some camouflage (thermal signature of motors/battery), still degraded by heavy rain/fog and by distance (small UAS = weak thermal signature at range) | Moderate, same ML pipeline shape as optical, but thermal-specific datasets are scarcer than optical drone datasets | Medium-high ($200-2000+ depending on resolution/NETD; genuinely thermal — not just "night vision" — sensors are the expensive line item) |
| **Acoustic array + signature classification** | **Lower** — real but genuinely less mature/reliable than vision-based detection. Rotor/motor acoustic signatures are detectable in principle (this is the premise behind SudarshanChakra/batear and published research), but: (a) effective range is short (tens of meters typically, worse in wind/ambient noise), (b) omnidirectional/ambient noise (traffic, wildlife, wind, generators) causes false positives, (c) multi-drone/multi-source disambiguation with an array is a harder DSP+ML problem than single-frame object detection, (d) far less mature open tooling/pretrained-model ecosystem than YOLO-family vision. Honest assessment: **useful as a corroborating/cueing signal, not as a standalone primary detector**, at least initially. | Day and night equally (no light dependency at all) | Short (tens of meters typical for small consumer mic arrays; specialized parabolic/array hardware extends this but adds cost and directionality complexity) | Robust to visual occlusion (fog, foliage, non-line-of-sight around obstacles) but *very* sensitive to wind noise and ambient acoustic clutter in a field environment | Higher than it looks — needs a phased mic array (not just one mic) for bearing estimation, careful gain-staging/windscreen hardware, and a less mature ML stack (fewer pretrained models, likely need to train from scratch on a real dataset) | Low-medium hardware ($50-400 for USB mic array kits; more for a purpose-built array), but higher effective cost once you count the ML development effort |

### Recommended priority: thermal first, optical second, acoustic third (as a corroborating signal)

- **Thermal is the right first pick** for a field-deployed counter-UAS system specifically
  *because* the operational need is EMCON/RF-silent drones — these are most plausibly used at
  night or in low-visibility conditions precisely because that's when RF-silence plus visual
  concealment together give the most attacker advantage. A camera-only (optical) system is
  blind exactly when this threat is most likely to be exploited. Thermal directly closes that
  gap. It reuses the same detection architecture (YOLO-style object detection) that all four
  surveyed camera repos already validate as workable, so the ML approach is not a research
  risk — an off-the-shelf detection architecture retrained on a thermal drone dataset is a
  known-tractable engineering task, not an open research problem.
- **Optical is a reasonable fast-follow**: cheaper hardware, same detection architecture,
  extends coverage to daytime high-resolution identification (useful for visual confirmation/
  classification once thermal has cued a detection), and can reuse most of the same
  inference pipeline code (swap sensor input, same or near-same model architecture/training
  loop).
- **Acoustic should be scoped as a corroborating cue, not a first standalone sensor.** It's
  the most operationally attractive on paper (omnidirectional, no line-of-sight requirement,
  works in any light) but is the least mature and hardest to make reliable stand-alone in an
  outdoor field environment with wind and ambient noise. Recommend treating it as a second-
  phase fusion input that raises confidence on an existing thermal/optical (or RF) detection,
  rather than a system that must independently confirm a contact on its own credibility.

## 3. Hardware shopping list (rough costs, same procurement-note style as this session's RF hardware notes)

| Item | Rough cost (USD) | Purpose | Notes |
|---|---|---|---|
| FLIR Lepton 3.5 breakout (e.g. PureThermal 2/Mini) | $200-250 | Thermal sensor core for a first thermal-detection prototype | Low resolution (160x120) but well-documented, USB-UVC output, widely used in hobbyist/prototype thermal-CV projects — good first spike hardware, not a fielded-unit spec |
| FLIR Boson or higher-res thermal module | $1,500-4,000+ | Higher-resolution/longer-range thermal core if Lepton-class range proves insufficient | Export-control (ITAR/EAR) considerations apply to higher-spec FLIR thermal cores — check before ordering; this is a real procurement friction point, not just a cost one |
| Raspberry Pi HQ Camera or similar global-shutter USB/CSI camera | $50-150 | Optical sensor for daytime detection/fast-follow phase | Cheap, well-supported, plenty of prior art |
| PTZ/zoom optical camera (if longer standoff range is required) | $200-1,000+ | Extends optical detection range beyond a fixed wide-angle lens | Adds a pan/tilt control integration surface — separate scoping item if pursued |
| ReSpeaker Mic Array v2.0 (4-mic circular array) or similar USB mic array | $60-100 | Acoustic sensing prototype, bearing estimation via multi-mic array | Consumer-grade; fine for a feasibility spike, likely insufficient range/directionality for a fielded unit |
| Purpose-built parabolic/shotgun mic + preamp | $150-500 | Extended-range, directional acoustic pickup if array approach is range-limited | Directional, not omnidirectional — trades coverage for range/rejection of ambient noise |
| Edge inference compute (NVIDIA Jetson Orin Nano, or equivalent) | $250-500 | Runs the vision (YOLO-style) or acoustic classifier model at the sensor node, keeps inference local/field-deployable rather than round-tripping to a central server | Matches this project's existing field-bridge pattern (local processing, JSON ingest to backend) rather than centralizing all inference |
| Weatherproof enclosure + mounting hardware (per sensor node) | $50-200 | Field survivability — this is a field-deployed system, not a lab demo | Easy to underestimate/forget in a hardware BOM; explicitly called out here |

**Total rough cost for a single first-phase thermal-only prototype node** (Lepton-class
sensor + Jetson-class compute + enclosure): roughly **$500-900**. This is a feasibility-spike
number, not a fielded-unit production cost (production units would likely need higher-res
thermal cores, ruggedized housings, and power/comms integration — a materially larger
follow-on cost once the spike validates the approach).

## 4. Data model integration plan

### New `source` values

Add source tags distinct from the existing `HACKRF` / `SIK_RADIO`:

- `THERMAL_CAM`
- `OPTICAL_CAM`
- `ACOUSTIC_ARRAY`

These follow the exact same pattern as the existing `source` field in
`DetectionIngestBody` (`backend/server.py` ~line 1255) — a new field-bridge-equivalent
process (e.g. a hypothetical `thermal_bridge.py` alongside `hackrf_rx.py`) posts to the same
`/api/detections/ingest` endpoint with its own `source` value.

### New `confidence_type`: `visual_confirmed`

The existing five-value enum (`heuristic_binary`, `ml_probability`, `protocol_verified`,
`advisory_only`, `unclassified_signal`) is deliberately an **epistemic-category** enum, not a
per-sensor enum (see `backend/CONFIDENCE_MODEL.md` — this is the same design principle this
project already committed to for `spectral_features_ml`, which was reserved but explicitly
NOT wired in until a real trained model exists). Applying that same discipline here:

A YOLO-style detector's output is, epistemically, **the same category as `ml_probability`** —
a real softmax/objectness-score probability from a trained model, with the same caveats
(closed-world class set, possible false positives on visually similar objects like birds).
**Do not invent `visual_confirmed` as a synonym for "the model output a number."** Reuse
`ml_probability`, and let `source` (`THERMAL_CAM`/`OPTICAL_CAM`) carry the "which sensor"
distinction — this mirrors how `ml_probability` today already covers the RF ML classifier
regardless of which physical HackRF unit produced the IQ capture.

Where a genuinely new epistemic category *would* be justified: if a human operator visually
confirms a detection (e.g. taps "confirmed" after viewing a live thermal/optical feed showing
an unambiguous drone), that is categorically different from a model's probability — it is a
human-verified ground truth, arguably even higher-trust than `protocol_verified`. That case
would warrant a genuinely new value, e.g. `human_visual_confirmed`, reserved for **explicit
operator confirmation via the UI**, not merely "a camera detected something." This is worth
flagging now but should not be built until the visual pipeline itself exists and an operator
confirmation workflow is actually designed (separate UX Architect scope).

**Acoustic classification** is the least mature of the three (see §2) — recommend it initially
report `unclassified_signal`-style epistemic caution (i.e., treat low-confidence acoustic
detections the same way the existing `unclassified_signal` design treats a weak RF softmax
read) rather than a confident `ml_probability` badge, until real-world false-positive rates
against wind/ambient noise are measured and known to be acceptable. This can be revisited once
an acoustic model is trained and validated (same "don't add the value until a real model
exists" discipline already applied to `spectral_features_ml`).

### Fusion question: does a camera + RF heuristic on the same object merge or stay separate?

Recommend **stay separate initially, with a display-level "co-located" hint, not a hard
merge** — for the following reasons, consistent with this project's existing
`CONFIDENCE_MODEL.md`/`DETECTION_DISPLAY_MODEL.md` design discipline of never manufacturing
false precision:

1. **Different physical observables, different failure modes.** An RF heuristic and a visual
   detector can each be independently wrong (RF: Wi-Fi/Bluetooth false positive; vision: bird,
   balloon, insect near the lens). Silently merging them into one contact the instant they
   coincide in time asserts a correlation ("these are the same physical object") that the
   system has not actually verified — no geolocation/bearing correlation exists yet between an
   RF bearing/RSSI estim ate and a camera's field-of-view azimuth. This project's own
   `_ml_wifi_reclassification`/`_ml_unclassified_display` precedent explicitly rejects
   collapsing distinguishable signals into one falsely-confident number; the same principle
   applies to cross-modality fusion.
2. **The actual RF-silent-drone use case underlines this**: the entire reason #83 exists is
   drones that are invisible to RF. In the target scenario there frequently will be *no*
   RF contact to merge with — thermal/optical/acoustic must stand alone as their own contact
   type on the Dashboard/Detection History, not be designed as a mere enrichment of RF
   contacts.
3. **A real merge requires a real correlation key** — e.g. bearing/azimuth overlap within a
   time window, or (if available later) a shared geolocation estimate — which does not exist
   yet in this codebase for any two independent sensors. Building that correlation logic is
   itself a nontrivial piece of work (essentially a lightweight multi-sensor track-fusion
   layer) and should be its own explicitly scoped follow-on, not something bolted on
   opportunistically while building the first camera/acoustic bridge.

**Recommended interim design**: each new sensor source posts its own independent detection
record via the existing ingest path (new `source` value, `confidence_type=ml_probability` for
vision or the cautious `unclassified_signal`-style treatment for acoustic). Dashboard/
Detection History render them as their own contacts, visually distinguishable by `source`
icon/badge (camera icon, thermal icon, acoustic icon vs. the existing RF antenna icon). If/when
a real correlation signal exists (e.g. both report roughly the same bearing within the same
few seconds), a follow-on B-series ADR can design an explicit "possible same-object, unverified
correlation" UI treatment (e.g. a dashed link between two rows) — short of an actual fusion
algorithm, do not silently combine them into one row's data.

## 5. Honesty check: program size

This is **not** a quick add comparable to this session's RF protocol-parser work (which
wrapped existing, already-installed, permissively-licensed real libraries into the existing
field-bridge pattern). Concretely, this program requires, in rough sequence:

1. **Hardware acquisition** — none of the required sensors (thermal core, camera, mic array,
   edge compute) exist in this project yet; §3's BOM must actually be purchased and arrive.
2. **Labeled training data** — no drone-vs-not-drone visual or acoustic dataset exists locally.
   Either find/adapt a permissively-licensed public dataset (verify license terms — many
   drone-detection datasets on Kaggle/Roboflow have research-only or unclear redistribution
   terms) or collect and label field data directly, which is itself a multi-week effort.
3. **Model training/fine-tuning** — retraining a YOLO-family (or permissively-licensed
   equivalent, per the licensing note in §1) detector on thermal imagery, and, separately,
   training an acoustic classifier from a much thinner prior-art base — is real ML engineering
   work, not integration work.
4. **New field-bridge process(es)** — a `thermal_bridge.py`/`acoustic_bridge.py` analogous to
   `hackrf_rx.py`, running inference at the edge and posting to `/api/detections/ingest`.
5. **Backend/data-model wiring** — the new `source` values and `confidence_type` handling
   described in §4 (this part genuinely is a small, well-precedented change, consistent with
   how `SIK_RADIO` was added alongside `HACKRF`).
6. **Frontend rendering** — new badge/icon treatment per `source`, per the existing
   `DETECTION_DISPLAY_MODEL.md` pattern (UX Architect scope, not this document's scope).
7. **Field validation** — testing-division approval per this project's standing workflow rule
   (no deployment without testing-division sign-off), including false-positive-rate testing
   against real ambient conditions (wind noise for acoustic; birds/insects for vision;
   weather robustness for both) before this can be trusted operationally.

Steps 1-3 are the dominant cost and are **multi-week, specialist work** (AI Engineer domain:
CV/audio model training, not CEMA/RF engineering), not something to fold into the existing
CEMA/RF specialist's workstream. Steps 4-6 are comparatively small and familiar once a working
model exists. Realistic estimate: a single-modality (thermal-first) feasibility spike —
hardware bring-up, a rough fine-tuned detector on a public or lightly-collected dataset, and
one field-bridge posting into the existing ingest API — is itself plausibly **2-4 weeks** for
one AI Engineer with the hardware in hand, before any field validation or production
hardening. Building all three modalities (thermal, optical, acoustic) to a fielded standard is
a multi-month, multi-person program, not a single task.

## 6. Recommendation / next step

- **Do not attempt all three modalities at once.**
- **Hand to AI Engineer** for a **single-modality feasibility spike on thermal detection
  first** (per §2's priority ranking): acquire one Lepton-class thermal module + one
  Jetson-class edge compute unit (§3 BOM, ~$500-900), clone and license-audit the four
  surveyed repos as real code (not metadata) to see whether any technique/dataset is directly
  reusable, fine-tune a permissively-licensed detector architecture (RT-DETR/YOLO-NAS/
  torchvision, not an AGPL/GPL YOLO fork per the sovereignty constraint) on a small
  drone-vs-not-drone thermal dataset, and produce one working `thermal_bridge.py`-style
  process posting `source=THERMAL_CAM`, `confidence_type=ml_probability` detections into the
  existing `/api/detections/ingest` endpoint end to end.
- Treat optical camera support as a near-term fast-follow reusing the same pipeline.
- Treat acoustic as an explicit second-phase corroborating signal, scoped separately once
  thermal/optical prove out the field-bridge + ingest pattern for non-RF sensors, and only
  reported with `unclassified_signal`-level caution until real false-positive rates are
  measured.
- Do not build cross-modality fusion/merge logic in this first pass — ship independent
  contacts per §4 and revisit fusion as its own scoped ADR once a real correlation signal
  (e.g. bearing estimation) exists on more than one modality.
