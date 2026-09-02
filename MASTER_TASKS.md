# MEGHDUT C3 — Master Task Sheet

_Terse tracker. Pending = one-line plan. Done collapses to a Shipped line. Updated 2026-09-02._
_Legend: 🔄 in progress · ⏸ queued · 🧱 blocked-on-hardware · 🧑 needs-user (bench) · 📋 deferred/roadmap_

## Governing (always-on, never violate)
- `tx_halted` never cleared except by commander; governed path only; ungoverned CLIs (`sik_mavlink_bridge.py`, `hackrf_jam.py --continuous`) fenced.
- `172.16.16.196` = READ-ONLY source of truth; never mutate/contact.
- Every shipped unit: independent verifier sign-off; no fake data / no fake-green.
- TX device pinning: TX=`…930c` (PA), RX/detection=`…a063`.

## In progress
- 🔄 **IFF friendly-fire override — deliberate rebuild** — ROE decision: commander override STAYS, but rebuilt as an explicit, single-use, commander-only, per-target friendly-fire ack (loud audit) replacing the silent standing `iff_override_authorized` flag → no accidental fratricide, and the gate test passes. _(agent running; server.py)_

## Staged — pending ONE batched .186 deploy (backend + rf-bridge + field-bridge + docker-compose.yml)
_Prereq: generate + set the SAME `CEMA_BRIDGE_TOKEN` on .186 (backend + each TX-bridge `.env`). Deploy after the IFF override lands + is verified; then verify plane + bridge_hello reject + tx_halt True + bridges dormant._
- ✅ **Security hardening** — `bridge_hello` identity credential (backend + jam/mavlink bridges) + ungoverned-CLI `-d` pinning. Isolation-verified (tx_authorization_holes 12/12; CLI pinning 3/3).
- ✅ **GNSS-spoof v1 DSP** — real L1 C/A (Gold codes/NAV/BPSK), 63 tests, INDEPENDENT REVIEW PASS. **Honest: valid signal, NOT yet a proven lock** — real-receiver validation is a later bench step. (Doc `GNSS_SIGNAL_SYNTH_HANDOFF.md` stale → update.)
- ✅ **FPV OSD extractor** — MAX7456 glyph-match (numpy-only), 22 tests, no-fabrication guard. Backend ingest wiring deferred (server.py, after batch).
- ⬜ **IFF friendly-fire override** — lands with the running agent + verifier.
- ⬜ **1 pre-existing gate-test failure** (maneuver_takeover) — resolved by the IFF override rebuild (was the test that surfaced the hole).

## Queued (software, no user needed)
- ⏸ **Passive radar software/GPU** — dual-channel wiring + GPU CAF port (`caf_fft_batched`→torch.fft/cupy) with bit-accuracy vs `caf_bruteforce`; list GPSDO+illuminator hardware gap. _(starts after security deploy)_
- ⏸ **UI light-mode finish** — GnssSpoof, MavlinkConsole, DetectionHistory, ProtocolLibrary, KillChain (still dark-only inputs).
- ⏸ **Strip external analytics** — remove emergent.sh + PostHog scripts from frontend build (sovereignty). _(chip: task_50fc5d7f)_
- ⏸ **Secrets hygiene** — chmod 600 `.186` `field-bridge/.env`; rotate if host shared.

## Protocol coverage — OVER-THE-AIR (design: ONE RF front-end → software demux; no per-protocol radios; no airborne wire-tap)
**Principle:** a flying drone is countered by its RF EMISSIONS (control link + video + telemetry), NEVER by reading its internal MSP UART / CAN bus (physically impossible remotely for anyone). MSP/DroneCAN drones ARE in scope — via RF.
- 🔄 **FPV OSD extraction** — glyph-match the demodulated analog video OSD → recover MSP-class telemetry (callsign/GPS/battery) over the air. The real "MSP airborne" path. _(building; analog only — digital video encrypted → jam it)_
- ⏸ **Control-link RF classification** — type ELRS/CRSF/DSMX/DJI from signature so any drone is identified. _(band heuristics exist; fine-grained ID a build; after security deploy)_
- ⏸ **RemoteID ingest** — pull Wi-Fi/BLE RemoteID from the EXISTING Kismet feed. Software, no hardware. _(after security deploy — touches server.py)_
- ⏸ **DJI DroneID** — detect-sweep CUES a capture window on the RX radio → decode. No dedicated radio (wideband SDR e.g. USRP B210 = clean future upgrade, NOT required now). _(after security deploy)_
- **Bench/forensic only (labeled, NOT airborne):** MSP + DroneCAN wire-parsers — exercise on a tethered test drone to prove the parser; never presented as airborne detection.
- **Takedown of MSP/DroneCAN drones** = JAM the control/video band (universal, deployed) + MAVLink takeover (unencrypted-MAVLink) + GNSS spoof (GPS-reliant). Validate in live-fire.

## Deferred hardware (optional upgrades, not required now)
- 📋 Wideband capture SDR (USRP B210-class ~56 MHz) — removes DJI-DroneID time-slice on the sweep radio.
- 📋 ELRS-capable RX / SDR demod — for CRSF-over-air (harder; FHSS).

## Needs user — bench session (drones + commander)
- 🧑 **Live-fire test** — dummy-load spine test (50Ω) first → live MAVLink force-land/RTH/disarm/takeover on **Pixhawk** + jams on **DJI/FPV**. Needs targets powered, SiK link, dummy load, commander arming; I verify each shot (correct radio, real ack, abort).
- 🧑 **GNSS-spoof real-receiver validation** — needs a GPS receiver / the drone's GPS (dummy load can't validate a spoof).
- 🧑 **Protocol decode vs real drones** — recover DJI DroneID serial, etc. (after decoder HW lands).
- 🧑 **Rotate `.186` login + kismet passwords** — user action, deferred earlier.

## Deferred / roadmap
- 📋 **Passive radar LIVE detection** — GPSDO shared clock + illuminator-of-opportunity survey (hardware).
- 📋 **RF fingerprinting / SEI** — labeled per-emitter IQ campaign (multi-week); open-set embedding.
- 📋 **GPU CAF as OB-06 Phase-1** — throughput reading only; true FPGA still needed for deterministic-latency/SWaP-C/cert.

## Shipped (verified)
- Stack migrated to `.186` (i7/128GB/NVMe/RTX-3060); byte-faithful data parity; reboot-survival.
- Encrypted backups, verified restorable (`~/MEGHDUT-C3-Backups/`).
- Console: blank → fielded-instrument (render-verified); real `/health`, ErrorBoundary, honest labels.
- WiFi identification-confidence fusion (`multidomain_fused`/`wifi_attributed`) + ML reject-guard.
- ML classifier on RTX 3060 (`torch 2.13.0+cu126`, `cuda:0`, hard CPU fallback) — third-eye verified.
- GPU utilization roadmap (`GPU_UTILIZATION_ROADMAP.md`).
- Drone-lab relocation: safe shutdown → move → bring-up; audit chain proven intact through move (seq 34627 hash held).
- Governed TX bridges deployed+dormant (`cema-rf-bridge` MAVLink, `cema-jam-bridge`); device pinning; false-green mitigation — security GO-WITH-FIXES + verifier GO.
- Sovereign map (offline grid) → OSM demo basemap + grid fallback; theme toggle restored + light-mode polish (core screens).
