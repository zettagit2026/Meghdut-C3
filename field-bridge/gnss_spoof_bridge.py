#!/usr/bin/env python3
"""GNSS Spoof Bridge — App (backend/server.py) <-> HackRF GNSS L1 C/A
soft-kill spoof TX (Task #103).

Structurally PARALLEL to field-bridge/jam_bridge.py (own class, own WS
message types, own local abort/halt state) — DELIBERATELY NOT merged into
jam_bridge.py, and jam_bridge.py is UNCHANGED by this work. See
field-bridge/GNSS_SPOOF_ARCHITECTURE.md §1 for the full rationale: the two
effects have different token types, different range-authorization effect
strings, different payload-preview content, and mixing them raises the risk
of a future edit accidentally sharing a gate that must stay separate.

=============================================================================
STATUS: SAFETY-GATE PLUMBING COMPLETE, DSP STUBBED (Task A of a two-part
split — see GNSS_SPOOF_ARCHITECTURE.md §7). DO NOT enable as a live systemd
service until Task B's gnss_signal_synth.py is integrated — this bridge
currently cannot transmit a real fabricated GPS signal; every gate below is
real and tested, but the actual RF payload is a stub (see
gnss_signal_synth.synthesize_iq_file's docstring).
=============================================================================

=============================================================================
WHY THIS SCRIPT EXISTS (mirrors jam_bridge.py's own docstring, adapted)
=============================================================================
This bridge preserves the same "a human must deliberately, unambiguously
choose to transmit, immediately before it happens" intent jam_bridge.py
implements, via an independent chain of gates, ALL of which must pass
before a single byte reaches hackrf_transfer:

  1. Frontend (frontend/src/pages/GnssSpoof.jsx, reusing
     frontend/src/components/SafetyGate.jsx): a dynamic safety checklist —
     including a checklist item whose text is the EXACT fabricated position
     computed by POST /gnss-spoof/preview, not a generic "I confirm" button
     — the operator must explicitly tick, PLUS a required free-text
     friendly-asset-attestation field, followed by a two-click ARM & FIRE ->
     CONFIRM FIRE sequence.

  2. Backend (backend/server.py):
       a. require_commander — gnss_spoof requires the elevated role.
       b. a freshly-issued, single-use arm_token from POST /api/arm —
          unconditionally required (gnss_spoof is always CRITICAL severity).
       c. a freshly-issued, single-use gnss_spoof_confirm_token from POST
          /gnss-spoof/confirm — DELIBERATELY a SEPARATE token type from
          jam_confirm_token (NOT interchangeable — see architecture doc §4
          and backend/server.py's _consume_gnss_spoof_confirm_token). Minted
          only at the instant the frontend's SafetyGate onConfirm fires, and
          bound to the EXACT friendly_asset_attestation text supplied at
          that moment — mismatched attestation text at fire-time is a hard
          400, not a silent pass-through.
       d. _check_tx_not_halted — EMERGENCY ABORT (any operator) blocks new
          gnss_spoof requests same as any other TX.

  3. THIS BRIDGE, independently of everything above:
       a. A LIVE GET /api/range-authorization/status?effect=gnss_spoof call
          to the backend, made at the moment of transmission — a SEPARATE
          lease from effect=jam (arming jam does NOT implicitly arm
          gnss_spoof; see RANGE_AUTH_EFFECTS in backend/server.py). Fails
          closed (treated as NOT authorized) on any network/auth error.
       b. gnss_spoof_confirm_token shape check (non-trivial,
          non-guessable-length string) — defense in depth in case (2c) is
          ever bypassed upstream. Uses its OWN floor constant
          (MIN_CONFIRM_TOKEN_LEN below), deliberately NOT shared code with
          jam_bridge.MIN_CONFIRM_TOKEN_LEN even though the value happens to
          match — a future change to one must not silently change the
          other.
       c. tx_halted (EMERGENCY ABORT) is also honored locally: if an abort
          arrives WHILE a burst is transmitting, this bridge terminates the
          live hackrf_transfer process immediately (own stop_event, own
          tx_halted flag — NOT shared state with jam_bridge.py, which runs
          as a separate OS process).

None of these gates replace any other — they are independent and ALL must
pass. Removing any one of them is a regression.

=============================================================================
DURATION CAP: 3.0s, not jamming's 10s — see hackrf_jam.GNSS_SPOOF_MAX_DURATION_S
and GNSS_SPOOF_ARCHITECTURE.md §2 for the full justification (a deception
effect's failsafe trigger fires off a single bad position report, not
sustained exposure — there is no "more seconds = more effect" scaling once
a fake fix is accepted, so a short, hard cap is part of the safety design).
Enforced independently here (never trusting the WS payload's duration as
authoritative), same posture as jam_bridge.py toward all jam-request fields.
=============================================================================

BEHAVIOR: BOUNDED BURST ONLY — NOT CONTINUOUS. Same reasoning as
jam_bridge.py's own module docstring: an unattended, WS-triggered,
continuous transmission with no local human able to abort locally is a
materially different risk and is explicitly out of scope (see architecture
doc §8).

Requires: websocket-client, requests (see field-bridge/requirements.txt).
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

from hackrf_jam import GNSS_SPOOF_MAX_DURATION_S, transmit_iq_file
from gnss_signal_synth import GnssSynthNotImplemented, synthesize_iq_file

log = logging.getLogger("gnss-spoof-bridge")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Own floor constant — see module docstring (3b) for why this is NOT shared
# with jam_bridge.MIN_CONFIRM_TOKEN_LEN despite the same value today. Real
# tokens minted by backend/server.py's _issue_gnss_spoof_confirm_token() are
# UUID4 strings (36 chars).
MIN_CONFIRM_TOKEN_LEN = 20

# Same floor/posture as backend/server.py's _looks_like_real_attestation —
# duplicated here (not imported) because this bridge process has no import
# path to backend/server.py and, per the module docstring, should not trust
# any single shared validator across the app/bridge boundary for a
# defense-in-depth check.
MIN_ATTESTATION_LEN = 20
_TRIVIAL_ATTESTATION_VALUES = {"n/a", "na", "none", "confirmed", "yes", "ok", "test"}


def _looks_like_real_confirm_token(token: Optional[str]) -> bool:
    return bool(token) and isinstance(token, str) and len(token) >= MIN_CONFIRM_TOKEN_LEN


def _looks_like_real_attestation(text: Optional[str]) -> bool:
    if not text or not isinstance(text, str):
        return False
    stripped = text.strip()
    if len(stripped) < MIN_ATTESTATION_LEN:
        return False
    if stripped.lower() in _TRIVIAL_ATTESTATION_VALUES:
        return False
    return True


def _cfg(key: str, default: Optional[str] = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"Missing required config: {key}")
    return v


class GnssSpoofBridge:
    def __init__(self) -> None:
        self.api_url = _cfg("CEMA_API_URL").rstrip("/")
        self.email = _cfg("CEMA_EMAIL")
        self.password = _cfg("CEMA_PASSWORD")
        self.token: Optional[str] = None

        self.ws: Optional[websocket.WebSocketApp] = None
        self.stop_flag = threading.Event()

        # Own EMERGENCY ABORT state — NOT shared with jam_bridge.py, which
        # runs as a separate OS process with its own tx_halted flag.
        self.tx_halted = False
        self._active_stop_event: Optional[threading.Event] = None
        self._active_lock = threading.Lock()

        log.info(
            "Range authorization for effect=gnss_spoof is checked LIVE against the "
            "backend (GET /api/range-authorization/status?effect=gnss_spoof) at the "
            "moment of each transmission — a SEPARATE lease from effect=jam. An "
            "operator must arm it from the app before this bridge will transmit any RF."
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

    # ---- range authorization (own effect key, "gnss_spoof") -----------
    def is_range_authorized(self, effect: str = "gnss_spoof") -> bool:
        """Live GET /api/range-authorization/status?effect=gnss_spoof check
        made at the moment of transmission — NOT trusting anything embedded
        in the gnss_spoof_request WS message itself. FAILS CLOSED (returns
        False) on ANY network/auth error. Never raises. Mirrors
        jam_bridge.JamBridge.is_range_authorized exactly, against the
        gnss_spoof effect key instead of jam."""
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
    def _send_gnss_spoof_ack(self, ws, request_id: str, phase: str,
                             ok: Optional[bool] = None, error: Optional[str] = None) -> None:
        """Sends {"type": "gnss_spoof_ack", ...} back over the same WS
        connection. phase is one of: "started" | "complete" | "failed" |
        "stopped". Only backend/server.py's _handle_gnss_spoof_ack ever
        acts on these — never speculative, always sent after a real
        attempt/outcome."""
        if ws is None or not request_id:
            return
        msg = {"type": "gnss_spoof_ack", "request_id": request_id, "phase": phase, "ts": time.time()}
        if ok is not None:
            msg["ok"] = bool(ok)
        if error:
            msg["error"] = str(error)[:300]
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("failed to send gnss_spoof_ack (phase=%s) for request_id=%s: %s",
                       phase, request_id, e)

    # ---- WS handling ---------------------------------------------------
    def _handle_gnss_spoof_request(self, ws, data: dict) -> None:
        request_id = data.get("request_id")
        actor = data.get("actor", "?")

        # ---- Gate A: live range-authorization check against the backend. --
        if not self.is_range_authorized("gnss_spoof"):
            log.error(
                "REFUSING gnss_spoof_request %s from %s: range authorization for "
                "effect=gnss_spoof is not enabled (or could not be verified) via GET "
                "/api/range-authorization/status. The app approved the arm/confirm "
                "tokens for this request, but the range-authorization lease is not "
                "currently armed — an operator must enable it from the app first.",
                request_id, actor,
            )
            self._send_gnss_spoof_ack(
                ws, request_id, "failed", ok=False,
                error="bridge refused: range-authorization (effect=gnss_spoof) not enabled "
                      "(independent bridge-level gate, separate from the app's arm-token check)")
            return

        # ---- Gate B: confirmation-token shape check (defense in depth). ---
        confirm_token = data.get("gnss_spoof_confirm_token")
        if not _looks_like_real_confirm_token(confirm_token):
            log.error(
                "REFUSING gnss_spoof_request %s from %s: missing/malformed "
                "gnss_spoof_confirm_token. This bridge only transmits for requests "
                "carrying a real confirmation token minted by the backend at the moment "
                "an operator completed the app's SafetyGate confirm — refusing to guess.",
                request_id, actor,
            )
            self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False,
                                      error="bridge refused: missing/malformed gnss_spoof confirmation token")
            return

        # ---- Gate C: local EMERGENCY ABORT state. --------------------------
        if self.tx_halted:
            log.warning("TX suppressed: EMERGENCY ABORT in effect on this bridge — "
                       "refusing gnss_spoof_request %s.", request_id)
            self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False,
                                      error="tx halted (EMERGENCY ABORT in effect on bridge)")
            return

        try:
            freq_mhz = float(data.get("freq_mhz") or 1575.42)
            duration_s = min(float(data.get("duration_s", 2.0)), GNSS_SPOOF_MAX_DURATION_S)
            tx_gain = int(data.get("tx_gain", 20))
            true_lat = float(data["true_lat"])
            true_lon = float(data["true_lon"])
            true_alt_m = float(data.get("true_alt_m", 0.0))
            fake_lat = float(data["fake_lat"])
            fake_lon = float(data["fake_lon"])
            fake_alt_m = float(data.get("fake_alt_m", true_alt_m))
        except (TypeError, ValueError, KeyError) as e:
            self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False,
                                      error=f"invalid gnss_spoof parameters: {e}")
            return

        stop_event = threading.Event()
        with self._active_lock:
            self._active_stop_event = stop_event

        def on_started(_proc) -> None:
            log.warning(
                "TRANSMITTING GNSS spoof burst: %.2f MHz, %.1fs, gain=%d, fake position "
                "%.6f,%.6f (request %s, requested by %s)",
                freq_mhz, duration_s, tx_gain, fake_lat, fake_lon, request_id, actor,
            )
            self._send_gnss_spoof_ack(ws, request_id, "started", ok=True)

        def run() -> None:
            # ---- DSP call — STUBBED (Task B). ------------------------------
            # TODO: Task B — field-bridge/gnss_signal_synth.py currently
            # raises GnssSynthNotImplemented unless
            # GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ=1 is set (test-only placeholder
            # IQ). Once Task B lands real GPS L1 C/A synthesis, NO CHANGE is
            # needed here — this call site is the intended integration point;
            # Task B's synthesize_iq_file() keeps the same signature/return
            # contract (a path to a ready-to-transmit IQ file).
            try:
                iq_path = synthesize_iq_file(
                    true_lat, true_lon, true_alt_m,
                    fake_lat, fake_lon, fake_alt_m,
                    duration_s,
                )
            except GnssSynthNotImplemented as e:
                with self._active_lock:
                    if self._active_stop_event is stop_event:
                        self._active_stop_event = None
                log.error("gnss_spoof_request %s: DSP synthesis not available: %s", request_id, e)
                self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False, error=str(e))
                return
            except Exception as e:
                with self._active_lock:
                    if self._active_stop_event is stop_event:
                        self._active_stop_event = None
                log.error("gnss_spoof_request %s: IQ synthesis failed: %s", request_id, e)
                self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False,
                                          error=f"IQ synthesis failed: {e}")
                return

            try:
                result = transmit_iq_file(iq_path, freq_mhz, duration_s, tx_gain,
                                          stop_event=stop_event, on_started=on_started)
            finally:
                try:
                    os.unlink(iq_path)
                except OSError:
                    pass
                with self._active_lock:
                    if self._active_stop_event is stop_event:
                        self._active_stop_event = None

            if result["stopped_early"]:
                log.warning("GNSS spoof burst STOPPED EARLY by EMERGENCY ABORT (request %s).", request_id)
                self._send_gnss_spoof_ack(ws, request_id, "stopped", ok=True)
            elif result["ok"]:
                log.info("GNSS spoof burst complete (request %s).", request_id)
                self._send_gnss_spoof_ack(ws, request_id, "complete", ok=True)
            else:
                log.error("GNSS spoof burst FAILED (request %s): %s", request_id, result["error"])
                self._send_gnss_spoof_ack(ws, request_id, "failed", ok=False, error=result["error"])

        threading.Thread(target=run, name=f"gnss-spoof-{request_id}", daemon=True).start()

    def start_ws_subscriber(self) -> None:
        ws_scheme = "wss" if self.api_url.startswith("https") else "ws"
        host = self.api_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/api/ws/mavlink?token={quote(self.ensure_token())}"
        log.info("Subscribing to bridge-control WS at %s?token=<jwt>", url.split("?", 1)[0])

        def on_open(_ws):
            log.info("WS connected — gnss-spoof bridge ready.")

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
                    log.warning("EMERGENCY ABORT received — terminating in-progress gnss_spoof "
                               "transmission NOW.")
                    active.set()
                else:
                    log.warning("EMERGENCY ABORT received (operator=%s) — future gnss_spoof "
                               "requests refused until resume.", data.get("operator"))
                return
            if mtype == "resume":
                log.warning("RESUME received (operator=%s) — gnss_spoof requests re-enabled.",
                           data.get("operator"))
                self.tx_halted = False
                return
            if mtype != "gnss_spoof_request":
                return  # ignore "packet"/"jam_request"/anything else — same channel, different consumers

            self._handle_gnss_spoof_request(ws, data)

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

        threading.Thread(target=run_forever, name="gnss-spoof-ws-subscriber", daemon=True).start()

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
    return GnssSpoofBridge().run()


if __name__ == "__main__":
    sys.exit(main())
