# MEGHDUT C3 — Hardware Procurement, mapped to Capability Unlocked

Single source of truth for every hardware item that gates a capability. Each row states
**the capability it unlocks** (and the Northern Command RFI para / PMO-Suraj task it
serves), so procurement can be prioritised by operational impact, not by part.

**Rule:** the software for most of these is already built and hardware-blocked — buying the
part *unlocks* it, it does not start a build. Update this file whenever a hardware
dependency is identified or an item is procured.

Status: **Procured** · **Partial** (some units, insufficient) · **Pending**.
Refs: RFI = Northern Command "Counter Drone System w/ Cyber Control" Appx A; PMO = existing task #.
_Reconciled 2026-09-05 against the live build + the Northern Command RFI + the protocol-library scope._

---

## On hand today (baseline)
| Item | What it currently gives us |
|---|---|
| 2× HackRF One (TX serial …930c + PA, RX …a063) | The whole current RF plane: RX energy sweep (6 bands, ~20 MHz IB, sequential ~3 s revisit), governed jam (continuous + swept), SDR-MAVLink inject, GNSS-spoof v1 (GPS-L1). **Single RX = the sequential-scan + no-DF + ~20 MHz-IB ceiling below.** |
| RTX 3060 workstation (.186, i7-14700/128 GB/NVMe) | ML classify + passive-radar CAF (GPU); C2/backend host. Below the RFI's RTX-5090 analysis-station spec. |
| TP-Link UB500 BT/BLE adapter | Kismet BLE + Classic passive scan → **RemoteID (BLE) + BT drone detection**. Procured. |

---

## TIER 1 — unlocks the RFI's decisive, currently-missing capabilities

| Item | Capability UNLOCKED | Serves | Status |
|---|---|---|---|
| **DF antenna array + coherent multi-channel receiver** (amplitude-comparison or phase-interferometer, ≥3–4 matched channels) | **Direction Finding — azimuth/elevation ≤3°** and **GCS/controller geolocation**. DF math is built (`field-bridge/direction_finding.py`, RSSI-ratio monopulse) + honest "UNKNOWN (no array)" today → **purely hardware-blocked**. This is the single biggest RFI gap. | RFI 4.2.4.4, 4.2.5.4 (GCS loc), 4.2.9; PMO #20/#120 | **Pending (design + buy)** |
| **Wideband IQ SDR(s)** (USRP B210/X310 or BladeRF-class, ≥56 MHz IB) — 1 per priority band or a wideband unit | **(a) Concurrent all-band scan** (RFI wants 433/800-900/2.4/5.2/5.8 GHz *simultaneous*; our single HackRF is sequential). **(b) Per-emitter SEI / per-protocol 2.4 GHz RC type-ID** (ELRS vs FrSky vs FlySky vs Spektrum — impossible on a 1 MHz-bin energy sweep). **(c) ≥62 MHz instantaneous bandwidth** (vs ~20 MHz HackRF). | RFI 4.2.1, 4.2.4; PMO #51/#55/#107 | **Pending** |
| **High-power PA chain (100 W / multi-channel) + high-gain directional antennas + LNAs** | **Operational RANGE + SENSITIVITY:** detection 5 km low / 10 km high band, sensitivity −90 dBm, jam/takeover 2.5 km / 5 km. Demonstrated ~1–2 km today; jam power 100 mW–10 W today. | RFI 4.2.4.1, 4.2.4.5, 4.2.7.1, 4.3.1-2; PMO #53/#54 [ARMY PRIORITY] | **Pending** |
| **Multi-constellation wideband GNSS SDR** (higher sample rate + front-end for L1/L2/L5/E-band) | **Multi-constellation, multi-band GNSS spoof + jam** — GPS(L1C/A,L1C,L2C,L5) / Galileo(E1,E5a,E5b,E6) / BeiDou(B1I,B1C,B2a,B2b,B3i) / GLONASS(L1,L2) **+ NavIC**. Our GNSS-spoof v1 is **GPS-L1 only**. | RFI 4.3.8-4.3.16, 4.3.18; | **Pending** |

## TIER 2 — unlocks a built-and-blocked capability, lower cost

| Item | Capability UNLOCKED | Serves | Status |
|---|---|---|---|
| **2nd synchronized SDR + GPSDO + reference (illuminator) antenna** | **Live passive bistatic radar** — detect **non-emitting / EMCON / "dark" drones** (kinematic, no RF-ID). CAF/detector DSP already built (`passive_radar/`, bit-accurate, GPU); source is a `NotImplementedError` stub pending this HW. | RFI (dark-drone coverage); PMO #57 [ARMY PRIORITY] | **Pending** |
| **USB Wi-Fi monitor-mode adapter** (Alfa AWUS036NHA/ACH, ~$30-40) | **Wi-Fi-drone SSID/OUI fingerprint** (Tello `TELLO-*`, Parrot, Autel, `DIRECT-*` softAP + drone OUIs) **+ Wi-Fi deauth-attack detection** — both are software-ready glue-bridges over the existing Kismet rail. Cheapest capability-per-rupee. | RFI 4.2.5 (ID); protocol-lib #1/#5 | **Pending** |
| **RTL-SDR + 1090 MHz antenna** (or any dump1090/readsb source) | **ADS-B feed** → the `adsb` protocol goes OFFLINE→READY (cooperative-aircraft deconfliction). Bridge already ingests a Beast/SBS feed. | RFI airspace deconfliction | **Pending** |
| **SiK / RFD900-class 915 MHz radio** (CP210x/FTDI serial) | **MAVLink RX sniffer** (`cema-mavlink-sniffer`) **+ RC-override / maneuver-takeover TX** (`cema-rf-bridge`, PL-011 land-at-coords). udev pin `/dev/cema-sik-adapter`. Software built + dormant. | RFI 4.2.7.2.2 (safe-land); PMO #132 | **Partial** (intermittent) |
| **GNSS receiver w/ jamming/spoofing flags** (u-blox ZED-F9-class) | **GNSS spoofing/jamming DETECTION** (vs our own TX) **+ real-receiver validation** of the GNSS-spoof (proves receiver lock — the current v1's open item). | RFI 4.2.10 (false-alarm), 4.3 validation | **Pending** |
| **Dedicated 3rd HackRF (or SDR)** | Frees a dedicated effector radio so **detect (RX) + jam + inject/operator-jam** don't contend for the 930c/a063 pair. | operational concurrency | **Pending** |

## TIER 3 — RFI system BOM (procurement/integration, our software runs on it)

| Item | Capability UNLOCKED | Serves | Status |
|---|---|---|---|
| **GPU analysis workstation** — Ryzen 9 9900X, **RTX 5090 32 GB**, 32 GB DDR5, 2 TB NVMe, Win 11 Pro | The RFI's Graphics & Analysis Station (AI inference, raw/IQ analytics, forensics). We run on RTX 3060 today → **upsize**. | RFI 4.4 | **Pending** |
| **Rugged MIL laptop + handheld tablet + onboard compute (8C/16T, 64 GB) + L2 managed switch** | The **C2 hardware** the MEGHDUT C2 software runs on (dual/triple display, single-operator). | RFI 4.5.1 | **Pending** |
| **FPGA acceleration board** | Deterministic-latency DSP (OB-06 CRITICAL); throughput closed on GPU, but cert/SWaP-C wants FPGA. | PMO #118 [ARMY CRITICAL] | **Pending (board + toolchain)** |
| **MIL-env ruggedisation** — MIL-STD-810E/F, **MIL-461 E/F EMI/EMC**, IP66/67, −20…+45 °C, lightning/surge | Environmental compliance for field/vehicular ops. Needs a hardware lab/partner. | RFI 4.6 | **Pending (partner)** |
| **Power** — 3 KVA DG set + 3500 Wh online UPS (8 h idle / 4 h active) | Field/on-move power for all subsystems. | RFI 4.7 | **Pending** |
| **SUV 4x4 (Toyota 1GD-FTV / Hilux-class) + upfit + HVAC** | The vehicle platform (static + on-move), Olive Green, ARAI/ICAT. | RFI 4.8 | **Pending (vehicle partner)** |
| **Telescoping mast / carbon tripod / Rohacell foam / 100 m ODU cable** | Man-portable + deployable-on-tripod form factor + 100 m remote operation. | RFI 4.1.1.7, 5.3.2; PMO #56/#124 | **Pending** |

## TIER 4 — extends coverage (niche / forensic / future)

| Item | Capability UNLOCKED | Serves | Status |
|---|---|---|---|
| **RC receiver ICs** (A7105 / CC2500 / CYRF6936) | **Bench chip-level RC frame decode** (FlySky/FrSky/Spektrum) — forensic, drone-in-hand only; NOT airborne. | protocol-lib #10 (FORENSIC) | **Pending** |
| **EO camera + thermal (FLIR-class) + acoustic array** | **Multi-domain detect of RF-passive/EMCON drones**; Planck thermal-extraction code built, unexercised. | PMO #83/#64/#123 | **Pending** |
| **Cellular-band SDR + cell scanner** | **4G/5G-controlled drone** uplink detection (hard, legally fraught). | protocol-lib #12 | **Pending (flag legal)** |
| **LoRa transceiver modules** (SX1276-class) | **IFF challenge-response beacon** for friendly assets (crypto/beacon software built). | PMO #60/#126/#128 | **Pending** |
| **MIL-STD-1553 / ARINC 429/664; STANAG 4586 / Link 16 interfaces** | Legacy military bus/datalink interfaces (Link 16 likely export-controlled — flag before buying). | PMO #61/#62 | **Pending (flag)** |

---

## Numeric shortfalls vs the RFI (hardware-driven — cannot be closed in software)
| Parameter | RFI / Army spec | Today (HackRF platform) | Closes with |
|---|---|---|---|
| DF accuracy | ≤3° az/el | **none** | DF array (Tier 1) |
| Band scan | all bands **concurrent** | sequential ~3 s revisit | wideband/multiple SDRs (Tier 1) |
| Detection range | 5 km / 10 km | ~1–2 km | PA + high-gain antennas (Tier 1) |
| Sensitivity | −90 dBm | front-end-bound | LNA + antenna (Tier 1) |
| Jam power | 100 W (multi-ch) | 100 mW–10 W | 100 W PA (Tier 1) |
| Instantaneous BW | ≥62 MHz | ~20 MHz | wideband SDR (Tier 1) |
| GNSS coverage | 4-5 constellations, many bands | GPS-L1 only | multi-constellation GNSS SDR (Tier 1) |
| Concurrent targets | 50 detect / 100 GNSS-mitigate | software ~16-32 | software scale + RF front-end |
| 2.4 GHz RC type-ID | make/model | family-level | wideband IQ SDR + SEI (Tier 1) |
| Encrypted-COTS takeover (DJI) | "cyber takeover" | ⚠ not injectable | **no hardware fixes this** — jam + GNSS-spoof are the defeat |

**Bottom line:** the software is largely built or buildable (see the RFI gap analysis + protocol-library scope); the operational spec is gated on **Tier 1** — a **DF array**, a **wideband/multi-SDR front-end**, a **100 W PA + gain antennas**, and a **multi-constellation GNSS SDR**. The cheapest high-value unlocks are **Tier 2** (Wi-Fi monitor adapter, 2nd SDR+GPSDO, GNSS receiver).
