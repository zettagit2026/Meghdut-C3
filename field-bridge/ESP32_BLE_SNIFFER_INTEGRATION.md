# ESP32 BLE Advertisement Scan Integration — Scoping (task #63/B5 follow-on)

## Status
Scoping only. **Nothing has been flashed or built against real hardware yet.**
No code changes to `backend/server.py` are required (see "confidence_type"
below). This document exists to establish the correct architecture before
any firmware or bridge script is written for real.

## 1. Board identification (recon performed 2026-07-28, primary/172.16.16.196)

Two separate USB serial devices are physically present on primary, and it
is important not to conflate them:

| USB ID | Device path | Identity |
|---|---|---|
| `303a:1001` Espressif | `/dev/ttyACM0` | **This is the procured board.** Confirmed via `esptool chip-id` (read-only): **ESP32-S3 (QFN56), silicon revision v0.2**, dual-core + LP core @ 240MHz, **Wi-Fi + BT5 (LE)**, 8MB embedded PSRAM, native USB-Serial/JTAG (no separate UART bridge chip — the 303a:1001 VID/PID *is* the ESP32-S3's own USB-CDC peripheral). MAC `b8:f8:62:f9:54:64`. Flash: 8MB (GigaDevice `c8`/`4017`), quad I/O, 3.3V. |
| `10c4:ea60` Silicon Labs CP210x | `/dev/ttyUSB1` (flaps as `ttyUSB0`/`ttyUSB1` across reconnects) | A **different, unrelated** USB-UART bridge device, generic serial number `0001`. Not examined further — out of scope for this task, and it was busy/unavailable during recon (something else already has it open). Do not confuse this with the BLE board; it is not an Espressif native-USB device. |

Also present: `1d50:6089` HackRF One (bus 2) — the project's existing SDR,
unrelated to this recon.

**Current firmware on the ESP32-S3 (`/dev/ttyACM0`) — confirmed by
read-only flash reads, nothing erased or reflashed:**

- Partition table at `0x8000` shows a standard **OTA-capable** ESP-IDF
  layout: `nvs`, `otadata`, `app0`, `app1`, `spiffs`.
- App image header + embedded `esp_app_desc_t` at `0x10000` reads:
  `arduino-lib-builder`, `IDF v4.4.1-472-gc9140caf8c`, built `Jul 4 2022`.
- Embedded string table includes `"ESP32_TEST_SEVER"` (sic), `"WIFI Setup
  done"`, `"WIFI TEST is OK!"`, `"BLE TEST IS OK!"` / `"BLE TEST IS FALT!"`
  (sic) — this is clearly the **stock vendor factory burn-in/self-test
  image** shipped on generic ESP32-S3 dev boards (tests Wi-Fi + BLE at
  power-on and reports pass/fail over serial/OLED). It is not our BLE
  scan-and-report firmware and was not written by this project.
- **This confirms BLE hardware is present and was factory-verified
  functional** ("BLE TEST IS OK!" string exists in the image, consistent
  with this being genuine, working BLE-capable silicon) — useful
  corroboration on top of the chip-id BT5(LE) feature flag.

**Nothing was erased, reflashed, or otherwise modified.** All operations
performed were `esptool chip-id`, `flash-id`, and `read-flash` (small,
targeted reads at `0x8000` and `0x10000`) — all read-only against the
existing flash contents. Per the task's explicit instruction, the factory
image was NOT touched pending confirmation of this integration plan.

## 2. Kismet remote-capture protocol — checked against the real Kismet
source tree (`../kismet`, sibling checkout, GPL-2.0, previously verified
as the real Kismet source by `kismet_bridge.py`)

Kismet does have a real, documented remote-capture mechanism
(`KismetExternal`), used by ~20+ `capture_*` datasource helpers in that
tree (`capture_bt_geiger`, `capture_sdr_rtl433`, `capture_antsdr_droneid`,
etc.). Concretely, on inspection:

- The framing/transport is implemented in `capture_framework.c` — a
  substantial C file using POSIX `pthread`, `socket`/`select`, `fork`/`exec`
  process management. This is designed for a **Linux host process**, not a
  microcontroller.
- Each `kismet_cap_*.py` helper (e.g.
  `capture_bt_geiger/KismetCaptureBtGeiger/kismet_cap_bt_geiger.py`)
  imports a generated `kismetexternal` **protobuf** package
  (`google.protobuf`-based message classes) and runs as an independent
  Python process spawned by `kismet_server`.
- There is no lightweight/embedded variant of this protocol in the tree —
  every real remote-capture datasource assumes a full OS process with a
  Python interpreter or native binary, protobuf serialization, and
  socket/pipe I/O to the parent `kismet_server`.

**Conclusion: implementing `KismetExternal` on bare ESP32-S3 firmware is
not practical.** It would require porting/writing a protobuf encoder,
a framing state machine, and process-lifecycle semantics that don't exist
on a microcontroller, for no real benefit over a direct serial bridge —
this would be reinventing Kismet's plumbing on hardware it was never
designed to run on, contrary to this project's clean-room/don't-reinvent
discipline. **Rejected.**

## 3. Recommended architecture: direct serial bridge, bypassing Kismet

Confirmed as the right call, not assumed:

- **Firmware** (`field-bridge/esp32_ble_scan_firmware/esp32_ble_scan_firmware.ino`,
  written as part of this scoping pass, **not yet flashed**): uses the
  standard Arduino-ESP32 `BLEDevice`/`BLEScan` API
  (`BLEDevice::getScan()->start()`, `BLEAdvertisedDeviceCallbacks`) to run
  a **passive** (no `SCAN_REQ` transmission) continuous BLE advertisement
  scan and print one JSON object per advertisement to USB-serial (native
  CDC-ACM on `/dev/ttyACM0`, 115200 baud). Fields: `mac`, `rssi`, `name`
  (if present in the advertisement), `uuids` (first service UUID if
  present), `ts_ms` (firmware-relative `millis()`, NOT wall-clock — the
  bridge script must stamp real time on receipt). This is a small, bounded,
  receive-only scan loop — no injection, no GATT connection, no jamming.
- **Bridge script** (`field-bridge/esp32_ble_scan_bridge.py` — **not yet
  built**, this task is scoping only): would follow the same skeleton as
  every other `field-bridge/*.py` script — read newline-delimited JSON off
  the serial port (`pyserial`), parse, and POST directly to
  `/api/detections/ingest` using the same `_post_with_reauth`-style
  auth/retry pattern already used by `hackrf_rx.py` / `kismet_bridge.py`.
  **No Kismet server involvement at all** — this device is not a Kismet
  datasource, so `kismet_bridge.py`'s REST-polling translation layer
  simply does not apply here; a Kismet-shaped intermediary would add a
  dependency and a process for zero benefit.

  Expected per-advertisement ingest body (draft — mirrors the existing
  `bt_det` shape in `hackrf_rx.py`, adapted for a real per-device BLE
  advertisement instead of an RF-band heuristic):

  ```json
  {
    "model": "BLE device (advertisement scan)",
    "protocol": "BLE (GAP advertisement, passive scan)",
    "threat_level": "LOW",
    "center_freq_ghz": 2.4,
    "bandwidth_mhz": 2.0,
    "rssi_dbm": -67,
    "snr_db": 0,
    "bearing_deg": 0.0,
    "distance_m": 0.0,
    "distance_estimated": false,
    "source": "ESP32_BLE_SCAN",
    "confidence_type": "advisory_only",
    "callsign": "AA:BB:CC:DD:EE:FF"
  }
  ```

  `snr_db` has no honest value here (no noise-floor measurement exists on
  a BLE scan result, unlike the HackRF's spectrum-based SNR) — draft as
  `0`/omitted rather than fabricating a number; needs a final decision
  when the bridge script is actually built, not invented here.
  `callsign` is repurposed to carry the MAC (matching how other bridges
  use free-text identity fields) — needs confirming against
  `DetectionIngestBody`'s actual intended semantics before real
  implementation, not assumed.

## 4. `confidence_type` classification

Per `backend/CONFIDENCE_MODEL.md` (ADR B4), the enum already has an exact
fit for this data: **`advisory_only`** — "Presence heuristic, explicitly
not an identity or threat claim," which is precisely what a BLE
advertisement scan is (you see a MAC/RSSI/name broadcasting itself; you
have not verified any protocol beyond the advertisement PDU itself, and
you have made no threat determination). This is the *same* enum value
`hackrf_rx.py`'s existing `bt_det` block already uses for its RF-heuristic
Bluetooth presence signal — no new enum value is justified. Per the ADR's
own convention (see the `bistatic_radar_detection` and
`multidomain_fused` entries), a new enum value is only warranted when the
data is *epistemically distinct* from all existing categories (a new kind
of measurement, verification, or derivation). A BLE advertisement scan is
not: it is presence-only, unverified, no-threat-claim — exactly
`advisory_only`'s existing definition. **Reuse it; do not add a new
value.**

No `backend/server.py` change is required — `confidence_type` is already
an unconstrained `Optional[str]` on `DetectionIngestBody`, confirmed in
the ADR's own text.

## 5. Honest scope limits

- **This is BLE advertisement/presence detection only** — MAC address,
  RSSI, advertised name (if broadcast), and first service UUID (if
  present). It is the standard BLE "observer" role per Bluetooth Core
  Spec Vol 6 Part B Sec 4.4.3.
- **This is NOT full 802.15.1/BLE link-layer protocol sniffing.** Sniffing
  the encrypted/unencrypted data channel traffic of an already-connected
  BLE link (as opposed to unconnected advertisement broadcasts) requires
  either following BLE's channel-hopping connection parameters in
  real time on radio hardware built for it — the well-known, real,
  citable example is **Nordic's nRF52840 "BLE Sniffer" firmware +
  the nRF Sniffer for Bluetooth LE Wireshark extcap plugin**
  (`nRF Sniffer for Bluetooth LE`, Nordic Semiconductor, distributed via
  nRF Connect for Desktop) — which is a **different chip family** (Nordic
  nRF52840, not this ESP32-S3) with silicon/firmware purpose-built for
  following connection hopping. No equivalent, working, citable
  open-source "full BLE connection sniffer" firmware for stock ESP32-S3
  was found in this scoping pass; claims of full-sniff capability on this
  specific board should be treated as unverified unless a specific,
  working project is found and cited later. If full protocol-level
  sniffing is later required, the correct answer is likely procuring an
  nRF52840 dongle for that specific purpose, not stretching this ESP32-S3
  board past its actual capability.
- No transmission/injection capability of any kind in the recommended
  firmware (passive scan only, `SCAN_REQ` not sent) — matches this
  project's RECEIVE-ONLY convention for every other field-bridge.
- The current factory test firmware ("ESP32_TEST_SEVER" burn-in image)
  remains untouched on the board as of this writing. Flashing the new
  firmware will overwrite it — get explicit confirmation before doing so
  as instructed, since it was not this project's image and its full
  original purpose/ownership wasn't confirmed beyond what its embedded
  strings reveal.

## 6. Next steps (not started)

1. Confirm with project owner: OK to overwrite the current factory
   test image on `/dev/ttyACM0` with `esp32_ble_scan_firmware.ino`?
2. If confirmed: build via Arduino-ESP32 core or PlatformIO
   (`platform = espressif32`, `board = esp32-s3-devkitc-1`,
   `framework = arduino`, pinned version per this project's
   `platformio.ini` convention — no `@latest`), flash to `/dev/ttyACM0`,
   verify JSON scan lines appear at 115200 baud before wiring up ingest.
3. Build `field-bridge/esp32_ble_scan_bridge.py` following the
   `hackrf_rx.py`/`kismet_bridge.py` auth/retry/ingest pattern, resolve
   the `snr_db`/`callsign` open questions in Sec. 3 above, add a
   `--use-test-fixture` offline mode matching this project's established
   testing convention, get testing-division sign-off before any live
   deploy.
