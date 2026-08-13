# MEGHDUT C3 — Server / Host Specification for Migration

_Prepared 2026-08-13. Grounded in the observed resource footprint of the running
system on the current host (172.16.16.196: 8 vCPU / 19 GB RAM / 96 GB disk,
Ubuntu 24.04), and sized to remove the pain points seen there — chiefly disk
pressure and single-core backend saturation — while leaving headroom for the
pending-hardware roadmap (2nd SDR bistatic radar, camera/thermal/acoustic
sensing, wider-band SDR, 40–50 concurrent targets)._

---

## 1. What actually has to run (the real workload)

**Containerised control plane (Docker Compose):**
- `mongo:7` — detections/tracks/mission-log/audit store. Light CPU, grows on disk unbounded today (needs retention + generous disk).
- `backend` — FastAPI/uvicorn. **Single-worker by design** (in-memory safety-gate state can't be shared across workers), so it is bound by **single-core speed**, not core count. Saturated one core under a 100-concurrent load test.
- `frontend` (nginx serving the React build) + `caddy` (TLS) — negligible.

**Field-bridge fleet (systemd Python services, run on the host, NOT containerised):**
- `hackrf_rx` — HackRF sweep + feature extraction (numpy).
- `ml_classify_bridge` — **PyTorch (ResNet-18) CPU inference** on IQ windows. The single most CPU/RAM-hungry component today (torch + a ~45 MB model checkpoint).
- `passive_radar` — CAF/Doppler DSP (numpy/scipy, FFT-heavy) — will get much heavier once the 2nd SDR is fitted.
- `mavlink_sniffer`, `fpv_video_bridge`, `kismet` + `kismet_bridge`, and (staged) `rf-bridge` for MAVLink TX.

**Peripherals over USB (this is a real sizing constraint):**
- HackRF One SDR — needs a **dedicated USB 2.0/3.0 high-speed (480 Mbps+) port on its own root hub** to stream IQ without sample drops.
- TP-Link UB500 Bluetooth (Kismet), SiK/RFD900 MAVLink radio, and — on the roadmap — a **2nd SDR + GPSDO** (bistatic radar), WiFi monitor adapter, and EO/thermal/acoustic sensors.

**Storage growth drivers (the #1 pain point on the current host — it ran 79–97 % full all session):**
- IQ captures (`field-bridge/` was ~17 GB), model checkpoints, Docker build cache/images (torch/CUDA layers alone are multi-GB), unbounded Mongo collections, per-bridge logs.

---

## 2. Recommended specification

### Tier A — **Comfortable baseline** (runs the current system with real headroom)

| Component | Spec | Why |
|---|---|---|
| **CPU** | 8 physical cores / 16 threads, **high single-thread performance** (e.g. modern Xeon E-23xx / Ryzen 7000 / Core i7-13xxx class, ≥3.5 GHz base, strong boost) | Backend is single-thread-bound → prioritise per-core speed over raw count. 8 cores comfortably runs backend + the torch inference bridge + DSP + Mongo concurrently. |
| **RAM** | **32 GB** ECC (min 16 GB) | Observed usage sat ~3–6 GB idle but spiked with torch + DSP + Docker builds; 32 GB removes all pressure and leaves room for the multi-domain-sensing ML to come. ECC for a system that must run unattended. |
| **Primary storage** | **1 TB NVMe SSD** (OS + Docker + app + Mongo) | Fast random I/O for Mongo + Docker; NVMe over SATA matters for build/rebuild speed. |
| **Bulk / capture storage** | **+1–2 TB SSD** (or NVMe) dedicated to IQ captures + models + logs | Keep the capture firehose off the OS/DB disk. This directly fixes the recurring disk-full problem. |
| **USB** | **≥4 USB 3.x ports across ≥2 independent root hubs/controllers** | So the HackRF gets its own high-speed lane and doesn't contend with Bluetooth/SiK/2nd-SDR. Verify with `lsusb -t` that RF devices land on separate buses. |
| **GPU** | Optional but **recommended**: 1× entry CUDA GPU (e.g. RTX 4000-Ada / RTX 4060 8–16 GB) | ResNet-18 inference is CPU-bound today; a GPU makes it trivial and, more importantly, unblocks the roadmap ML (EO/thermal/acoustic classification, RF-fingerprinting/SEI). Not required to run today. |
| **Network** | 2× 1 GbE (or 1× 2.5/10 GbE) | One for ops/management, headroom for multi-sensor/multi-host growth. |
| **OS** | **Ubuntu 24.04 LTS** (match current) | Zero migration surprises: same kernel family (btusb ≥5.16 for the UB500, CP210x for SiK), same Docker/systemd conventions, same `linux-firmware`. |

### Tier B — **Future-proof** (headroom for the full Army-priority roadmap)

Add on top of Tier A when the pending hardware lands:

| Driver | Bump to |
|---|---|
| 2nd SDR + GPSDO passive **bistatic radar** (heavy real-time CAF/FFT on two coherent streams) | 12–16 physical cores, **64 GB** RAM, and a **USB 3.0 controller with genuine per-port bandwidth** (or PCIe SDR) so two SDRs + GPSDO stream without contention |
| **Multi-domain sensing** (EO camera + thermal + acoustic ML) | The recommended GPU becomes **required** (RTX 4000-Ada 20 GB or better); +1 TB storage for imagery/audio |
| **40–50 concurrent targets** (track manager currently tuned to 32) | The 64 GB RAM + faster cores above; still single-worker-bound until the safety-state is migrated to a shared store (a software task, not hardware) |
| **FPGA acceleration** (OB-06, Army-CRITICAL) | A PCIe slot free for an FPGA accelerator card (board selection still open) — ensure the chassis has ≥1 free **PCIe x8/x16** slot |

### Field / deployable option (Army requirement #56 is man-portable)
If this migrates toward the ruggedized field unit rather than a rack/tower:
- **Ruggedised small-form-factor / embedded x86** with the Tier-A CPU/RAM/NVMe, wide-input DC power (vehicle/battery), fanless or filtered cooling, and **multiple externally-accessible USB 3.x ports** for the SDR/radio/Bluetooth fleet.
- Note: an ARM SBC (e.g. Jetson) is **not** a drop-in — the stack assumes x86-64 (torch wheels, Docker images, HackRF/pymavlink tooling all built/tested on x86-64). A Jetson would be a separate porting effort; recommend staying x86-64 for a clean migration.

---

## 3. Bottom line

- **Minimum to migrate cleanly today:** 8 fast cores, 32 GB RAM, 1 TB NVMe + 1 TB capture SSD, ≥4 USB 3.x on ≥2 controllers, Ubuntu 24.04. This alone eliminates the two real pain points seen on the current host (disk pressure, single-core backend contention).
- **Prioritise, in order:** (1) **storage** — generous + split OS/DB vs captures, (2) **single-thread CPU speed**, (3) **USB topology** for the RF fleet, (4) **RAM to 32 GB**. GPU is optional now, becomes the deciding factor for the multi-domain/SEI roadmap.
- **Don't over-invest in core count** — the backend can't use it until its in-memory safety-gate state is moved to a shared store (a deliberate single-worker design; changing it is a software project, flagged in the architecture critique).
- **Stay on x86-64 + Ubuntu 24.04 LTS** for a zero-surprise migration; the whole toolchain (torch, Docker, HackRF, pymavlink, Kismet, the udev rules for the SiK/Bluetooth adapters) is built and tested there.

### Migration mechanics (when the box is ready)
Data/state that must move (everything else rebuilds from git):
- MongoDB volume (`cema_mongo_data`) — `mongodump`/restore, holds detections/tracks/mission-log/audit chain.
- Caddy CA volumes (`cema_caddy_data`/`_config`) — or regenerate the internal CA + re-trust on operator machines.
- The host `.env` files (backend + field-bridge + rf-bridge) — secrets, device paths.
- The udev rules (`99-cema-sik-adapter.rules`) and the field-bridge venv/model checkpoints.
- Re-run the `scripts/deploy.sh` flow to place code; rebuild the Docker images on the new host.
