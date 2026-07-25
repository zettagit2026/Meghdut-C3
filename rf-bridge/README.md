# CEMA cUAS — RF Bridge (MAVLink Serial TX/RX Bridge)

**task #36 consolidation notice**: this directory used to also contain a
HackRF wide-band scanner (`hackrf_scanner.py` + `diagnose.py`) with its own
sweep/peak-detect/waterfall pipeline. That was a duplicate, earlier
iteration of what `field-bridge/hackrf_rx.py` does — and does more
completely (device serial pinning via `HACKRF_RX_SERIAL`, re-auth-on-401,
explicit RX-only safety framing, distance estimation). It has been removed
from this directory. **All HackRF detection/scanning now lives exclusively
in `field-bridge/`** (see `../field-bridge/README.md`).

This directory now contains exactly one thing: `mavlink_bridge.py`, the
live MAVLink serial TX/RX bridge. It has no equivalent in `field-bridge/` —
`field-bridge/mavlink_sniffer.py` is a passive RX-only protocol sniffer, and
`field-bridge/sik_mavlink_bridge.py` is a manual one-shot CLI action
injector with the older static-env-var gate. Neither subscribes to the
backend's `ws://.../api/ws/mavlink` control channel or does live per-frame
range-authorization checks the way `mavlink_bridge.py` does — that live,
continuous, app-driven TX/RX path is what `backend/server.py` actually
talks to (see the extensive comments there referencing
`rf-bridge/mavlink_bridge.py`).

- **FPV telemetry radio on `/dev/ttyUSB0`** (SiK / RFD900) → real MAVLink
  transmit to and receive from the target drone.

This bridge is a plain host-side Python service (not dockerised — USB
passthrough into Docker is brittle and offers no benefit here).

---

## 1. Prerequisites

- Debian / Ubuntu (22.04+) with `sudo`.
- **FPV telemetry ground module** (SiK-family or RFD900) on `/dev/ttyUSB0`
  paired with the airborne module on the drone.
- The CEMA cUAS backend already running (see `../INSTALL.md`).
- If you also need HackRF wide-band detection, install/run
  `field-bridge/` separately — this directory no longer provides that.

## 2. Install

```bash
cd rf-bridge
chmod +x install-deps.sh run.sh
./install-deps.sh
# log out and back in so the 'dialout' group takes effect
```

`install-deps.sh` will:

- Install `pkg-config`, `python3-venv`, `python3-pip`.
- Add your user to `dialout` and `plugdev` groups.
- Create a Python `.venv` and install `pyserial`, `pymavlink`,
  `websocket-client`.
- Copy `env.example` → `.env` (edit if the backend isn't on `localhost:8001`).

Verify:

```bash
ls -l /dev/ttyUSB0        # should exist
```

## 3. Configure

Edit `.env`:

```
CEMA_API_URL=http://localhost:8001      # or http://drone-lab01:8001
CEMA_EMAIL=operator@cema.mil
CEMA_PASSWORD=cema@2026

MAVLINK_SERIAL=/dev/ttyUSB0
MAVLINK_BAUD=57600
MAVLINK_RX_ENABLED=1
```

The SiK default baud rate is 57600. If your radio is set to 115200, change it.

## 4. Run

```bash
./run.sh          # start the mavlink bridge, foreground
./run.sh bridge   # same, explicit form
```

You should see (in the app UI):

- **New detections** flow in with `source=SIK_RADIO` when
  `MAVLINK_RX_ENABLED=1` and real MAVLink traffic decodes off the wire.
- Any packet you craft in the **MAVLink Console** or deploy via the
  **Payload Library** is **written straight to the radio on `/dev/ttyUSB0`**
  → **broadcast to the drone**.

## 5. Auto-start on boot (optional)

```bash
sudo cp -r . /opt/cema/rf-bridge
sudo cp cema-rf-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cema-rf-bridge
journalctl -u cema-rf-bridge -f
```

## 6. Architecture

```
                         ┌──────────────────────────────────┐
                         │      CEMA cUAS Web App           │
                         │  (backend + frontend + Mongo)    │
                         └────────────┬─────────────────────┘
                     REST + WebSocket │  /api/*  /api/ws/mavlink
                                      │
     ┌────────────────────────────────┴─────────────────────────────┐
     │            RF BRIDGE (this dir) — MAVLink only                │
     │                                                                │
     │                     mavlink_bridge.py                         │
     │                     ─ WS subscribe TX                         │
     │                     ─ pymavlink RX/TX                         │
     │                     ─ /dev/ttyUSB0                            │
     └───────────────────────────────┬──────────────────────────────┘
                                      │
                              USB ┌───┴────┐
                                  │ SiK/RFD│
                                  └────────┘
                                      │
                                 ═══ RF air ═══
                                      │
                              Drone MAVLink C2
                              (bidirectional)

  HackRF wide-band detection/jamming is a SEPARATE process tree — see
  ../field-bridge/ (hackrf_rx.py, hackrf_jam.py, jam_bridge.py, etc.).
```

## 7. Legal / operational warning

Transmitting on 433/868/915 MHz **may require a license** depending on your
country and power. This bridge sends real MAVLink takedown commands the
moment you click a payload in the UI. Only use in a **screened test range**
or against **your own** drone.

`RESTRICTED` — for MoD-style evaluation. Not for public / operational deployment.

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Permission denied: /dev/ttyUSB0` | You aren't in `dialout` group — `sudo usermod -a -G dialout $USER` then log out/in. |
| MAVLink TX has no effect | Check your radio's air-side baud & net-ID match. Try `mavproxy.py --master=/dev/ttyUSB0` to verify link independently. |
| Need HackRF waterfall/detection | Not here anymore — see `../field-bridge/README.md`. |
