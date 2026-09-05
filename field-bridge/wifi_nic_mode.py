#!/usr/bin/env python3
"""SAFETY-CRITICAL Wi-Fi NIC mode-arbiter for MEGHDUT C3 active Wi-Fi defeat.

WHAT THIS IS (and, just as importantly, WHAT IT NEVER TOUCHES)
=============================================================================
The active Wi-Fi-defeat capability needs the DEDICATED injection/TX NIC (NIC2,
`WIFI_TX_IFACE`) to be in different 802.11 modes for its two mechanisms:

  * 802.11 deauth injection (send_deauth) needs NIC2 in MONITOR mode.
  * Unauthenticated Parrot-ARSDK3 / Ryze-Tello command injection over the
    drone's OPEN softAP (inject_arsdk_command / tello_command) needs NIC2 in
    MANAGED mode, ASSOCIATED to that open softAP, with L3 (DHCP) up.

This module is the ONLY thing that flips NIC2 between those modes. The crux of
the design (see .omc/plans/wifi-defeat-active-cuas-plan.md §"The crux — NIC mode
exclusivity" and §"Safety spine") is that a Wi-Fi NIC can be in only ONE of
{monitor-RX detection, monitor-TX deauth, managed-client inject} at a time. The
delivered hardware answer is a DEDICATED 2nd NIC: detection RX stays on NIC1
(Kismet), and ALL Wi-Fi TX/mode-switching happens on NIC2 = `WIFI_TX_IFACE`.

THE ONE INVARIANT THAT NEVER CHANGES: this arbiter only ever switches NIC2's
mode. It STRUCTURALLY refuses — fail-closed, running NO command — to operate on
any interface that is not the pinned `WIFI_TX_IFACE`, so it can never `iw`/`ip`
the detection NIC and knock Kismet detection off the air. This mirrors the pin
binding in wifi_defeat_primitives.py (`_wifi_tx_pinning_error`): a caller passing
the detection/RX NIC is REFUSED, not silently allowed through.

FAIL-CLOSED CONTRACT (mirrors wifi_defeat_primitives.py)
=============================================================================
Every public entry point returns the module's standard
`{"ok": bool, "error": Optional[str], ...}` shape and NEVER raises. Any
subprocess non-zero exit, missing binary, timeout, or unexpected exception is
caught and reported as `ok=False` (fail-closed) so the bridge can send an honest
ack instead of crashing. A refuse (bad/blank/wrong iface, malformed argument)
runs NO command at all.

NO real NIC is required to import or unit-test this module: the ONLY function
that runs a real subprocess (`_run`, argv LIST, never shell=True) is injected via
the `runner=` parameter on every entry point, so the tests pass a recording fake
and no real `iw` / `ip` / `dhclient` is ever executed.

MODE-ARBITER LOCK
=============================================================================
`wifi_nic_mode_lock()` (mirrors hackrf_device_lock's flock-based cross-process
mutex) serializes NIC2 mode changes: the governed bridge holds it across the
WHOLE engagement (mode-switch -> transmit -> restore) so two concurrent requests
can never corrupt NIC2's mode (e.g. request B flipping NIC2 to managed while
request A is mid-deauth in monitor). A second contender gets WifiNicModeBusy
rather than racing.

!! LIVE-HARDWARE CAVEAT — VALIDATE ON MONDAY'S REAL CARD !!
=============================================================================
The EXACT `iw` / `ip` / `dhclient` argument vectors below (monitor-mode set,
open-network association, DHCP client choice) are the standard robust sequences
but are UNVERIFIED against the specific 2nd Alfa card / driver going in on
Monday. They MUST be validated on the real unit (e.g. `iw dev <iface> set type
monitor` acceptance, `iw ... connect` for the open softAP, `dhclient` vs
`dhcpcd`) before this arbiter drives a live engagement. The fail-closed spine,
the WIFI_TX_IFACE pin binding, the argument validation and the argv-list
(no-shell) safety here do NOT depend on those exact commands and are correct as
written; only the specific command strings are pending live confirmation.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

# A runner takes an argv LIST and returns (returncode, stderr_text).
Runner = Callable[[List[str]], Tuple[int, str]]

# --- argument validation (defense in depth; argv-list already blocks shell
# injection, these bound the values to sane shapes and reject garbage early) ---
# Linux netdev names: bounded, restricted charset (IFNAMSIZ is 16, allow a touch
# more for monNN variants). NEVER contains a shell metacharacter. Leading `-` is
# rejected too (cheap insurance — iface is operator-pinned, not attacker input,
# but a name starting with `-` could still be misread as an option by a getopt
# parser downstream).
_IFACE_RE = re.compile(r"^(?!-)[A-Za-z0-9_.-]{1,32}$")
# 6-octet MAC, colon- or dash-separated. Same shape wifi_defeat_primitives uses.
_MAC_RE = re.compile(r"^([0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}$")
# An 802.11 SSID is up to 32 bytes; the drone softAPs are ASCII (TELLO-*,
# ANAFI-*, DIRECT-*). Reject empty / over-long / control characters, AND a
# leading `-` (defense in depth: the SSID comes from a DETECTED softAP —
# attacker-controllable — and is passed as a positional argv token to `iw dev
# <iface> connect <ssid> <bssid>`; an SSID like `-w` or `--help` could otherwise
# be consumed by iw's getopt as an OPTION rather than the SSID). Passed as a
# single argv element (never through a shell), so this is a shape bound, not a
# shell-injection guard — it closes a getopt-confusion vector instead.
_SSID_RE = re.compile(r"^(?!-)[\x20-\x7e]{1,32}$")

DEFAULT_CMD_TIMEOUT_S = 15.0

# Mode-arbiter lock (mirrors hackrf_device_lock). Cross-process advisory flock so
# NIC2 mode changes are serialized even if the arbiter is ever driven from more
# than one process; within the single bridge process it serializes the request
# threads too (each acquisition opens its own fd, so a second contender blocks).
WIFI_NIC_MODE_LOCK_PATH = os.environ.get(
    "CEMA_WIFI_NIC_MODE_LOCK_PATH", "/tmp/cema_wifi_nic_mode.lock")
LOCK_ACQUIRE_TIMEOUT_S = 5.0
LOCK_POLL_INTERVAL_S = 0.05


class WifiNicModeBusy(Exception):
    """Raised when the NIC2 mode-arbiter lock could not be acquired within the
    timeout — another wifi-defeat engagement currently holds NIC2. Callers treat
    this like any other refused cycle (log clearly, send a failed ack, do not
    crash), the same convention hackrf_device_lock's HackrfDeviceBusy uses."""


@contextlib.contextmanager
def wifi_nic_mode_lock(timeout_s: float = LOCK_ACQUIRE_TIMEOUT_S):
    """Hold an exclusive, advisory, cross-process lock on NIC2's mode for the
    duration of the `with` block. The governed bridge wraps the ENTIRE
    engagement (mode-switch -> transmit -> restore) in this so no two requests
    can be mid-mode-change on NIC2 at once. Raises WifiNicModeBusy if the lock
    isn't acquired within `timeout_s`. Always releases on the way out, including
    when the wrapped code raises. Mirrors hackrf_device_lock()."""
    fd = os.open(WIFI_NIC_MODE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
    acquired = False
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise WifiNicModeBusy(
                        f"could not acquire NIC2 mode-arbiter lock "
                        f"({WIFI_NIC_MODE_LOCK_PATH}) within {timeout_s}s — another "
                        f"wifi-defeat engagement is currently using NIC2.")
                time.sleep(LOCK_POLL_INTERVAL_S)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- the ONLY real-subprocess function (injectable everywhere for tests) ------
def _run(argv: List[str], timeout: float = DEFAULT_CMD_TIMEOUT_S) -> Tuple[int, str]:
    """Run ONE command as an argv LIST — never shell=True, so no argument can be
    interpreted as a shell metacharacter / injected command. Returns
    (returncode, stderr_text). This is the single seam every public entry point
    routes through; the unit tests inject a fake in its place so no real `iw` /
    `ip` / `dhclient` ever runs."""
    proc = subprocess.run(  # noqa: S603 — argv list, shell=False, validated args
        argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout)
    stderr = proc.stderr.decode(errors="replace") if proc.stderr else ""
    return proc.returncode, stderr


# --- pin binding (mirrors wifi_defeat_primitives._pinned_wifi_tx_iface) --------
def _pinned_wifi_tx_iface() -> Optional[str]:
    """Live-read WIFI_TX_IFACE, treating missing OR whitespace-only as UNSET
    (fail-closed — a blank pin must never be mistaken for a real one). Returns
    the stripped iface name when a real pin is set, else None. Deliberately a
    LOCAL copy of the primitive's identically-named helper (not shared code) so a
    future change to one cannot silently change the other."""
    return (os.environ.get("WIFI_TX_IFACE") or "").strip() or None


def _pin_guard(iface: Any) -> Optional[str]:
    """SAFETY-CRITICAL fail-closed gate shared by every entry point. Returns None
    only when `iface` is a non-blank name that EQUALS the pinned WIFI_TX_IFACE;
    otherwise returns a refuse-message and the caller runs NO command.

    This is what STRUCTURALLY prevents the arbiter ever running `iw`/`ip` against
    the detection NIC: a blank/unset pin refuses, and any `iface` that is not the
    pinned TX NIC refuses. Mirrors wifi_defeat_primitives._wifi_tx_pinning_error's
    bound-to-the-pin behavior — but with NO dev opt-out: the arbiter is only ever
    allowed to touch the one dedicated TX NIC, full stop."""
    pinned = _pinned_wifi_tx_iface()
    if not pinned:
        return ("REFUSING NIC mode-switch (fail-closed): WIFI_TX_IFACE is not set. "
                "The arbiter refuses to run iw/ip against an unpinned interface — "
                "an unpinned switch could reconfigure the RX/detection NIC and take "
                "Kismet detection off the air. Pin the dedicated injection NIC via "
                "WIFI_TX_IFACE (the governed bridge does this through systemd).")
    if not isinstance(iface, str) or not iface.strip():
        return ("REFUSING NIC mode-switch (fail-closed): empty/blank iface — the "
                "arbiter only ever switches the pinned dedicated TX NIC "
                f"({pinned!r}), never an unspecified interface.")
    if iface.strip() != pinned:
        return (f"REFUSING NIC mode-switch (fail-closed): iface {iface!r} does not "
                f"match the pinned WIFI_TX_IFACE {pinned!r}. The mode-arbiter only "
                f"ever switches the DEDICATED injection NIC — it must NEVER touch the "
                f"detection NIC (NIC1/Kismet). Repoint WIFI_TX_IFACE only at the NIC "
                f"you intend to transmit on.")
    return None


def _validate_iface(iface: str) -> Optional[str]:
    """Reject an iface name that isn't a well-formed netdev name (defense in
    depth on top of the pin bind — even the pinned value must be sane before it
    reaches an argv)."""
    if not _IFACE_RE.match(iface.strip()):
        return (f"REFUSING NIC mode-switch (fail-closed): iface {iface!r} is not a "
                f"well-formed network-interface name.")
    return None


def _validate_ssid(ssid: Any) -> Optional[str]:
    if not isinstance(ssid, str) or not _SSID_RE.match(ssid):
        return (f"REFUSING association (fail-closed): SSID {ssid!r} is missing or "
                f"not a well-formed 1-32 char printable-ASCII SSID.")
    return None


def _validate_bssid(bssid: Any) -> Optional[str]:
    if not isinstance(bssid, str) or not _MAC_RE.match(bssid.strip()):
        return (f"REFUSING association (fail-closed): BSSID {bssid!r} is not a "
                f"well-formed MAC address.")
    return None


def _validate_channel(channel: Any) -> Tuple[Optional[int], Optional[str]]:
    """Return (channel_int_or_None, error). None channel is allowed (skip channel
    set). A present channel must be a plausible 802.11 channel number."""
    if channel is None:
        return None, None
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None, f"REFUSING NIC mode-switch (fail-closed): channel {channel!r} is not an integer."
    if not (1 <= ch <= 196):
        return None, f"REFUSING NIC mode-switch (fail-closed): channel {ch} out of the 1-196 range."
    return ch, None


def _run_sequence(steps: List[List[str]], runner: Runner) -> Dict[str, Any]:
    """Run each argv LIST in order, STOPPING at the first non-zero exit or raised
    exception (fail-closed — a half-applied mode is reported, not pushed past).
    Returns {"ok","error","ran"} where `ran` is the argv lists actually executed
    (for test assertions). Never raises."""
    ran: List[List[str]] = []
    for argv in steps:
        try:
            rc, err = runner(argv)
        except Exception as e:  # missing binary, timeout, injected failure, etc.
            return {"ok": False,
                    "error": f"command {argv!r} failed: {e}", "ran": ran}
        ran.append(argv)
        if rc != 0:
            return {"ok": False,
                    "error": f"command {argv!r} exited {rc}: {err[:200]}", "ran": ran}
    return {"ok": True, "error": None, "ran": ran}


# --- public arbiter entry points ---------------------------------------------
# NOTE: these do NOT acquire wifi_nic_mode_lock() themselves — the governed
# bridge holds it across the whole engagement (switch -> transmit -> restore), so
# self-acquiring here would self-deadlock. Call them under a held lock.

def ensure_monitor(iface: str, channel: Optional[Any] = None,
                   runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Put the PINNED TX NIC into MONITOR mode (for 802.11 deauth injection).

    Sequence (standard iw/ip): `ip link set <iface> down` -> `iw dev <iface> set
    type monitor` -> `ip link set <iface> up`, and — when a channel is given —
    `iw dev <iface> set channel <channel>`. See the module LIVE-HARDWARE CAVEAT:
    the exact iw acceptance must be validated on Monday's real card.

    Fail-closed: refuses (ok=False, NO command run) if `iface` is blank or is not
    the pinned WIFI_TX_IFACE (never touches the detection NIC), or on a malformed
    iface/channel. Any subprocess non-zero/exception -> ok=False. Never raises.
    Returns {"ok","error","mode","iface","ran"}."""
    guard = _pin_guard(iface)
    if guard:
        return {"ok": False, "error": guard, "mode": "monitor", "iface": iface, "ran": []}
    iface = iface.strip()
    ierr = _validate_iface(iface)
    if ierr:
        return {"ok": False, "error": ierr, "mode": "monitor", "iface": iface, "ran": []}
    ch, cherr = _validate_channel(channel)
    if cherr:
        return {"ok": False, "error": cherr, "mode": "monitor", "iface": iface, "ran": []}

    steps: List[List[str]] = [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
    ]
    if ch is not None:
        steps.append(["iw", "dev", iface, "set", "channel", str(ch)])

    result = _run_sequence(steps, runner or _run)
    result["mode"] = "monitor"
    result["iface"] = iface
    return result


def ensure_managed_associated(iface: str, ssid: Any, bssid: Any,
                              channel: Optional[Any] = None,
                              runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Put the PINNED TX NIC into MANAGED mode, associate to the drone's OPEN
    softAP (`ssid`/`bssid`) and bring up L3 via DHCP (for ARSDK/Tello command
    injection over the open network).

    Sequence (standard iw/ip for an OPEN network): `ip link set <iface> down` ->
    `iw dev <iface> set type managed` -> `ip link set <iface> up` -> `iw dev
    <iface> connect <ssid> [<bssid>]` (open, no key) -> `dhclient <iface>`. See
    the module LIVE-HARDWARE CAVEAT: the exact `iw connect` form for the open
    softAP and the DHCP client (`dhclient` vs `dhcpcd`) must be validated on
    Monday's real card; the fail-closed spine and pin binding do not depend on
    them.

    Fail-closed: refuses (ok=False, NO command run) if `iface` is blank or is not
    the pinned WIFI_TX_IFACE, or on a malformed iface/ssid/bssid/channel. A
    failed associate OR DHCP step -> ok=False (no half-open association pushed
    past). Never raises. Returns {"ok","error","mode","iface","ran"}."""
    guard = _pin_guard(iface)
    if guard:
        return {"ok": False, "error": guard, "mode": "managed", "iface": iface, "ran": []}
    iface = iface.strip()
    ierr = _validate_iface(iface)
    if ierr:
        return {"ok": False, "error": ierr, "mode": "managed", "iface": iface, "ran": []}
    serr = _validate_ssid(ssid)
    if serr:
        return {"ok": False, "error": serr, "mode": "managed", "iface": iface, "ran": []}
    berr = _validate_bssid(bssid)
    if berr:
        return {"ok": False, "error": berr, "mode": "managed", "iface": iface, "ran": []}
    _, cherr = _validate_channel(channel)
    if cherr:
        return {"ok": False, "error": cherr, "mode": "managed", "iface": iface, "ran": []}

    ssid = ssid  # validated printable ASCII
    bssid = bssid.strip()
    # Open network: `iw connect <ssid> <bssid>` with no key material. The BSSID
    # pins the association to the one target softAP (never a look-alike).
    steps: List[List[str]] = [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "managed"],
        ["ip", "link", "set", iface, "up"],
        ["iw", "dev", iface, "connect", ssid, bssid],
        # Bring up L3 so the UDP command datagram can be routed to the softAP.
        ["dhclient", iface],
    ]

    result = _run_sequence(steps, runner or _run)
    result["mode"] = "managed"
    result["iface"] = iface
    return result


def restore_safe(iface: str, runner: Optional[Runner] = None) -> Dict[str, Any]:
    """Return the PINNED TX NIC to a known SAFE baseline after an engagement (or
    on abort/stop): tear down any drone-AP association and leave the NIC in
    monitor mode with the link cycled, so NIC2 is NEVER left associated to a
    drone softAP.

    Sequence: `dhclient -r <iface>` (release lease, best-effort) -> `iw dev
    <iface> disconnect` (drop any association, best-effort) -> `ip link set
    <iface> down` (the guaranteed-safe state — a down link can neither associate
    nor transmit) -> `iw dev <iface> set type monitor` -> `ip link set <iface>
    up`. The lease-release and disconnect are BEST-EFFORT (the NIC may not be
    associated), so their non-zero exit does not fail the restore; reaching the
    safe baseline (link cycled to monitor) is what determines ok.

    Fail-closed pin binding still applies — restore_safe REFUSES a non-pinned
    iface too, so it can never disconnect/down the detection NIC. Never raises.
    Returns {"ok","error","mode","iface","ran"}. A restore failure is only ever
    logged by the bridge (it touches NIC2 only) and must never block the halt."""
    guard = _pin_guard(iface)
    if guard:
        return {"ok": False, "error": guard, "mode": "safe", "iface": iface, "ran": []}
    iface = iface.strip()
    ierr = _validate_iface(iface)
    if ierr:
        return {"ok": False, "error": ierr, "mode": "safe", "iface": iface, "ran": []}

    run = runner or _run
    ran: List[List[str]] = []
    # BEST-EFFORT teardown of any association: tolerate non-zero / raise (the NIC
    # may simply not be associated). Never let these abort the restore.
    for argv in (["dhclient", "-r", iface], ["iw", "dev", iface, "disconnect"]):
        try:
            run(argv)
        except Exception:
            pass
        ran.append(argv)

    # SAFE baseline (this part matters): cycle the link down and back into
    # monitor mode. A failure HERE is reported so the bridge can log it.
    safe_steps: List[List[str]] = [
        ["ip", "link", "set", iface, "down"],
        ["iw", "dev", iface, "set", "type", "monitor"],
        ["ip", "link", "set", iface, "up"],
    ]
    result = _run_sequence(safe_steps, run)
    result["ran"] = ran + result.get("ran", [])
    result["mode"] = "safe"
    result["iface"] = iface
    return result


# `teardown` is the same operation as restore_safe (tear NIC2 down to the safe
# baseline). Exposed under both names because the plan/safety-spine refers to it
# both ways ("restore after an op" / "teardown on abort/stop").
teardown = restore_safe
