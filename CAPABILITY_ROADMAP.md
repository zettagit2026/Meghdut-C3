# MEGHDUT C3 — Capability Development Action Plan

Counter-UAS / CEMA capability roadmap for PMO Suraj (Indian Army CEMA), mapping
Army RFI/objective requirements to what is **actually built in this codebase**
vs. what remains to build, phased against the **actual migration target box**.

### Ground-truth hardware: Lenovo ST550 (the box we are migrating to)

Pre-used rental. **Dual Xeon Octa @ 2.10 GHz** (many cores, **low single-thread
clock**), **32 GB ECC**, **3×1.2 TB SAS HDD (spinning) + RAID**, **USB only**,
**no GPU**, **single HackRF (1-SDR) for now**, **free PCIe slots
(B-upgradable)**.

Classify this as **Tier-A-class, NOT Tier B**, and note where it is *below* even
the `SERVER_SPECIFICATION.md` Tier-A recommendation:

| Spec dimension | Tier-A recommendation | ST550 reality | Consequence |
|---|---|---|---|
| Single-thread CPU | ≥3.5 GHz, strong boost | **2.10 GHz** | Backend is single-worker-bound → **the low clock directly caps backend throughput**; makes the shared-state refactor (below) *more* urgent, not less |
| RAM | 32 GB (min 16) | 32 GB ✓ | OK for today; **blocks 40–50 concurrent targets** (that needs 64 GB) |
| Primary disk | 1 TB **NVMe** | **spinning SAS HDD + RAID** | Slower Mongo/Docker random I/O and IQ-capture firehose; watch disk-full (the recurring pain point); retention + split volumes matter more on spinning disk |
| GPU | optional (recommended) | **none** | Blocks multi-domain EO/thermal/acoustic ML + RF-fingerprint/SEI at scale; CPU ML inference still works for today's gated bursts |
| 2nd SDR + GPSDO | roadmap | **not present** | Blocks passive bistatic radar + real DF bearing |
| PCIe expansion | ≥1 free x8/x16 | **free slots present** | **Good news: B-upgradable** — GPU, FPGA (OB-06), and USB 2nd-SDR can all be added later |

**Net:** the ST550 runs everything in the "BUILT" and "software-only" columns
today. Every `HW-gated` item below is now tagged with **the specific add-on the
ST550 still needs** — because the box itself will not provide it until upgraded.

**Authoring discipline (honesty rule):** every "BUILT" claim below is grounded
in a named file that was read for this plan. A **scope/architecture document is
never counted as a shipped capability** — those are marked
`DESIGNED-not-built`. Software that implements a real algorithm but cannot
produce a *field-valid* result until hardware arrives is marked
`BUILT (sw) / HW-gated` with the exact gating hardware named.

> **Important cross-document correction surfaced by this audit:**
> `RFI_RESPONSE_DRAFT.md` is now **stale in the capability-forward direction**.
> Several items it describes as "roadmapped but not yet built" have since been
> built in software. The clearest example: **RFI 1.11 (RC-style manoeuvring)**
> — the RFI says *"roadmapped but not yet built in this codebase"*, but
> `field-bridge/mavlink_takeover.py` (244 lines) + payload **PL-011 MANEUVER
> TAKEOVER** now implement sustained `RC_CHANNELS_OVERRIDE` takeover. The RFI
> draft should be reconciled before submission. This roadmap reflects the
> **code**, not the stale draft.

---

## 1. Capability Ledger

Status legend:
- **BUILT** — implemented in code and (for non-RF-TX items) usable now.
- **BUILT (sw) / HW-gated** — real algorithm implemented and unit-tested, but a
  *field-valid* result is blocked on named hardware.
- **DESIGNED-not-built** — a scope/architecture doc exists; **no working
  implementation**. (Or: a STUB placeholder file exists.)
- **PARTIAL** — implemented for one class/band/mode, not the full ask.
- **NOT-STARTED** — no code, no design doc.

### 1a. Attack / effect capabilities (RFI items + OB IDs)

| Req ID | Capability | Status | File evidence | "Done" means |
|---|---|---|---|---|
| RFI 1.1 / 1.3 | MAVLink malicious-code / command injection (RTH, land, disarm, flight-termination, mode/home spoof) | **BUILT** | `backend/mavlink_codec.py`, `backend/payload_library.py` PL-001..PL-010 | Byte-accurate v1/v2 COMMAND_LONG frames accepted by an unsigned-MAVLink FC on the matching link |
| OB-01 | Bulk / broadcast takedown | **BUILT** | `mavlink_codec.broadcast_takedown()`, PL-010; `DOC_CORRECTIONS_MEMO.md` §1 confirms gap-doc is stale | `target_system=0` broadcast reaches all listeners |
| OB-03 / RFI 1.5 / 1.6 | Physical-parameter exploitation (PROPELLER STOP, MEMORY ERASE, AUTOPILOT REBOOT, HOME SPOOF, PARAM/STORAGE) | **BUILT** | PL-005/006/007/008/009 in `payload_library.py` | Real MAVLink frames for each effect, gated + supervised-range only |
| **RFI 1.11 / 1.12** | RC-style manoeuvring / controlled-landing takeover | **BUILT** (RFI draft says "not built" — STALE) | `field-bridge/mavlink_takeover.py`, PL-011 sustained `RC_CHANNELS_OVERRIDE`, `sustained` flag in `payload_library.py` | Sustained override walks an unsigned craft to controlled landing; bounded 30 s + abort-per-frame |
| RFI 1.4 | Comms disruption / jamming (link denial) | **BUILT (sw) / HW-gated** | `field-bridge/hackrf_jam.py`, `jam_bridge.py`, `/payloads/jam` + `/jam/confirm` gates in `server.py` | Band-limited TX forces link-loss failsafe; effective power is HW-gated (100 mW–10 W actual vs 100 W spec — `DOC_CORRECTIONS_MEMO.md` §2) |
| RFI 1.7 | Telemetry spoofing / suppression (false GCS picture) | **BUILT** | `payload_library.py` (SET_MESSAGE_INTERVAL suppression), `mavlink_codec.py` | Spoofed/suppressed telemetry stream, safest live demo |
| OB-02 / SOL-02 | Prioritised engagement planner (controller-first, human-authorised) | **BUILT** | `backend/engagement_planner.py` (428 ln); pure proposal, no TX path | Ranked `PROPOSED_REQUIRES_HUMAN_AUTHORIZATION` plan; controller node first |
| RFI 1.13 | GNSS "soft-kill" spoof (deception, not denial) | **DESIGNED-not-built** (safety plumbing BUILT; DSP is a STUB) | Plumbing: `gnss_spoof_bridge.py`, `/gnss-spoof/*` gates. **DSP core `gnss_signal_synth.py` is an explicit STUB** (raises `GnssSynthNotImplemented`) | L1 C/A Gold codes + fabricated NAV synth producing valid IQ — **not written** |
| RFI 1.13 | Effectiveness vs encrypted links (DJI OcuSync / signed MAVLink) | **DESIGNED-not-built** (verdict: injection infeasible/unproven) | `NON_MAVLINK_EXPLOITATION_HANDOFF.md` — ELRS infeasible (bind-UID PRNG), OcuSync unproven, Herelink unstudied | Honest answer today = detect + disrupt only, not inject |
| RFI 1.9 | Broadcast vs targeted injection modes | **BUILT** | `broadcast_takedown()` + targeted `target_system`/`target_component` | Both modes selectable |

### 1b. Detection / sensing / ISR capabilities

| Req ID | Capability | Status | File evidence | "Done" means |
|---|---|---|---|---|
| RFI 1.18 | Passive RF energy detection + spectral fingerprint | **BUILT** | `field-bridge/hackrf_rx.py` (real `hackrf_sweep` subprocess, not simulated), `rf_features.py` | Live sweep → feature extraction → `/detections/ingest` |
| P0 (PRD) | Real SDR ingest (not simulated) | **BUILT** | `hackrf_rx.py` shells real `hackrf_sweep`; `iq_capture.py` real IQ | Real device data flowing (PRD listed this as P0 backlog — now done) |
| — | RF ML classifier (ResNet-18 on IQ) | **BUILT (sw) / HW-gated (GPU optional)** | `ml_classify_bridge.py`, `gamutrf_infer.py`, `ml_calibration.py` (OOD/entropy gating) | CPU inference works today; GPU only for scale |
| — | Protocol decode/parse fleet (DroneID, CRSF, FrSky, FlySky, Spektrum, HoTT, MSP, DroneCAN, CANopen, LTM, ADS-B, RemoteID, Parrot) | **BUILT** | ~20 parser files in `field-bridge/` + tests | Each decodes real framing; DroneID verified vs bundled captures only |
| OB-04 | Multi-target track manager (concurrent tracks, N-of-M confirm, coast/death) | **BUILT (sw) / HW-gated for 40–50** | `backend/track_manager.py` (569 ln), tuned to 32 | Persistent tracks now; 40–50 needs 64 GB / faster cores (Tier B) |
| OB-02 | Swarm classifier (Type I–IV taxonomy, controller candidate) | **BUILT (sw) / partially-gated** | `backend/swarm_classifier.py` (344 ln) | Temporal-correlation clustering works; DoA-based clustering blocked (no bearing — see below) |
| Req d / #43 / C10 | Passive bistatic radar (CAF/Doppler, DSI suppression, CFAR detector) | **BUILT (sw) / HW-gated** | `field-bridge/passive_radar/` (caf.py, dsi_suppression.py, alignment.py, detector.py, geometry.py, channel_source.py) + tests; `passive_radar_bridge.py` | Full CAF chain runs on synthetic/recorded IQ **now**; real detection blocked on **2nd SDR + GPSDO** (Tier B) |
| OB-05 / task #20 | Direction finding / bearing (amplitude-comparison) | **BUILT (sw) / HW-gated** | `field-bridge/direction_finding.py` (319 ln) — real monopulse math, honest `available=False` on <2 antennas | Real bearing blocked on **2nd HackRF + 2 matched directional antennas + calibration pass** |
| #83 | Camera / thermal / acoustic (non-RF, EMCON drones) | **DESIGNED-not-built** (scaffold only, no model) | `thermal_bridge.py` scaffold (no trained model, no camera), `multidomain_fusion.py` (unplugged), `train_thermal_detector.py`; `CAMERA_THERMAL_ACOUSTIC_SCOPE.md` | Needs sensor HW + labeled dataset + trained model + field validation |

### 1c. Platform / acceleration / delivery

| Req ID | Capability | Status | File evidence | "Done" means |
|---|---|---|---|---|
| **OB-06** (Army-CRITICAL) | FPGA acceleration backbone | **DESIGNED-not-built** | `FPGA_ACCELERATION_SCOPE.md` — no HDL, no board; GPU interim path identified | Validated FPGA CAF/channelizer core; needs PCIe slot (Tier B) + HDL workstream |
| OB-05 | Coherent multi-channel exciter / TX beamforming | **DESIGNED-not-built** | `ANTENNA_ARRAY_EXCITER_SCOPE.md` — phase-coherence work not started | Needs USRP N310/X410 + OctoClock (or multi-HackRF CLKIN) + calibration |
| RFI 1.2 / 1.8 | Multi-band / simultaneous multi-protocol | **PARTIAL** | Demonstrated 915/433 MHz (`hackrf_rx.py` bands); `DOC_CORRECTIONS_MEMO.md` §2: 400 MHz–6 GHz *claimed* vs 915/433 *demonstrated* | Simultaneous SiK+2.4+5.8 GHz needs multiple front-ends / wideband SDR (HW) |
| RFI 1.14 | 20 km power/range characterization | **NOT-STARTED** (correctly deferred) | RFI 1.14 marks it a field-trial deliverable; not desk-estimated | Calibrated field-trial number at STEAG |
| P2 (PRD) | PDF mission report (leave-behind) | **BUILT** | `server.py:4732 /report/mission.pdf` (reportlab) | Evaluator leave-behind PDF |
| P2 (PRD) | Role separation | **PARTIAL** | `server.py` two-role RBAC (operator/commander); no separate Analyst | Analyst/Operator/Commander three-role split not done |
| — | Safety-gate spine (arm/confirm tokens, range-auth lease, IFF, tx-halt, hash-chained audit) | **BUILT** | `server.py` gate chain, `RANGE_AUTHORIZATION_REDESIGN.md`, `TX_HALT_PERSISTENCE_SCOPE.md`, `iff_*` | Every RF-TX effect is human-in-loop + audited |

---

## 2. Gap Analysis — claim vs. reality, software vs. hardware

### 2a. Where the RFI response and code diverge (honest delta)

1. **RFI understates the build (1.11).** The draft says RC manoeuvring is *not
   built*; `mavlink_takeover.py` + PL-011 build it. **Action: update the RFI
   draft up.** (Do not, however, upgrade the demo claim past what's been
   *tested against a real craft* — the takeover is verified in code/tests, not
   against a live airframe.)

2. **RFI/compliance docs overstate power, range, band, bandwidth.**
   `DOC_CORRECTIONS_MEMO.md` §2 catalogs four unreconciled range figures
   (3/5/7/20 km vs ~1–2 km demonstrated), 100 W vs 100 mW–10 W actual jamming,
   62 MHz vs ~20 MHz HackRF bandwidth, 400 MHz–6 GHz vs 915/433 demonstrated.
   These are **hardware-gated**, not software bugs. The RFI 1.14 answer already
   handles this honestly (defer to field trial) — the *other* docs must be
   brought into line with it.

3. **The FHSS pairing caveat (real logic gap).** `DOC_CORRECTIONS_MEMO.md` §4:
   SiK injection only works against a **pre-paired reference craft**, not an
   arbitrary never-paired adversary drone. This is a genuine operational
   ceiling that `CEMA_Compliance_v1.4.docx`'s unconditional "Yes" hides.
   Non-MAVLink handoff confirms ELRS is structurally infeasible to inject
   passively.

4. **"Encrypted link" answer is honest and should stay honest (1.13).** Against
   OcuSync/signed-MAVLink the delivered capability is **detect + disrupt**, not
   inject. `NON_MAVLINK_EXPLOITATION_HANDOFF.md` is explicit; do not let a
   demo imply injection here.

5. **GNSS spoof is half-built and easy to overclaim.** Safety plumbing is
   real and QA-passed; the **DSP synth is a STUB**. The spoof cannot fire a
   real fabricated fix today. Additionally the single-PRN caveat
   (`GNSS_SIGNAL_SYNTH_HANDOFF.md` §4) means even once built it likely needs
   **jam-then-spoof** (multi-PRN or overpower) to actually move a fix.

### 2b. Software-only vs. hardware-gated — against the actual ST550

The ST550 runs everything in the first rows below **today, as delivered**. Every
remaining row names **the specific add-on the ST550 still needs** — the box has
free PCIe slots and spare USB, so each is a bolt-on, not a re-buy.

| Gap | Software-only? | Runs on ST550 as-is? | Add-on the ST550 still needs |
|---|---|---|---|
| GNSS synth DSP (`gnss_signal_synth.py`) | **Yes** (pure DSP) | **Yes** (uses the 1 HackRF TX) | none |
| 3-role RBAC | **Yes** | **Yes** | none |
| Direction-finding wiring / synthetic test | **Yes** | **Yes** | none (real bearing needs antennas below) |
| Passive-radar validation on recorded IQ | **Yes** | **Yes** | none (real detection needs 2nd SDR below) |
| Multi-target 40–50 concurrent | **Yes** (track logic exists) | **Partly** | **64 GB RAM** (has 32) **+** shared-state refactor — see clock note |
| Direction finding real bearing | Algorithm done | No | **2nd HackRF (USB) + 2 matched directional antennas + calibration** |
| Passive radar real detection | Algorithm done | No | **2nd SDR + GPSDO** (USB) |
| Multi-band simultaneous (1.2/1.8) | Partly | No | **additional SDR front-ends / wideband SDR** (USB or PCIe) |
| Multi-domain EO/thermal/acoustic ML, RF-fingerprint/SEI | Software + data + sensor | No | **GPU (PCIe)** + sensors + edge compute + labeled data |
| FPGA accel — OB-06 (Army-CRITICAL) | **No** (HDL workstream) | No | **PCIe FPGA/RFSoC card** + Vivado/Vitis + **FPGA/RTL engineer** |
| TX beamforming — OB-05 | **No** | No | **USRP N310/X410 + OctoClock** + phase-cal + VNA |
| 20 km range char (1.14) | **No** | No | calibrated field trial @ STEAG (depends on beamforming/power HW) |

**The 2.10 GHz single-thread ceiling changes the 40–50-target calculus.** The
backend is single-worker by design (in-memory safety-gate state can't be shared
across workers — `SERVER_SPECIFICATION.md`). On a *fast* core that one worker
copes; on the ST550's **slow 2.10 GHz core it will not**, and the dual-Octa core
count is useless to it until the safety-state moves to a shared store. So
"40–50 concurrent targets" is **both** a RAM problem (32→64 GB) **and** a
software problem (shared-state refactor), and the **low clock makes the software
refactor the harder-gating half** — it is the only way to put the box's many
cores to work. Do the refactor (Phase 1) *before* buying RAM.

**One-line frame:** the software is far ahead of the hardware, and the ST550 is
**Tier-A-class but PCIe-upgradable**. It fully runs the BUILT + software-only
column now. The pacing levers are all bolt-ons: a **2nd SDR/GPSDO** (passive
radar + DF), a **GPU** (multi-domain ML), and — the long pole — a **PCIe FPGA
card + an FPGA/RTL engineer** (OB-06).

---

## 3. Phased Action Plan

Every RF-TX work item carries the project convention gate: **human-in-loop +
adversarial (third-eye) review + independent verifier** before field use.
Effort: **S** ≤ few days · **M** ~1–2 weeks · **L** multi-week/multi-person.

### Phase 0 — Immediately post-migration on the ST550 (runs as-is)

| # | Work item | Depends on | Effort | Gate | Build → Review agents |
|---|---|---|---|---|---|
| 0.1 | Migration mechanics: restore Mongo dump, Caddy CA, `.env`, udev rules, rebuild Docker, re-run `deploy.sh` | ST550 ready | S | verifier (post-restore smoke) | DevOps Automator → verifier |
| 0.2 | Reconcile `RFI_RESPONSE_DRAFT.md` + compliance docs (1.11 up; range/power/band/bandwidth down to honest figures; FHSS + encrypted caveats in) | none | S | code-reviewer (doc-vs-code) | Technical Writer → critic |
| 0.3 | Re-run full field-bridge + backend test suite on new hardware; confirm real `hackrf_sweep` ingest live | 0.1 | S | verifier | test-engineer → verifier |
| 0.4 | Split USB topology per `lsusb -t`; give HackRF its own root hub; add spinning-disk mitigations (Mongo retention + split IQ-capture volume off OS/DB disk) | ST550 USB + HDD | S | verifier | DevOps Automator |
| 0.5 | Reconcile ungoverned break-glass TX CLIs (`sik_mavlink_bridge.py` + `hackrf_jam.py`) — retire or bring under tx-halt + live-lease gate; correct the SESSION_HANDOFF invariant | security decision (retire vs govern) | S (retire) / M (govern) | RF-TX: human-in-loop + adversarial + verifier (already reviewed x2) | security-reviewer (done x2) → executor → verifier |

Note: two independent security reviews CONFIRMED the "every RF-transmit path is gated"
invariant is currently false — both `sik_mavlink_bridge.py` and `hackrf_jam.py` transmit
real RF outside the spine via the same gap (static env var + confirm flag + prompt, no
tx-halt/lease/arm-token/IFF). This is one gap class across two files, not two unrelated
findings — treat 0.5 as a single reconciliation, not two separate patches.

### Phase 1 — Software-only capability closure (runs on ST550, no add-ons)

| # | Work item | Depends on | Effort | Gate | Build → Review agents |
|---|---|---|---|---|---|
| 1.1 | **GNSS synth DSP** — implement `gnss_signal_synth.py` (Gold codes, NAV subframes 1–3, BPSK IQ) per `GNSS_SIGNAL_SYNTH_HANDOFF.md`; wire jam-then-spoof | HackRF TX | **L** | **RF-TX: human-in-loop + adversarial + verifier**; PRN/NAV round-trip self-test | AI Engineer / Embedded Firmware Engineer → security-reviewer → verifier |
| 1.2 | 3-role RBAC (Analyst/Operator/Commander) | none | S | code-reviewer | Backend Architect → code-reviewer |
| 1.3 | Multi-target scale prep: move in-memory safety-gate state to shared store so backend can use >1 worker (unlocks 40–50 without waiting on cores) | none | M | adversarial (safety-state race) + verifier | Backend Architect → critic → verifier |
| 1.4 | DF bridge `df_amplitude_bridge.py` skeleton wired to `direction_finding.py` (runs today on synthetic; ready for antennas) | `direction_finding.py` | S | verifier | executor → verifier |
| 1.5 | Passive-radar validation against real recorded IQ (171210ship dataset) to prove CAF chain pre-hardware | `passive_radar/` | S | verifier | scientist → verifier |
| 1.6 | GPU-accelerate CAF (`caf_fft_batched` → cupy/torch) — OB-06 Phase-1 interim. **ST550 has no GPU**, so this moves to Phase 2 unless a PCIe GPU is added | PCIe GPU add-on | M | verifier (bit-accuracy vs `caf_bruteforce`) | AI Engineer → verifier |

### Phase 2 — ST550 + a bolt-on (names the add-on per item)

| # | Work item | Depends on | Effort | Gate | Build → Review agents |
|---|---|---|---|---|---|
| 2.1 | **DF real bearing** — 2nd-HackRF concurrent sweep + calibration table | **ADD: 2nd HackRF (USB) + 2 directional antennas** | M | verifier + field cal sign-off | executor → verifier |
| 2.2 | **Passive radar real detection** — `DualRTLSDRSource`, GPSDO sync, site DSI tuning, illuminator feasibility | **ADD: 2nd SDR + GPSDO (USB)** | **L** | verifier + field | AI Engineer / scientist → verifier |
| 2.3 | Multi-target 40–50 concurrent (retune from 32) | **ADD: 64 GB RAM** + 1.3 shared-state done | M | load test + verifier | Backend Architect → Performance Benchmarker → verifier |
| 2.4 | Multi-band simultaneous (1.2/1.8) | **ADD: extra SDR front-ends / wideband SDR** | M | RF-TX gate + verifier | Embedded Firmware Engineer → security-reviewer |
| 2.5 | Thermal detection spike (sensor + model + `thermal_bridge.py` fill-in) | **ADD: GPU (PCIe) + thermal core + edge compute + dataset** | **L** | field FP-rate validation + verifier | AI Engineer → verifier |

### Phase 3 — Long-lead flagship / field-trial (PCIe add-ons + hiring)

| # | Work item | Depends on | Effort | Gate | Build → Review agents |
|---|---|---|---|---|---|
| 3.1 | **OB-06 FPGA backbone (Army-CRITICAL)** — procure RFSoC/USRP, HDL CAF/channelizer core vs `caf_bruteforce` reference, PCIe host interface | **ADD: PCIe FPGA/RFSoC card** (ST550 slot free) + **FPGA/RTL engineer** | **L (multi-month)** | quantization/bit-accuracy validation + verifier | FPGA/RTL specialist (hire/contract) → critic → verifier |
| 3.2 | **OB-05 TX beamforming** — USRP N310/X410 + OctoClock, phase-cal, beamform exciter | **ADD: array HW + USRP + OctoClock + VNA** | **L** | RF-metrology + RF-TX gate + verifier | Embedded Firmware Engineer → security-reviewer → verifier |
| 3.3 | **RFI 1.14 — 20 km range characterization** — calibrated field trial at STEAG | beamforming/power HW (3.2) | M (field campaign) | live-fire authorization + field | Sales Engineer / field team → verifier |
| 3.4 | Multi-domain fusion live wiring (`multidomain_fusion.py`) once ≥2 sensors + correlation key exist | 2.5 + bearing (2.1) | M | verifier | AI Engineer → verifier |

---

## 4. Sequencing Rationale

1. **Phase 0 first because honesty is the cheapest risk-reducer.** The largest
   *near-term* exposure is not a missing capability — it is the **RFI/compliance
   docs disagreeing with the code and with each other** (`DOC_CORRECTIONS_MEMO.md`).
   Reconciling docs (0.2) costs days and prevents an evaluator catching an
   overclaim (100 W, 20 km, 400 MHz–6 GHz) that would taint everything else.
   Migration mechanics (0.1/0.3) gate *all* later work.

2. **Phase 1 mines already-written software before spending on hardware.** GNSS
   synth (1.1), DF wiring (1.4), passive-radar recorded-IQ validation (1.5), and
   the shared-state refactor (1.3) all deliver on the **ST550 as delivered** —
   no add-on needed. They convert `DESIGNED`/`sw-only` items into demonstrable
   ones at software cost — the best ROI in the plan. 1.3 also *pre-clears* the
   40–50 target bottleneck, and on the ST550's 2.10 GHz core it is doubly
   important (it is the only way the dual-Octa core count ever helps the
   single-worker backend) — so on this box the RAM buy is pointless until 1.3 is
   done.

3. **OB-06 FPGA is Army-CRITICAL but correctly last (Phase 3).** It is flagged
   Army-CRITICAL, yet `FPGA_ACCELERATION_SCOPE.md` is clear it is a **multi-month
   HDL workstream needing a skill the team does not yet have** plus a PCIe FPGA
   card. Good news on the ST550: **the PCIe slot is free**, so the hardware side
   is a bolt-on, not a re-buy — the pole is the board procurement + hiring an
   FPGA/RTL engineer, not chassis capacity. The GPU CAF interim (1.6) **cannot
   run on the ST550 today** (no GPU) — it needs its own PCIe GPU add-on — and
   the Army must confirm whether OB-06's "FPGA" wording is about throughput (GPU
   substitutes) or deterministic-latency/SWaP-C/certification (GPU does **not**
   substitute — scope doc §4). That answer decides whether a GPU is even worth
   adding.

4. **Passive radar + DF + beamforming cluster around the 2nd SDR/GPSDO + array
   purchase.** One USB/PCIe SDR investment unlocks 2.1, 2.2, and feeds 3.2 — so
   they are sequenced immediately after that procurement lands, maximizing return
   on a single buy (`ANTENNA_ARRAY_EXCITER_SCOPE.md` §1: OB-05 uniquely closes
   *both* range and bearing gaps). The ST550's USB ports take the 2nd SDR; the
   USRP array is the later PCIe/networked add.

5. **#56 man-portable shapes the FPGA-vs-GPU call.** If the target migrates to a
   ruggedized field unit (`SERVER_SPECIFICATION.md` field option), stay
   **x86-64 + Ubuntu 24.04** (a Jetson/ARM port is a separate effort) and the
   SWaP-C argument makes OB-06 FPGA *more* likely mandatory (favoring the FPGA
   card over a power-hungry GPU). Note the ST550 is a **tower/rack** box, so it
   is a lab/fixed-site host, not itself the man-portable unit — confirm whether
   the fielded form factor is a separate build.

6. **1.14 (20 km) is deliberately the last deliverable** because a *real* number
   depends on the beamforming/power hardware (3.2) that produces the EIRP to
   characterize. Any earlier number is a desk guess the RFI already, correctly,
   refuses to give.

---

## 4b. Candidate next-demo capabilities — ranked options (you pick)

You have **not** chosen a demo target yet. Below is a decision-ready shortlist,
ranked by our recommendation, each with what it proves, its hardware/safety
dependency **on the ST550**, and the honest catch. **Pick one and we front-load
it in Phase 1.**

| Rank | Candidate demo | Proves to the evaluator | Runs on ST550? | Effort to demo-ready | Safety profile | Honest catch |
|---|---|---|---|---|---|---|
| **1 (safest, ready now)** | **Telemetry spoof / suppression (RFI 1.7)** + kill-chain console | Live CEMA effect on the GCS picture end-to-end, on the real UI | **Yes, today** | **S** — BUILT | Lowest — no airframe risk (RFI 1.7 is explicitly the safest class) | Least "wow"; it degrades the picture, doesn't take the aircraft |
| **2 (highest impact, ready now)** | **RC-manoeuvre / controlled-landing takeover (RFI 1.11, PL-011)** | Actual takeover — walks an unsigned craft to a controlled landing; the capability the RFI draft wrongly says isn't built | **Yes, single HackRF** | **S–M** — BUILT in code; needs a **supervised reference craft** + live-fire authorization | **High** — RF-TX + physical effect; full gate chain + human-in-loop + adversarial + verifier; supervised range only (STEAG) | Verified in code/tests, **not yet against a live airframe**; works only on unsigned/pre-paired MAVLink (`DOC_CORRECTIONS_MEMO.md` §4) — don't demo against an arbitrary adversary drone |
| **3 (detection story, near-ready)** | **Multi-band passive detect + classify (RFI 1.2/1.8/1.18)** | Real `hackrf_sweep` detection + protocol-parse + track/kill-chain across bands | **Partly** — 915/433 today on the 1 SDR; *simultaneous* multi-band needs a 2nd front-end | **S** for sequential (demoable now); **M + ADD SDR** for true simultaneous | Passive RX only — **no TX, zero regulatory footprint** (RFI 1.16 detect-only variant) | "Simultaneous multi-band" is the overclaim to avoid — demo it as **prioritized sequential** unless a 2nd SDR is added |
| **4 (flagship, not demoable yet)** | **FPGA acceleration (OB-06, Army-CRITICAL)** | The Army's flagged-CRITICAL backbone | **No** | **L (multi-month)** — DESIGNED-not-built | RF-adjacent; validation-heavy | No HDL, no board, no FPGA engineer yet; a demo here is **months out** — set expectation, don't promise it early |
| **5 (field-trial, last)** | **20 km range characterization (RFI 1.14)** | A real, calibrated range number | **No** | **M field campaign** — NOT-STARTED | Live-fire, highest authorization bar | Depends on beamforming/power HW (3.2); any pre-trial number is a guess. This is a **deliverable**, not a bench demo |

**Our recommendation if you want one demo:** lead with **#2 (RC-manoeuvre
takeover)** as the headline capability *because it is genuinely built and is the
most convincing counter-UAS effect*, backstopped by **#1 (telemetry spoof)** as
the zero-risk warm-up on the same console. Both run on the ST550 with the single
HackRF. Reserve #3 as the "and it also detects" segment (sequential, not
simultaneous). Explicitly frame #4/#5 as **roadmap**, not demo — that honesty is
itself a credibility asset with a technical evaluation team.

---

## 5. Decisions / Inputs Needed From You

These are business/programme facts and hardware choices this plan cannot invent.
Placeholders left in the RFI are marked.

**Business / RFI placeholders (block submission):**
- **RFI 1.14** — real operational range target and the STEAG field-trial number
  (currently 4 unreconciled figures; only ~1–2 km demonstrated).
- **RFI 1.17** — delivery timeline post-contract (regressed from "~11 months" to
  "[TO BE COMPLETED]" — pick one).
- **RFI 1.19** — indigenization position (hardware sourcing/manufacture vs
  DoT/MoD requirements).
- **RFI 1.20 / 1.21 / 1.22** — support/SLA model, recommended capability
  category, OEM status.

**Hardware add-on decisions (ST550 is fixed; these are the bolt-ons to authorize):**
- **Which add-on first?** Ranked by capability unlocked: (a) **2nd SDR + GPSDO
  (USB)** → passive radar + real DF bearing; (b) **64 GB RAM** → 40–50 targets
  (only *after* the 1.3 refactor); (c) **GPU (PCIe)** → multi-domain ML; (d)
  **PCIe FPGA card** → OB-06 (longest lead — order + hire in parallel).
- **Man-portable (#56)?** The ST550 is a tower/rack lab host, **not** the fielded
  unit. Confirm whether a separate ruggedized x86 build is in scope; if so it
  sharpens the OB-06 FPGA-vs-GPU (SWaP-C) call. Stay x86-64 + Ubuntu 24.04.
- **OB-06 intent:** is "FPGA" a throughput requirement (a PCIe GPU could
  substitute) or deterministic-latency / SWaP-C / certification (GPU cannot)?
  This decides whether to buy a GPU at all.
- **OB-05 interpretation:** confirm TX-beamforming-primary (per scope doc §1) and
  the **target band(s)** + element count before any USRP/array procurement.

**Programme priority:**
- **Demo target — you said "prepare the list first," so pick from §4b.** Nothing
  in this plan assumes a demo. Our ranked recommendation (§4b): lead with **#2
  RC-manoeuvre takeover (RFI 1.11)** + **#1 telemetry spoof (1.7)** as the
  zero-risk warm-up — both BUILT and both run on the ST550's single HackRF. Tell
  us your pick and we front-load it in Phase 1.

---

## Ledger summary (counts)

- **BUILT:** 12 — MAVLink injection (1.1/1.3), broadcast/OB-01, physical-param/OB-03,
  RC takeover/1.11, telemetry spoof/1.7, broadcast-vs-targeted/1.9, engagement
  planner/OB-02, passive RF detection/1.18, real SDR ingest (P0), protocol-parser
  fleet, PDF report, safety-gate spine.
- **BUILT (sw) / HW-gated:** 6 — jamming/1.4, RF ML classifier, track manager/OB-04,
  swarm classifier/OB-02, passive radar CAF, direction finding.
- **PARTIAL:** 2 — multi-band/1.2·1.8, role separation.
- **DESIGNED-not-built:** 4 — GNSS spoof DSP/1.13, non-MAVLink injection/1.13,
  camera-thermal-acoustic/#83, OB-06 FPGA, OB-05 beamforming *(scope docs only;
  the last two are the highest-effort)*.
- **NOT-STARTED:** 1 — 20 km range characterization/1.14 (correctly deferred to field).

### Top 3 sequencing recommendations

1. **Do Phase 0 doc reconciliation before anything ships** — the RFI/compliance
   docs contradict the code (1.11 understated) and each other (range/power/band).
   Cheapest, highest-leverage risk reduction; an evaluator will find the
   overclaims otherwise.
2. **Front-load the software-only wins — they all run on the ST550 as delivered
   (Phase 1)** — GNSS synth DSP, DF bridge wiring, passive-radar recorded-IQ
   validation, and the shared-state refactor turn already-written scaffolding
   into demonstrable capability at software cost with no add-on. On this box the
   shared-state refactor is doubly urgent: the 2.10 GHz single-thread core means
   it, not the RAM buy, is what actually unblocks 40–50 targets.
3. **Order the bolt-ons in unlock order and start the FPGA pole now** — the ST550
   is PCIe-upgradable, so each capability is a bolt-on: **2nd SDR/GPSDO (USB)**
   first (passive radar + DF), then **64 GB RAM** (after the refactor), **GPU
   (PCIe)** for multi-domain ML, and the long pole **PCIe FPGA card + an
   FPGA/RTL engineer** for OB-06 (Army-CRITICAL) — its multi-month board
   procurement + hiring lead time, not coding, is the critical path, so start it
   in parallel with Phase 1. **Separately: pick a demo target from §4b** — our
   recommendation is RC-manoeuvre takeover (1.11) + telemetry spoof (1.7), both
   BUILT and both running on the ST550's single HackRF.
