#!/usr/bin/env python3
"""SDR MAVLink Inject Bridge — App (backend/server.py) <-> HackRF over-the-air
GFSK MAVLink command injection (the no-pairing, adversary-grade takeover path).

Structurally PARALLEL to field-bridge/gnss_spoof_bridge.py and
field-bridge/jam_bridge.py (own class, own WS message types, own local
abort/halt state). jam_bridge.py / gnss_spoof_bridge.py are UNCHANGED by this
work.

=============================================================================
WHAT THIS RADIATES (and, just as importantly, WHAT IT DOES NOT)
=============================================================================
The paired-SiK takeover (field-bridge/mavlink_takeover.py + the SiK radio,
PL-011 via /payloads/deploy) only reaches a drone we are ALREADY PAIRED WITH on
a SiK radio we control. THIS bridge is the ADVERSARY-GRADE alternative: it
takes a byte-accurate MAVLink command frame built by
field-bridge/sdr_mavlink_inject.py (which reuses backend/mavlink_codec.py
verbatim for the bytes), GFSK-modulates it onto baseband IQ matching the PHY of
a 3DR/SiK/RFD900-style telemetry link, and radiates it over the air at the
target link's frequency via the pinned TX HackRF — no SiK pairing, no shared
NetID.

HONEST FIDELITY / SCOPE (project rule: do NOT overclaim capability):
  * WORKS against a FIXED-FREQUENCY, UNENCRYPTED MAVLink telemetry link (a
    3DR/SiK/RFD900 radio parked on one channel with hopping DISABLED, or a
    transparent GFSK serial link carrying raw MAVLink). The injected
    COMMAND_LONG is indistinguishable from the real ground station.
  * DOES NOT WORK against FHSS / frequency-hopping links (SiK/RFD900 DEFAULT
    configs hop across the ISM band on a NetID-derived sequence). Hop-pattern
    following is NOT implemented (v1) — see sdr_mavlink_inject.py's "REMAINING".
    A fixed-frequency burst lands on a hopping target only ~1/N of the time.
  * N/A for MAVLink-signed / encrypted / proprietary control links (DJI
    OcuSync, ELRS/CRSF, DSMX, etc.) — there is no unauthenticated MAVLink to
    inject into. Against those the defeat is JAMMING (field-bridge/hackrf_jam.py
    via jam_bridge.py), which remains the universal defeat.
The backend enforces this same encrypted/FHSS honesty gate BEFORE the request
ever reaches this bridge (mavlink_codec.link_is_overridable /
classify_override_link on the target detection's protocol).

=============================================================================
INTERPRETER REQUIREMENT — NOTE THE DIFFERENCE FROM operator_jam_bridge.py
=============================================================================
Unlike the OPERATOR jam bridge (operator_jam_bridge.py), this bridge does NOT
require GNU Radio / gr-osmosdr. field-bridge/sdr_mavlink_inject.py is PURE
numpy (it writes an interleaved-int8 IQ file); the actual transmit is the same
hackrf_transfer subprocess jam_bridge.py / gnss_spoof_bridge.py already use
(field-bridge/hackrf_jam.transmit_iq_file). So this bridge runs in the PLAIN
field-bridge venv (numpy + requests + websocket-client) — the same dedicated
venv jam_bridge.py uses — NOT the GNU-Radio-enabled system python operator-jam
needs. If numpy / the sdr_mavlink_inject module cannot be imported, or
hackrf_transfer is not installed, this bridge FAILS CLOSED with a clean error;
it never falls through to an ungoverned transmit.

=============================================================================
IT ADDS *NO* NEW AUTHORIZATION PATH — the SAME governed spine as every TX
=============================================================================
Before a single byte reaches hackrf_transfer, an independent chain of gates —
ALL of which must pass — is enforced (the backend-side gates are enforced in
backend/server.py's deploy_mavlink_sdr_inject before this bridge is ever
messaged; the bridge-side gates below are enforced here again):

  Backend (backend/server.py deploy_mavlink_sdr_inject):
     a. require_commander.
     b. single-use arm_token bound to effect=mavlink_sdr_inject AND this target.
     c. single-use mavlink_sdr_inject_confirm_token (SafetyGate two-step proof;
        NOT interchangeable with jam / gnss_spoof confirm tokens).
     d. IFF fratricide interlock — a takeover aimed at a CONFIRMED-FRIENDLY is
        hard-blocked unless the single-use, target-bound commander friendly-fire
        ack is presented.
     e. _check_tx_not_halted (EMERGENCY ABORT).
     f. backend-side range-authorization lease (effect=mavlink_sdr_inject).
     g. encrypted/FHSS honesty gate (never transmit uselessly).

  THIS BRIDGE, independently:
     a. LIVE GET /api/range-authorization/status?effect=mavlink_sdr_inject at the
        moment of transmission — a SEPARATE lease from effect=jam / effect=mavlink
        (arming those does NOT arm this). Fails closed on any network/auth error.
     b. mavlink_sdr_inject_confirm_token shape check (defense in depth). OWN
        floor constant (MIN_CONFIRM_TOKEN_LEN below), deliberately NOT shared
        with jam_bridge / gnss_spoof_bridge.
     c. HACKRF_TX_SERIAL device-pin — REQUIRED. Without a pinned TX serial this
        bridge FAILS CLOSED (it will NOT fall back to index-0 "whichever HackRF
        responds first", which could key the RX detection radio).
     d. local EMERGENCY ABORT (tx_halted): refuses new requests AND terminates
        an in-progress hackrf_transfer immediately.

None of these gates replace any other. Removing any one is a regression.

BEHAVIOR: OPERATOR-CONTROLLED — the injected command is re-emitted `repeat`
times back-to-back (operator-set, no artificial cap), OR, when the request
sets continuous=True, re-emitted on a loop (hackrf_transfer -R) until the
operator stops it. Per the commander directive there is no artificial repeat
or on-air-window ceiling. The kill-switch is unchanged: EMERGENCY ABORT /
tx_halt terminates the live hackrf_transfer immediately (stop_event +
tx_halt_check are both honored), so a continuous inject is always
switchable-off.

Requires: websocket-client, requests, numpy (see field-bridge/requirements.txt).

Env (systemd EnvironmentFile, see cema-sdr-mavlink-bridge.service):
  CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD  — same as the other bridges.
  CEMA_BRIDGE_TOKEN                          — diagnostic bridge-identity secret.
  HACKRF_TX_SERIAL                           — REQUIRED: the pinned TX unit's
                                               serial (e.g. the ...930c TX unit).
                                               Without it this bridge fails
                                               closed rather than key the RX radio.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from typing import Optional
from urllib.parse import quote

import requests
import websocket  # websocket-client

from hackrf_jam import transmit_iq_file
import hackrf_jam

log = logging.getLogger("sdr-mavlink-inject-bridge")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Guarded import of the pure-numpy PHY modulator. Imported at module load but
# wrapped so a missing numpy / module on a field host does NOT crash the
# service at import — instead each inject request fails cleanly with
# "SDR MAVLink inject module unavailable" (never an ungoverned transmit). This
# module deliberately does NOT depend on GNU Radio (see the interpreter note in
# the module docstring), so unlike operator_jam_bridge.py there is no gnuradio
# import to guard here — only the numpy-backed modulator.
try:
    import sdr_mavlink_inject as _inj
    _INJ_IMPORT_ERROR: Optional[str] = None
except Exception as e:  # pragma: no cover - only hit on a broken field host
    _inj = None
    _INJ_IMPORT_ERROR = f"{type(e).__name__}: {e}"

# Own floor constant — deliberately NOT shared with jam_bridge.MIN_CONFIRM_TOKEN_LEN
# / gnss_spoof_bridge.MIN_CONFIRM_TOKEN_LEN despite the same value today, so a
# future change to one cannot silently change the others. Real tokens minted by
# backend/server.py's _issue_mavlink_sdr_inject_confirm_token() are UUID4 (36 chars).
MIN_CONFIRM_TOKEN_LEN = 20

# Commander directive: NO artificial repeat/window cap on the takeover path.
# repeat is now operator-controlled (only floored at 1); a request may also ask
# for continuous=True to re-emit the command on a loop until the operator stops
# it. MAX_REPEAT is retained ONLY as a non-binding default-library reference
# value (it is no longer enforced as a ceiling). The kill-switch is unchanged:
# every transmit still stops instantly on EMERGENCY ABORT (stop_event) / tx_halt.
MAX_REPEAT = 20

# Commands this bridge will build (mirrors sdr_mavlink_inject.COMMAND_BUILDERS
# and the backend MavlinkSdrInjectBody pattern). A request for anything else is
# refused rather than silently defaulted.
SUPPORTED_COMMANDS = {"force_land", "rth", "disarm", "flight_termination", "maneuver_takeover"}


def _looks_like_real_confirm_token(token: Optional[str]) -> bool:
    return bool(token) and isinstance(token, str) and len(token) >= MIN_CONFIRM_TOKEN_LEN


def _cfg(key: str, default: Optional[str] = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"Missing required config: {key}")
    return v


class SdrMavlinkInjectBridge:
    def __init__(self) -> None:
        self.api_url = _cfg("CEMA_API_URL").rstrip("/")
        self.email = _cfg("CEMA_EMAIL")
        self.password = _cfg("CEMA_PASSWORD")
        self.token: Optional[str] = None
        # Bridge-identity secret (same role as jam_bridge.py's). Best-effort;
        # never gates the independent bridge-side gates below.
        self.bridge_token: Optional[str] = os.environ.get("CEMA_BRIDGE_TOKEN") or None

        # Device-pin: the ACTUAL pin is hackrf_jam.HACKRF_TX_SERIAL (read at
        # hackrf_jam import time and used to build `-d <serial>` for every
        # hackrf_transfer). We surface it here for the fail-closed check — if it
        # is unset we refuse to transmit rather than key the RX detection radio.
        self.tx_serial = hackrf_jam.HACKRF_TX_SERIAL
        if not self.tx_serial:
            log.warning(
                "HACKRF_TX_SERIAL is not set — SDR MAVLink inject FAILS CLOSED without a "
                "pinned TX serial (it will not fall back to index-based 'hackrf=0', which "
                "could key the RX detection radio). Set HACKRF_TX_SERIAL to the TX unit "
                "serial before engaging SDR MAVLink inject.")

        if _inj is None:
            log.warning(
                "sdr_mavlink_inject module NOT importable at startup (%s). SDR MAVLink "
                "inject requests will fail cleanly until this is resolved (this bridge "
                "needs numpy + the field-bridge deps — it does NOT need GNU Radio).",
                _INJ_IMPORT_ERROR)

        self.ws: Optional[websocket.WebSocketApp] = None
        self.stop_flag = threading.Event()

        # Own EMERGENCY ABORT state — NOT shared with the other bridges, which
        # run as separate OS processes with their own tx_halted flags.
        self.tx_halted = False
        self._active_stop_event: Optional[threading.Event] = None
        self._active_lock = threading.Lock()

        log.info(
            "Range authorization for effect=mavlink_sdr_inject is checked LIVE against the "
            "backend (GET /api/range-authorization/status?effect=mavlink_sdr_inject) at the "
            "moment of each transmission — a SEPARATE lease from effect=jam / effect=mavlink. "
            "An operator must arm it from the app before this bridge will transmit any RF."
        )

    # ---- auth --------------------------------------------------------
    def login(self) -> str:
        r = requests.post(f"{self.api_url}/api/auth/login",
                          json={"email": self.email, "password": self.password},
                          timeout=10)
        r.raise_for_status()
        self.token = r.json()["token"]
        log.info("Authenticated as %s", self.email)
        return self.token

    def ensure_token(self) -> str:
        return self.token or self.login()

    # ---- range authorization (own effect key, "mavlink_sdr_inject") ----
    def is_range_authorized(self, effect: str = "mavlink_sdr_inject") -> bool:
        """Live GET /api/range-authorization/status?effect=mavlink_sdr_inject check
        made at the moment of transmission — NOT trusting anything embedded in the
        mavlink_inject_request WS message itself. FAILS CLOSED (returns False) on
        ANY network/auth error. Never raises. Mirrors
        gnss_spoof_bridge.GnssSpoofBridge.is_range_authorized exactly, against the
        mavlink_sdr_inject effect key."""
        try:
            r = requests.get(
                f"{self.api_url}/api/range-authorization/status",
                params={"effect": effect},
                headers={"Authorization": f"Bearer {self.ensure_token()}"},
                timeout=10,
            )
            if r.status_code == 401:
                self.token = None
                r = requests.get(
                    f"{self.api_url}/api/range-authorization/status",
                    params={"effect": effect},
                    headers={"Authorization": f"Bearer {self.ensure_token()}"},
                    timeout=10,
                )
            r.raise_for_status()
            return bool(r.json().get("enabled") is True)
        except Exception as e:
            log.warning(
                "range-authorization status check FAILED for effect=%s (%s) — "
                "treating as NOT authorized (fail closed).", effect, e,
            )
            return False

    # ---- ack helper ----------------------------------------------------
    def _send_ack(self, ws, request_id: str, phase: str,
                  ok: Optional[bool] = None, error: Optional[str] = None) -> None:
        """Sends {"type": "mavlink_inject_ack", ...} back over the same WS
        connection. phase is one of: "started" | "complete" | "failed" |
        "stopped". Only backend/server.py's _handle_mavlink_inject_ack ever acts
        on these — never speculative, always sent after a real attempt/outcome."""
        if ws is None or not request_id:
            return
        msg = {"type": "mavlink_inject_ack", "request_id": request_id, "phase": phase, "ts": time.time()}
        if ok is not None:
            msg["ok"] = bool(ok)
        if error:
            msg["error"] = str(error)[:300]
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("failed to send mavlink_inject_ack (phase=%s) for request_id=%s: %s",
                       phase, request_id, e)

    # ---- WS handling ---------------------------------------------------
    def _handle_inject_request(self, ws, data: dict) -> None:
        request_id = data.get("request_id")
        actor = data.get("actor", "?")

        # ---- Gate A: live range-authorization check against the backend. --
        if not self.is_range_authorized("mavlink_sdr_inject"):
            log.error(
                "REFUSING mavlink_inject_request %s from %s: range authorization for "
                "effect=mavlink_sdr_inject is not enabled (or could not be verified) via GET "
                "/api/range-authorization/status. The app approved the arm/confirm tokens for "
                "this request, but the range-authorization lease is not currently armed — an "
                "operator must enable it from the app first.",
                request_id, actor,
            )
            self._send_ack(
                ws, request_id, "failed", ok=False,
                error="bridge refused: range-authorization (effect=mavlink_sdr_inject) not enabled "
                      "(independent bridge-level gate, separate from the app's arm-token check)")
            return

        # ---- Gate B: confirmation-token shape check (defense in depth). ---
        confirm_token = data.get("mavlink_sdr_inject_confirm_token")
        if not _looks_like_real_confirm_token(confirm_token):
            log.error(
                "REFUSING mavlink_inject_request %s from %s: missing/malformed "
                "mavlink_sdr_inject_confirm_token. This bridge only transmits for requests "
                "carrying a real confirmation token minted by the backend at the moment an "
                "operator completed the app's SafetyGate confirm — refusing to guess.",
                request_id, actor,
            )
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="bridge refused: missing/malformed mavlink_sdr_inject confirmation token")
            return

        # ---- Gate C: HACKRF_TX_SERIAL device-pin (fail closed). -----------
        if not self.tx_serial:
            log.error(
                "REFUSING mavlink_inject_request %s: HACKRF_TX_SERIAL is not set — refusing "
                "to transmit without a pinned TX serial (would risk keying the RX detection "
                "radio).", request_id)
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="bridge refused: no pinned TX serial (HACKRF_TX_SERIAL unset) — "
                                 "fail closed rather than key the RX radio")
            return

        # ---- Gate D: SDR modulator availability (fail closed, clean error). --
        if _inj is None:
            log.error("REFUSING mavlink_inject_request %s: sdr_mavlink_inject unavailable (%s).",
                     request_id, _INJ_IMPORT_ERROR)
            self._send_ack(ws, request_id, "failed", ok=False,
                           error=f"SDR MAVLink inject module unavailable: {_INJ_IMPORT_ERROR}")
            return

        # ---- Gate E: local EMERGENCY ABORT state. -------------------------
        if self.tx_halted:
            log.warning("TX suppressed: EMERGENCY ABORT in effect on this bridge — refusing "
                       "mavlink_inject_request %s.", request_id)
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="tx halted (EMERGENCY ABORT in effect on bridge)")
            return

        try:
            command = str(data.get("command", "force_land"))
            if command not in SUPPORTED_COMMANDS:
                raise ValueError(f"unsupported command {command!r} (supports {sorted(SUPPORTED_COMMANDS)})")
            target_system = int(data.get("target_system", 1))
            target_component = int(data.get("target_component", 1))
            center_freq_mhz = float(data.get("center_freq_mhz", _inj.DEFAULT_CENTER_FREQ_MHZ))
            air_rate_bps = float(data.get("air_rate_bps", _inj.DEFAULT_AIR_DATA_RATE_BPS))
            deviation_hz = float(data.get("deviation_hz", _inj.DEFAULT_DEVIATION_HZ))
            bt = float(data.get("bt", _inj.DEFAULT_BT))
            bit_order = str(data.get("bit_order", _inj.DEFAULT_BIT_ORDER))
            tx_gain = int(data.get("tx_gain", 20))
            # NO artificial cap (commander directive): operator-controlled repeat
            # (floored at 1 only). continuous=True re-emits the command on a loop
            # until the operator stops it (still tx_halt/abort-stoppable).
            repeat = max(1, int(data.get("repeat", 3)))
            continuous = bool(data.get("continuous"))
        except (TypeError, ValueError) as e:
            self._send_ack(ws, request_id, "failed", ok=False,
                           error=f"invalid mavlink_sdr_inject parameters: {e}")
            return

        # A target_system of 0 broadcasts to every craft in range — the backend
        # already refuses this, but re-check here (defense in depth) so a stale/
        # replayed WS payload can never drive a broadcast injection from a bridge.
        if target_system in (0, None):
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="bridge refused: target_system 0/None would broadcast to all craft")
            return

        stop_event = threading.Event()
        with self._active_lock:
            self._active_stop_event = stop_event

        def on_started(_proc) -> None:
            log.warning(
                "TRANSMITTING SDR MAVLink inject: cmd=%s -> sys %d @ %.3f MHz, air=%.0f bps, "
                "repeat=%d, gain=%d (request %s, requested by %s)",
                command, target_system, center_freq_mhz, air_rate_bps, repeat, tx_gain,
                request_id, actor,
            )
            self._send_ack(ws, request_id, "started", ok=True)

        params = {
            "command": command,
            "target_system": target_system,
            "target_component": target_component,
            "center_freq_mhz": center_freq_mhz,
            "air_rate_bps": air_rate_bps,
            "deviation_hz": deviation_hz,
            "bt": bt,
            "bit_order": bit_order,
            "tx_gain": tx_gain,
            "repeat": repeat,
            "continuous": continuous,
            "request_id": request_id,
            "actor": actor,
        }

        def run() -> None:
            result = self._do_inject(params, stop_event, on_started)
            with self._active_lock:
                if self._active_stop_event is stop_event:
                    self._active_stop_event = None
            if result.get("stopped_early"):
                log.warning("SDR MAVLink inject STOPPED EARLY by EMERGENCY ABORT (request %s).", request_id)
                self._send_ack(ws, request_id, "stopped", ok=True)
            elif result.get("ok"):
                log.info("SDR MAVLink inject complete (request %s).", request_id)
                self._send_ack(ws, request_id, "complete", ok=True)
            else:
                log.error("SDR MAVLink inject FAILED (request %s): %s", request_id, result.get("error"))
                self._send_ack(ws, request_id, "failed", ok=False, error=result.get("error"))

        threading.Thread(target=run, name=f"sdr-mavlink-inject-{request_id}", daemon=True).start()

    def _do_inject(self, params: dict, stop_event: threading.Event, on_started) -> dict:
        """Build the byte-accurate MAVLink frame, GFSK-modulate it to an IQ file,
        and transmit it DEVICE-PINNED (hackrf_jam.transmit_iq_file addresses the
        pinned HACKRF_TX_SERIAL via `-d <serial>` and serializes behind that
        unit's per-serial lock). Bounded + abortable (stop_event). Returns the
        {"ok","stopped_early","error"} shape the base handler acks on. Never
        raises — a modulation/TX failure is reported as ok=False, never an
        uncaught crash or an ungoverned transmit."""
        try:
            frame = _inj.build_command_frame(
                params["command"], params["target_system"], params["target_component"])
            # Keep the generated IQ at the SAME sample rate hackrf_transfer plays
            # it back at (hackrf_jam.SAMPLE_RATE_HZ) — a mismatch would time-scale
            # every symbol period and make the GFSK off-rate/undecodable.
            iq_path = _inj.write_iq_file(
                frame, None,
                sample_rate_hz=hackrf_jam.SAMPLE_RATE_HZ,
                air_data_rate_bps=params["air_rate_bps"],
                deviation_hz=params["deviation_hz"],
                bt=params["bt"],
                bit_order=params["bit_order"],
                repeat=params["repeat"],
            )
            # On-air playback time of the file (its real length). Used as the
            # bounded failsafe deadline for a one-shot inject. NO artificial cap
            # (commander directive) — the window follows the real content length.
            info = _inj.describe_modulation(
                frame,
                sample_rate_hz=hackrf_jam.SAMPLE_RATE_HZ,
                air_data_rate_bps=params["air_rate_bps"],
                deviation_hz=params["deviation_hz"],
                bt=params["bt"],
                bit_order=params["bit_order"],
                repeat=params["repeat"],
            )
            # continuous -> None: transmit_iq_file loops the frame (hackrf_transfer
            # -R) until the operator stops it (stop_event / tx_halt). Otherwise a
            # bounded window equal to the frame's own on-air length.
            tx_window_s = None if params.get("continuous") else \
                max(0.05, float(info.get("on_air_duration_s", 0.05)))
        except Exception as e:
            return {"ok": False, "stopped_early": False,
                    "error": f"SDR MAVLink inject modulation failed: {e}"}

        try:
            result = transmit_iq_file(
                iq_path, params["center_freq_mhz"], tx_window_s, params["tx_gain"],
                stop_event=stop_event, on_started=on_started,
                # Poll tx_halt too so a continuous inject stops instantly on
                # EMERGENCY ABORT even if the stop_event path is ever missed.
                tx_halt_check=lambda: self.tx_halted)
        finally:
            try:
                os.unlink(iq_path)
            except OSError:
                pass
        return result

    def start_ws_subscriber(self) -> None:
        ws_scheme = "wss" if self.api_url.startswith("https") else "ws"
        host = self.api_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/api/ws/mavlink?token={quote(self.ensure_token())}"
        log.info("Subscribing to bridge-control WS at %s?token=<jwt>", url.split("?", 1)[0])

        def on_open(_ws):
            log.info("WS connected — sdr-mavlink-inject bridge ready.")
            # Announce this connection as the SDR-MAVLink-inject TX consumer so
            # the backend can honestly report at fire time whether a bridge is
            # actually subscribed (and warn when none is). Includes the shared
            # bridge-identity secret so the backend accepts THIS self-advertisement
            # but rejects a browser/console session forging the same message.
            # Best-effort; never gates the independent bridge-side gates.
            try:
                hello = {"type": "bridge_hello", "consumers": ["mavlink_sdr_inject"]}
                if self.bridge_token:
                    hello["token"] = self.bridge_token
                _ws.send(json.dumps(hello))
            except Exception as e:
                log.warning("failed to send bridge_hello: %s", e)

        def on_message(ws, raw):
            try:
                data = json.loads(raw)
            except Exception:
                return
            mtype = data.get("type")

            if mtype == "abort":
                self.tx_halted = True
                with self._active_lock:
                    active = self._active_stop_event
                if active is not None:
                    log.warning("EMERGENCY ABORT received — terminating in-progress SDR "
                               "MAVLink inject transmission NOW.")
                    active.set()
                else:
                    log.warning("EMERGENCY ABORT received (operator=%s) — future mavlink_inject "
                               "requests refused until resume.", data.get("operator"))
                return
            if mtype == "resume":
                log.warning("RESUME received (operator=%s) — mavlink_inject requests re-enabled.",
                           data.get("operator"))
                self.tx_halted = False
                return
            if mtype != "mavlink_inject_request":
                return  # ignore "packet"/"jam_request"/"gnss_spoof_request"/anything else

            self._handle_inject_request(ws, data)

        def on_error(_ws, err):
            log.warning("WS error: %s", str(err)[:300])

        def on_close(_ws, code, reason):
            log.warning("WS closed (%s %s); reconnecting in 2s", code, reason)

        def run_forever():
            while not self.stop_flag.is_set():
                self.ws = websocket.WebSocketApp(
                    url, on_open=on_open, on_message=on_message,
                    on_error=on_error, on_close=on_close,
                )
                try:
                    self.ws.run_forever(ping_interval=30, ping_timeout=10)
                except Exception as e:
                    log.warning("ws crash: %s", e)
                if not self.stop_flag.is_set():
                    time.sleep(2)

        threading.Thread(target=run_forever, name="sdr-mavlink-inject-ws-subscriber", daemon=True).start()

    def run(self) -> int:
        self.login()
        self.start_ws_subscriber()

        import signal

        def _sig(*_):
            log.info("stopping.")
            self.stop_flag.set()
            with self._active_lock:
                if self._active_stop_event is not None:
                    self._active_stop_event.set()
            if self.ws:
                try: self.ws.close()
                except Exception: pass
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        while not self.stop_flag.is_set():
            time.sleep(0.5)
        return 0


def main() -> int:
    return SdrMavlinkInjectBridge().run()


if __name__ == "__main__":
    sys.exit(main())
