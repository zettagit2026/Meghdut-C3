# MEGHDUT C3 — Master Task Sheet

_Terse tracker. Pending = one-line plan. Done collapses to a Shipped line. Updated 2026-09-02._
_Legend: 🔄 in progress · ⏸ queued · 🧱 blocked-on-hardware · 🧑 needs-user (bench) · 📋 deferred/roadmap_

## Governing (always-on, never violate)
- `tx_halted` never cleared except by commander; governed path only; ungoverned CLIs (`sik_mavlink_bridge.py`, `hackrf_jam.py --continuous`) fenced.
- `172.16.16.196` = READ-ONLY source of truth; never mutate/contact.
- Every shipped unit: independent verifier sign-off; no fake data / no fake-green.
- TX device pinning: TX=`…930c` (PA), RX/detection=`…a063`.

## GUI-only mandate (GOVERNING — operator NEVER runs a terminal)
- ✅ **"TX Online / Enable Engagement" console control** — SHIPPED (see Shipped). Operator now needs zero terminal.
- Audit every operator action for CLI leaks; each must be a console control.

## Operator-Jam (their code as a governed second mode)
- 🔄 **Operator Jam mode** — runs the operator's own CEMA_Jammer (GNU Radio, `/CEMA/operator-jam/`, UNMODIFIED) as a selectable jam mode beside MEGHDUT barrage, so they can A/B which defeats a target. Committed `0a842fd`, **independently verified GO (0 blockers, spine intact, tx_halt→409, bounded/abort real, honest audit).** Verifier flagged ONE pre-live-fire hardening → **device-pin now made self-enforcing (fail-closed if the pin can't be proven applied — import-form-independent); IN FLIGHT.** Then re-verify → deploy (needs GNU Radio + gr-osmosdr on .186; enable cema-operator-jam-bridge.service; set OPERATOR_JAM_DIR + HACKRF_TX_SERIAL).

## Next queue (backend-touching software; sequence deploys on server.py)
- ✅ **DJI DroneID decoder — DONE (READY).** User approved AGPLv3. DroneSecurity cloned to `/CEMA/DroneSecurity` (`9ff8198`, UNMODIFIED, LICENSE intact); deps `bitarray`+`crcmod` into bridge venv; `np.complex`(removed numpy≥1.24) fixed via a process-scoped compat shim OUTSIDE the OSS tree (systemd drop-in scoped to `cema-droneid-cued` only, shared venv untouched). Crash-loop gone (112→0), `droneid → READY`, yields RX radio (control_link decodes kept climbing). See Shipped.
- 🔄 **SDR MAVLink injection → governed capability BUILT (`e874a58`)** — first-class `mavlink_sdr_inject` effect, full spine + IFF fratricide interlock + honesty gate (encrypted/FHSS/unknown→422) + 930c pin fail-closed + bounded/abort; GUI page + coupled unit (no whitelist change); pure-numpy (plain venv, no GNU Radio). 24+19+146 tests green. **Security review GO (LOW, 0 crit/high/med) + independent verifier GO (0 blockers, 7 gates re-derived).** → dormant deploy IN FLIGHT.
- ⏸ **HOLISTIC hardening — TX device-pin at the source** — shared radiator `hackrf_jam.transmit_iq_file`/`_tx_device_args` falls back to index-0 when `HACKRF_TX_SERIAL` unset, so EVERY TX bridge (jam/operator-jam/gnss/mavlink) relies on its own bridge Gate-C to avoid keying the RX. ONE upstream fix: make the shared radiator fail-closed (no index-0 fallback) in the multi-HackRF/production context — hardens all TX paths at once. Own reviewed change (shared primitive; watch the break-glass CLIs + single-HackRF dev). Not a blocker (Gate-C protects governed paths today).

## 18-PROTOCOL AUTOPILOT (deploy+expose+test all 18 parser modules; spec `.omc/autopilot/spec-18-protocols.md`)
- 🔄 **Phase 1 planning IN FLIGHT.** Reconciliation done: NO documented 35/36 (that was the RFUAV airframe dataset) — there ARE 18 real parser modules, all with code. State: 4 OTA deployed (control_link LIVE + remoteid/droneid/fpv_osd READY); 5 OTA-capable not-wired (flysky_afhds/frsky_accst/spektrum_dsm/adsb/parrot_arsdk); 9 forensic wire (crsf/dronecan/ltm/msp/canopen/sik_wire/dshot/frsky_smartport/graupner_hott — need UART/CAN tap, bench-only). Plan → wire 5 OTA live + expose all 18 on the board honestly + deploy+test. NO overclaim (jam = universal defeat; these = detect/ID only).
- ⏸ **GNSS fidelity phase** — solved ephemeris + GPS-time + Doppler dynamics; then real-receiver validation (bench).

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
- **Protocol library DEPLOYED to .186 (`cad26af` + DroneSecurity decoder, 2026-09-04, verified GO + honest board) — ALL 4 OVER-THE-AIR PROTOCOLS HEALTHY:** **control_link = LIVE** (real: classifying contacts off the detection plane), **remoteid / fpv_osd / droneid = READY** (feeds/decoder up, armed, honest — no drone/video/DJI present). 331 files sha256-verified; backend+frontend REBUILT (baked-image trap avoided); OPERATIONAL(×4)/FORENSIC(×4) grouping. DroneID cued (not blind-sweep), decoder = AGPLv3 DroneSecurity UNMODIFIED, PROVEN not to starve the RX radio. End-state clean: `tx_halted=True`, TX bridges dormant+disabled, detections incrementing.
- **Fielded GUI engage-flow + root host-helper DEPLOYED to .186 (`84dff1c`, 2026-09-04) — OPERATOR NEEDS ZERO TERMINAL:** commander-gated GUI Resume-TX/Halt + TX-Online/Stand-Down. Root host-helper daemon (`cema-tx-helper.service`, AF_UNIX socket 0660 root:root, frozen 9-cmd whitelist, `shell=False`, NoNewPrivileges) — **container→host→systemctl round-trip PROVEN working** (the "Bring TX Online" button functions). Both Phase-4 gates GREEN (security GO, verifier 10/10). Audit-verified: chain INTACT (cryptographic genesis→head recompute, 0 mismatches); a real governed **JAM fired under commander authority today (07:31–07:32Z, 6 JAM events, chain-recorded)** = partial live-fire validation; `tx_halted` re-halts fail-closed every restart.
- **GPU CAF port + frontend finish deployed (`5999bb9`):** GPU passive-radar CAF (torch.fft, `CEMA_CAF_DEVICE`, CPU-default / GPU-opt-in) — **bit-accurate vs the `caf_bruteforce` oracle** (~3e-7, independently re-verified on the RTX 3060), 124× / 154 ms per block, on-box GPU tests 7/7. Frontend light-mode complete + **external analytics STRIPPED** (console loads **zero** external scripts — render-verified air-gap-clean). _(GPU CAF path opt-in until a live-radar workstream; a live detection still needs 2nd SDR+GPSDO+illuminator.)_
- **Batch `46e647e` deployed to .186 (2026-09-02, independently reviewed):** `bridge_hello` identity (`CEMA_BRIDGE_TOKEN` set 3× on host) · **IFF friendly-fire fratricide interlock** — a confirmed friendly is hard-blocked; only path is a commander-minted single-use target-bound audited ack (silent standing override removed) · **frontend fratricide-override UI** (commander-only, verbatim-phrase gate) · **GNSS-spoof v1 DSP** · **FPV OSD extractor** · ungoverned-CLI TX `-d` pinning. Deploy verified: plane undisturbed, `tx_halted` True, bridges dormant, new bundle renders clean. _(Full fratricide-UI render pending a confirmed-friendly target — bench.)_
- Stack migrated to `.186` (i7/128GB/NVMe/RTX-3060); byte-faithful data parity; reboot-survival.
- Encrypted backups, verified restorable (`~/MEGHDUT-C3-Backups/`).
- Console: blank → fielded-instrument (render-verified); real `/health`, ErrorBoundary, honest labels.
- WiFi identification-confidence fusion (`multidomain_fused`/`wifi_attributed`) + ML reject-guard.
- ML classifier on RTX 3060 (`torch 2.13.0+cu126`, `cuda:0`, hard CPU fallback) — third-eye verified.
- GPU utilization roadmap (`GPU_UTILIZATION_ROADMAP.md`).
- Drone-lab relocation: safe shutdown → move → bring-up; audit chain proven intact through move (seq 34627 hash held).
- Governed TX bridges deployed+dormant (`cema-rf-bridge` MAVLink, `cema-jam-bridge`); device pinning; false-green mitigation — security GO-WITH-FIXES + verifier GO.
- Sovereign map (offline grid) → OSM demo basemap + grid fallback; theme toggle restored + light-mode polish (core screens).
