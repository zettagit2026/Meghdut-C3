#!/usr/bin/env python3
"""MAVLink Bridge — App ↔ SiK / RFD900 / FPV telemetry radio on /dev/ttyUSB*.

TX path  : subscribes to  ws://<backend>/api/ws/mavlink  (JWT auth via query),
           and forwards every emitted MAVLink frame straight to the serial
           port as raw bytes → radio → drone.

RX path  : reads MAVLink frames coming FROM the drone through the same radio.
           Whenever a new (system_id) is seen it is registered as a detection
           in the app via POST /api/detections/ingest so it shows up on the
           Command Center dashboard.

RANGE AUTHORIZATION: before forwarding ANY frame to the serial radio, this
bridge makes its own independent, live GET /api/range-authorization/status
?effect=mavlink call to the backend (see common.CemaClient.is_range_authorized
and backend/RANGE_AUTHORIZATION_REDESIGN.md) and refuses to transmit if that
lease is not currently enabled. This replaces the previous static
CEMA_AUTHORIZED_RANGE=1 bridge-host env var with a GUI-armed, auto-expiring
(15 min) lease controlled from the app. It is an ADDITIONAL, independent
check — it does not replace require_commander, arm_token, or the
tx_halted/EMERGENCY-ABORT handling below, all of which are unchanged. Fails
closed (treats as NOT authorized) on any network/auth error talking to the
backend.

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
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import quote

import serial
import websocket  # websocket-client
from pymavlink import mavutil

from common import CemaClient, cfg, cfg_int

# F-1: the SUSTAINED RC-override maneuver-takeover (PL-011) primitive lives in
# the field-bridge package. This rf-bridge is the ONLY component that actually
# writes frames to the radio, so the sustained controlled-landing loop must be
# driven from HERE. Import run_sustained_takeover from the sibling field-bridge
# dir (which itself adds backend/ to sys.path for mavlink_codec).
_FIELD_BRIDGE = str(Path(__file__).resolve().parent.parent / "field-bridge")
if _FIELD_BRIDGE not in sys.path:
    sys.path.insert(0, _FIELD_BRIDGE)
try:
    from mavlink_takeover import run_sustained_takeover  # noqa: E402
except Exception as _e:  # pragma: no cover - field-bridge must be co-deployed
    run_sustained_takeover = None
    logging.getLogger("mav-bridge").warning(
        "sustained-takeover primitive unavailable (%s) — PL-011 sustained "
        "packets will be refused, not single-shot transmitted.", _e,
    )

log = logging.getLogger("mav-bridge")

# INFO #173 hardening: short TTL for the range-auth lease cache used ONLY by the
# sustained-takeover _halted() hot path. <= 500ms per the reviewer's bound.
RANGE_AUTH_CACHE_TTL_S = 0.5


class _RangeAuthLeaseCache:
    """Short-TTL, fail-closed cache around the range-authorization lease poll
    used by the sustained-takeover _halted() check (INFO #173 hardening).

    WHY: the sustained loop polls _halted() before EVERY frame (~20/s at 20Hz).
    Without caching, each poll makes a BLOCKING HTTP GET
    /api/range-authorization/status, so (a) backend latency throttles the
    effective frame rate and (b) worst-case abort-detection latency is bounded
    by the HTTP timeout instead of the ~50ms frame period. Caching the lease for
    a short TTL (<= 500ms) makes ~2 range-auth polls/sec instead of ~20 (~10x
    less backend load) and removes the per-frame blocking call from the hot path.

    FAIL-CLOSED INVARIANTS (this is the critical part — do NOT weaken):
      * Only a POSITIVE ("authorized") lease is ever cached, and only for TTL.
        A poll that returns not-authorized, times out, or raises is treated as
        lease-OFF (halt) AND INVALIDATES the cache (value=False, expiry reset),
        so a previously-cached "authorized" value can NEVER persist past an
        error or an expiry.
      * The EMERGENCY-ABORT flag (tx_halted) is NOT handled here — it is a local
        boolean checked separately, every frame, with NO caching (see _halted()).
        Only the range-auth HTTP poll is cached.
      * Worst-case: a lease that flips off (or a backend error) is detected
        within at most one frame period + TTL (<= ~550ms at 20Hz + 500ms TTL).

    Clock (`now`) and `poll` are injected so this is deterministically testable.
    is_range_authorized() itself already fails closed (returns False, never
    raises) on any backend error; the try/except here is defense-in-depth so a
    fail-closed halt survives even a poll callable that raises."""

    def __init__(self, poll, ttl_s: float = RANGE_AUTH_CACHE_TTL_S, now=time.monotonic) -> None:
        self._poll = poll
        self._ttl_s = max(0.0, float(ttl_s))
        self._now = now
        self._authorized = False   # last known lease state
        self._expires_at = 0.0     # monotonic deadline of a cached positive lease
        self.poll_count = 0        # exposed for tests: underlying polls actually made

    def authorized(self) -> bool:
        """Return the (possibly cached) live lease state. Fail-closed on error."""
        t = self._now()
        # Reuse a still-fresh POSITIVE lease without hitting the backend.
        if self._authorized and t < self._expires_at:
            return True
        # Cache miss / expired / previously-off: make a fresh live poll.
        self.poll_count += 1
        try:
            live = bool(self._poll())
        except Exception:
            live = False  # defense-in-depth: any raise => fail closed
        if live:
            self._authorized = True
            self._expires_at = t + self._ttl_s
            return True
        # Not authorized / error => halt AND invalidate so nothing stale survives.
        self._authorized = False
        self._expires_at = 0.0
        return False


def _send_tx_ack(ws, request_id: str, ok: bool, error: Optional[str] = None) -> None:
    """Send a real ack back to the server over the same WS connection this
    packet arrived on, after an actual write_frame() attempt (success or
    exception) — this is what lets the server ever transition a detection out
    of AWAITING_ACK. Never sent speculatively / before the write is attempted."""
    if ws is None or not request_id:
        return
    ack = {"type": "tx_ack", "request_id": request_id, "ok": bool(ok), "ts": time.time()}
    if error:
        ack["error"] = str(error)[:300]
    try:
        ws.send(json.dumps(ack))
    except Exception as e:
        log.warning("failed to send tx_ack for request_id=%s: %s", request_id, e)


class MavlinkBridge:
    def __init__(self) -> None:
        self.client = CemaClient()
        self.client.login()

        self.serial_path = cfg("MAVLINK_SERIAL", "/dev/ttyUSB0")
        self.baud = cfg_int("MAVLINK_BAUD", 57600)
        self.rx_enabled = cfg_int("MAVLINK_RX_ENABLED", 1) == 1
        # Bridge-identity secret proving this is a REAL MAVLink TX bridge (not a
        # browser session) when we advertise ourselves as the "mavlink" TX
        # consumer via bridge_hello. Loaded from the bridge host's .env
        # (CEMA_BRIDGE_TOKEN); the backend validates it before trusting our
        # self-advertisement, so a console session cannot forge a fake TX
        # consumer to mask the "NO TX BRIDGE SUBSCRIBED" warning (TX-review
        # MEDIUM). Optional — unset simply means the backend won't register us
        # and defaults to warning "no TX bridge". Never gates the TX path below.
        self.bridge_token = cfg("CEMA_BRIDGE_TOKEN", "") or None

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
        # NOTE: this now RAISES on any failure (including "port not open")
        # instead of silently swallowing it. The caller (on_message, below)
        # depends on that to send an honest tx_ack(ok=False) back to the
        # server — this is precisely the gap that let a deploy report
        # "success" to the operator while nothing was ever written to the
        # real radio.
        if not self.ser or not self.ser.is_open:
            raise RuntimeError("serial port not open")
        self.ser.write(frame)
        self.ser.flush()

    # ---- F-1: sustained maneuver-takeover driver -------------------------
    def _run_sustained(self, ws, pkt: dict, request_id: str) -> None:
        """Drive the bounded, immediately-abortable RC-override controlled
        landing on the live radio. Called only for sustained (PL-011) packets.

        Two independent, LIVE, per-frame kill conditions are wired into the
        primitive's abort check (polled BEFORE every single frame, so either
        stops the stream within one frame period):

          1. tx_halted — the EMERGENCY ABORT flag set by a server 'abort' WS
             message. Stops an in-progress controlled landing immediately.
          2. LIVE range-authorization — is_range_authorized('mavlink') is
             re-polled before EVERY frame (not just once at start), so a lease
             that expires or is disabled mid-stream terminates the takeover.
             Fails closed (treated as halt) on any backend/network error.

        Nothing after an abort is transmitted. Duration and rc_rate are taken
        from the packet; per the commander directive the duration is
        OPERATOR-CONTROLLED (no artificial hard cap) and may be continuous —
        the kill-switch above (tx_halt / range-auth-off, both per-frame) is the
        real safety, not a wall-clock ceiling."""
        if run_sustained_takeover is None:
            _send_tx_ack(ws, request_id, False,
                         "sustained-takeover primitive unavailable on this bridge")
            return

        target_system = int(pkt.get("target_system") or 0)
        target_component = int(pkt.get("target_component") or 1)
        duration_s = float(pkt.get("duration_s") or 0.0)
        rc_rate_hz = float(pkt.get("rc_rate_hz") or 0.0)
        # Commander directive: operator-controlled duration (no artificial cap).
        # continuous=True re-emits the controlled-landing frame until the operator
        # stops it (EMERGENCY ABORT / range-auth-off, both routed through
        # _halted). The kill-switch is unchanged.
        continuous = bool(pkt.get("continuous"))

        # INFO #173 hardening: cache the range-auth lease for a short TTL so the
        # per-frame _halted() check does NOT make a blocking HTTP GET on every
        # single frame. Fresh per takeover run so no state leaks between runs.
        # NOTE: tx_halted is deliberately NOT routed through this cache — see
        # below.
        range_auth = _RangeAuthLeaseCache(
            poll=lambda: self.client.is_range_authorized("mavlink"),
            ttl_s=RANGE_AUTH_CACHE_TTL_S,
        )

        def _halted() -> bool:
            # Checked before EVERY frame by the primitive. Either an EMERGENCY
            # ABORT or a lease that is no longer live terminates the stream.
            #
            # tx_halted (EMERGENCY ABORT) is a local boolean and is checked here
            # EVERY frame with NO caching — the abort flag must never be stale,
            # so an EMERGENCY ABORT still stops the stream within one frame
            # period, unchanged by this hardening.
            if self.tx_halted:
                return True
            # The range-auth lease is the only thing cached (short TTL). A lease
            # that expires/errors is detected within one frame period + TTL, and
            # the cache fails closed + self-invalidates on any poll error, so a
            # stale "authorized" value can't carry the stream past an expiry.
            if not range_auth.authorized():
                log.warning("sustained takeover HALTING: range-auth (mavlink) no "
                            "longer live (request_id=%s)", request_id)
                return True
            return False

        log.info("TX → serial (SUSTAINED takeover): tgt_sys=%s dur=%.2fs rate=%.1fHz "
                 "request_id=%s", target_system, duration_s, rc_rate_hz, request_id)
        try:
            res = run_sustained_takeover(
                send_frame=self.write_frame,
                target_system=target_system,
                target_component=target_component,
                duration_s=duration_s,
                rc_rate_hz=rc_rate_hz,
                continuous=continuous,
                tx_halted=_halted,
                target_protocol=pkt.get("target_protocol"),
                target_link_legacy_mavlink=bool(pkt.get("target_link_legacy_mavlink")),
            )
        except Exception as e:
            log.error("SUSTAINED takeover crashed (request_id=%s): %s", request_id, e)
            _send_tx_ack(ws, request_id, False, f"sustained takeover error: {e}")
            return

        log.info("SUSTAINED takeover done (request_id=%s): frames=%d release=%d "
                 "stopped_early=%s not_applicable=%s reason=%s", request_id,
                 res.frames_sent, res.release_frames_sent, res.stopped_early,
                 res.not_applicable, res.reason)
        # ok=True with >0 frames means the radio actually carried the stream.
        # not_applicable / error => honest failure ack (nothing effective TX'd).
        if res.not_applicable or res.error or (not res.ok):
            _send_tx_ack(ws, request_id, False, res.error or res.reason)
        elif res.frames_sent == 0:
            # e.g. aborted before the first frame — nothing reached the radio.
            _send_tx_ack(ws, request_id, False, res.reason or "no frames transmitted")
        else:
            _send_tx_ack(ws, request_id, True)

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
            # Announce this connection as the MAVLink TX consumer so the backend
            # can honestly report "a TX bridge IS subscribed" at fire time — and,
            # crucially, warn the operator when NONE is (closing the false-green
            # gap where a deploy with no bridge looked 'in flight' until an 8s
            # TX_TIMEOUT). Browsers/telemetry viewers never send this, so they
            # never count as a TX consumer. Includes the shared bridge-identity
            # secret (CEMA_BRIDGE_TOKEN) so the backend accepts THIS
            # self-advertisement but rejects a browser/console session forging
            # the same message (TX-review MEDIUM). Best-effort: a failure here
            # does not affect the RX or TX gating below.
            try:
                hello = {"type": "bridge_hello", "consumers": ["mavlink"]}
                if self.bridge_token:
                    hello["token"] = self.bridge_token
                ws.send(json.dumps(hello))
            except Exception as e:
                log.warning("failed to send bridge_hello: %s", e)

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

            pkt = data.get("packet", {})
            request_id = pkt.get("request_id")

            if self.tx_halted:
                log.warning("TX suppressed: EMERGENCY ABORT in effect — dropping frame, not forwarding to serial.")
                _send_tx_ack(_ws, request_id, False, "tx halted (EMERGENCY ABORT in effect on bridge)")
                return

            # ---- Range-authorization gate (replaces the old static
            # CEMA_AUTHORIZED_RANGE env var — see
            # backend/RANGE_AUTHORIZATION_REDESIGN.md). Independent,
            # additional check made at the moment of transmission, live
            # against the backend — NOT trusting any value embedded in this
            # WS message itself, so a stale/replayed packet can't carry
            # stale authorization forward past an expiry/disable that
            # happened in between. Fails closed on any network/auth error.
            if not self.client.is_range_authorized("mavlink"):
                log.error(
                    "REFUSING to forward frame for request_id=%s: range authorization "
                    "for effect=mavlink is not enabled (or could not be verified) via "
                    "GET /api/range-authorization/status. An operator must arm it from "
                    "the app before this bridge will transmit to serial.",
                    request_id,
                )
                _send_tx_ack(_ws, request_id, False,
                            "bridge refused: range-authorization (effect=mavlink) not enabled")
                return

            # ---- F-1: SUSTAINED maneuver-takeover (PL-011) dispatch ----------
            # A sustained packet is NOT a single frame — it carries a bounded
            # controlled-landing PLAN (mode/duration_s/rc_rate_hz). Previously
            # this handler ignored that metadata and wrote exactly ONE frame,
            # so the primitive never actually ran in the live path. Now dispatch
            # into run_sustained_takeover, which re-emits the RC-override frame
            # at rc_rate_hz for the server-clamped duration and aborts within one
            # frame period on tx-halt OR a mid-stream range-lease expiry.
            if pkt.get("mode") == "rc_override_takeover" or pkt.get("sustained"):
                # Run the bounded stream on a SEPARATE thread so this WS reader
                # thread stays free to receive a subsequent 'abort' message and
                # flip self.tx_halted — which the sustained loop polls before
                # every frame. If we ran it inline here, on_message would block
                # and the EMERGENCY ABORT could never be delivered mid-stream.
                threading.Thread(
                    target=self._run_sustained, args=(_ws, pkt, request_id),
                    name=f"sustained-{request_id[:8]}", daemon=True,
                ).start()
                return

            hex_str = pkt.get("hex")
            if not hex_str:
                return
            try:
                frame = binascii.unhexlify(hex_str)
            except binascii.Error as e:
                _send_tx_ack(_ws, request_id, False, f"invalid hex payload: {e}")
                return
            log.info("TX → serial: msgid=%s tgt_sys=%s len=%d bytes (%s) request_id=%s",
                     pkt.get("decoded", {}).get("message_id"),
                     pkt.get("target_system"),
                     len(frame),
                     pkt.get("payload_name") or "manual",
                     request_id)
            try:
                self.write_frame(frame)
            except Exception as e:
                # Real failure signal — the frame did NOT reach the radio.
                log.error("TX FAILED writing to serial (request_id=%s): %s", request_id, e)
                _send_tx_ack(_ws, request_id, False, str(e))
                return
            log.info("TX confirmed written to serial (request_id=%s)", request_id)
            _send_tx_ack(_ws, request_id, True)

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
                # Let exceptions propagate — on_message's try/except around
                # write_frame() is what turns a real failure here into an
                # honest tx_ack(ok=False) back to the server. Swallowing it
                # here (as the previous version did) is exactly what allowed
                # a deploy to look successful when nothing reached the radio.
                self._pymav.write(frame)
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
