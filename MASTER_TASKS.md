# MEGHDUT C3 — Master Task Sheet

_Terse tracker. Pending = one-line plan. Done collapses to a Shipped line. Updated 2026-09-02._
_Legend: 🔄 in progress · ⏸ queued · 🧱 blocked-on-hardware · 🧑 needs-user (bench) · 📋 deferred/roadmap_

## Governing (always-on, never violate)
- `tx_halted` never cleared except by commander; governed path only; ungoverned CLIs (`sik_mavlink_bridge.py`, `hackrf_jam.py --continuous`) fenced.
- `172.16.16.196` = READ-ONLY source of truth; never mutate/contact.
- Every shipped unit: independent verifier sign-off; no fake data / no fake-green.
- TX device pinning: TX=`…930c` (PA), RX/detection=`…a063`.

## In progress
- 🔄 **Security hardening + gate tests** — `bridge_hello` identity credential + ungoverned-CLI `-d` pinning; run 3 backend integration tests (test_e2e_deploy_bridge / tx_authorization_holes / maneuver_takeover_gates) on throwaway backend; deploy + verify plane. _(agent running)_
- 🔄 **GNSS-spoof** — v1 DSP built (Gold codes/NAV/BPSK, 34 tests). NEXT: review+verifier; fix 1 contradicted bridge test; fidelity phase (solved ephemeris + GPS-time + Doppler dynamics); real-receiver validation. **Honest: valid signal, NOT yet a proven lock.**

## Queued (software, no user needed)
- ⏸ **Passive radar software/GPU** — dual-channel wiring + GPU CAF port (`caf_fft_batched`→torch.fft/cupy) with bit-accuracy vs `caf_bruteforce`; list GPSDO+illuminator hardware gap. _(starts after security deploy)_
- ⏸ **UI light-mode finish** — GnssSpoof, MavlinkConsole, DetectionHistory, ProtocolLibrary, KillChain (still dark-only inputs).
- ⏸ **Strip external analytics** — remove emergent.sh + PostHog scripts from frontend build (sovereignty). _(chip: task_50fc5d7f)_
- ⏸ **Secrets hygiene** — chmod 600 `.186` `field-bridge/.env`; rotate if host shared.

## Blocked on hardware — protocol decoders (all RX-only proven; awaiting user procurement)
- 🧱 **DroneID (DJI)** — needs 3rd HackRF (RTL-SDR too narrow) OR build time-share on existing HackRF.
- 🧱 **RemoteID** — build Kismet-feed extract ingest (software, no HW) OR dedicated monitor-Wi-Fi + BLE-5 dongle.
- 🧱 **CRSF** — 3.3V USB-UART (CP2102/FT232, 400k+ baud) tapping Pixhawk/FPV FC UART.
- 🧱 **MSP** — USB-UART tap to FC MSP UART + build ingest loop.
- 🧱 **DroneCAN** — CANable 2.0 (gs_usb) / PEAK PCAN-USB on the drone CAN bus.

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
