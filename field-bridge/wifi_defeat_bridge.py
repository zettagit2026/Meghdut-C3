#!/usr/bin/env python3
"""Wi-Fi Defeat Bridge — App (backend/server.py) <-> real active Wi-Fi drone
defeat TX (802.11 deauth + unencrypted Parrot ARSDK3 / Ryze-DJI Tello command
injection).

Structurally IDENTICAL to field-bridge/jam_bridge.py and
field-bridge/sdr_mavlink_inject_bridge.py (own class, own WS message types, own
local abort/halt state, the SAME governed A/B/C gate chain). jam_bridge.py /
sdr_mavlink_inject_bridge.py are UNCHANGED by this work.

=============================================================================
WHAT THIS RADIATES (and, just as importantly, WHAT IT DOES NOT) — HONEST
=============================================================================
Two HONEST active-defeat mechanisms against a Wi-Fi-controlled drone (see
.omc/plans/wifi-defeat-active-cuas-plan.md, mechanisms 1 & 2). Neither is a
"takeover" of an arbitrary drone:

  1. 802.11 deauth / disassoc (mode="deauth") — inject management frames
     spoofed as the drone softAP BSSID so the controller<->drone link drops and
     the airframe hits its OWN link-loss failsafe (RTH / hover / land; the
     operator does NOT choose which). This is a LINK-DROP, never a command, and
     is a no-op against 802.11w/PMF and un-targetable against a randomized /
     renamed SSID. FRATRICIDE-CRITICAL: a deauth MUST name one specific unicast
     softAP BSSID — the primitive REFUSES broadcast/empty/malformed BSSIDs.
  2. Unauthenticated command injection over the drone's OPEN softAP
     (mode="arsdk_land"/"arsdk_emergency"/"tello_land"/"tello_emergency") —
     send a byte-exact `land`/`emergency` command over UDP. Applies ONLY to an
     unencrypted Parrot ARSDK3 or Ryze/DJI Tello airframe; a targeted
     unauthenticated command against a cooperative unencrypted target, NOT
     takeover of an arbitrary drone. The command bytes are built by
     field-bridge/wifi_arsdk_encode.py (Parrot IDs verified against the BSD-3
     arsdk-xml catalog and cited there; the encoder RAISES
     UnverifiedCommandError for any uncited command rather than field a guess —
     this bridge handles that refusal as a clean, no-TX failure).

The byte-on-the-wire and the fail-closed transmit guards (WIFI_TX_IFACE
device-pin, fratricide BSSID scope, prompt abort) all live in
field-bridge/wifi_defeat_primitives.py — this bridge NEVER bypasses them; it
just drives them under the governed spine, exactly as jam_bridge.py drives
hackrf_jam.py.

=============================================================================
IT ADDS *NO* NEW AUTHORIZATION PATH — the SAME governed spine as every TX
=============================================================================
Before a single frame / datagram leaves the injection NIC, an independent chain
of gates — ALL of which must pass — is enforced. The heavier per-target gates
(commander role, single-use arm token, effect-specific wifi_defeat confirm
token minting, per-target IFF/fratricide ack, PMF-honesty gate, unencrypted-
target gate) are enforced UPSTREAM in the backend (P3) before this bridge is
ever messaged. THIS bridge, independently, re-checks — in order — the same
three last-line gates every other governed TX bridge does:

  Gate A: LIVE GET /api/range-authorization/status?effect=<wifi_deauth|
          arsdk_inject> at the moment of transmission — the effect keyed to the
          request's mode, NOT trusting anything embedded in the WS payload. A
          stale/replayed message cannot carry a since-expired lease forward.
          Fails closed on any network/auth error (is_range_authorized never
          raises). Re-polled DURING a continuous deauth via make_tx_halt_check
          so a mid-stream lease expiry stops TX too, not only an operator abort.
  Gate B: wifi_defeat_confirm_token shape check (defense in depth). OWN floor
          constant (MIN_CONFIRM_TOKEN_LEN below), deliberately NOT shared with
          the other bridges — a future change to one must not silently change
          the others.
  Gate C: local EMERGENCY ABORT (tx_halted): refuses new requests AND
          terminates an in-progress deauth loop immediately (stop_event).

None of these gates replace any other. Removing any one is a regression.

The WIFI_TX_IFACE device-pin and the broadcast-BSSID fratricide guard are NOT
re-implemented here (that would risk drift): they are enforced fail-closed at
the source in wifi_defeat_primitives.py, which refuses (ok=False, no TX, no
raise) an unpinned / wrong-NIC transmit or a broadcast/empty/malformed target
BSSID, and this bridge reports that refusal as an honest failed ack.

BEHAVIOR: OPERATOR-CONTROLLED. A deauth runs CONTINUOUS (count None / <= 0)
until the operator stops it, or for a bounded burst count. A command inject is a
single targeted datagram. The kill-switch is unchanged: EMERGENCY ABORT /
tx_halt terminates a live deauth loop immediately (stop_event + tx_halt_check
are both polled BEFORE every frame by the primitive), so a continuous deauth is
always switchable-off.

Requires: websocket-client, requests (see field-bridge/requirements.txt). scapy
is imported lazily by wifi_defeat_primitives ONLY at real deauth TX time.

Env (systemd EnvironmentFile, see cema-wifi-defeat-bridge.service):
  CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD  — same as the other bridges.
  CEMA_BRIDGE_TOKEN                          — diagnostic bridge-identity secret.
  WIFI_TX_IFACE                              — the dedicated injection NIC. The
                                               primitive FAILS CLOSED without it
                                               rather than key the RX/detection
                                               NIC and take Kismet detection down.
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

from range_auth_lease import RangeAuthLease, make_tx_halt_check
from wifi_defeat_primitives import (
    inject_arsdk_command,
    send_deauth,
    tello_command,
)
from wifi_arsdk_encode import (
    TelloCommandError,
    UnverifiedCommandError,
    encode_ardrone3_piloting,
    encode_tello,
)
from wifi_nic_mode import (
    WifiNicModeBusy,
    ensure_managed_associated,
    ensure_monitor,
    restore_safe,
    wifi_nic_mode_lock,
)

log = logging.getLogger("wifi-defeat-bridge")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Own floor constant — deliberately NOT shared with jam_bridge.MIN_CONFIRM_TOKEN_LEN
# / sdr_mavlink_inject_bridge.MIN_CONFIRM_TOKEN_LEN despite the same value today,
# so a future change to one cannot silently change the others. Real tokens minted
# by backend/server.py's wifi_defeat confirm-token issuer (P3) are UUID4 (36 chars);
# this floor exists only to reject trivially-fabricated values ("true"/"1"/"yes")
# reaching this point — the backend already did the real single-use validation.
MIN_CONFIRM_TOKEN_LEN = 20

# The wifi-defeat modes this bridge will drive. A request for anything else is
# refused rather than silently defaulted. Each maps to exactly one range-auth
# effect (Gate A) and one primitive dispatch below.
MODE_DEAUTH = "deauth"
MODE_ARSDK_LAND = "arsdk_land"
MODE_ARSDK_EMERGENCY = "arsdk_emergency"
MODE_TELLO_LAND = "tello_land"
MODE_TELLO_EMERGENCY = "tello_emergency"
SUPPORTED_MODES = frozenset({
    MODE_DEAUTH, MODE_ARSDK_LAND, MODE_ARSDK_EMERGENCY,
    MODE_TELLO_LAND, MODE_TELLO_EMERGENCY,
})

# Range-authorization effect keyed to the mode. deauth is its OWN lease
# (effect=wifi_deauth); the three command-injection modes share the
# effect=arsdk_inject lease. Arming one does NOT arm the other — the two
# mechanisms are authorized independently.
EFFECT_WIFI_DEAUTH = "wifi_deauth"
EFFECT_ARSDK_INJECT = "arsdk_inject"


def _looks_like_real_confirm_token(token: Optional[str]) -> bool:
    return bool(token) and isinstance(token, str) and len(token) >= MIN_CONFIRM_TOKEN_LEN


def _effect_for_mode(mode: str) -> Optional[str]:
    """Range-auth effect (Gate A key) for a wifi-defeat mode, or None for an
    unsupported mode (which is refused before Gate A — an unknown effect cannot
    be authorized, so this fails closed)."""
    if mode == MODE_DEAUTH:
        return EFFECT_WIFI_DEAUTH
    if mode in (MODE_ARSDK_LAND, MODE_ARSDK_EMERGENCY,
                MODE_TELLO_LAND, MODE_TELLO_EMERGENCY):
        return EFFECT_ARSDK_INJECT
    return None


def _cfg(key: str, default: Optional[str] = None) -> str:
    v = os.environ.get(key, default)
    if v is None:
        raise RuntimeError(f"Missing required config: {key}")
    return v


class WifiDefeatBridge:
    def __init__(self) -> None:
        self.api_url = _cfg("CEMA_API_URL").rstrip("/")
        self.email = _cfg("CEMA_EMAIL")
        self.password = _cfg("CEMA_PASSWORD")
        self.token: Optional[str] = None
        # Bridge-identity secret (same role as jam_bridge.py's). Best-effort;
        # never gates the independent bridge-side A/B/C gates below.
        self.bridge_token: Optional[str] = os.environ.get("CEMA_BRIDGE_TOKEN") or None

        self.ws: Optional[websocket.WebSocketApp] = None
        self.stop_flag = threading.Event()

        # Own EMERGENCY ABORT state — NOT shared with the other bridges, which
        # run as separate OS processes with their own tx_halted flags. Unlike the
        # single-datagram inject, a continuous deauth may have a REAL injection
        # loop running that must be stopped (stop_event), not just have future
        # requests refused.
        self.tx_halted = False
        self._active_stop_event: Optional[threading.Event] = None
        self._active_lock = threading.Lock()

        if not (os.environ.get("WIFI_TX_IFACE") or "").strip():
            log.warning(
                "WIFI_TX_IFACE is not set — wifi_defeat_primitives will FAIL CLOSED "
                "without a pinned injection NIC (it will NOT key the RX/detection NIC "
                "and take Kismet detection off the air). Pin the injection NIC via "
                "WIFI_TX_IFACE (the governed bridge does this through systemd) before "
                "engaging wifi defeat.")

        log.info(
            "Range authorization for wifi defeat is checked LIVE against the backend "
            "(GET /api/range-authorization/status?effect=wifi_deauth for deauth, "
            "effect=arsdk_inject for command injection) at the moment of each "
            "transmission — SEPARATE leases an operator must arm from the app first."
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

    # ---- range authorization (per-effect: wifi_deauth / arsdk_inject) ----
    def is_range_authorized(self, effect: str) -> bool:
        """Live GET /api/range-authorization/status?effect=<effect> check made at
        the moment of transmission — NOT trusting anything embedded in the
        wifi_defeat_request WS message itself (a stale/replayed message must not
        carry a since-expired/disabled lease forward). FAILS CLOSED (returns
        False) on ANY network/auth error — unreachable backend, timeout, 401/403,
        malformed response. Never raises. Mirrors jam_bridge.is_range_authorized /
        sdr_mavlink_inject_bridge.is_range_authorized, keyed per wifi-defeat
        effect."""
        try:
            r = requests.get(
                f"{self.api_url}/api/range-authorization/status",
                params={"effect": effect},
                headers={"Authorization": f"Bearer {self.ensure_token()}"},
                timeout=10,
            )
            if r.status_code == 401:
                # Token may have expired — refresh once and retry.
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
        """Sends {"type": "wifi_defeat_ack", ...} back over the same WS
        connection. phase is one of: "started" | "complete" | "failed" |
        "stopped". Only backend/server.py's wifi-defeat ack handler ever acts on
        these — never speculative, always sent after a real attempt/outcome."""
        if ws is None or not request_id:
            return
        msg = {"type": "wifi_defeat_ack", "request_id": request_id, "phase": phase, "ts": time.time()}
        if ok is not None:
            msg["ok"] = bool(ok)
        if error:
            msg["error"] = str(error)[:300]
        try:
            ws.send(json.dumps(msg))
        except Exception as e:
            log.warning("failed to send wifi_defeat_ack (phase=%s) for request_id=%s: %s",
                       phase, request_id, e)

    # ---- WS handling ---------------------------------------------------
    def _handle_defeat_request(self, ws, data: dict) -> None:
        request_id = data.get("request_id")
        actor = data.get("actor", "?")

        # ---- Mode resolution (NOT a gate, but must precede the effect-specific
        # Gate A): an unsupported / unknown mode maps to no range-auth effect and
        # so cannot be authorized — refuse fail-closed before anything else.
        mode = str(data.get("mode", "")).strip()
        effect = _effect_for_mode(mode)
        if effect is None:
            self._send_ack(ws, request_id, "failed", ok=False,
                           error=f"bridge refused: unsupported wifi-defeat mode {mode!r} "
                                 f"(supports {sorted(SUPPORTED_MODES)})")
            return

        # ---- Gate A: live range-authorization check against the backend. --
        # Keyed to THIS request's effect (wifi_deauth vs arsdk_inject), fetched
        # fresh at the moment of transmission — independent of anything the app
        # already checked/forwarded. Fails closed on any network/auth error.
        if not self.is_range_authorized(effect):
            log.error(
                "REFUSING wifi_defeat_request %s from %s (mode=%s): range authorization for "
                "effect=%s is not enabled (or could not be verified) via GET "
                "/api/range-authorization/status. The app approved the arm/confirm tokens for "
                "this request, but the range-authorization lease is not currently armed — an "
                "operator must enable it from the app first.",
                request_id, actor, mode, effect,
            )
            self._send_ack(
                ws, request_id, "failed", ok=False,
                error=f"bridge refused: range-authorization (effect={effect}) not enabled "
                      "(independent bridge-level gate, separate from the app's arm-token check)")
            return

        # ---- Gate B: confirmation-token shape check (defense in depth). ---
        confirm_token = data.get("wifi_defeat_confirm_token")
        if not _looks_like_real_confirm_token(confirm_token):
            log.error(
                "REFUSING wifi_defeat_request %s from %s: missing/malformed "
                "wifi_defeat_confirm_token. This bridge only transmits for requests carrying a "
                "real confirmation token minted by the backend at the moment an operator "
                "completed the app's SafetyGate confirm — refusing to guess.",
                request_id, actor,
            )
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="bridge refused: missing/malformed wifi_defeat confirmation token")
            return

        # ---- Gate C: local EMERGENCY ABORT state. -------------------------
        if self.tx_halted:
            log.warning("TX suppressed: EMERGENCY ABORT in effect on this bridge — refusing "
                       "wifi_defeat_request %s.", request_id)
            self._send_ack(ws, request_id, "failed", ok=False,
                           error="tx halted (EMERGENCY ABORT in effect on bridge)")
            return

        try:
            # The dedicated injection NIC. Read LIVE from the environment so the
            # value the primitive's fail-closed pin gate re-reads is the same one
            # passed here (unset -> the primitive refuses, fail-closed).
            iface = os.environ.get("WIFI_TX_IFACE")
            target_bssid = data.get("target_bssid")
            softap = data.get("softap")
            client_mac = data.get("client_mac")
            # SSID of the drone's OPEN softAP (e.g. TELLO-* / ANAFI-* / DIRECT-*).
            # Only used by the mode-arbiter for the managed-mode association
            # (arsdk/tello); ignored for deauth.
            ssid = data.get("ssid")
            channel = data.get("channel")
            if channel is not None:
                channel = int(channel)
            # deauth is CONTINUOUS by default (count None / <= 0 -> until stopped);
            # a positive count is a bounded burst. Only meaningful for deauth.
            raw_count = data.get("count")
            count = None if raw_count is None else int(raw_count)
        except (TypeError, ValueError) as e:
            self._send_ack(ws, request_id, "failed", ok=False,
                           error=f"invalid wifi_defeat parameters: {e}")
            return

        stop_event = threading.Event()
        with self._active_lock:
            self._active_stop_event = stop_event

        # tx_halt_check stops TX on EITHER local EMERGENCY ABORT (self.tx_halted)
        # OR the range-auth LEASE for THIS effect going unauthorized mid-stream —
        # re-polling the SAME live source Gate A used (is_range_authorized(effect)),
        # TTL-cached so a per-frame poll never hammers the backend, fail-closed.
        # So a continuous deauth stops within one poll interval of a bare lease
        # expiry too, not only on an operator abort — mirrors jam_bridge's
        # _do_transmit / mavlink_takeover.py's _halted().
        tx_halt_check = make_tx_halt_check(
            lambda: self.tx_halted,
            RangeAuthLease(lambda: self.is_range_authorized(effect)))

        def on_started() -> None:
            log.warning(
                "TRANSMITTING wifi defeat: mode=%s effect=%s iface=%s target_bssid=%s "
                "softap=%s channel=%s (request %s, requested by %s)",
                mode, effect, iface, target_bssid, softap, channel, request_id, actor,
            )
            self._send_ack(ws, request_id, "started", ok=True)

        params = {
            "iface": iface,
            "target_bssid": target_bssid,
            "softap": softap,
            "client_mac": client_mac,
            "ssid": ssid,
            "channel": channel,
            "count": count,
            "request_id": request_id,
            "actor": actor,
        }

        def run() -> None:
            result: Optional[dict] = None
            try:
                # ---- MODE-EXCLUSIVITY: hold the NIC2 mode-arbiter lock across the
                # WHOLE engagement (mode-switch -> transmit -> restore) so a
                # concurrent request cannot corrupt NIC2's mode mid-op. A second
                # contender gets WifiNicModeBusy and a clean failed ack — never a
                # race. Mirrors jam_bridge's hackrf_device_lock discipline.
                with wifi_nic_mode_lock():
                    try:
                        # ---- PRECONDITION (fail-closed, ADDITIONAL to Gates
                        # A/B/C): put NIC2 into the REQUIRED mode BEFORE any TX.
                        # deauth needs monitor; the arsdk/tello injects need
                        # managed+associated to the open softAP. If the arbiter
                        # returns not-ok, ABORT the request fail-closed — the
                        # primitive is NEVER called, and a clean failed ack is
                        # sent. The arbiter refuses fail-closed for any
                        # non-WIFI_TX_IFACE, so this can never touch the detection
                        # NIC.
                        switch = self._switch_nic_mode(mode, params)
                        if not switch.get("ok"):
                            log.error(
                                "REFUSING wifi_defeat_request %s (mode=%s): NIC2 mode-switch "
                                "FAILED (fail-closed, NO TX): %s", request_id, mode, switch.get("error"))
                            self._send_ack(
                                ws, request_id, "failed", ok=False,
                                error=f"bridge refused: NIC2 mode-switch failed before TX "
                                      f"(fail-closed, no primitive called): {switch.get('error')}")
                            return
                        result = self._do_defeat(mode, params, stop_event, tx_halt_check, on_started)
                    finally:
                        # After the op completes OR on abort/stop (the primitive
                        # returns stopped_early on abort), AND even after a failed
                        # mode-switch (NIC2 may be half-configured), return NIC2 to
                        # the SAFE baseline so it is NEVER left associated to a
                        # drone AP. A restore failure is logged but — since it only
                        # touches NIC2 — must NEVER block the halt.
                        restore = restore_safe(params["iface"])
                        if not restore.get("ok"):
                            log.warning(
                                "NIC2 restore_safe/teardown after wifi defeat FAILED (request %s): "
                                "%s — NIC2 only, does NOT block the halt.",
                                request_id, restore.get("error"))
            except WifiNicModeBusy as e:
                log.warning("REFUSING wifi_defeat_request %s: NIC2 mode-arbiter busy — another "
                            "wifi-defeat engagement holds NIC2 (%s).", request_id, e)
                self._send_ack(
                    ws, request_id, "failed", ok=False,
                    error=f"bridge refused: NIC2 mode-arbiter busy (another wifi-defeat op in "
                          f"progress) — {e}")
                return
            finally:
                with self._active_lock:
                    if self._active_stop_event is stop_event:
                        self._active_stop_event = None

            if result is None:
                return  # mode-switch failed -> failed ack already sent above.
            if result.get("stopped_early"):
                log.warning("wifi defeat STOPPED EARLY by EMERGENCY ABORT / lease expiry (request %s).",
                           request_id)
                self._send_ack(ws, request_id, "stopped", ok=True)
            elif result.get("ok"):
                log.info("wifi defeat complete (request %s).", request_id)
                self._send_ack(ws, request_id, "complete", ok=True)
            else:
                log.error("wifi defeat FAILED (request %s): %s", request_id, result.get("error"))
                self._send_ack(ws, request_id, "failed", ok=False, error=result.get("error"))

        threading.Thread(target=run, name=f"wifi-defeat-{request_id}", daemon=True).start()

    def _switch_nic_mode(self, mode: str, params: dict) -> dict:
        """Put NIC2 (params['iface'] == WIFI_TX_IFACE) into the mode this request
        needs BEFORE any TX, via the fail-closed wifi_nic_mode arbiter:
          - deauth              -> ensure_monitor (monitor mode for injection)
          - arsdk_*/tello_*     -> ensure_managed_associated (managed + associated
                                   to the OPEN softAP + DHCP up)
        Returns the arbiter's {"ok","error",...} dict (never raises). The arbiter
        refuses fail-closed for any iface that is not the pinned WIFI_TX_IFACE, so
        this can STRUCTURALLY never reconfigure the detection NIC."""
        iface = params["iface"]
        if mode == MODE_DEAUTH:
            return ensure_monitor(iface, channel=params.get("channel"))
        # arsdk_land/arsdk_emergency/tello_land/tello_emergency -> managed client
        # associated to the drone's OPEN softAP (ssid/bssid) with L3 up.
        return ensure_managed_associated(
            iface, params.get("ssid"), params.get("target_bssid"),
            channel=params.get("channel"))

    def _do_defeat(self, mode: str, params: dict, stop_event: threading.Event,
                   tx_halt_check, on_started) -> dict:
        """Dispatch the mode to its fail-closed TX primitive and return the
        primitive's {"ok","stopped_early","error",...} result dict (never raises).

        The primitives re-guard the WIFI_TX_IFACE device-pin (bound to `iface`)
        and, for deauth, the broadcast/empty/malformed BSSID fratricide scope —
        this bridge NEVER bypasses those. For the command-injection modes the
        command bytes are built by wifi_arsdk_encode FIRST; if the encoder refuses
        an uncited command (UnverifiedCommandError) or an unknown Tello token
        (TelloCommandError) NOTHING is transmitted (on_started is not fired) and a
        clean failed result is returned. on_started() fires only once encoding has
        succeeded and the primitive is about to put bytes on the wire."""
        iface = params["iface"]

        if mode == MODE_DEAUTH:
            # LINK-DROP: spoofed deauth/disassoc against the ONE target softAP
            # BSSID (the primitive refuses a broadcast/empty/malformed BSSID).
            on_started()
            return send_deauth(
                iface, params["target_bssid"], params["client_mac"],
                params["channel"], params["count"],
                stop_event=stop_event, tx_halt_check=tx_halt_check)

        if mode in (MODE_ARSDK_LAND, MODE_ARSDK_EMERGENCY):
            command = "land" if mode == MODE_ARSDK_LAND else "emergency"
            try:
                command_bytes = encode_ardrone3_piloting(command)
            except (UnverifiedCommandError, ValueError) as e:
                # Honesty gate: an uncited / unverified ARSDK command is refused
                # by the encoder — NO TX, clean failure (on_started NOT fired).
                return {"ok": False, "stopped_early": False,
                        "error": f"wifi defeat refused (unverified ARSDK command '{command}'): {e}"}
            on_started()
            return inject_arsdk_command(
                iface, params["softap"], command_bytes,
                stop_event=stop_event, tx_halt_check=tx_halt_check)

        if mode in (MODE_TELLO_LAND, MODE_TELLO_EMERGENCY):
            command = "land" if mode == MODE_TELLO_LAND else "emergency"
            try:
                token, default_addr = encode_tello(command)
            except (TelloCommandError, ValueError) as e:
                return {"ok": False, "stopped_early": False,
                        "error": f"wifi defeat refused (unknown Tello command '{command}'): {e}"}
            on_started()
            # Prefer the request's softAP target; fall back to the encoder's
            # verified default Tello control address when the request omits it.
            return tello_command(
                iface, params["softap"] or default_addr, token,
                stop_event=stop_event, tx_halt_check=tx_halt_check)

        # Unreachable: _handle_defeat_request already refused unsupported modes
        # before Gate A. Fail closed defensively rather than silently no-op.
        return {"ok": False, "stopped_early": False,
                "error": f"bridge refused: unsupported wifi-defeat mode {mode!r}"}

    def start_ws_subscriber(self) -> None:
        ws_scheme = "wss" if self.api_url.startswith("https") else "ws"
        host = self.api_url.split("://", 1)[1]
        url = f"{ws_scheme}://{host}/api/ws/mavlink?token={quote(self.ensure_token())}"
        log.info("Subscribing to bridge-control WS at %s?token=<jwt>", url.split("?", 1)[0])

        def on_open(_ws):
            log.info("WS connected — wifi-defeat bridge ready.")
            # Announce this connection as the wifi_defeat TX consumer so the
            # backend can honestly report at fire time whether a bridge is
            # actually subscribed (and warn when none is). Includes the shared
            # bridge-identity secret so the backend accepts THIS self-advertisement
            # but rejects a browser/console session forging the same message.
            # Best-effort; never gates the independent bridge-side gates.
            try:
                hello = {"type": "bridge_hello", "consumers": ["wifi_defeat"]}
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
                    log.warning("EMERGENCY ABORT received — terminating in-progress wifi "
                               "defeat transmission NOW.")
                    active.set()
                else:
                    log.warning("EMERGENCY ABORT received (operator=%s) — future wifi_defeat "
                               "requests refused until resume.", data.get("operator"))
                return
            if mtype == "resume":
                log.warning("RESUME received (operator=%s) — wifi_defeat requests re-enabled.",
                           data.get("operator"))
                self.tx_halted = False
                return
            if mtype != "wifi_defeat_request":
                return  # ignore "packet"/"jam_request"/anything else — shared channel

            self._handle_defeat_request(ws, data)

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

        threading.Thread(target=run_forever, name="wifi-defeat-ws-subscriber", daemon=True).start()

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
    return WifiDefeatBridge().run()


if __name__ == "__main__":
    sys.exit(main())
