#!/usr/bin/env python3
"""MAVLink Bridge — App ↔ SiK / RFD900 / FPV telemetry radio on /dev/ttyUSB*.

TX path  : subscribes to  ws://<backend>/api/ws/mavlink  (JWT auth via query),
           and forwards every emitted MAVLink frame straight to the serial
           port as raw bytes → radio → drone.

RX path  : reads MAVLink frames coming FROM the drone through the same radio.
           Whenever a new (system_id) is seen it is registered as a detection
           in the app via POST /api/detections/ingest so it shows up on the
           Command Center dashboard.

Requires: pyserial, pymavlink, websocket-client.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Dict, Optional
from urllib.parse import quote

import serial
import websocket  # websocket-client
from pymavlink import mavutil

from common import CemaClient, cfg, cfg_int

log = logging.getLogger("mav-bridge")


class MavlinkBridge:
    def __init__(self) -> None:
        self.client = CemaClient()
        self.client.login()

        self.serial_path = cfg("MAVLINK_SERIAL", "/dev/ttyUSB0")
        self.baud = cfg_int("MAVLINK_BAUD", 57600)
        self.rx_enabled = cfg_int("MAVLINK_RX_ENABLED", 1) == 1

        self.ser: Optional[serial.Serial] = None
        self.ws: Optional[websocket.WebSocketApp] = None
        self.stop_flag = threading.Event()
        self.known_systems: Dict[int, float] = {}  # sysid → last_seen_ts
        # SECURITY #3: server-side "abort"/"resume" WS messages are now
        # authoritative here too. Previously this bridge had no handling at
        # all for an "abort" message type — /emergency/abort only broadcast a
        # cooperative notice that nothing here ever acted on, so frames kept
        # being forwarded to the radio after an "emergency abort". Now we stop
        # forwarding to serial until an explicit "resume" is received.
        self.tx_halted = False

    # ---- serial ----------------------------------------------------------
    def open_serial(self) -> None:
        log.info("Opening %s @ %d baud", self.serial_path, self.baud)
        self.ser = serial.Serial(self.serial_path, self.baud, timeout=0.1)
        log.info("Serial link up.")

    def write_frame(self, frame: bytes) -> None:
        if not self.ser or not self.ser.is_open:
            return
        try:
            self.ser.write(frame)
            self.ser.flush()
        except serial.SerialException as e:
            log.warning("serial write failed: %s", e)

    # ---- WS subscribe (TX path: app → radio → drone) ---------------------
    def start_ws_subscriber(self) -> None:
        base = self.client.base.rstrip("/")
        ws_scheme = "wss" if base.startswith("https") else "ws"
        host = base.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/api/ws/mavlink?token={quote(self.client.ensure_token())}"

        # Log the URL WITHOUT the token so the user can see exactly what we hit.
        log.info("Subscribing to MAVLink WS at %s?token=<jwt>",
                 url.split("?", 1)[0])

        def on_open(ws):
            log.info("WS connected to app for MAVLink TX subscription.")

        def on_message(_ws, msg):
            try:
                data = json.loads(msg)
            except Exception:
                return

            mtype = data.get("type")
            if mtype == "abort":
                if not self.tx_halted:
                    log.warning(
                        "EMERGENCY ABORT received from server (operator=%s) — "
                        "halting all TX to serial until resume.",
                        data.get("operator"),
                    )
                self.tx_halted = True
                return
            if mtype == "resume":
                log.warning(
                    "RESUME received from server (operator=%s) — TX to serial re-enabled.",
                    data.get("operator"),
                )
                self.tx_halted = False
                return
            if mtype != "packet":
                return

            if self.tx_halted:
                log.warning("TX suppressed: EMERGENCY ABORT in effect — dropping frame, not forwarding to serial.")
                return

            pkt = data.get("packet", {})
            hex_str = pkt.get("hex")
            if not hex_str:
                return
            try:
                frame = binascii.unhexlify(hex_str)
            except binascii.Error:
                return
            log.info("TX → serial: msgid=%s tgt_sys=%s len=%d bytes (%s)",
                     pkt.get("decoded", {}).get("message_id"),
                     pkt.get("target_system"),
                     len(frame),
                     pkt.get("payload_name") or "manual")
            self.write_frame(frame)

        def on_error(_ws, err):
            emsg = str(err)
            log.warning("WS error: %s", emsg[:300])
            if "404" in emsg:
                log.error(
                    "  → The backend at %s does NOT have the /api/ws/mavlink route. "
                    "Your docker BACKEND container is running an older server.py. "
                    "Rebuild it:  docker compose build --no-cache backend && "
                    "docker compose up -d backend",
                    self.client.base,
                )

        def on_close(_ws, code, reason):
            log.warning("WS closed (%s %s); reconnecting in 2s", code, reason)

        def run_forever():
            while not self.stop_flag.is_set():
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=on_open,
                    on_message=on_message,
                    on_error=on_error,
                    on_close=on_close,
                )
                try:
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    log.warning("ws crash: %s", e)
                if not self.stop_flag.is_set():
                    time.sleep(2)

        t = threading.Thread(target=run_forever, name="ws-subscriber", daemon=True)
        t.start()

    # ---- RX path (drone → radio → serial → parse → app) -----------------
    def start_rx_thread(self) -> None:
        if not self.rx_enabled:
            log.info("RX disabled; skipping drone→app ingest thread.")
            return

        def run():
            # Use pymavlink to demux the byte stream. We attach it directly
            # to the same serial object via its file descriptor.
            mav = mavutil.mavlink_connection(
                f"{self.serial_path}",
                baud=self.baud,
                source_system=255,
                source_component=190,
            )
            log.info("pymavlink RX parser attached to %s.", self.serial_path)
            while not self.stop_flag.is_set():
                try:
                    m = mav.recv_match(blocking=True, timeout=1)
                except Exception as e:
                    log.warning("mavlink recv error: %s", e)
                    time.sleep(0.5)
                    continue
                if m is None:
                    continue
                if m.get_type() == "BAD_DATA":
                    continue

                sysid = m.get_srcSystem()
                now = time.time()
                # First time we see this system id, register a detection.
                if sysid not in self.known_systems:
                    self.known_systems[sysid] = now
                    try:
                        det = {
                            "callsign": f"MAV-{sysid}",
                            "model": "MAVLink UAV",
                            "protocol": f"MAVLink v{mav.WIRE_PROTOCOL_VERSION}",
                            "threat_level": "HIGH",
                            "center_freq_ghz": 0.433,  # SiK default; unknown otherwise
                            "bandwidth_mhz": 0.25,
                            "rssi_dbm": -70.0,
                            "snr_db": 20.0,
                            "system_id": int(sysid),
                            "component_id": int(m.get_srcComponent()),
                            "encrypted": False,
                            "source": "SIK_RADIO",
                        }
                        r = self.client.post("/api/detections/ingest", det)
                        log.info("+ MAVLink drone sysid=%s → %s", sysid, r.get("callsign"))
                    except Exception as e:
                        log.warning("detection ingest failed for sysid=%s: %s", sysid, e)
                else:
                    self.known_systems[sysid] = now

                # log low-volume message types for visibility
                if m.get_type() in ("HEARTBEAT", "STATUSTEXT", "GPS_RAW_INT",
                                     "GLOBAL_POSITION_INT", "COMMAND_ACK"):
                    log.debug("RX sysid=%s type=%s", sysid, m.get_type())

        t = threading.Thread(target=run, name="rx-parser", daemon=True)
        t.start()

    # ---- lifecycle -------------------------------------------------------
    def run(self) -> int:
        try:
            self.open_serial()
        except serial.SerialException as e:
            log.error("Cannot open %s: %s", self.serial_path, e)
            log.error("Hint: sudo usermod -a -G dialout $USER  (then log out & back in)")
            return 2

        # NOTE: pymavlink opens its OWN handle to the serial port for RX.
        # To avoid two handles on the same tty, we close ours and let
        # pymavlink own it — but then we need pymavlink for TX too. To keep
        # things simple, if RX is enabled we use pymavlink to do both TX
        # (via mav.mav.buffer / write) and RX. If RX is disabled, we keep
        # the plain pyserial object for TX only.
        if self.rx_enabled:
            self.ser.close()
            self.ser = None
            # rebuild via pymavlink so both directions share one handle
            self._pymav = mavutil.mavlink_connection(
                self.serial_path, baud=self.baud,
                source_system=255, source_component=190,
            )
            def write_via_pymav(frame: bytes) -> None:
                try:
                    self._pymav.write(frame)
                except Exception as e:
                    log.warning("pymav write failed: %s", e)
            self.write_frame = write_via_pymav  # type: ignore

            # RX loop reads from self._pymav
            def rx_loop():
                log.info("pymavlink RX parser attached to %s.", self.serial_path)
                while not self.stop_flag.is_set():
                    m = self._pymav.recv_match(blocking=True, timeout=1)
                    if m is None or m.get_type() == "BAD_DATA":
                        continue
                    sysid = m.get_srcSystem()
                    if sysid not in self.known_systems:
                        self.known_systems[sysid] = time.time()
                        try:
                            det = {
                                "callsign": f"MAV-{sysid}",
                                "model": "MAVLink UAV",
                                "protocol": f"MAVLink v{self._pymav.WIRE_PROTOCOL_VERSION}",
                                "threat_level": "HIGH",
                                "center_freq_ghz": 0.433,
                                "bandwidth_mhz": 0.25,
                                "rssi_dbm": -70.0,
                                "snr_db": 20.0,
                                "system_id": int(sysid),
                                "component_id": int(m.get_srcComponent()),
                                "encrypted": False,
                                "source": "SIK_RADIO",
                            }
                            r = self.client.post("/api/detections/ingest", det)
                            log.info("+ MAVLink drone sysid=%s → %s", sysid, r.get("callsign"))
                        except Exception as e:
                            log.warning("detection ingest failed sysid=%s: %s", sysid, e)
            threading.Thread(target=rx_loop, name="rx-parser", daemon=True).start()

        self.start_ws_subscriber()

        # signal handling
        def _sig(*_):
            log.info("stopping.")
            self.stop_flag.set()
            if self.ws:
                try: self.ws.close()
                except Exception: pass
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        while not self.stop_flag.is_set():
            time.sleep(0.5)
        return 0


def main() -> int:
    logging.getLogger().setLevel(logging.INFO)
    return MavlinkBridge().run()


if __name__ == "__main__":
    sys.exit(main())
