#!/usr/bin/env python3
"""Cross-process mutual-exclusion lock for one or more physical HackRF devices.

WHY THIS EXISTS
---------------
Two independent long-running processes in this field-bridge can each try to
open the HackRF at any time:
  - hackrf_rx.py       : runs `hackrf_sweep` roughly every 3s (see SWEEP_TIMEOUT_S,
                         _one_sweep()/sweep_band() in hackrf_rx.py).
  - ml_classify_bridge.py : runs a gate-check `hackrf_sweep` roughly every 12s
                         (4x hackrf_rx.py's interval, see its module docstring),
                         and -- only when its energy gate passes -- an additional
                         `hackrf_transfer` IQ capture via iq_capture.py's
                         capture_iq().

A Reality Checker review found NO device-access coordination existed between
these two processes: they relied purely on timing separation (12s vs 3s
interval) to avoid colliding. HackRF only supports one open libusb handle at a
time, so if both processes ever open the device at the same moment, at least
one `hackrf_sweep`/`hackrf_transfer` invocation fails outright. This module
adds a simple, robust flock()-based mutex so only one of the two processes can
be mid-subprocess-call (hackrf_sweep OR hackrf_transfer) against the device at
any given moment -- the other blocks (briefly, bounded) or skips that cycle
gracefully rather than colliding or hanging forever.

WHY A LOCKFILE + fcntl.flock(), NOT SOMETHING FANCIER
------------------------------------------------------
Both processes are independent OS processes (no shared memory/IPC -- see
ml_classify_bridge.py's own docstring on why it polls rather than sharing
in-process state with hackrf_rx.py). A single well-known lockfile path plus
POSIX advisory `flock()` is the simplest correct cross-process mutex for this
case: the OS releases the lock automatically even if a process is killed
(no stale-lock cleanup logic needed), and Python's `fcntl.flock()` gives us
this for free with no extra dependency.

UPDATE (second physical HackRF acquired, 2026-07): a second HackRF unit now
exists. It is NOT yet passed through to this VM at the hypervisor level, and
no direction-finding pipeline consumes it yet -- see
DIRECTION_FINDING_NOTES.md for that groundwork and what's still missing.
What DOES matter today: once two devices are both addressable, they must
NOT share one lockfile -- each physical HackRF has its own independent
libusb handle, so serializing both units behind a single mutex would block
sweeps/captures on unit A while unit B is busy for no real reason (and vice
versa). `hackrf_device_lock()` below now derives a per-device lockfile path
from an optional `serial` argument, so each device gets its own mutex.
Callers that omit `serial` (every current call site, until
HACKRF_RX_SERIAL/HACKRF_SERIAL/hackrf_config.py role assignment is actually
configured) keep contending for the original shared path -- so existing
single-device behavior and cross-process coordination between
hackrf_rx.py / ml_classify_bridge.py / droneid_decode_bridge.py is
UNCHANGED by this update.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import time
from typing import Iterator, Optional

# Shared/default lockfile path, used when no device serial is given (backward
# -compat default -- see UPDATE note above). This was the only path when only
# one physical HackRF existed; it remains correct as the fallback for callers
# that don't (yet) know/care which physical unit they're touching.
HACKRF_DEVICE_LOCK_PATH = os.environ.get("CEMA_HACKRF_LOCK_PATH", "/tmp/cema_hackrf_device.lock")

_SERIAL_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _lock_path_for(serial: Optional[str]) -> str:
    """Return the per-device lockfile path for `serial`, or the shared
    default path if `serial` is falsy (None/""). A small pure function so
    the per-device derivation is trivially testable without touching real
    filesystem/locking state."""
    if not serial:
        return HACKRF_DEVICE_LOCK_PATH
    # HackRF serials are hex strings in practice, but this value feeds
    # directly into a filesystem path -- don't trust that blindly, strip
    # anything that isn't alnum/underscore/dot/dash.
    safe = _SERIAL_SAFE_RE.sub("_", serial)
    return f"/tmp/cema_hackrf_device_{safe}.lock"

# Bounded acquisition timeout. hackrf_rx.py's own SWEEP_TIMEOUT_S (8.0s) is the
# generous ceiling for a *single* hackrf_sweep pass to detect a wedged device;
# a lock wait should be much shorter than that -- a healthy sweep/transfer
# holds the lock for well under a second to a few seconds, so 5s gives a
# waiting caller a reasonable chance to get in after the current holder
# finishes, without blocking so long that it itself looks like a wedged/hung
# cycle to its own caller.
LOCK_ACQUIRE_TIMEOUT_S = 5.0
LOCK_POLL_INTERVAL_S = 0.05


class HackrfDeviceBusy(Exception):
    """Raised when a HackRF device lock could not be acquired within the
    timeout -- i.e. another process is currently mid-sweep/mid-capture
    against THAT SAME physical device (same serial, or both on the shared
    default path if no serial is used). Callers should treat this exactly
    like any other skipped/failed cycle (log clearly, do not crash), the
    same pattern hackrf_rx.py already uses for a wedged hackrf_sweep
    timeout."""


@contextlib.contextmanager
def hackrf_device_lock(timeout_s: float = LOCK_ACQUIRE_TIMEOUT_S,
                        serial: Optional[str] = None) -> Iterator[None]:
    """Context manager that holds an exclusive, advisory, cross-process lock
    on ONE physical HackRF device for the duration of the `with` block.

    `serial`, if given, scopes the lock to that specific device's own
    lockfile (`/tmp/cema_hackrf_device_{serial}.lock`) so two different
    physical HackRFs never contend for each other's lock. If omitted
    (default), this falls back to the original shared lockfile path --
    correct, unchanged behavior for every current single-device caller.

    Wrap ONLY the actual subprocess invocation (hackrf_sweep / hackrf_transfer)
    in this, not the surrounding Python bookkeeping -- keep the critical
    section as short as possible so another caller waiting on the SAME
    device isn't kept waiting any longer than the real device access
    requires.

    Raises HackrfDeviceBusy if the lock isn't acquired within `timeout_s`
    seconds (another process is currently holding the lock for this same
    device/path). Always releases the lock on the way out, including when
    the wrapped code raises.
    """
    lock_path = _lock_path_for(serial)
    # Open (create if needed) the lockfile. Never deleted -- it is just a
    # mutex handle, its content is irrelevant, and leaving it in place avoids
    # a race where one process deletes it while another is opening it.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
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
                    raise HackrfDeviceBusy(
                        f"could not acquire HackRF device lock ({lock_path}) "
                        f"within {timeout_s}s -- another process is currently using the device."
                    )
                time.sleep(LOCK_POLL_INTERVAL_S)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
