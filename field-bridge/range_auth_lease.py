#!/usr/bin/env python3
"""Shared range-authorization LEASE gate for the continuous-TX field bridges.

THE INVARIANT THIS ENFORCES (holistic)
======================================
A CONTINUOUS RF effect — a continuous/swept jam (field-bridge/jam_bridge.py via
hackrf_jam.transmit_burst/transmit_sweep), a continuous SDR-MAVLink inject
(field-bridge/sdr_mavlink_inject_bridge.py via hackrf_jam.transmit_iq_file), and
the sustained MAVLink takeover (field-bridge/mavlink_takeover.py) — must STOP not
only on EMERGENCY ABORT / tx_halt but ALSO the instant the range-authorization
LEASE EXPIRES mid-stream. A bare lease expiry, WITHOUT any operator abort, must
terminate an in-progress transmission within one poll interval — the same way an
abort does.

The bridges already make a LIVE range-auth check ONCE at request start (their
"Gate A": GET /api/range-authorization/status?effect=...). This helper re-polls
that SAME live source DURING the transmission, on every per-frame / per-poll
halt check, closing the gap where a 15-minute lease could expire while a
continuous jam / inject kept transmitting because only tx_halt was re-polled.

WHY A SHORT-TTL CACHE
=====================
hackrf_jam.transmit_burst / transmit_sweep / transmit_iq_file poll their
tx_halt_check very frequently (every ~0.1s in _supervise_transfer, and before
every sweep dwell). Hitting the backend on EVERY poll would hammer it and add
per-poll latency, so this mirrors the MAVLink takeover's range_auth.authorized()
0.5s-TTL live check: the live status is cached for a short TTL and re-fetched
only when it goes stale. A 0.5s TTL bounds the worst-case "kept transmitting
after the lease expired" window to TTL + one poll interval (sub-second), while
EMERGENCY ABORT / tx_halt stay INSTANT — they are the local boolean checked
FIRST in make_tx_halt_check() and do NOT go through this cache.

FAIL-CLOSED
===========
Any error from the underlying check (unreachable backend, timeout, 401/403,
malformed body) is treated as NOT authorized. The bridges' is_range_authorized()
already never raises and already fails closed; this helper still guards the call
and fails closed independently, so a broken predicate can never keep a
transmitter keyed.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# Non-binding module default TTL, mirroring the MAVLink takeover's 0.5s live
# range-auth re-check. Tests may monkeypatch this (or pass ttl_s explicitly) to
# make the re-check deterministic; production callers use the default.
DEFAULT_TTL_S = 0.5


class RangeAuthLease:
    """Short-TTL, fail-closed cache over a live range-authorization check.

    check_authorized: zero-arg callable returning True iff the range-auth lease
    is currently armed for this effect (the bridges pass a bound
    is_range_authorized(effect); it already fails closed on any error). Its
    result is cached for ttl_s so a high-frequency per-frame halt check does not
    hit the backend on every poll. now() is injectable for deterministic tests.
    """

    def __init__(
        self,
        check_authorized: Callable[[], bool],
        ttl_s: Optional[float] = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._check = check_authorized
        self._ttl_s = DEFAULT_TTL_S if ttl_s is None else max(0.0, float(ttl_s))
        self._now = now
        self._lock = threading.Lock()
        self._cached = False
        self._expires_at = float("-inf")  # force a live check on the first call

    def authorized(self) -> bool:
        """True iff the lease is (still) armed. Re-polls the live source when the
        cached value is older than ttl_s; fails closed (False) on any error."""
        with self._lock:
            t = self._now()
            if t >= self._expires_at:
                try:
                    self._cached = bool(self._check())
                except Exception:
                    self._cached = False  # fail closed — never assume authorized
                self._expires_at = t + self._ttl_s
            return self._cached


def make_tx_halt_check(
    tx_halted: Callable[[], bool],
    lease: RangeAuthLease,
) -> Callable[[], bool]:
    """Build a tx_halt_check (the predicate hackrf_jam's transmit primitives poll
    every iteration) that returns True when EITHER the local EMERGENCY ABORT
    (tx_halted) is set OR the range-auth lease is no longer authorized.

    This is exactly the MAVLink takeover's _halted() semantics — "tx_halt OR
    range-auth lost" — reused by the continuous jam and continuous SDR-inject so
    all three continuous paths stop UNIFORMLY on a bare lease expiry, not only on
    an operator abort. tx_halted is checked FIRST (a local boolean, instant), so
    an EMERGENCY ABORT is never delayed by the lease's (TTL-cached) live poll. A
    raising tx_halted predicate fails SAFE (treated as halt)."""
    def _halted() -> bool:
        try:
            if tx_halted():
                return True
        except Exception:
            return True  # fail safe — a broken predicate must not keep TX keyed
        return not lease.authorized()
    return _halted
