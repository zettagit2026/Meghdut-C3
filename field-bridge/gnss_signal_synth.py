#!/usr/bin/env python3
"""GNSS L1 C/A signal synthesis — the DSP core of Task #103's GNSS-spoof
("soft-kill") capability. See field-bridge/GNSS_SPOOF_ARCHITECTURE.md for
the full design and the §7 task split.

=============================================================================
STATUS: STUB — Task B (parallel workstream), NOT this task (Task A).
=============================================================================
This module is deliberately a STUB. Task A (backend/server.py's
effect=gnss_spoof range-authorization + token/attestation/preview gate
chain, field-bridge/gnss_spoof_bridge.py's WS bridge + gate chain,
frontend/src/pages/GnssSpoof.jsx) is safety-gate plumbing and does NOT
require GNSS-domain/DSP knowledge — see the architecture doc's explicit
recommendation to split this out to a DSP-focused owner (Embedded Firmware
Engineer / AI Engineer with SDR experience).

Real implementation (Task B) needs to:
  1. Generate a fabricated-but-structurally-valid ephemeris/almanac for a
     plausible false receiver position (fake_lat/fake_lon/fake_alt_m below).
  2. Generate GPS L1 C/A PRN (Gold code) chipping sequences per-satellite.
  3. Encode a NAV message (subframes 1-3 minimum) carrying that fabricated
     ephemeris.
  4. BPSK-modulate the NAV message onto the PRN codes at the 1.023 Mcps C/A
     chipping rate.
  5. Generate IQ samples at a sample rate compatible with HackRF (matching
     hackrf_jam.SAMPLE_RATE_HZ=20_000_000, or a lower L1-C/A-specific rate —
     a DSP-owner decision, not made here) and write them to a temp IQ file
     in the same interleaved-int8 format hackrf_jam.py already uses.

Reference technique (NOT copied — reimplemented cleanly against this
project's own license posture): osqzss/gps-sdr-sim (MIT, OSI-permissive),
the de facto public reference implementation for GPS L1 C/A signal
simulation. See the architecture doc §0 for the full citation/vendoring
guidance.

This module has NO networking/WS/HTTP code — it is independently
unit-testable (feed it a true position + fake offset, assert it emits a
valid IQ file of correct sample rate/duration) without any HackRF hardware
or bridge/backend code present, exactly as the architecture doc specifies.
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

# Matches hackrf_jam.SAMPLE_RATE_HZ — the DSP owner (Task B) should confirm
# whether a lower, L1-C/A-specific rate is more appropriate; this constant
# is a placeholder default, not a considered DSP decision.
SAMPLE_RATE_HZ = 20_000_000

# Set this env var to opt IN to a placeholder (silent, all-zero) IQ file
# instead of raising NotImplementedError — lets Task A's gate-chain
# integration tests exercise the FULL path (including a real
# hackrf_transfer invocation against a real, if content-free, IQ file)
# end-to-end before Task B's real DSP lands. Never set in production —
# gnss_spoof_bridge.py's own docstring and systemd unit must make clear
# this capability is NOT to be enabled as a live service until Task B is
# integrated (see that file + the top-level task instructions).
_PLACEHOLDER_ENV = "GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ"


class GnssSynthNotImplemented(NotImplementedError):
    """Raised by synthesize_iq_file() when no placeholder IQ is requested —
    distinguishes "DSP not implemented yet" from a generic NotImplementedError
    so gnss_spoof_bridge.py can give an honest, specific failure reason."""


def synthesize_iq_file(
    true_lat: float,
    true_lon: float,
    true_alt_m: float,
    fake_lat: float,
    fake_lon: float,
    fake_alt_m: float,
    duration_s: float,
    sample_rate: int = SAMPLE_RATE_HZ,
) -> str:
    """TODO(Task B): replace this stub with real GPS L1 C/A signal
    synthesis (ephemeris/almanac fabrication, PRN generation, NAV message
    encoding, BPSK modulation — see module docstring).

    Returns a path to a temp IQ file (interleaved int8, matching
    hackrf_jam.py's format) that field-bridge/gnss_spoof_bridge.py hands to
    hackrf_jam.transmit_iq_file() exactly as it would a real synthesized
    file — the bridge's gate chain does not need to know or care whether
    the file's CONTENT is a real spoof signal or a placeholder; that
    distinction lives entirely in this module.

    Raises GnssSynthNotImplemented unless GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ is
    set truthy in the environment, in which case it writes a silent
    (all-zero) IQ file of the correct sample rate/duration instead — for
    Task A's gate-chain testing ONLY, never for production transmission.
    """
    if os.environ.get(_PLACEHOLDER_ENV, "").lower() not in ("1", "true", "yes"):
        raise GnssSynthNotImplemented(
            "gnss_signal_synth.synthesize_iq_file() is a Task A stub — real GPS L1 C/A "
            "signal synthesis is Task B's parallel-track deliverable and has not been "
            "integrated yet. Set GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ=1 to generate a "
            "placeholder (silent) IQ file for gate-chain testing only — this must NEVER "
            "be set in a production/live deployment."
        )
    n = int(duration_s * sample_rate)
    f = tempfile.NamedTemporaryFile(suffix=".iq", delete=False)
    try:
        # Silent (all-zero) IQ — content-free placeholder, correct size/duration
        # only. Written in chunks to avoid a huge single bytes() allocation for
        # longer durations.
        chunk_n = min(n, 1_000_000)
        zero_chunk = bytes(2 * chunk_n)  # interleaved I/Q, 1 byte each -> 2 bytes/sample
        remaining = n
        while remaining > 0:
            take = min(remaining, chunk_n)
            f.write(zero_chunk[: 2 * take])
            remaining -= take
        f.flush()
    finally:
        f.close()
    return f.name
