# CEMA Field Bridge — real hardware companion scripts

These scripts connect the CEMA cUAS console to **real** hardware:
HackRF One (RX + TX) and a SiK 915MHz telemetry radio.

They are deliberately kept **outside** the Docker app and gated behind an
explicit safety flag, because two of the three scripts here transmit RF.

## Components

| Script | Direction | What it does | Legal footing |
|---|---|---|---|
| `hackrf_rx.py` | **Receive only** | Sweeps 2.4/5.8GHz (DJI OcuSync/Wi-Fi video) and 915MHz (SiK ISM band), does energy detection, posts real detections + waterfall rows to the console via `/api/spectrum/ingest` and `/api/detections/ingest`. | Legal anywhere — RX only, no transmission. |
| `sik_mavlink_bridge.py` | **Transmit** (serial → SiK radio → paired MAVLink craft) | Sends the exact same byte-accurate MAVLink packets the console crafts (`mavlink_codec.py`) out over a real SiK radio serial link to a **paired** ArduPilot/PX4 test craft, and mirrors the result back into the console via `/api/mavlink/broadcast`. | Only run against your own paired MAVLink craft, in a location/test window where transmission is authorized (STEAG, under Army Signals supervision). |
| `hackrf_jam.py` | **Transmit** (HackRF TX) | Emits a bounded-duration interference waveform on a chosen frequency (SiK 915MHz ISM band or DJI 2.4/5.8GHz video/control band) to demonstrate link-disruption ("CEMA attack" RFI item 1.4). | **RF transmission — only run at STEAG under Army Signals spectrum authorization.** Do not run this at home or any unlicensed/unsupervised location. |

## Safety gating

Both TX scripts refuse to run unless **both** of the following are true:

1. Environment variable `CEMA_AUTHORIZED_RANGE=1` is set.
2. You pass `--i-confirm-authorized-range` on the command line.

This is a deliberate two-factor confirmation, not a technical restriction —
it exists so nobody fat-fingers a live transmission outside the one place
(STEAG demo, Army-supervised) where these two scripts are meant to run.
The RX script (`hackrf_rx.py`) has no such gate — it never transmits.

## Install (on the field laptop, not the Docker host)

```bash
pip install pyserial numpy requests
# HackRF tools (RX + TX):
#   macOS:   brew install hackrf
#   Ubuntu:  sudo apt install hackrf libhackrf-dev
```

## Usage

```bash
# 1. Passive detection — safe anywhere, run this first
# Credentials/console URL can be passed as flags, or (preferred, especially under
# systemd) via env vars CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD.
python3 hackrf_rx.py --console-url http://<console-host>:8001 --email operator@meghaduta.mil --password <current-admin-password>

# 2. Real MAVLink injection over SiK — only at STEAG, paired craft only
export CEMA_AUTHORIZED_RANGE=1
python3 sik_mavlink_bridge.py --port /dev/ttyUSB0 --baud 57600 \
  --console-url http://<console-host>:8001 --email operator@cema.mil --password cema@2026 \
  --i-confirm-authorized-range --action rth --target-sys 1

# 3. HackRF link-disruption demo — only at STEAG
export CEMA_AUTHORIZED_RANGE=1
python3 hackrf_jam.py --freq-mhz 915 --bandwidth-khz 500 --duration-s 5 \
  --i-confirm-authorized-range
```

## Running `hackrf_rx.py` as a systemd service (RX only)

`cema-hackrf-rx.service` is the single canonical unit for the passive RX
bridge. It does **not** hardcode credentials — it loads them from an
`EnvironmentFile` at deploy time. Before enabling it on any host:

1. Edit the unit's `User=`, `WorkingDirectory=`, and `EnvironmentFile=` paths
   to match that host (same convention as `rf-bridge/cema-rf-bridge.service`
   — the checked-in file is a template, not a working per-host config).
2. Create the env file referenced by `EnvironmentFile=` (it is intentionally
   **not** committed to the repo) containing at minimum:
   ```
   CEMA_API_URL=http://localhost:8001
   CEMA_EMAIL=operator@meghaduta.mil
   CEMA_PASSWORD=<this host's current admin password from its docker-compose .env>
   ```
   `chmod 600` that file.
3. Only then:
   ```bash
   sudo cp cema-hackrf-rx.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now cema-hackrf-rx.service
   ```

There is deliberately only one RX unit file in the repo now — the previous
`cema-hackrf-rx-backup.service` (which duplicated this file with different
hardcoded user/path and the same stale plaintext credentials) has been
removed. Do not recreate per-host copies; parameterize the one unit instead.

This section covers the RX-only detection bridge exclusively. It does not
apply to `rf-bridge/cema-rf-bridge.service`, which also runs the MAVLink TX
bridge and is out of scope here — do not enable/start that unit as part of
this change.

## Frequency notes for tomorrow's demo

- **SiK radio (your telemetry link)**: 915MHz ISM, FHSS, ~20-64 channels depending on firmware config. `sik_mavlink_bridge.py` talks to it over serial (AT commands / transparent passthrough) — the radio itself does the RF modulation, you're just pushing MAVLink bytes through it, same as MAVProxy would.
- **DJI Mini**: proprietary OcuSync 2.0/3.0 (2.4GHz + 5.8GHz frequency-hopping, encrypted/authenticated). `hackrf_rx.py` can detect and fingerprint its RF signature (hop pattern, channel occupancy, RSSI) — this is genuinely useful and demoable. `hackrf_jam.py` can demonstrate **denial** (broadband interference forcing RTH/land failsafe) on its bands. Full protocol-level command injection/hijack against DJI's proprietary link is **not** implemented here — that requires reverse-engineering OcuSync's authentication, which is out of scope for this build. Be upfront about this distinction with the evaluators: MAVLink craft → full kill-chain (detect → disrupt → inject → hijack); DJI → detect → disrupt (jam), not inject/hijack.
