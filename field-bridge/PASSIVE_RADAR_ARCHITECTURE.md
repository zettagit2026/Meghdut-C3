# Passive Bistatic Radar — Software Scaffolding Architecture (design doc)

Status: DESIGN ONLY — no production code in this doc is meant to be run as-is.
Author: Software Architect agent, 2026-07-25.
Scope: Army directive priority (d), task #43 (C10) — CAF/Doppler software
scaffolding for RF-passive drone detection. Full dual-SDR + GPSDO hardware
build is task #57 and is explicitly OUT of scope here.

---

## 1. What the reference implementation (`~/Desktop/zettagit/passive_radar`) actually is

Read directly (not just the README): `dual_rtl_sdr.grc` (GNU Radio Companion
flowgraph, 828 lines), `171210ship/goship.m` + `simulation.m` (GNU Octave
processing), and the FOSDEM 2018 / Rev. Sci. Instrum. paper it's based on
(Feng, Friedt, Cherniak, Sato).

**Hardware/acquisition side (`dual_rtl_sdr.grc`):**
- Two `osmosdr_source` blocks (RTL-SDR via gr-osmosdr), one antenna aimed at
  the DVB-T illuminator (reference channel), one Yagi-Uda aimed at the
  surveillance volume (surveillance channel).
- Both channels sample the **same DVB-T transmitter's frequency** — there is
  no separate "radar transmitter"; the DVB-T broadcast itself is the
  illuminator of opportunity. This is why it's called *passive*.
- `blocks_interleave` + `blocks_file_sink` write both channels to one
  interleaved binary file (complex, i.e. I/Q pairs per channel, then
  interleaved channel-to-channel).
- The two RTL-SDRs are **clock-synced from a common reference** (this is the
  hard hardware requirement task #57 exists to solve — RTL-SDRs have no
  native multi-device clock sync, so this needs an external reference/GPSDO
  distribution or a modified dongle).
- The README explicitly documents a 0MQ Pub/Sub stage used for the
  *multi-azimuth scanning* variant: the acquisition flowgraph runs
  **continuously** (never stops/restarts — restarting introduces a random
  USB-bus timing offset that breaks the cross-correlation alignment), and a
  ZMQ Sub block is connected/disconnected to a file sink only once a given
  azimuth has mechanically stabilized. **The 0MQ layer is a stream-tap /
  gating mechanism, not a detection-distribution or networking mechanism.**
  It solves one specific problem: "let the acquisition run forever, sample
  only when we trust the antenna position." That's a mechanical-rotator
  integration detail, not a CAF architecture detail.

**Processing side (`goship.m`, confirmed peer-reviewed against real ship
target data + a synthetic dataset in `simulation.m`):**
1. Read both channels as interleaved I/Q (works for int8/int16/int32/float,
   parameterized by `datatype`).
2. **USB-bus delay correction**: initial `xcorr(ref, mes)` over the full
   first buffer to find the fixed (but a-priori unknown, introduced by
   independent USB transfers) sample offset between the two channels, then
   permanently slice both buffers to align t=0. This is a **one-time-per-run
   calibration** step, distinct from the per-block CAF below. It exists
   *because* there's no shared sample clock/trigger between the two USB
   dongles — a real synchronized-source design (or even just a shared
   sample clock) could eliminate this step entirely; it is a workaround for
   consumer RTL-SDR hardware, not an inherent requirement of passive radar.
3. **Direct Signal Interference (DSI) suppression** (optional,
   `dsi_suppression` flag): builds a small bank of range-shifted copies of
   the reference signal (`Index1=-9..Index2=+9` sample shifts) and removes
   their least-squares-optimal projection from the surveillance channel
   (`mes = mes - X1*(pinv(X1)*mes)`). This kills the strong direct-path
   breakthrough (and any static/near-zero-Doppler clutter) that otherwise
   swamps the cross-ambiguity map and hides real (weak, moving) targets in
   its sidelobes — the before/after figures in `171210ship/` show this
   concretely (green circle = real ship target, invisible without DSI
   removal, visible with it).
4. **Cross-Ambiguity Function (CAF)**, the actual "CAF/Doppler processing
   chain": for each candidate Doppler shift `fd` in a scan range
   (`freq=[-200:4:200]` Hz here), demodulate the surveillance channel by
   `exp(j*2*pi*fd*t)`, cross-correlate against the reference channel
   (`xcorr(ref, mesdop, dN)`), and keep a windowed range-lag slice
   (`dN-negdist : dN+maxdist`). Stacking these slices over all `fd` gives a
   2D **range-Doppler map** (image) per ~0.5s integration window
   (`N=fs`, i.e. 1 second of samples split as a 0.5s reference block against
   which the surveillance block is searched). This is the classic passive
   radar CAF: `CAF(τ,fd) = Σ ref[n] · conj(surv[n]·e^{-j2π fd n/fs})` shifted
   by lag τ, which is exactly what steps 4a (`mesdop=mes.*exp(...)`) + 4b
   (`xcorr`) implement, just done as brute-force lag-by-lag correlation
   rather than FFT-batch (see §4 below on complexity implications).
5. Output is one range-Doppler PNG/`.mat` pair per integration window
   (`imagesc(freq, range_km, rangedop)`), i.e. this is fundamentally a
   sliding-window imaging pipeline, not a single-shot detector — target
   detection/tracking (peak-picking, CFAR, track association) is **not
   implemented here at all**; the repo stops at "here is the range-Doppler
   map," a human (or downstream code) reads it.

**What's hardware-specific vs. portable, concretely:**

| Piece | Portable / hardware-agnostic | Hardware-specific (RTL-SDR / DVB-T assumption) |
|---|---|---|
| CAF math (step 4) | Yes — pure signal processing on two complex baseband streams at a common sample rate | No |
| DSI suppression (step 3) | Yes — same LS-projection technique works against any illuminator once you have ref+surveillance channels | No |
| USB-delay self-alignment (step 2) | Conceptually portable (any two independently-clocked ADCs need it) but its *specific need* goes away with real hardware sync | Yes — this is a workaround for unsynchronized consumer dongles |
| Two-channel acquisition topology (ref antenna + surveillance antenna, same tuned frequency) | Yes — pattern generalizes to any illuminator (DVB-T2, FM, GSM/LTE, Wi-Fi) as long as both channels are tuned to the same reference-carrying frequency/band | Partially — RTL-SDR's ~2.4-2.8 MS/s ceiling and 8-bit ADC constrain which illuminators are usable (DVB-T's ~8 MHz channel needs decimation/care; FM's 200 kHz is comfortably within range) |
| 0MQ pub/sub azimuth gating | Yes as a *pattern* (decouple "keep the SDR streaming" from "only persist stable-azimuth segments") | Assumes a mechanically rotated directional antenna; not needed for a fixed-antenna deployment |
| GRC flowgraph itself | No — GNU Radio Companion + gr-osmosdr + literal RTL-SDR sources | Yes, entirely |
| Octave scripts | No — file-format- and Octave-specific plumbing around the math | Partially (file layout assumptions) |

**Bottom line on adoption:** the *genuinely transferable* asset is the CAF +
DSI-suppression **algorithm**, not the acquisition scaffolding. The GRC
flowgraph and Octave file-reading code should not be ported — they're a
2018 rapid-prototyping setup (GRC binary capture + Octave batch
post-processing) built around exactly-two-RTL-SDR availability, offline
batch analysis, and a human looking at PNGs. This project needs a live,
Python, streaming-into-`detection_ingest` pipeline, so the porting boundary
is: re-implement the CAF/DSI math in Python/NumPy against a clean internal
interface, and treat everything upstream of "I have two aligned complex
IQ streams at a known sample rate" as a hardware-dependent adapter that
doesn't exist yet.

---

## 2. Design: where this lives and how it plugs in later

### 2.1 Module boundary

New directory, not a single script — this pipeline has enough internal
structure (and enough hardware-blocked vs. software-only separation) that
cramming it into one `passive_radar_bridge.py` analogous to `hackrf_rx.py`
would hide the seam this task exists to preserve:

```
field-bridge/passive_radar/
  __init__.py
  channel_source.py       # ABSTRACT interface: "give me aligned dual IQ streams"
    - DualChannelSource (ABC): .read_block(n_samples) -> (ref: np.ndarray[complex64], surv: np.ndarray[complex64], fs: float)
    - SyntheticDualChannelSource   # buildable/testable NOW — no hardware
    - RecordedFileDualChannelSource # buildable/testable NOW — plays back recorded IQ (e.g. the passive_radar repo's own 171210ship dataset, or gqrx/GNU Radio file captures)
    - DualRTLSDRSource  # HARDWARE-BLOCKED stub — task #57's implementation target
  alignment.py             # USB/inter-channel delay estimation + slicing (xcorr-based, port of goship.m step 2) — testable NOW against synthetic + recorded data
  dsi_suppression.py        # LS-projection DSI removal (port of goship.m step 3) — testable NOW, pure math
  caf.py                    # Cross-Ambiguity Function / range-Doppler map (port of goship.m step 4) — testable NOW, pure math; this is the actual "CAF/Doppler processing chain"
  detector.py                # NEW, not in reference repo: CFAR/peak-picking over the range-Doppler map -> discrete (range, doppler, snr) detections. The reference repo stops at "here's an image"; this project needs discrete detections to feed detection_ingest, so peak detection has to be designed fresh. Testable NOW against synthetic/recorded CAF output.
  geometry.py                # Bistatic geometry: (bistatic_range, doppler) -> (approximate ground range/bearing, radial speed) given known illuminator position + receiver position + baseline. HARDWARE-BLOCKED for real bearing accuracy (needs a real directional/rotated surveillance antenna or an antenna array — a single fixed Yagi gives coarse bearing at best, from antenna boresight, not true angle-of-arrival), but the math/interfaces are designable and unit-testable now with assumed geometry.
  illuminator_profile.py     # Data-driven description of "what am I illuminating with" (see §3) — NOT hardcoded DVB-T2. Buildable NOW.
  passive_radar_bridge.py    # Bridge script analogous to hackrf_rx.py: wires a DualChannelSource -> alignment -> dsi_suppression -> caf -> detector -> geometry -> POST /api/detections/ingest. Runs NOW end-to-end against Synthetic/RecordedFile sources; swaps to DualRTLSDRSource with zero changes to anything downstream of channel_source.py once task #57 lands hardware.
  test_caf.py / test_dsi_suppression.py / test_alignment.py / test_detector.py  # unit tests against synthetic + (optionally) the reference repo's real 171210ship dataset
```

This mirrors the existing `field-bridge/` convention (`hackrf_rx.py` as the
live bridge, `hackrf_config.py`/`rf_features.py`/`iq_capture.py` as
supporting modules it imports, per the pattern already established by
`ml_classify_bridge.py`'s explicit "imported, not duplicated" design note).
`passive_radar_bridge.py` plays the `hackrf_rx.py` role; everything else in
the new subpackage plays the supporting-module role.

### 2.2 The `DualChannelSource` seam is the entire point of this design

This is the one interface that must be pinned down correctly, because it is
what makes the rest of the pipeline hardware-agnostic:

```python
class DualChannelSource(ABC):
    """Yields aligned, complex-baseband reference and surveillance streams
    at a common, known sample rate. Implementations may be synthetic,
    file-replay, or live dual-SDR hardware -- callers downstream of this
    interface must not care which."""

    @property
    @abstractmethod
    def sample_rate_hz(self) -> float: ...

    @abstractmethod
    def read_block(self, n_samples: int) -> tuple[np.ndarray, np.ndarray]:
        """Returns (ref_iq, surv_iq), both complex64, length n_samples.
        Raises StopIteration when exhausted (file sources) or blocks
        until available (live sources)."""

    def close(self) -> None: ...
```

- `SyntheticDualChannelSource` generates its own reference signal (band-
  limited noise, standing in for a real broadcast/illuminator waveform) and
  a surveillance channel built as `ref + delayed_and_doppler_shifted(ref) *
  attenuation + noise`, i.e. exactly what `simulation.m` already does
  (`sur=ref+[ref(101:end);ref(1:100)].*lo`) — this is directly portable,
  should be re-implemented in Python rather than shelled out to Octave, and
  is what `test_caf.py`/`test_dsi_suppression.py` should assert correctness
  against (known injected delay + known injected Doppler -> CAF peak must
  land at the corresponding (range-lag, Doppler-bin) within a tolerance).
- `RecordedFileDualChannelSource` replays a pre-recorded interleaved or
  split I/Q capture — this can consume the **actual public dataset the
  reference repo cites** (`171210ship_ch1/ch2.sigmf-data` on iqengine.org)
  for an even stronger validation than synthetic data: run the new Python
  CAF against real recorded ship-target IQ and confirm the same target
  shows up in the same range/Doppler bin the original Octave processing
  found (cross-check against the repo's own `.mat`/PNG outputs). This is
  fully buildable and testable now with zero new hardware — it's just
  downloading an existing dataset.
- `DualRTLSDRSource` is declared as an interface/stub only. Its
  implementation needs: two RTL-SDRs, a shared reference clock/GPSDO
  (or an accepted post-hoc alignment fallback per §2.1's alignment.py), and
  whatever OS-level access `gr-osmosdr`/`pyrtlsdr` requires — this is
  task #57's scope, not this task's. The stub should raise
  `NotImplementedError("blocked on task #57 hardware")` so nothing
  silently pretends to work without hardware.

### 2.3 What's genuinely buildable and testable NOW (no 2nd SDR needed)

- `alignment.py` — inter-channel delay estimation via cross-correlation
  (direct port of `goship.m`'s bus-delay logic). Testable against synthetic
  data with a known injected delay.
- `dsi_suppression.py` — LS-projection DSI removal. Testable against
  synthetic data (verify DSI power drops by X dB after suppression,
  matching the qualitative before/after effect in the reference repo's own
  figures) and, ideally, against the real recorded dataset to reproduce the
  repo's own documented before/after result.
- `caf.py` — the actual CAF/range-Doppler computation. This is the core
  deliverable of "CAF/Doppler software scaffolding" and is 100% software-
  only: it operates on two complex arrays and a sample rate, nothing about
  it references SDR hardware. **This is where the DSP-capable engineer's
  real implementation work concentrates.** Worth flagging for handoff: the
  reference repo's CAF is brute-force `xcorr` per Doppler bin
  (`freq=[-200:4:200]`, 101 bins × full-length `xcorr` each) — for a live
  Python pipeline this should be redesigned as an FFT-batched CAF (batch
  multiply by a Doppler-shift matrix in the frequency domain, one FFT per
  block instead of ~100 correlations) purely for throughput; this is a
  known, standard passive-radar optimization (Direct/ECA/CAF-via-FFT
  methods in the open literature) and should be a design decision made
  explicitly during implementation, not silently inherited from the
  Octave prototype's brute-force approach.
- `detector.py` — peak-picking/CFAR over the range-Doppler map to emit
  discrete detections. New relative to the reference repo (which stops at
  the image). Fully testable against synthetic CAF output with known
  injected targets (single target, multiple targets, targets near the
  DSI-suppressed zero-Doppler region).
- `illuminator_profile.py` — see §3, fully software/config, no hardware
  dependency.
- `geometry.py`'s math (bistatic range/Doppler equations, given assumed
  transmitter/receiver positions) — testable now with assumed/placeholder
  geometry; only the *real accuracy* of bearing/range depends on real
  hardware and a real deployment site survey.
- `passive_radar_bridge.py` wired end-to-end against
  `SyntheticDualChannelSource` or `RecordedFileDualChannelSource`, posting
  real-format detections to a test/scratch instance of
  `/api/detections/ingest` — this validates the *entire* pipeline and
  integration contract before any hardware exists.

### 2.4 What remains hardware-blocked (task #57's scope, not this task's)

- `DualRTLSDRSource`'s actual implementation (talking to two real RTL-SDRs).
- Real clock synchronization / GPSDO distribution between the two receivers
  — `alignment.py`'s software delay-correction is a *mitigation*, not a
  substitute, for genuine hardware sync; without either, any deployed
  passive radar detection is unvalidated.
- Real-world DSI suppression tuning against this deployment's actual RF
  environment (multipath, actual illuminator signal characteristics at the
  real site) — the LS-projection *algorithm* is portable and buildable now,
  but its tuned parameters (`Index1`/`Index2` shift range, etc.) are
  necessarily site- and hardware-specific and cannot be finalized without
  live captures.
- True bearing/angle-of-arrival accuracy — a single fixed directional
  antenna gives, at best, "target is somewhere in this antenna's beamwidth"
  resolution; genuine bearing requires either a rotator (per the reference
  repo's 0MQ-gated azimuth-scan pattern) or an antenna array, both hardware
  decisions belonging to task #57 (or a follow-on task if a rotator is
  wanted beyond #57's 2nd-SDR-and-GPSDO scope — flag this explicitly to
  whoever scopes #57, since the reference repo's azimuth-scanning pattern
  implies a **third** piece of hardware, a rotator, that isn't mentioned in
  #57's stated 2-SDR-and-GPSDO scope).
- The illuminator feasibility check itself (§3) — confirming what emitter
  is actually usable at the deployment site is a field/RF-survey task, not
  a design or software task, and per this session's established context
  has explicitly **not** been done yet.

---

## 3. Illuminator-agnostic design (do not hardcode DVB-T2)

The reference repo hardcodes DVB-T because that's what its authors had
available in Sendai. This project's actual illuminator has **not been
confirmed** (the DVB-T2/Doordarshan feasibility check for the real
deployment site is explicitly outstanding). The scaffolding must not assume
DVB-T2 survives that check — FM radio, GSM/LTE base stations, DAB, Wi-Fi
APs, and other broadcast/cellular illuminators are all established options
in the passive-radar literature, each with different bandwidth, waveform
periodicity/ambiguity properties, and CAF sidelobe behavior.

Nothing in `alignment.py`, `dsi_suppression.py`, or `caf.py` needs to know
*what* the illuminator is — they operate purely on "two complex baseband
streams tuned to wherever the illuminator's energy is." The only place
illuminator identity matters is:

```python
@dataclass
class IlluminatorProfile:
    name: str                 # e.g. "DVB-T2", "FM_BROADCAST", "GSM_BTS", "DAB"
    center_freq_hz: float
    channel_bandwidth_hz: float
    min_sample_rate_hz: float  # Nyquist-driven floor for this illuminator's bandwidth
    ambiguity_notes: str       # e.g. FM's near-periodic structure creates range ambiguities at multiples of ~ known lag; DVB-T's OFDM structure has different autocorrelation properties -- worth a one-line note per profile so the DSP engineer knows what sidelobe/ambiguity behavior to expect, not a full derivation here
    known_transmitter_locations: list[LatLonAlt]  # populated once the site survey / feasibility check (currently outstanding) confirms a candidate
```

`passive_radar_bridge.py` takes an `IlluminatorProfile` as a config/CLI
argument (analogous to how `hackrf_rx.py` takes a band config) rather than
a hardcoded constant. `geometry.py`'s bistatic equations take transmitter
position from the active profile, not a hardcoded Sendai-DVB-T assumption.
This means: once the DVB-T2/Doordarshan feasibility check lands (or an
alternative illuminator is chosen instead), it's a new `IlluminatorProfile`
entry and a config change, not a pipeline redesign. If the eventual chosen
illuminator has meaningfully different bandwidth than what a single RTL-SDR
(or whatever SDR task #57 selects) can sample, that's a hardware capability
constraint for task #57 to account for, not something this scaffolding
needs to solve today — flagging it here so it isn't lost.

---

## 4. Data flow into the existing detection pipeline

`passive_radar_bridge.py` posts to `/api/detections/ingest` using the
**same request shape and conventions already established this session**
(`DetectionIngestBody` in `backend/server.py`, confirmed by reading its
current fields around line 1240-1310):

- `source`: propose `"PASSIVE_RADAR"` — a new, distinct value (not
  `"HACKRF"`), since this is a structurally different detection mechanism
  (bistatic range-Doppler, not RSSI/energy heuristic or protocol decode).
- `distance_m`: the real bistatic-range-derived estimate from the CAF peak
  lag. **`distance_estimated` should be set `False`** for this source once
  real hardware is live — per the existing field's own doc comment,
  `distance_estimated=True` means "RSSI path-loss model guess," and this is
  a genuine time-of-flight-derived range (same epistemic category the field
  was designed to distinguish), not a path-loss guess. Flag this explicitly
  for the implementing engineer since it's an easy default to get backwards.
- `bearing_deg`: from `geometry.py`, populated only to whatever accuracy
  the current antenna setup supports (see §2.4 — coarse/boresight-only
  until a rotator or array exists); should probably ship with its own
  accuracy caveat surfaced to the frontend, following the precedent of
  `ml_gated`/`validated_against_live_signal` flags already used elsewhere
  in this codebase to honestly communicate confidence caveats.
- `speed_ms`: derivable from the Doppler-bin of the CAF peak
  (`speed = fd * c / (2 * f_illuminator)`, the same relation
  `171210ship/README.md` already documents: `f_D = 2·f_c·v/c`).
- `confidence_type`: propose a **new** value, e.g.
  `"bistatic_radar_detection"`, added to the enum-like set documented in
  `backend/CONFIDENCE_MODEL.md` alongside the existing
  `heuristic_binary`/`ml_probability`/`protocol_verified`/`advisory_only`/
  `unclassified_signal` values — a CFAR-thresholded CAF peak is its own
  distinct epistemic category (a real physical-layer detection statistic,
  not an RSSI heuristic, not an ML softmax, not a protocol decode), and
  forcing it into an existing bucket would misrepresent what kind of
  confidence it is, contradicting the documented purpose of that field.
  This requires a small `backend/server.py` change (new accepted enum
  value + doc line in `CONFIDENCE_MODEL.md`) — flagged here as part of the
  handoff, not made here.
- `protocol_confirmed`: `False` — passive radar detects *something moving*,
  it does not decode any protocol.
- `rssi_dbm`/`snr_db`: repurpose as CAF peak SNR (peak-to-sidelobe or
  peak-to-noise-floor ratio in the range-Doppler map) rather than leaving
  them at defaults — gives the frontend/operator a real quality signal
  using an existing field rather than requiring a new one.
- `model`: something like `"passive-bistatic-radar-caf-v1"` per existing
  convention of tagging the specific method/checkpoint in this field.

No changes to `detection_ingest`'s merge-by-(`source`,time-window) logic
are needed architecturally — `"PASSIVE_RADAR"` as a new `source` value will
naturally not merge with existing `HACKRF`/`SIK_RF_HEURISTIC` detections,
which is correct: a passive-radar detection and an RF-energy-heuristic
detection of the same physical drone are two independent evidentiary
signals, not duplicates, exactly like ml_classify_bridge.py is designed to
layer onto (not replace) hackrf_rx.py's own detections. Whether/how to
later correlate a passive-radar track with a co-located HACKRF/ML detection
of the same physical target (sensor fusion) is a real, valuable next
question but is out of scope for this scaffolding task — flagging it for a
future task rather than solving it here.

---

## 5. Concrete handoff spec (for the implementing DSP engineer)

1. Implement `field-bridge/passive_radar/channel_source.py`:
   `DualChannelSource` ABC + `SyntheticDualChannelSource` (port of
   `simulation.m`) + `RecordedFileDualChannelSource` (reads split or
   interleaved I/Q files, parameterized dtype like `goship.m`'s
   `datatype`). Leave `DualRTLSDRSource` as a stub raising
   `NotImplementedError`.
2. Implement `alignment.py`: cross-correlation-based inter-channel delay
   estimate + slice (direct port of `goship.m`'s bus-delay logic, lines
   computing `xc=abs(xcorr(ref,mes))` and the subsequent `pos`-based
   slicing). Unit test: synthetic source with known injected delay,
   assert estimated delay matches within 1 sample.
3. Implement `dsi_suppression.py`: LS-projection removal (direct port of
   `goship.m`'s `dsi_suppression` block, the `X1`/`pinv` least-squares
   step). Unit test: assert DSI/zero-Doppler energy is suppressed by at
   least N dB relative to un-suppressed baseline on synthetic data.
4. Implement `caf.py`: range-Doppler map computation. Start as a direct,
   readable port of `goship.m`'s Doppler-bin loop for correctness
   validation, then evaluate an FFT-batched reimplementation for
   throughput (see §2.3's note on brute-force vs FFT-batch CAF) once
   correctness is established — do not skip straight to the optimized
   version without a correctness baseline to test it against. Unit test:
   synthetic source with known injected (delay, Doppler) pair, assert the
   CAF peak lands in the corresponding (range-lag, Doppler) bin within
   tolerance. **Stretch validation**: replay the real
   `171210ship_ch1/ch2.sigmf-data` dataset (from iqengine.org, cited in
   the reference repo's own README) through this and sanity-check the
   result resembles the reference repo's own published before/after DSI
   figures.
5. Implement `detector.py`: CFAR or simple threshold peak-picking over the
   CAF output -> list of `(range_m, doppler_hz, snr_db)` detections. New
   design relative to the reference repo (which stops at the image); keep
   it simple (cell-averaging CFAR or top-K peak-picking with SNR
   threshold) rather than over-engineering a tracker here — track
   association across successive CAF frames, if wanted, is a reasonable
   future extension, not part of this handoff.
6. Implement `geometry.py`: bistatic range/Doppler-to-speed equations
   (`f_D = 2·f_c·v/c`, per `171210ship/README.md`) and a placeholder
   bearing model (boresight of the surveillance antenna, with an explicit
   accuracy caveat field) given an `IlluminatorProfile`'s transmitter
   position and an assumed/config'd receiver position.
7. Implement `illuminator_profile.py`: `IlluminatorProfile` dataclass (see
   §3) + at least a `DVB_T2_PLACEHOLDER` and an `FM_BROADCAST_PLACEHOLDER`
   profile to prove the abstraction isn't secretly DVB-T2-only — do not
   wait for the site survey to build this.
8. Implement `passive_radar_bridge.py`: CLI script analogous to
   `hackrf_rx.py`'s structure — takes a `DualChannelSource` implementation
   and an `IlluminatorProfile` as config, runs
   alignment -> dsi_suppression -> caf -> detector -> geometry, and POSTs
   to `/api/detections/ingest` with the field mapping in §4. Should support
   `--source synthetic` and `--source recorded-file <path>` end-to-end now;
   `--source rtlsdr-dual` should exist as a documented-but-`NotImplementedError`
   option, not be silently omitted, so the integration point is visible in
   the CLI surface today.
9. Backend change (small, flagged not made here): add
   `"bistatic_radar_detection"` to the `confidence_type` values documented
   in `backend/CONFIDENCE_MODEL.md`, and confirm `DetectionIngestBody`
   already accepts `source="PASSIVE_RADAR"` as a free-form string (it does
   today, `source: str` has no enum constraint at the Pydantic level per
   the current model) — verify no downstream code (frontend detection
   rendering, `_heuristic_display`/`_ml_unclassified_display` dispatch)
   assumes a fixed/known `source` set before shipping.
10. Do not implement `DualRTLSDRSource` or attempt real dual-RTL-SDR
    acquisition — that is task #57.

### Explicit test plan without hardware
- Unit tests (steps 2-6) against synthetic data with known ground truth.
- Integration test: `passive_radar_bridge.py --source synthetic` running
  end-to-end, posting to a scratch/test instance of the backend (per this
  project's standing rule to never write test detections into a real/demo
  tenant), and confirming the detection appears correctly rendered
  (including the `bearing accuracy caveat` and `bistatic_radar_detection`
  confidence type) in the frontend.
- Optional but valuable: replay real recorded IQ from the reference repo's
  own cited public dataset for an even stronger non-synthetic correctness
  check before any local hardware exists.

---

## 6. Summary: buildable now vs. hardware-blocked

**Buildable and testable now (software-only):**
alignment.py, dsi_suppression.py, caf.py (the actual CAF/Doppler chain),
detector.py, geometry.py's math, illuminator_profile.py,
SyntheticDualChannelSource, RecordedFileDualChannelSource,
passive_radar_bridge.py wired end-to-end against those two sources,
the `backend/server.py`/`CONFIDENCE_MODEL.md` additions in §4/§5.9,
all unit + integration tests described above.

**Hardware-blocked (task #57, or a rotator/array task beyond #57's stated
scope — flag this gap explicitly):**
DualRTLSDRSource's real implementation, real inter-receiver clock sync
(GPSDO distribution), site-tuned DSI suppression parameters, real bearing
accuracy beyond antenna-boresight, and the DVB-T2/Doordarshan (or
alternative) illuminator feasibility confirmation itself.
