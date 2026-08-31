# Drone-Lab Bring-Up Runbook — meghdut-srv02 (172.16.16.186)

_For powering the box back on after its physical move to the drone lab, ahead of
the live drone dry-run. Written from the pre-move safe-shutdown snapshot
(2026-08-31). Pre-move state: Docker stack cleanly stopped (`docker compose
stop`, WiredTiger graceful, volumes intact), all field-bridge/kismet writer
units stopped, box gracefully powered off after independent verifier GO._

**Frozen parity anchors captured before shutdown (re-verify these after bring-up):**
- Audit chain head: **`mission_log` seq = 34627**, `entry_hash = 30df6f4c…`
- Mongo counts: detections **21740**, mission_log **47187**, users **2**
- Protective backup (Mac): `~/MEGHDUT-C3-Backups/pre-drone-lab-move-20260831/…tar.gz.gpg`
  sha256 `68888ce76c2e426e1ccf49d881dbe5c133e55aed6f514cfdf693f46dfd56a9c4` (verified restorable)

---

## 0. Physical (before power)
- Cable the network into **`eno1` only** (MAC `6c:0b:5e:4b:65:a9`). **Do NOT** use the
  2nd onboard NIC `enp3s0` — it is unconfigured and would DHCP to ~`172.16.16.6`, not `.186`.
- Re-attach the USB sensor plane: **HackRF One** (1d50:6089), **SiK radio / CP210x**
  (10c4:ea60 → `/dev/cema-sik-adapter`), **AR9271 WiFi** (0cf3:9271, monitor),
  **TP-Link BT/BLE** (2357:0604, Kismet hci). udev rules are installed, so names re-pin.
- If the dry-run needs GPS, attach the **UB500 / u-blox** now — it was NOT enumerated
  pre-move and has no pinned `/dev/cema-gps` name (use its raw `ttyACM*/ttyUSB*`).

## 1. Power on + confirm the box is back as .186
```bash
# from the Mac
ping -c3 172.16.16.186
ssh biswajit@172.16.16.186 'hostname; ip -4 addr show eno1 | grep inet'
```
- Expect `meghdut-srv02` and `inet 172.16.16.186/24`.
- **IP-drift caveat:** a higher-priority netplan profile (`90-NM-*.yaml`, `match:{}`,
  `dhcp4:true`) exists. Static currently wins, but if the box comes up on a DHCP
  address instead of `.186`, reassert static (the source of truth is
  `/etc/netplan/50-cloud-init.yaml`) and `sudo netplan apply`, then re-check.

## 2. Confirm USB devices re-enumerated with stable names
```bash
ssh biswajit@172.16.16.186 'lsusb; ls -l /dev/cema-* ; ip link | grep -i wlx'
```
- Expect `/dev/cema-sik-adapter → ttyUSB*`, HackRF present (1d50:6089), the AR9271
  monitor interface up. If a name is missing, re-plug that device (udev re-pins it).

## 3. Bring the stack up (containers were `stop`ped, so they will NOT auto-start)
```bash
ssh biswajit@172.16.16.186 'cd /CEMA/joydipdemo && docker compose start'   # or: docker compose up -d
# wait for Mongo healthy BEFORE the bridges:
ssh biswajit@172.16.16.186 'docker ps --format "{{.Names}} {{.Status}}"'
```
- Wait until `cema-mongo` shows `healthy`. The writer systemd units are `enabled` and
  will have tried to auto-start on boot (harmless retry errors until the stack is up).

## 4. (Re)start the field-bridge writers, then confirm all green
```bash
ssh biswajit@172.16.16.186 'sudo systemctl restart cema-hackrf-rx cema-mavlink-sniffer \
  cema-fpv-bridge cema-ml-classify-bridge cema-kismet cema-kismet-bridge'
ssh biswajit@172.16.16.186 'systemctl is-active cema-hackrf-rx cema-mavlink-sniffer \
  cema-fpv-bridge cema-ml-classify-bridge cema-kismet cema-kismet-bridge'
```
- Confirm the ML bridge is on the GPU again: `journalctl -u cema-ml-classify-bridge -n 20`
  should log `ML inference device = cuda:0 (NVIDIA GeForce RTX 3060)`. (If CUDA misbehaves,
  `CEMA_ML_DEVICE=cpu` is the one-env-var revert to the known-good CPU path.)

## 5. VERIFY ZERO DATA LOSS + fail-closed safety (the gate before the dry-run)
- **Audit-chain head unchanged:** query `mission_log` max `seq` — must be **≥ 34627**
  with `entry_hash` beginning `30df6f4c…` at seq 34627 (the chain must be continuous;
  new post-boot events extend it, they must not rewrite it).
- **Mongo counts** ≥ the frozen values (detections 21740 / mission_log 47187).
- **`tx_halted` is True** (`GET /api/health` as operator, or confirm the fail-closed
  default re-asserted on boot). **Do NOT clear it.**
- **Ingest sources live:** `GET /api/health` shows the RX bridges reporting.
- Load the console (Caddy) and confirm it renders + shows real `/health` status.

## 6. Only then — drone dry-run
- Power the target drone + SiK link; walk the 8–10 min `DEMO_PLAYBOOK.md` script.
- All governed TX paths remain gated (commander → tx-halt → arm token → IFF interlock →
  range-auth lease). The two ungoverned break-glass CLIs are NOT part of the dry-run.

---

**Rollback (only if bring-up is broken / data looks wrong):** the pre-move backup at
`~/MEGHDUT-C3-Backups/pre-drone-lab-move-20260831/` is verified restorable via
`scripts/restore.sh`. Restore into a scratch namespace and compare before touching the
live DB — never restore over good live data without confirming the live data is actually lost.
