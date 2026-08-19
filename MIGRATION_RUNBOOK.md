# MEGHDUT C3 — Migration Cutover Runbook (host relocation to new hardware)

_Planning/documentation artifact. Written 2026-08-19 for the migration of the MEGHDUT C3
CEMA counter-UAS system off the current primary (`172.16.16.196`, 8 vCPU / 19 GB / 96 GB,
Ubuntu 24.04) onto the confirmed new box arriving tomorrow._

### Target box (GROUND TRUTH) — Lenovo ThinkSystem ST550, pre-used, **on rental**

> **Treat the ST550 as a BRIDGE host, not the final platform** (rented, pre-used). The migration
> path this runbook implements is **"Tier-A-class capacity now, Tier-B-upgradable later"** — good
> capacity today, with real deviations from `SERVER_SPECIFICATION.md` that are surfaced honestly
> below rather than papered over.

| Component | ST550 as delivered | Spec (§2 Tier A) says | Verdict |
|---|---|---|---|
| CPU | 2× Intel Xeon octa-core @ **2.10 GHz** (dual-socket, ~16 physical cores) | 8 fast cores, **high single-thread speed ≥3.5 GHz** | **DEVIATION** — many-core / low-clock is the *opposite* of what the single-thread-bound backend wants. See Deviation ②. |
| RAM | **32 GB ECC** | 32 GB ECC | **MEETS** Tier A. |
| Storage | 3× 1.2 TB **SAS HDD (spinning)** + integrated RAID (0/1/5/10) | **1 TB NVMe** + 1–2 TB capture SSD | **DEVIATION** — capacity is fine (RAID5 ≈ 2.4 TB usable fixes disk-full), but spinning SAS is materially slower random-I/O than NVMe for Mongo + Docker builds + torch load. See Deviation ①. |
| USB | 6 rear + front USB ports | ≥4 USB 3.x across ≥2 root hubs | **UNVERIFIED** — must confirm root-hub layout with `lsusb -t` on arrival (dual-socket boxes often hang USB off one socket). See Deviation ③. |
| Expandability | PCIe slots free | free PCIe x8/x16 for future FPGA/GPU | **GOOD** — Tier-B-upgradable: GPU (multi-domain ML), FPGA (OB-06), 2nd SDR via USB. See Deviation ④. |
| OS | "Linux/Debian compatible" | Ubuntu 24.04 LTS (match current) | **Install Ubuntu 24.04** to match the current host and avoid toolchain revalidation. See §2.0 / Deviation ⑤. |

**The five deviations, handled explicitly in the steps below:**
- **① Spinning SAS, not NVMe** → RAID layout that keeps captures off the DB/OS disk; consider adding an
  SSD for Mongo+Docker if a bay is free; expect slower first-build and tune Docker (§2.0, §2.4, §4).
- **② Low base clock (2.10 GHz) vs single-thread-bound backend** → per-request backend latency may be
  *worse* than the old host despite more cores; the cores help the bridge fleet/DSP, not the backend.
  Benchmark backend single-request latency post-migration and compare (§6.11).
- **③ USB root-hub layout unverified** → explicit `lsusb -t` + NUMA/root-hub check before cutover (§2.6).
- **④ PCIe expandability** → record free-slot inventory as a post-migration check; this is the Tier-B path (§6.12).
- **⑤ OS choice** → install Ubuntu 24.04 LTS; if only Debian is possible, flag torch/Docker/HackRF
  toolchain revalidation cost (§2.0).

> **Scope of THIS document.** This is a plan. Nothing here has been executed. Every command
> block in sections 2–5 is marked **EXECUTE ON NEW HOST — NOT NOW** and must be run by a human
> (or an explicitly-tasked agent) on the new box during the actual cutover window, not from this
> planning session. The Dev Mac remains **code-only**: all docker/build/test/RF runs happen on
> the target host, never locally (per `SESSION_HANDOFF.md`).

> **Grounding.** All state/mechanics below are drawn from repo files and cited inline:
> `SERVER_SPECIFICATION.md` §2–3, `scripts/deploy.sh`, `scripts/check_deploy_drift.sh`,
> `docker-compose.yml`, `field-bridge/README.md`, `field-bridge/REBOOT_SURVIVAL_CHECKLIST.md`,
> `rf-bridge/ACTIVATION.md`, `HARDWARE_PROCUREMENT.md`, `INSTALL.md`, `SECURITY_TLS_NOTE.md`,
> `.env.example`, `rf-bridge/env.example`, `SESSION_HANDOFF.md`. Items the repo does **not**
> specify are flagged **[GAP]** rather than invented.

---

## 0. Baseline being migrated (current state, from repo + handoff)

- **Containerised control plane (Docker Compose):** 4 containers — `cema-mongo` (mongo:7),
  `cema-backend` (FastAPI/uvicorn, **single-worker by design**), `cema-frontend` (nginx+React),
  `cema-caddy` (TLS via internal CA). (`docker-compose.yml`, `SERVER_SPECIFICATION.md` §1.)
- **Field-bridge fleet:** repo tracks **12** systemd unit files, but only **6** run today:
  `cema-hackrf-rx`, `cema-mavlink-sniffer`, `cema-fpv-bridge`, `cema-ml-classify-bridge`,
  `cema-kismet`, `cema-kismet-bridge` — all `NRestarts=0` (`SESSION_HANDOFF.md`). The other
  units (`cema-crsf-parser`, `cema-dronecan-parser`, `cema-gamutrf-adapter`, `cema-jam-bridge`,
  `cema-ltm-parser`, and `rf-bridge/cema-rf-bridge`) are **inert/dormant** and must stay that way.
- **TX is fail-closed HALTED.** `_tx_halted = True` is the module-level default in
  `backend/server.py` (line 729); it is in-memory only and **re-halts on every backend boot**,
  surfaced as `tx_halted` in `GET /api/health`. Clearing it is a commander action
  (`POST /api/emergency/resume`) and must NOT happen as part of migration.
- **SiK / RFD900 MAVLink radio is physically ABSENT** (`/dev/cema-sik-adapter` not present;
  `HARDWARE_PROCUREMENT.md`). rf-bridge stays dormant until the radio lands (`rf-bridge/ACTIVATION.md`).

---

## 1. Pre-arrival checklist (do TODAY on the Mac; confirm about the new box before it lands)

### 1a. On the Dev Mac today (code-only prep — safe, no mutation of any host)

- [ ] **Confirm the repo is the canonical, clean source of truth.**
      `git status` clean; note `SESSION_HANDOFF.md` flags local is/was **ahead of origin/main**
      (commits `1c22fb3`..`fe5f04f`) with **no credential helper here — the user pushes manually.**
      Decide: push to origin first, or migrate from the local working tree. The deploy script builds
      its file set from `git ls-files`, so **only committed, tracked files ship** — verify nothing
      needed is still uncommitted/untracked.
- [ ] **Confirm the toolchain the deploy scripts require exists on the Mac:** Homebrew bash ≥4 at
      `/opt/homebrew/opt/bash/bin/bash` (both scripts hardcode this shebang), plus `sshpass`, `rsync`,
      `shasum`, `ssh` (`scripts/deploy.sh`, `scripts/check_deploy_drift.sh`).
- [ ] **Inventory the host-only state that git does NOT carry** — this is the entire migration risk
      surface (nothing in git; must be pulled from the OLD host and re-placed on the new one).
      Per `SERVER_SPECIFICATION.md` §3 and `.gitignore`:
      - Docker named volumes: `cema_mongo_data`, `cema_caddy_data`, `cema_caddy_config`.
      - Root `.env` (compose secrets), `field-bridge/.env`, `rf-bridge/.env` (all gitignored).
      - udev rule `/etc/udev/rules.d/99-cema-sik-adapter.rules` (**not tracked in git** — confirmed host-only).
      - field-bridge Python venv (`field-bridge/.venv`) and the ML model checkpoint
        `resnet18_leesburg_split_0.02_1_current.pt` (**not tracked in git**; see §3 note on its path).
      - EnvironmentFiles referenced by the systemd units (host-local, `chmod 600`).
      - Any IQ captures / FPV captures / logs the team wants for after-action review (optional to move).
- [ ] **Capture the CURRENT deployed version** for rollback reference: read
      `/CEMA/joydipdemo/DEPLOYED_VERSION` on the old host and record the git short-SHA.
- [ ] **Pre-stage secret regeneration decisions** (do not paste live secrets into this doc):
      whether to carry the existing `.env` secrets verbatim or rotate `JWT_SECRET` /
      `IFF_BRIDGE_API_KEY` / `ADMIN_PASSWORD` at cutover. Note: rotating `JWT_SECRET` invalidates
      existing operator sessions; `IFF_BRIDGE_API_KEY` must stay identical between backend `.env`
      and `field-bridge/.env` (`.env.example`, `field-bridge/README.md`).
- [ ] **[GAP] Confirm how the ML checkpoint `.pt` will be obtained on the new host.** It is not in
      git. `ml_classify_bridge.py` docstring cites a `/tmp/...` default path while
      `REBOOT_SURVIVAL_CHECKLIST.md` (#133) says the default was moved off `/tmp` onto a persistent
      path under `field-bridge/`. Resolve which path is authoritative on the OLD host (`readlink`/
      `CEMA_ML_CHECKPOINT`) before copying, and place it on the new host at the same path the unit expects.

### 1b. Confirm about the ST550 before/on arrival (must-verify against the spec)

The ST550 is the **confirmed** box. It is **not a generic Tier-A machine** — it maps to
"Tier-A-class capacity, with real deviations". Verify each row on arrival and record the result;
the deviations are the ones flagged in the target-box table above and are handled step-by-step below.

| Confirm on ST550 | Spec (§2) | Status / action |
|---|---|---|
| CPU: 2× Xeon octa @ 2.10 GHz | 8 fast cores, **≥3.5 GHz single-thread** | **DEVIATION ②** — many-core/low-clock. Cores help the bridge fleet + DSP; backend (single-worker, single-thread-bound) may be *slower* per request. Benchmark at §6.11. |
| RAM: 32 GB ECC | 32 GB ECC | **MEETS.** No action. |
| Storage: 3× 1.2 TB SAS HDD + RAID (0/1/5/10) | 1 TB NVMe + 1–2 TB SSD | **DEVIATION ①** — spinning SAS. Capacity fine; random-I/O slower. RAID layout + optional SSD add + Docker tuning at §2.0/§2.4/§4. |
| USB: 6 rear + front | ≥4 USB 3.x on ≥2 root hubs | **DEVIATION ③ — UNVERIFIED.** Run `lsusb -t` on arrival; confirm HackRF on its own USB 3.x root hub, not shared with BT/SiK; check NUMA/root-hub layout (dual-socket) at §2.6. |
| PCIe slots free | free x8/x16 for FPGA/GPU | **DEVIATION ④ — GOOD.** Record free-slot inventory (§6.12); this is the Tier-B upgrade path (GPU + FPGA/OB-06). |
| OS: "Linux/Debian compatible" | Ubuntu 24.04 LTS | **DEVIATION ⑤ — install Ubuntu 24.04** to match current host; if only Debian, flag torch/Docker/HackRF revalidation (§2.0). |
| Arch: x86-64 (Xeon) | x86-64 | **MEETS.** torch wheels / Docker / HackRF / pymavlink are all x86-64. |
| GPU | optional today | Not fitted as delivered; add later via PCIe for the multi-domain/SEI roadmap. Not a blocker. |
| Network | 2× 1 GbE / 2.5-10 GbE | Record the ST550's **IP/DNS** — needed for `CORS_ORIGINS`, operator access, and the deploy scripts' `REMOTE_HOST`. |

**Decisions to lock with the user before cutover** (see §8): the ST550 is a **rented bridge host**
(plan the eventual move to a final NVMe/high-clock platform), RAID level, whether to add an SSD for
Mongo+Docker, GPU/FPGA timing, new host IP/DNS, whether the SiK radio + new sensors arrive with the
box, and operator-machine CA re-trust.

---

## 2. New-host base provisioning  — **EXECUTE ON NEW HOST — NOT NOW**

Ordered, copy-pasteable. Run as the deploy user (`biswajit`, matching the scripts' `REMOTE_USER`)
unless a step says `sudo`.

### 2.0 FIRST — RAID configuration + OS install (do this before anything else)  (Deviations ①, ⑤)

The ST550 ships as bare metal with 3× 1.2 TB SAS HDD and an integrated RAID controller. Configure the
array in the controller's setup utility (Lenovo XClarity / the RAID adapter BIOS, entered during POST)
**before** installing the OS.

**Recommended RAID layout — keep the DB/OS off the capture firehose (spec §3):**

- **Preferred (if a 4th bay/SSD can be added — see 2.0b):** put OS + Docker + Mongo on a **fast SSD
  (single or RAID1 pair)**; use the 3× SAS HDD as **RAID5 (≈2.4 TB usable)** for the IQ-capture
  firehose + FPV captures + logs. This most closely restores the NVMe-vs-capture split the spec wants.
- **HDD-only fallback (3× SAS, no SSD):** two workable options —
  - **Option A (favour DB safety + capacity):** all 3 disks in **RAID5 (≈2.4 TB usable)**, then
    partition: a dedicated OS+Docker+Mongo partition/logical-volume, and a separate large partition
    for captures/logs. Simple, but Mongo and captures share the same spindles (I/O contention).
  - **Option B (favour DB latency):** **RAID1** across 2 disks for OS+Docker+Mongo (≈1.2 TB, mirrored,
    faster small random reads than RAID5's parity), 3rd disk standalone (or hot-spare) for captures.
    Less total capacity, but isolates the DB spindles from the capture firehose — better for the
    slow-disk Mongo concern. **Recommended if no SSD is added.**
- Avoid RAID0 for anything holding the DB or audit chain — no redundancy, and this box holds the
  tamper-evident mission/audit data.

> **[GAP]** The repo does not prescribe a RAID level. Pick per the above with the user; record the
> chosen level, member disks, and stripe/partition map in the migration log.

### 2.0b Consider adding a SATA/NVMe SSD for Mongo + Docker (Deviation ①)

Spinning SAS will make Mongo random-I/O, Docker image build/rebuild, and torch model load **materially
slower** than the current host's disk. If the ST550 has a **free drive bay or M.2/PCIe slot**, strongly
consider adding even a modest SATA/NVMe SSD and placing `/var/lib/docker` + the Mongo data volume on it.
This is the single highest-leverage mitigation for the storage deviation. Record whether a bay/slot is
free (this also informs §6.12 PCIe inventory).

### 2.0c Install the OS (Deviation ⑤)

Install **Ubuntu 24.04 LTS x86-64** to match the current host (btusb ≥5.16 for the UB500, CP210x for
SiK, same Docker/systemd conventions, same torch/HackRF/pymavlink toolchain — zero revalidation).
The box is described as "Linux/Debian compatible"; if for any reason only Debian is installable, **flag
it**: the torch wheels, Docker images, HackRF tooling, and udev conventions were built/tested on Ubuntu
24.04, so Debian would require re-validating that whole toolchain (a cost, not a blocker) — get user
sign-off before going that route.

```bash
# 2.1 OS baseline — confirm Ubuntu 24.04 LTS x86-64 (spec §2 requires the match)
lsb_release -a
uname -m                      # must report x86_64

# 2.2 Base packages + firmware for the RF fleet
sudo apt update && sudo apt -y upgrade
sudo apt -y install linux-firmware hackrf libhackrf-dev python3-venv python3-pip \
                    rsync openssh-server

# 2.3 Docker Engine + compose v2 (INSTALL.md: Docker Engine + docker compose v2 on Linux)
#     Use Docker's official apt repo, then verify:
docker --version
docker compose version

# 2.4 Storage layout (spec §2/§3: split OS/DB disk from the capture firehose) — realise the §2.0 RAID plan
#     Mount the capture array (RAID5 spindle set, or the standalone 3rd disk) and point IQ/FPV captures
#     + model checkpoints + logs at it, keeping them OFF the OS+Docker+Mongo disk/partition.
#     [GAP] The repo does not prescribe an exact mountpoint — pick one (e.g. /data) and make the
#     field-bridge capture dirs + CEMA_ML_CHECKPOINT resolve onto it. Record the choice.
#     If an SSD was added (2.0b): put /var/lib/docker + the Mongo volume on the SSD (fast small random I/O),
#     captures on the SAS array (sequential firehose). This is the key mitigation for the spinning-disk deviation.
lsblk
sudo mkdir -p /data                          # example capture mountpoint (adjust to the RAID plan)
# e.g. move Docker's data-root onto the SSD if present: edit /etc/docker/daemon.json {"data-root":"/mnt/ssd/docker"} then restart docker

# 2.5 Create the app root exactly where the deploy scripts expect it
sudo mkdir -p /CEMA/joydipdemo
sudo chown -R "$USER":"$USER" /CEMA

# 2.6 USB ROOT-HUB VERIFICATION — Deviation ③ (spec §2 explicit requirement; UNVERIFIED on ST550)
#     The ST550 has 6 rear + front ports but the root-hub topology is unknown until checked here.
#     Plug in the HackRF (+ UB500 Bluetooth, + SiK if present) and confirm the HackRF lands on its
#     OWN USB 3.x root hub / controller, not sharing a bus with the other RF devices.
lsusb            # confirm each device is enumerated
lsusb -t         # confirm RF devices are on SEPARATE buses/root hubs, and HackRF on a 5000M (USB3) hub
#     Dual-socket caveat: USB controllers on a 2-socket box may all hang off ONE socket / NUMA node.
#     If HackRF shares a hub with BT/SiK, physically move it to a different rear port and re-check
#     lsusb -t until it sits on its own high-speed root hub before declaring the topology acceptable.
lscpu | grep -i numa                          # note NUMA nodes; correlate with the USB controller layout
# (optional) confirm HackRF sees full USB3 throughput without sample drops during a short hackrf sweep test

# 2.7 User / SSH — the deploy scripts SSH in as biswajit with a password (sshpass).
#     Confirm sshd is up and the account can log in from the Mac. Key-based auth is preferred
#     but the scripts are written for password auth via PRIMARY_SSH_PASS/keychain.
sudo systemctl enable --now ssh

# 2.8 Firewall / exposure (mirror the old host's posture from docker-compose.yml + SECURITY_TLS_NOTE.md)
#     - mongo is bound 127.0.0.1:27017 (no auth) — MUST NOT be LAN-reachable.
#     - backend is bound 127.0.0.1:8001 (host-local only; the bridges POST to localhost:8001).
#     - caddy publishes 443/80 on ${BIND_HOST:-0.0.0.0}.
#     Set a host firewall (e.g. ufw) allowing SSH + 443/80 only; deny inbound 27017/8001 from the LAN.
```

**TLS note (SECURITY_TLS_NOTE.md):** TLS today is terminated **only** at Caddy via its internal CA
(`tls internal`, `cema_caddy_data` holds the root + leaf certs). There is no public ACME. Preserving
`cema_caddy_data` (see §3) keeps the existing CA so operator machines that already trust it keep working.
If instead you **regenerate** the CA on the new host, every operator machine must re-trust the new root
(see §8). The nginx/backend hops remain plain HTTP behind Caddy by design — do not half-add HSTS/redirect
here (that is a separate, human-approved infra task per the note).

---

## 3. State migration  — **EXECUTE ON NEW HOST — NOT NOW**

This is the only data that does not rebuild from git (`SERVER_SPECIFICATION.md` §3). **Old host stays
running and untouched** until §6 validation passes. Run these from the Mac (or new host) with the OLD
host still live so both are reachable. Replace `<OLD_HOST>` = `172.16.16.196`, `<NEW_HOST>` = the new IP.

### 3.1 MongoDB (`cema_mongo_data`) — detections/tracks/mission-log/audit chain

```bash
# --- on OLD host: dump the live DB from inside the running mongo container ---
ssh biswajit@<OLD_HOST> \
  "docker exec cema-mongo sh -c 'mongodump --db cema_cuas_db --archive' " \
  > /tmp/cema_mongo_<OLD_SHA>.archive
# (DB_NAME is cema_cuas_db per docker-compose.yml.)

# --- on NEW host: start ONLY mongo first (see §5), then restore ---
cat /tmp/cema_mongo_<OLD_SHA>.archive | \
  ssh biswajit@<NEW_HOST> "docker exec -i cema-mongo sh -c 'mongorestore --archive --drop'"
```
> The migration must preserve the **tamper-evident append-time hash-chain audit log** intact
> (`SESSION_HANDOFF.md`). An archive dump/restore of the whole `cema_cuas_db` preserves stored
> documents byte-for-byte; §6 includes an audit-chain integrity check as a PASS/FAIL gate.
> **[GAP]** The repo does not ship a standalone "verify audit chain" CLI — confirm the verification
> path (backend endpoint or query) with the backend owner before cutover.

### 3.2 Caddy CA volumes (`cema_caddy_data`, `cema_caddy_config`) — preserve OR regenerate

**Option A — preserve (keeps existing operator trust; preferred):** copy the volume contents.
```bash
# on OLD host: tar the named volume from a throwaway container
ssh biswajit@<OLD_HOST> \
  "docker run --rm -v cema_caddy_data:/d -w /d busybox tar cf - ." \
  > /tmp/cema_caddy_data.tar
# on NEW host: create the volume and unpack into it (after compose has created it, or precreate)
docker volume create cema_caddy_data
cat /tmp/cema_caddy_data.tar | \
  ssh biswajit@<NEW_HOST> "docker run --rm -i -v cema_caddy_data:/d -w /d busybox tar xf -"
# (repeat for cema_caddy_config)
```
`docker-compose.yml` warns: `cema_caddy_data` holds the internal CA root + issued leaf certs and
**must persist**; regenerating the root invalidates copies already distributed to operator machines.

**Option B — regenerate:** skip the copy, let Caddy mint a fresh CA on first boot, then **re-trust the
new root on every operator machine** (§8). Choose A unless the user explicitly wants a fresh CA.

### 3.3 The three `.env` files (all gitignored — never shipped by deploy.sh)

Recreate on the new host from the OLD host's live values (or from `.env.example` / `rf-bridge/env.example`
if rotating secrets). Do **not** paste live secret values into this runbook.

| File on new host | Keys (source) |
|---|---|
| `/CEMA/joydipdemo/.env` | `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `IFF_BRIDGE_API_KEY`, `CORS_ORIGINS`, optional `SENSOR_LAT/LON/LABEL`, optional `BIND_HOST` (`.env.example`, `docker-compose.yml`). **Update `CORS_ORIGINS` to the new host's hostname/IP.** |
| `field-bridge/<EnvironmentFile>` (referenced by `cema-hackrf-rx.service` etc.; `chmod 600`) | `CEMA_API_URL=http://localhost:8001`, `CEMA_EMAIL`, `CEMA_PASSWORD`, and for the IFF bridge `IFF_BRIDGE_API_KEY` (must match backend `.env`) (`field-bridge/README.md`). |
| `/CEMA/joydipdemo/rf-bridge/.env` (`chmod 600`) | `CEMA_API_URL`, `CEMA_EMAIL`, `CEMA_PASSWORD`, `MAVLINK_SERIAL`, `MAVLINK_BAUD`, `MAVLINK_RX_ENABLED` (`rf-bridge/env.example`). Needed only when rf-bridge is activated (§8). |

> Compose does **not** auto-inject root `.env` values into containers unless each key is explicitly
> listed under `environment:` (a real past incident, noted in `docker-compose.yml`). The current
> compose already lists them — do not remove those lines.

### 3.4 udev rule `99-cema-sik-adapter.rules` (host-only, not in git)

```bash
# Copy the rule from OLD host to NEW host and reload udev:
scp biswajit@<OLD_HOST>:/etc/udev/rules.d/99-cema-sik-adapter.rules /tmp/
sudo cp /tmp/99-cema-sik-adapter.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```
The rule pins a CP210x SiK adapter `10c4:ea60 serial=0001` → `/dev/cema-sik-adapter`
(`rf-bridge/ACTIVATION.md`). **If the arriving radio is a different chip/serial (e.g. an FTDI RFD900
`0403:xxxx`), the symlink will not appear** — read the new device's ids with
`udevadm info -a -n /dev/ttyUSB0 | grep -E 'idVendor|idProduct|serial'`, add a matching
`SYMLINK+="cema-sik-adapter"` line, reload, and re-check `ls -l /dev/cema-sik-adapter`.

### 3.5 field-bridge venv + ML model checkpoint (host-only, not in git)

```bash
# Rebuild the venv fresh on the new host (cleaner than copying a venv across machines):
cd /CEMA/joydipdemo/field-bridge
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install pyserial numpy requests torch    # field-bridge/README.md deps + torch for ML bridge
# Verify the interpreter can import the heavy deps (mirrors rf-bridge/ACTIVATION.md preflight):
.venv/bin/python3 -c "import serial, numpy, requests; print('base deps OK')"
.venv/bin/python3 -c "import torch; print('torch OK')"

# Copy the ML checkpoint (NOT in git) to the path the unit/CEMA_ML_CHECKPOINT expects (see §1b GAP):
scp biswajit@<OLD_HOST>:<OLD_CHECKPOINT_PATH>/resnet18_leesburg_split_0.02_1_current.pt \
    <NEW_CHECKPOINT_PATH>/
```
> Per `REBOOT_SURVIVAL_CHECKLIST.md`, the `cema-ml-classify-bridge` unit's `ExecStart` must point at
> the field-bridge **venv** python (it imports `torch`), but the checked-in unit still reads
> `/usr/bin/python3` — **the deployer must edit `ExecStart=` per host** to the venv interpreter. Same
> caution applies to `cema-hackrf-rx`/`cema-fpv-bridge` (they import `numpy`; pinning is pending work
> #138). Place the checkpoint on the persistent bulk disk (§2.4), not on any tmpfs/`/tmp` path.

---

## 4. Code deploy + image rebuild  — **EXECUTE FROM MAC / ON NEW HOST — NOT NOW**

Code ships via `scripts/deploy.sh` (scp+checksum, version-stamped, dry-run default, `--apply` to
mutate — it never restarts/rebuilds anything). **Before running, repoint the scripts at the new host:**
`REMOTE_HOST` in both `scripts/deploy.sh` and `scripts/check_deploy_drift.sh` is hardcoded to
`172.16.16.196` — change to `<NEW_HOST>` (or migrate the script change through git first).

```bash
# 4.1 From the Mac — dry-run first (stages + full sha256 verify, live tree untouched):
export PRIMARY_SSH_PASS="$(security find-generic-password -a biswajit -s cema-primary-ssh -w)"
/opt/homebrew/opt/bash/bin/bash scripts/deploy.sh --dry-run

# 4.2 Apply (moves verified files atomically into /CEMA/joydipdemo; writes DEPLOYED_VERSION):
/opt/homebrew/opt/bash/bin/bash scripts/deploy.sh --apply
#   Default dirs: backend,frontend,field-bridge,rf-bridge,docker-compose.yml,caddy
#   Only git-tracked files ship (.env, logs, captures, models, certs are never touched).

# 4.3 On the NEW host — build the Docker images (deploy.sh deliberately does NOT build):
cd /CEMA/joydipdemo
export DOCKER_BUILDKIT=1        # BuildKit: better layer caching, less rebuild churn on the slow SAS disk
docker compose build            # builds backend + frontend images from source
```

> **Deviation ① — expect a SLOWER first build on spinning SAS.** The torch/CUDA layers are multi-GB
> (spec §1) and image build/rebuild is I/O-heavy; on SAS HDD this will take materially longer than the
> current host. Mitigations: enable BuildKit (above); if an SSD was added (§2.0b) put `/var/lib/docker`
> on it; keep the build cache warm and prune only stale artifacts, not the whole cache, between
> rebuilds:
> ```bash
> docker builder prune --filter until=168h    # prune build cache older than a week, keep recent layers
> docker image prune -f                        # remove dangling images only (not tagged ones)
> ```
> Do **not** `docker system prune -a` casually here — on this disk a cold rebuild is expensive.

---

## 5. Bring-up ordering + service enablement  — **EXECUTE ON NEW HOST — NOT NOW**

Order matters: DB first (so restore lands in a clean DB), then the rest of the control plane, then the
6 active bridges. **Keep rf-bridge / crsf / dronecan / gamutrf / jam / ltm units INERT. TX comes up
fail-closed by design — do nothing to clear it.**

```bash
# 5.1 Start ONLY mongo, then run the §3.1 mongorestore into it:
cd /CEMA/joydipdemo
docker compose up -d mongo
#   ... perform §3.1 mongorestore now ...

# 5.2 Bring up the rest of the control plane (backend waits on mongo healthcheck):
docker compose up -d            # backend + frontend + caddy (+ mongo already up)
docker compose ps               # expect 4 containers: cema-mongo/backend/frontend/caddy

# 5.3 Install + enable ONLY the 6 active bridge units (edit each unit's User=/WorkingDirectory=/
#     EnvironmentFile=/ExecStart= for this host FIRST — the checked-in units are templates).
for u in cema-hackrf-rx cema-mavlink-sniffer cema-fpv-bridge \
         cema-ml-classify-bridge cema-kismet cema-kismet-bridge; do
  sudo cp /CEMA/joydipdemo/field-bridge/$u.service /etc/systemd/system/
done
sudo systemctl daemon-reload
for u in cema-hackrf-rx cema-mavlink-sniffer cema-fpv-bridge \
         cema-ml-classify-bridge cema-kismet cema-kismet-bridge; do
  sudo systemctl enable --now $u.service
done

# 5.4 DO NOT enable these (stay dormant, per SESSION_HANDOFF + rf-bridge/ACTIVATION.md):
#     cema-rf-bridge (RX+TX, the only radio-writing unit), cema-crsf-parser, cema-dronecan-parser,
#     cema-gamutrf-adapter, cema-jam-bridge, cema-ltm-parser.
#     rf-bridge activation is a SEPARATE, human-armed step and only when the SiK radio is present
#     AND /dev/cema-sik-adapter resolves — and requires stopping cema-mavlink-sniffer first (single
#     serial port). See §8 and rf-bridge/ACTIVATION.md. Not part of this cutover.
```

---

## 6. Post-cutover validation gate (read-only — the checks a verifier would run)

Run every check; **ALL must PASS** before the cutover is declared complete and before the old host is
retired (§7). These are read-only and mutate nothing.

| # | Check | Command / source | PASS criteria |
|---|---|---|---|
| 6.1 | **4 containers healthy** | `docker compose ps` on new host | `cema-mongo`, `cema-backend`, `cema-frontend`, `cema-caddy` all `Up`; mongo `healthy`. |
| 6.2 | **Backend health + TX fail-closed** | `curl -s http://localhost:8001/api/health` | Responds; **`tx_halted: true`** (default on boot per `server.py:729`). Anything else = FAIL, stop. |
| 6.3 | **6 bridges active, no restart loops** | `systemctl is-active <unit>` + `systemctl show -p NRestarts <unit>` for the 6 units | all `active`; **`NRestarts=0`** each (`SESSION_HANDOFF.md`, `REBOOT_SURVIVAL_CHECKLIST.md`). |
| 6.4 | **Dormant units really dormant** | `systemctl is-enabled cema-rf-bridge cema-crsf-parser cema-dronecan-parser cema-gamutrf-adapter cema-jam-bridge cema-ltm-parser` | each `disabled`/inactive; `/dev/cema-sik-adapter` behaviour matches radio-present-or-not. |
| 6.5 | **App reachable over TLS** | browse `https://<NEW_HOST>/` via Caddy; operator login works | login OK; if CA was preserved (§3.2 Option A) no new trust prompt on an already-trusted operator machine. |
| 6.6 | **Data restored** | via app / `mongosh` count of detections/tracks/mission-log | counts consistent with the old host's DB. |
| 6.7 | **Audit chain intact** | tamper-evident hash-chain verification (**[GAP]** confirm the exact verifier with backend owner — repo ships no standalone CLI) | chain verifies end-to-end, no break at the restore boundary. |
| 6.8 | **Deploy drift clean** | from Mac: `PRIMARY_SSH_PASS=… /opt/homebrew/opt/bash/bin/bash scripts/check_deploy_drift.sh` (pointed at new host) | `status=CLEAN`, `undocumented_drift=0`, `pending_deploy=0` (exit 0). |
| 6.9 | **preflight** | run `preflight.sh` on new host | bridge heartbeat / TX-service checks green (`preflight.sh`, `REBOOT_SURVIVAL_CHECKLIST.md`). |
| 6.10 | **Reboot survival** | reboot the new host, re-run 6.1–6.3 | containers auto-restart (`restart: unless-stopped`), 6 bridges come back, **`tx_halted` re-defaults `true`**. |
| 6.11 | **Backend single-request latency benchmark (Deviation ②)** | time a representative authenticated endpoint on the new host vs the OLD host, e.g. `for i in $(seq 20); do curl -s -o /dev/null -w '%{time_total}\n' http://localhost:8001/api/health; done` (and a heavier authenticated read) | **Record and COMPARE to the old host.** Expect the low 2.10 GHz clock to make single-request latency *equal-or-worse* despite more cores (backend is single-worker). If materially worse, flag to the user — the fix is the shared-safety-state refactor (a software project, spec §2/§3), not more cores. Not a hard FAIL by itself, but a required measured datapoint. |
| 6.12 | **PCIe / expandability inventory (Deviation ④)** | `sudo lspci -vv \| grep -iE 'bridge|slot'`; physically note free x8/x16 slots + free drive bays | Record free-slot inventory: confirms the Tier-B upgrade path (GPU for multi-domain ML, FPGA for OB-06, SSD bay for §2.0b). Informational PASS. |
| 6.13 | **Disk I/O sanity (Deviation ①)** | note Mongo query responsiveness + Docker build wall-time observed during cutover | Record as baseline; if DB/query latency is unacceptable on SAS, escalate the §2.0b add-an-SSD option. Informational. |

---

## 7. Rollback / abort plan

**Guiding principle: the OLD host stays fully intact and running until §6 passes end-to-end.** The
migration is additive — nothing on `172.16.16.196` is stopped, wiped, or reconfigured during §2–6.

- **Cutover criteria (go/no-go):** declare cutover complete only when **all of §6.1–6.10 PASS**. Any
  FAIL → do not retire the old host; triage on the new host with the old one still serving.
- **Abort / rollback:** if the new host cannot be brought green in the cutover window, **revert
  operations to the old host** (it never stopped). On the new host: `docker compose down` and leave the
  6 bridge units disabled. No data is lost because the old `cema_mongo_data` was only *dumped* (read),
  never mutated.
- **Split-brain guard:** do **not** run both hosts as live operator endpoints simultaneously (two
  writers to two DBs diverge). Keep the new host in validation-only until the switch, then point
  operators at exactly one host.
- **Old-host retirement (only after PASS + a soak period agreed with the user):** keep it powered and
  read-only as a warm fallback for an agreed window before decommissioning. Record final
  `DEPLOYED_VERSION` from both hosts for the migration log.

---

## 8. Open risks & decisions needing the user

1. **ST550 is a rented, pre-used BRIDGE host — plan the exit.** It gives Tier-A-class capacity now but
   deviates from the spec on the two things the spec prioritised most: **storage is spinning SAS, not
   NVMe** (Deviation ①), and **CPU is low-clock/many-core, the opposite of the single-thread priority**
   (Deviation ②). Decide with the user: accept these for the interim, and set the trigger/timeline for
   moving to a final owned platform (fast NVMe + high-clock CPU) — especially before the Tier-B roadmap
   hardware (2nd SDR+GPSDO, EO/thermal/acoustic, 40–50 targets, FPGA/OB-06, all **Pending** in
   `HARDWARE_PROCUREMENT.md`) actually lands and makes the deviations bite harder.
2. **RAID level + add-an-SSD decision (Deviation ①).** Confirm the RAID layout from §2.0 (recommend
   RAID1 DB/OS + capture spindle, or RAID5+partition) and whether to add a SATA/NVMe SSD (§2.0b) for
   Mongo+Docker — the single highest-leverage fix for the slow-disk deviation. Needs a free bay/slot.
3. **GPU / FPGA timing (Deviation ④).** None fitted as delivered. The ST550's free PCIe slots make it
   upgradable: GPU for the multi-domain/SEI roadmap, FPGA card for OB-06. Confirm when (if) to populate
   them; neither blocks cutover (ResNet-18 is CPU-bound today).
4. **Is the SiK/RFD900 radio present on arrival?** It was **Pending (~2026-08-13)** and physically
   absent at last check. If it arrives with the box: which chip/serial? (drives whether the existing
   udev rule matches — §3.4). rf-bridge activation stays a **separate, human-armed** step (stop
   `cema-mavlink-sniffer` first — single serial port) and is **not** part of this cutover.
5. **Any new sensors on arrival** (WiFi monitor NIC, GPS, 2nd SDR, camera/thermal/acoustic)? Each is
   its own bring-up; none are required for the base cutover.
6. **New host IP / DNS.** Needed to set `CORS_ORIGINS`, repoint `REMOTE_HOST` in both deploy scripts,
   and update operator access. Not specified in the repo.
7. **Caddy CA: preserve vs regenerate** (§3.2). Preserve = zero operator re-trust; regenerate = every
   operator machine must re-trust the new root. User decision.
8. **Operator-machine CA re-trust.** If regenerating (or if new operator machines are added), plan the
   re-trust distribution of the Caddy internal-CA root.
9. **Secret rotation at cutover** (`JWT_SECRET` / `IFF_BRIDGE_API_KEY` / `ADMIN_PASSWORD`). Carry
   verbatim, or rotate? `IFF_BRIDGE_API_KEY` must stay identical across backend and field-bridge envs.
10. **git push state.** Local was ahead of `origin/main` with manual push only (`SESSION_HANDOFF.md`).
   Confirm the exact commit to migrate from, and push before migrating if origin is to be the source.
11. **[GAP] Audit-chain verification tool** (§6.7) and **[GAP] ML checkpoint canonical path** (§1b) —
    both need the backend/field-bridge owner to confirm before cutover; the repo alone is ambiguous.
12. **[GAP] Bulk-disk mountpoint** (§2.4) — the repo doesn't prescribe one; pick it, then make the
    capture dirs + `CEMA_ML_CHECKPOINT` resolve onto it and record the choice.
13. **TESTING vs deployment audience matrix.** `SESSION_HANDOFF.md` open item #3: everything is
    OPERATOR-ONLY during testing; the full multi-audience matrix must be defined **before final
    deployment** — confirm whether this migration is "final deployment" for that purpose.
```

