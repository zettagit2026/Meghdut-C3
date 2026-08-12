# MEGHDUT C3 — Consolidated Hardware Procurement Requirements

Single source of truth for every hardware item currently blocking software/capability
work on this project. Consolidates items previously scattered across the task
tracker (`tiers and tasks.rtf`), `meeting notes.md`, and various task descriptions.
Update this file whenever a new hardware dependency is identified or an item is
procured — do not let hardware needs live only inside individual task descriptions
going forward.

Status legend: **Pending** (not yet procured) · **Partial** (some units on hand,
insufficient for full spec) · **Procured** (on hand, unblocks the linked task).

_Last reconciled 2026-08-12 against the live deploy host (172.16.16.196)._

---

## RF Front-End / Transceivers

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| USB Bluetooth adapter (HCI-class) | **TP-Link UB500, Realtek RTL8761 (USB 2357:0604), dual-mode BLE + Classic.** Recognized natively by the Linux `btusb` driver (kernel ≥5.16; box runs 6.8), firmware in `linux-firmware`. Kismet BT/BLE passive scanning live on primary as `hci1` (pinned via `kismet_site.conf`), real BLE device detected end-to-end. NOTE: BLE + Bluetooth Classic only — has **no** dedicated non-Bluetooth capability. Two earlier wrong buys (ESP32-S3, nRF52840 Sense) are superseded by this. | #63, #132 | **Procured** |
| SiK / RFD900-class MAVLink radio (915MHz) | Serial (CP210x/FTDI) telemetry radio for the MAVLink RX sniffer (`cema-mavlink-sniffer`) AND the TX takeover bridge (`cema-rf-bridge`, RC-override / maneuver-takeover). **Currently physically ABSENT** (`/dev/cema-sik-adapter` not present; `lsusb` shows no USB-serial radio). Both services degrade gracefully (sniffer idles waiting-for-device; rf-bridge staged dormant). udev rule `99-cema-sik-adapter.rules` pins a CP210x `10c4:ea60 serial=0001` → `/dev/cema-sik-adapter`; a different chip/serial needs a rule tweak (see `rf-bridge/ACTIVATION.md`). | #132, RC-override takeover (B7) | **Pending** (expected ~2026-08-13; was on-hand earlier, now unplugged) |
| Directional antennas (2+ matched) | For amplitude-comparison direction-finding. DF math (RSSI-ratio monopulse) now built (`field-bridge/direction_finding.py`) + honest "UNKNOWN (no DF array)" state — purely hardware-blocked | #20 | Pending |
| USB GPS module | For auto-detecting sensor position (Option A) | #24 | Pending |
| USB WiFi monitor-mode adapter | Alfa AWUS036NHA or similar, ~$30-35. For passive WiFi (802.11) drone/OUI detection — SEPARATE from the Bluetooth adapter above | #70 | Pending |
| ANTSDR (or equivalent) | For real DJI DroneID decode validation. Decoder built + CRC-checked; only live-hardware validation blocked | #81 | Pending |
| LoRa transceiver modules | Asset-side low-power module(s) (e.g. RFM95/SX1276-class) + interrogator-side bidirectional module, for IFF challenge-response/beacon — sized to friendly-asset count. IFF crypto/beacon software fully built + hardened; purely hardware-blocked | #60, #126, #128 | Pending |
| Antenna array + multi-channel exciter | Full array design, not yet scoped in hardware terms | #120 | Pending (design first) |

## Power / Amplification

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| 100W / 7-channel PA chain | Army spec: 100W across 7×30W channels. Actual current PA hardware: 100mW–10W — ~3 orders of magnitude short | #53 | Pending |
| Frequency synthesizer | Real Army requirement (meeting notes item 9), evaluated but not procured | #107 (evaluated), procurement pending | Pending |

## Range / Coverage

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| Antenna gain + RF/CEMA range hardware | Army spec ≥7km (raised from 5km 2026-07-25 directive); demonstrated range only ~1-2km today | #54 [ARMY PRIORITY] | Pending |
| Full 400MHz–6GHz continuous coverage hardware | Software sub-band expansion done (#51); RF front-end for full continuous band not procured. RFI-Response admits current build demonstrated only at 915MHz/433MHz | #55 [ARMY PRIORITY] | Pending |

## Bistatic Radar

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| 2nd SDR unit | For passive bistatic radar full capability | #57 [ARMY PRIORITY] | Pending |
| GPSDO (GPS-disciplined oscillator) | Timing reference for bistatic radar coherence | #57 [ARMY PRIORITY] | Pending |

## Compute / Processing

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| FPGA acceleration hardware | Army requirement OB-06, marked CRITICAL. Current stack is Python/HackRF on general CPU only, no FPGA anywhere | #118 [ARMY CRITICAL] | Pending (needs board selection + toolchain scoping) |

## Legacy Military Protocol Interfaces

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| MIL-STD-1553 / ARINC 429/664 interface hardware | Band D | #61 | Pending |
| STANAG 4586 / Link 16 hardware | Band E. Link 16 likely export-controlled regardless of build effort — flag to leadership before procuring | #62 | Pending |

## Mechanical / Ruggedization

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| Telescoping mast | From meeting notes; untracked until 2026-07-25 audit | #56, #124 | Pending |
| Carbon-fiber tripod | From meeting notes; untracked until 2026-07-25 audit | #56, #124 | Pending |
| Rohacell RF foam | From meeting notes; untracked until 2026-07-25 audit | #56, #124 | Pending |
| 100m ODU (outdoor unit) cable, per spec | From meeting notes; untracked until 2026-07-25 audit | #56, #124 | Pending |
| Battery / weight / cabling for man-portable form factor | Band D, static + vehicular operation | #56, #58 | Pending |

## Multi-Domain Sensing (for RF-passive/EMCON drones)

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| Camera (EO) sensor | Scoped in `CAMERA_THERMAL_ACOUSTIC_SCOPE.md` (#83), not yet procured | #123 | Pending |
| Thermal camera | Scoped (#83); Planck-formula extraction code built (#64) but never exercised against real FLIR-calibrated output — no camera/sample R-JPEG on hand | #64, #123 | Pending |
| Acoustic sensor array | Scoped (#83), not yet procured | #123 | Pending |

## Swarm-Scale

| Item | Spec / Notes | Blocks | Status |
|---|---|---|---|
| Hardware sized for 40-50 simultaneous targets | Army ask is 40-50; current software spec (SOL-04) targets only ≥16 (scalable to 32) — under half the requirement even before hardware is considered | #59 | Pending |

---

## Known numeric shortfalls vs. Army spec (procurement-relevant, from 2026-07-25 requirements audit)

- **Range**: spec ≥5-7km, demonstrated ~1-2km — biggest gap.
- **Jamming power**: spec 100W/7ch, actual 100mW-10W.
- **Instantaneous bandwidth**: spec 62MHz/channel, actual ~20MHz (HackRF-limited).
- **Frequency coverage**: spec 400MHz-6GHz continuous, actual demonstrated only at 915MHz/433MHz.
- **Concurrent targets**: Army wants 40-50, current spec targets ≥16 (scalable to 32).

These are the hardware-driven gaps between the current HackRF-based platform and the
Army's stated parameters — closing most of them requires a hardware tier upgrade
(higher-power PA, wider-instantaneous-bandwidth SDR, real antenna array), not
additional software work.
