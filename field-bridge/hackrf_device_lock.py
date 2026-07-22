#!/usr/bin/env python3
"""Cross-process mutual-exclusion lock for the single physical HackRF device.

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
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import time
from typing import Iterator

# Shared lockfile path. Single physical HackRF device, so a single fixed path
# is intentional -- every caller across every process in this field-bridge
# must contend for the SAME file to get real mutual exclusion.
HACKRF_DEVICE_LOCK_PATH = os.environ.get("CEMA_HACKRF_LOCK_PATH", "/tmp/cema_hackrf_device.lock")

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
    """Raised when the HackRF device lock could not be acquired within the
    timeout -- i.e. the other process is currently mid-sweep/mid-capture.
    Callers should treat this exactly like any other skipped/failed cycle
    (log clearly, do not crash), the same pattern hackrf_rx.py already uses
    for a wedged hackrf_sweep timeout."""


@contextlib.contextmanager
def hackrf_device_lock(timeout_s: float = LOCK_ACQUIRE_TIMEOUT_S) -> Iterator[None]:
    """Context manager that holds an exclusive, advisory, cross-process lock
    on the shared HackRF device for the duration of the `with` block.

    Wrap ONLY the actual subprocess invocation (hackrf_sweep / hackrf_transfer)
    in this, not the surrounding Python bookkeeping -- keep the critical
    section as short as possible so the other process isn't kept waiting any
    longer than the real device access requires.

    Raises HackrfDeviceBusy if the lock isn't acquired within `timeout_s`
    seconds (the other process is currently holding it). Always releases the
    lock on the way out, including when the wrapped code raises.
    """
    # Open (create if needed) the lockfile. Never deleted -- it is just a
    # mutex handle, its content is irrelevant, and leaving it in place avoids
    # a race where one process deletes it while another is opening it.
    fd = os.open(HACKRF_DEVICE_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o666)
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
                        f"could not acquire HackRF device lock ({HACKRF_DEVICE_LOCK_PATH}) "
                        f"within {timeout_s}s -- another process is currently using the device."
                    )
                time.sleep(LOCK_POLL_INTERVAL_S)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
