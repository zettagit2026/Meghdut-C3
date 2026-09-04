#!/usr/bin/env python3
"""Jam Bridge — App (backend/server.py) <-> real HackRF barrage-jam TX.

Connects to the same control WebSocket the MAVLink bridge uses
(ws://<backend>/api/ws/mavlink, JWT auth via query string), listens for
{"type": "jam_request", ...} messages sent by POST /api/payloads/jam, and
executes a REAL bounded-duration HackRF noise-burst transmission via
field-bridge/hackrf_jam.py's transmit_burst() — the exact same
hackrf_transfer invocation used by that script's interactive CLI.

=============================================================================
WHY THIS SCRIPT EXISTS (and why it does not just shell out to hackrf_jam.py)
=============================================================================
hackrf_jam.py's CLI safety gate ends in a blocking input() call: the operator
must physically type 'TRANSMIT' at a terminal before any RF leaves the
HackRF. That is a genuine, deliberate, hard-to-fumble-into human
confirmation. A WS-driven bridge process has no terminal to type into, so
that exact mechanism cannot be reused verbatim — but the INTENT behind it
(a human must deliberately, unambiguously choose to transmit, immediately
before it happens) must be preserved, not quietly dropped.

This script preserves that intent with a chain of independent gates, ALL of
which must pass before a single byte reaches hackrf_transfer:

  1. Frontend (frontend/src/pages/Jamming.jsx, reusing
     frontend/src/components/SafetyGate.jsx's existing rigor): a 5-point
     safety checklist the operator must explicitly tick, followed by a
     two-click ARM & FIRE -> CONFIRM FIRE sequence. This is the human
     attention/deliberateness gate — the actual replacement for physically
     typing 'TRANSMIT'.

  2. Backend (backend/server.py):
       a. require_commander — jamming requires the elevated role, same as
          any other kinetic/broadcast action.
       b. a freshly-issued, single-use arm_token from POST /api/arm — the
          existing CRITICAL-severity second factor, unconditionally required
          for jamming (jamming is always treated as CRITICAL severity).
       c. a freshly-issued, single-use jam_confirm_token from POST
          /api/jam/confirm — minted ONLY at the instant the frontend's
          SafetyGate onConfirm fires (i.e. only after step 1's checklist +
          two-click confirm actually happened). This token is forwarded,
          already-consumed-by-the-backend, in the jam_request message this
          bridge receives — its presence and shape is the bridge's evidence
          that a real human confirmation occurred upstream, not a value any
          caller could casually fabricate ("true", "1", etc. are rejected —
          see _looks_like_real_confirm_token below).
       d. _check_tx_not_halted — EMERGENCY ABORT (any operator) blocks new
          jam requests same as any other TX.

  3. THIS BRIDGE, independently of everything above:
       a. A LIVE GET /api/range-authorization/status?effect=jam call to the
          backend, made at the moment of transmission (see
          is_range_authorized() below and
          backend/RANGE_AUTHORIZATION_REDESIGN.md for the full threat model).
          This replaces the former static CEMA_AUTHORIZED_RANGE=1 bridge-host
          env var: it is still a deliberately SEPARATE gate from the app-side
          arm-token/UI-confirmation chain above — the app approving a jam
          request (arm_token + jam_confirm_token) is necessary but not
          sufficient; the range-authorization lease must ALSO be currently
          armed (an explicit, GUI-driven, auto-expiring 15-minute decision an
          operator makes from the app, per RANGE_AUTHORIZATION_REDESIGN.md).
          This bridge does NOT trust any authorization value embedded in the
          jam_request message itself — it makes its own independent call
          every time, so a stale/replayed WS message can't carry forward an
          authorization that has since expired or been disabled. Fails
          closed (treated as NOT authorized) on any network/auth error
          reaching the backend.
       b. jam_confirm_token shape check (non-trivial, non-guessable-length
          string) — defense in depth in case (2c) is ever bypassed upstream.
       c. tx_halted (EMERGENCY ABORT) is also honored locally: if an abort
          arrives WHILE a burst is transmitting, this bridge terminates the
          live hackrf_transfer process immediately (see stop_event below) —
          not just refuses new requests, actually kills an in-progress
          transmission. This closes a gap the naive port of the old
          bounded-duration model would have left open.

None of these gates replace any other — they are independent and ALL must
pass. Removing any one of them (frontend checklist, arm_token,
jam_confirm_token, or this bridge's own live range-authorization check) is a
regression and must not be done to make testing "more convenient".

=============================================================================
BEHAVIOR: BOUNDED BURST ONLY — NOT CONTINUOUS
=============================================================================
hackrf_jam.py supports an interactive --continuous mode (repeated bursts
until the *local* operator presses Ctrl+C at the terminal). That mode is
DELIBERATELY NOT exposed through this bridge / the app UI: an unattended,
WS-triggered, continuous RF transmission with no local human able to press
Ctrl+C is a materially different (and much riskier) capability than a
single hard-capped burst, and building it was explicitly out of scope for
this integration. Every jam this bridge performs is a single bounded burst,
capped at hackrf_jam.MAX_DURATION_S seconds (currently 10s), matching
transmit_burst()'s own enforcement. If a longer engagement window is ever
needed, that should be a fresh, separately-reviewed safety decision — not a
side effect of this integration.

Requires: websocket-client, requests, numpy (see field-bridge/requirements.txt).
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

from hackrf_jam import BAND_PRESETS_MHZ, MAX_DURATION_S, transmit_burst

log = logging.getLogger("jam-bridge")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Any confirmation token shorter than this is refused outright. Real tokens
# minted by backend/server.py's _issue_jam_confirm_token() are UUID4 strings
# (36 chars) — this floor exists purely to reject trivially-fabricated values
# like "true"/"1"/"yes" reaching this point, not to actually validate the
# token cryptographically (the backend already did the real, single-use
# validation before this message was ever sent).
MIN_CONFIRM_TOKEN_LEN = 20


def _looks_like_real_confirm_token(token: Optional[str]) -> bool:
    return bool(token) and isinstance(token, str) and len(token) >= MIN_CONFIRM_TOKEN_LEN


def _cfg(key: str, default: Optional[str] = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"Missing required config: {key}")
    return v


class JamBridge:
    # Which jam_mode this bridge is responsible for. The backend now stamps
    # every jam_request with jam_mode ("meghdut" = this built-in barrage jam,
    # "operator" = the operator's own jammer handled by
    # field-bridge/operator_jam_bridge.py's OperatorJamBridge subclass). Two
    # bridges can share this one WS channel without double-firing: each acts
    # ONLY on requests whose mode matches its own JAM_MODE (see
    # _handles_mode below). A request with no jam_mode is treated as
    # "meghdut" for backward compatibility.
    JAM_MODE = "meghdut"

    def __init__(self) -> None:
        self.api_url = _cfg("CEMA_API_URL").rstrip("/")
        self.email = _cfg("CEMA_EMAIL")
        self.password = _cfg("CEMA_PASSWORD")
        self.token: Optional[str] = None
        # Bridge-identity secret proving this is a REAL jam bridge (not a browser
        # session) when we advertise ourselves as the jam TX consumer via
        # bridge_hello. Loaded from the systemd EnvironmentFile (field-bridge/
        # .env: CEMA_BRIDGE_TOKEN); the backend validates it before trusting our
        # self-advertisement. Optional — if unset here, the backend simply won't
        # register us and its honest signal defaults to "no TX bridge subscribed"
        # (over-warns, never falsely reassures). Never gates our OWN jam gates.
        self.bridge_token: Optional[str] = os.environ.get("CEMA_BRIDGE_TOKEN") or None

        self.ws: Optional[websocket.WebSocketApp] = None
        self.stop_flag = threading.Event()

        # Mirrors rf-bridge/mavlink_bridge.py's tx_halted handling: EMERGENCY
        # ABORT is authoritative here too, and — unlike the MAVLink bridge,
        # which only has queued frames to drop — this bridge may have a REAL
        # hackrf_transfer process running that must be killed, not just have
        # future requests refused.
        self.tx_halted = False
        self._active_stop_event: Optional[threading.Event] = None
        self._active_lock = threading.Lock()

        log.info(
            "Range authorization for effect=jam is now checked LIVE against the "
            "backend (GET /api/range-authorization/status?effect=jam) at the moment "
            "of each transmission, not via a static env var — an operator must arm "
            "it from the app before this bridge will transmit any RF."
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

    # ---- range authorization (replaces the old static CEMA_AUTHORIZED_RANGE
    # bridge-host env var — see backend/RANGE_AUTHORIZATION_REDESIGN.md) ----
    def is_range_authorized(self, effect: str = "jam") -> bool:
        """Live GET /api/range-authorization/status?effect=jam check made at
        the moment of transmission — NOT trusting anything embedded in the
        jam_request WS message itself (a stale/replayed message must not be
        able to carry stale authorization forward past an expiry/disable
        that happened in between). FAILS CLOSED (returns False) on ANY
        network/auth error — unreachable backend, timeout, 401/403,
        malformed response — identical in spirit to the old
        `CEMA_AUTHORIZED_RANGE != "1"` fail-closed behavior. Never raises."""
        try:
            r = requests.get(
                f"{self.api_url}/api/range-authorization/status",
                params={"effect": effect},
                headers={"Authorization": f"Bearer {self.ensure_token()}"},
                timeout=10,
            )
            if r.status_code == 401:
                # Token may have expired — refresh once and retry, same
                # retry-on-401 convention as rf-bridge/common.py.
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
    def _send_jam_ack(self, ws, request_id: str, phase: str,
                      ok: Optional[bool] = None, error: Optional[str] = None) -> None:
        """Sends {"type": "jam_ack", ...} back over the same WS connection.
        phase is one of: "started" | "complete" | "failed" | "stopped".
        Only backend/server.py's _handle_jam_ack ever acts on these — never
        speculative, always sent after a real attempt/outcome."""
        if ws is None or not request_id:
            return
        msg = {"type": "jam_ack", "request_id": request_id, "phase": phase, "ts": time.time()}
        if ok is not None:
            msg["ok"] = bool(ok)
        if error:
            msg["error"] = str(error)[:300]
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("failed to send jam_ack (phase=%s) for request_id=%s: %s", phase, request_id, e)

    # ---- mode routing (two bridges, one WS channel) --------------------
    def _handles_mode(self, data: dict) -> bool:
        """True iff THIS bridge should act on this jam_request. Each bridge
        owns exactly one jam_mode; a request with no jam_mode defaults to
        'meghdut'. This is a routing filter only — it is NOT a safety gate and
        never relaxes one; the request the matching bridge DOES pick up still
        passes every gate below unchanged."""
        return (data.get("jam_mode") or "meghdut") == self.JAM_MODE

    # ---- transmit (overridable so OperatorJamBridge can swap the radiator) --
    def _do_transmit(self, params: dict, stop_event: threading.Event,
                     on_started) -> dict:
        """Perform the actual bounded, abortable RF transmission and return a
        result dict {"ok", "stopped_early", "error"}. Default = MEGHDUT's
        built-in barrage jam (hackrf_jam.transmit_burst). OperatorJamBridge
        overrides ONLY this method to route through the operator's own jammer;
        every gate in _handle_jam_request above it is shared, unchanged."""
        return transmit_burst(
            params["freq_mhz"], params["bandwidth_khz"], params["duration_s"],
            params["tx_gain"], stop_event=stop_event, on_started=on_started)

    # ---- WS handling ---------------------------------------------------
    def _handle_jam_request(self, ws, data: dict) -> None:
        request_id = data.get("request_id")
        actor = data.get("actor", "?")

        # ---- Mode routing (NOT a gate): ignore requests for the other jam
        # mode so the meghdut and operator bridges never double-fire on the
        # same shared WS channel. The matching bridge still runs every gate.
        if not self._handles_mode(data):
            return

        # ---- Gate A: live range-authorization check against the backend. --
        # Independent of anything the app already checked/forwarded in this
        # very message — a fresh GET at the moment of transmission, not a
        # value trusted from this WS payload. Replaces the old static
        # CEMA_AUTHORIZED_RANGE bridge-host env var (see
        # backend/RANGE_AUTHORIZATION_REDESIGN.md). Fails closed on any
        # network/auth error (is_range_authorized never raises).
        if not self.is_range_authorized("jam"):
            log.error(
                "REFUSING jam_request %s from %s: range authorization for effect=jam "
                "is not enabled (or could not be verified) via GET "
                "/api/range-authorization/status. The app approved the arm/confirm "
                "tokens for this request, but the range-authorization lease is not "
                "currently armed — an operator must enable it from the app first.",
                request_id, actor,
            )
            self._send_jam_ack(ws, request_id, "failed", ok=False,
                              error="bridge refused: range-authorization (effect=jam) not enabled "
                                    "(independent bridge-level gate, separate from the app's arm-token check)")
            return

        # ---- Gate B: confirmation-token shape check (defense in depth). ---
        jam_confirm_token = data.get("jam_confirm_token")
        if not _looks_like_real_confirm_token(jam_confirm_token):
            log.error(
                "REFUSING jam_request %s from %s: missing/malformed jam_confirm_token. "
                "This bridge only transmits for requests carrying a real confirmation "
                "token minted by the backend at the moment an operator completed the "
                "app's two-step SafetyGate confirm — refusing to guess.",
                request_id, actor,
            )
            self._send_jam_ack(ws, request_id, "failed", ok=False,
                              error="bridge refused: missing/malformed jam confirmation token")
            return

        # ---- Gate C: local EMERGENCY ABORT state. --------------------------
        if self.tx_halted:
            log.warning("TX suppressed: EMERGENCY ABORT in effect on this bridge — refusing jam_request %s.",
                       request_id)
            self._send_jam_ack(ws, request_id, "failed", ok=False,
                              error="tx halted (EMERGENCY ABORT in effect on bridge)")
            return

        try:
            band = data.get("band")
            freq_mhz = data.get("freq_mhz") or BAND_PRESETS_MHZ.get(band)
            bandwidth_khz = float(data.get("bandwidth_khz", 500))
            duration_s = min(float(data.get("duration_s", 5)), MAX_DURATION_S)
            tx_gain = int(data.get("tx_gain", 20))
        except (TypeError, ValueError) as e:
            self._send_jam_ack(ws, request_id, "failed", ok=False, error=f"invalid jam parameters: {e}")
            return
        if not freq_mhz:
            self._send_jam_ack(ws, request_id, "failed", ok=False,
                              error="no band/freq_mhz resolved for this request")
            return

        stop_event = threading.Event()
        with self._active_lock:
            self._active_stop_event = stop_event

        def on_started(_proc) -> None:
            log.warning(
                "TRANSMITTING real RF jam burst: %.1f MHz, %.0fkHz BW, %.1fs, gain=%d "
                "(request %s, requested by %s)",
                freq_mhz, bandwidth_khz, duration_s, tx_gain, request_id, actor,
            )
            self._send_jam_ack(ws, request_id, "started", ok=True)

        params = {
            "band": band,
            "freq_mhz": freq_mhz,
            "bandwidth_khz": bandwidth_khz,
            "duration_s": duration_s,
            "tx_gain": tx_gain,
            "request_id": request_id,
            "actor": actor,
        }

        def run() -> None:
            result = self._do_transmit(params, stop_event, on_started)
            with self._active_lock:
                if self._active_stop_event is stop_event:
                    self._active_stop_event = None
            if result["stopped_early"]:
                log.warning("Jam burst STOPPED EARLY by EMERGENCY ABORT (request %s).", request_id)
                self._send_jam_ack(ws, request_id, "stopped", ok=True)
            elif result["ok"]:
                log.info("Jam burst complete (request %s).", request_id)
                self._send_jam_ack(ws, request_id, "complete", ok=True)
            else:
                log.error("Jam burst FAILED (request %s): %s", request_id, result["error"])
                self._send_jam_ack(ws, request_id, "failed", ok=False, error=result["error"])

        threading.Thread(target=run, name=f"jam-{request_id}", daemon=True).start()

    def start_ws_subscriber(self) -> None:
        ws_scheme = "wss" if self.api_url.startswith("https") else "ws"
        host = self.api_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/api/ws/mavlink?token={quote(self.ensure_token())}"
        log.info("Subscribing to bridge-control WS at %s?token=<jwt>", url.split("?", 1)[0])

        def on_open(_ws):
            log.info("WS connected — jam bridge ready.")
            # Announce this connection as the JAM TX consumer so the backend can
            # honestly report at fire time whether a jam bridge is actually
            # subscribed (and warn the operator when none is) — see the MAVLink
            # bridge's identical bridge_hello and backend WSManager handling.
            # Includes the shared bridge-identity secret (CEMA_BRIDGE_TOKEN) so
            # the backend accepts THIS self-advertisement but rejects a browser/
            # console session forging the same message (TX-review MEDIUM).
            # Best-effort; never gates the independent bridge-side jam gates.
            try:
                hello = {"type": "bridge_hello", "consumers": ["jam"]}
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
                    log.warning("EMERGENCY ABORT received — terminating in-progress jam transmission NOW.")
                    active.set()
                else:
                    log.warning("EMERGENCY ABORT received (operator=%s) — future jam requests refused "
                               "until resume.", data.get("operator"))
                return
            if mtype == "resume":
                log.warning("RESUME received (operator=%s) — jam requests re-enabled.", data.get("operator"))
                self.tx_halted = False
                return
            if mtype != "jam_request":
                return  # ignore "packet" (MAVLink) and anything else — same channel, different consumers

            self._handle_jam_request(ws, data)

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

        threading.Thread(target=run_forever, name="jam-ws-subscriber", daemon=True).start()

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
    return JamBridge().run()


if __name__ == "__main__":
    sys.exit(main())
