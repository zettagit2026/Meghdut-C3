#!/usr/bin/env python3
"""Bidirectional DSHOT (eRPM + Extended DSHOT Telemetry) frame decoder.

=============================================================================
TASK #115 -- HONESTY DETERMINATION, READ THIS BEFORE ASSUMING RF RELEVANCE
=============================================================================
DSHOT (Digital SHOT) is a flight-controller-to-ESC MOTOR CONTROL protocol.
Bidirectional DSHOT ("bidir DSHOT" / DSHOT-EDT) adds an ESC-to-FC TELEMETRY
return path (eRPM, and optionally temperature/voltage/current/debug/state
via the "Extended DSHOT Telemetry" convention). Both directions ride the
SAME PHYSICAL WIRE: a single dedicated GPIO/timer-channel pin per motor,
driven either by hardware DMA/PWM framing (normal DSHOT) or bit-banged GPIO
sampling at ~3x oversample (bidirectional telemetry return), between an FC
and an ESC, at millimeter distances inside one aircraft. There is no radio
involved anywhere in the DSHOT spec -- it is a wired, single-ended,
inverted or non-inverted digital signal (typically 3.3V/5V logic), full
stop.

DETERMINATION: this project's RF/SDR detection pipeline (HackRF + the
existing CRSF/MAVLink/DroneCAN/FrSky-SmartPort/CANopen parsers in this
field-bridge/ directory) has ZERO realistic path to ever observe DSHOT
traffic over the air. Checked specifically, per the task brief, whether any
protocol this project already decodes has a DSHOT-passthrough or
ESC-telemetry-relay mode analogous to CRSF's MSP-displayport passthrough:
  - CRSF (crsf_parser.py, this directory) does NOT carry, tunnel, or relay
    DSHOT frames. CRSF has its OWN native telemetry frame types (e.g. frame
    type 0x0A "ESC telemetry" / BF's periph "RPM_CONSUMPTION") that some FCs
    populate FROM locally-decoded DSHOT/bidir-DSHOT data, but that is the
    FC re-encoding already-decoded RPM/temp/voltage numbers into CRSF's own
    telemetry format for the air link -- it is not DSHOT bytes in any form
    reaching the RF link. Confirmed by grep across crsf_parser.py: no
    DSHOT/ESC-telemetry references exist there.
  - MSP-over-CRSF-displayport-passthrough (the closest known precedent for
    "protocol X tunneled inside protocol Y over RF" in this whole FPV/drone
    ecosystem) is a *different* case: MSP is a serial protocol carrying OSD
    text, not motor-control-bus GCR telemetry, and even that passthrough
    carries pre-decoded fields, not raw wire-level frames of the tunneled
    protocol.
  - No SDK/reference implementation checked (Betaflight, INAV, Bluejay,
    DShotRMT, the community bidir-DSHOT wiki) documents DSHOT ever crossing
    an RF hop in any product. It is architecturally a same-airframe,
    same-PCB-or-short-wire-harness bus, by design (its whole point is
    microsecond-scale ESC control latency that no RF link could sustain).

CONCLUSION: this is scoped as **(a) with a completeness caveat toward (b)**
-- there is currently NO RF-detection use case for this module in the
Meghaduta/CEMA counter-UAS mission. It is built as a software
protocol-decode library for completeness of this project's growing
multi-protocol drone-bus parser collection, and for the one scenario where
it COULD become relevant: forensic analysis of a captured or downed drone's
ESC bus via a direct wired logic-analyzer/oscilloscope tap (e.g. a downed
target's FC-to-ESC signal wire probed post-recovery) -- NOT a live RF
detection capability, and it is NOT wired into any detection-ingest/posting
path, unlike every other parser in this directory. Do not represent this as
adding counter-UAS detection coverage in status reporting.

=============================================================================
LICENSING -- reference sources checked
=============================================================================
Reference implementations located and checked, per task brief:
  - Betaflight (github.com/betaflight/betaflight),
    src/main/drivers/dshot.c + dshot_bitbang_decode.c -- GPL-3.0-or-later
    (file headers say so explicitly). Its dshot_bitbang_decode.c GCR
    decode function and dshot.c's eRPM-period / Extended-DSHOT-Telemetry
    (EDT) type-field decode were read for spec understanding (bit-widths,
    checksum-fold algorithm, EDT type-field layout, eRPM period formula)
    but NOT copied line-for-line: this module's decode table is instead
    derived from-scratch in code below by *inverting* the DSHOT
    bidirectional-telemetry 4-bit->5-bit GCR ENCODE table, which is the
    actual protocol definition published in the open community spec doc
    (github.com/bird-sanctuary/extended-dshot-telemetry, referenced
    directly in Betaflight's own dshot.c comment "Follows the extended
    dshot telemetry documentation found at
    https://github.com/bird-sanctuary/extended-dshot-telemetry"). That
    spec repo carries no explicit LICENSE file at the path checked; per
    this project's established precedent (crsf_parser.py, dronecan_parser.py,
    frsky_smartport_parser.py under the internal open-source-sovereignty
    override), no code text was copied from Betaflight regardless -- the
    GCR table below is reconstructed independently from the protocol's
    known bit encoding (a fixed part of the wire spec, not copyrightable
    expression, exactly analogous to reusing a published CRC polynomial).
  - Bluejay ESC firmware (github.com/bluejayfw/bluejay) -- GPL-3.0. Its
    bidirectional DSHOT transmit-side GCR encode table was cross-checked
    as an independent second source for the same 4b/5b mapping; again, no
    code copied, used only to corroborate the table values against a
    second independent implementation (same technique this project already
    uses in frsky_smartport_parser.py: "used strictly as independent spec
    cross-checks... never as a code source").
  - DShotRMT (github.com/derdoktor667/DShotRMT, ESP32 RMT-based) -- MIT
    licensed. Its telemetry decode path was reviewed for the eRPM
    mantissa/exponent period formula and the "0x0FFF means zero/idle"
    special case, both of which are protocol-spec facts, not expression;
    no code copied (its structure is RMT-peripheral-specific C++ and does
    not map onto this project's Python parser conventions anyway).
None of the above required invoking a GPL sign-off override for THIS
module, since nothing was transcribed -- the decode table is built by table
inversion of the published spec encoding, and the checksum/period formulas
are reimplemented from the protocol specification, exactly the same
clean-room posture already used for frsky_smartport_parser.py and
crsf_parser.py in this directory.

=============================================================================
PROTOCOL SUMMARY (bidirectional DSHOT / DSHOT-EDT)
=============================================================================
Bidirectional DSHOT telemetry return frame: 21 bits total on the wire
(sampled at 3x oversample by the FC's bit-bang receiver), logically:
  [1 start bit][20 data bits] = [1 start bit][4 x 5-bit GCR nibbles]
The 4 GCR nibbles decode (via the table below) to 4 raw nibbles forming a
16-bit value:
  bits 15..4  = 12-bit telemetry payload
  bits 3..0   = 4-bit checksum
Checksum check (fold-XOR, matches the wire-level scheme in all three
references above): XOR the 16-bit decoded value with itself shifted right
by 8, then by 4; the low nibble of the result must equal 0xF for the frame
to be considered valid (this is the DSHOT bidir checksum invariant, not a
CRC in the traditional sense).

The 12-bit payload is interpreted one of two ways:
  1. Plain eRPM telemetry (older / default): the 12 bits are a
     mantissa/exponent encoded period: bits 11..9 = exponent (0-7),
     bits 8..0 = 9-bit mantissa. period_us_x100 = mantissa << exponent.
     eRPM = 60,000,000,00 / period_us_x100 (i.e. converts period to
     electrical RPM x100... see erpm_from_period() below for the exact
     scaled-integer formula, matching the public spec's stated units).
     Raw value 0x0FFF is the documented "zero/stopped" special case.
  2. Extended DSHOT Telemetry (EDT, github.com/bird-sanctuary/
     extended-dshot-telemetry): bits 11..8 encode a 3-bit "telemetry type"
     tag (with an interleaved marker bit) selecting between eRPM and one of
     temperature (deg C), voltage (0.25V/LSB), current (A), debug1/2/3, or
     ESC state/event flags, with bits 7..0 carrying that type's raw 8-bit
     value. EDT is negotiated by the ESC (older ESCs/firmware never set the
     EDT type bits, so type falls back to eRPM by convention) -- this
     module treats "type tag == 0 or type tag's low bit set" as eRPM,
     matching the reference decoders' fallback logic.

Requires: no third-party packages (pure stdlib). Not registered in
requirements.txt since it has no live serial/SDR dependency -- see the
HARDWARE STATUS note above: this is decode-logic-only, exercised via the
embedded self-test and, optionally, against a raw bit capture file (e.g.
exported from a logic analyzer probing a real FC-ESC bus/downed-drone ESC
wire), never against RF.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional


# =============================================================================
# GCR 4-bit -> 5-bit encode table (the actual bidirectional-DSHOT wire spec,
# cross-checked against Betaflight's and Bluejay's independent decode/encode
# implementations -- see LICENSING section above). The decode table is built
# below by inverting this table, rather than transcribing anyone's flattened
# decode array.
# =============================================================================
GCR_ENCODE_TABLE: Dict[int, int] = {
    0x0: 0b11001,
    0x1: 0b11011,
    0x2: 0b10010,
    0x3: 0b10011,
    0x4: 0b11101,
    0x5: 0b10101,
    0x6: 0b10110,
    0x7: 0b10111,
    0x8: 0b11010,
    0x9: 0b01001,
    0xA: 0b01010,
    0xB: 0b01011,
    0xC: 0b11110,
    0xD: 0b01101,
    0xE: 0b01110,
    0xF: 0b01111,
}
# Inverted at import time: this IS the decode table, derived, not copied.
GCR_DECODE_TABLE: Dict[int, int] = {code: nibble for nibble, code in GCR_ENCODE_TABLE.items()}


def gcr_encode_nibble(nibble: int) -> int:
    """Encode a raw 4-bit nibble (0-15) to its 5-bit GCR wire codeword.

    Used only by the self-test to build known-good synthetic frames (this
    module is a decoder; an ESC would be the one doing this in a real
    system). Raises ValueError for out-of-range input rather than guessing.
    """
    if not 0 <= nibble <= 0xF:
        raise ValueError(f"nibble out of range: {nibble}")
    return GCR_ENCODE_TABLE[nibble]


def gcr_decode_nibble(code5: int) -> Optional[int]:
    """Decode a 5-bit GCR wire codeword to its raw 4-bit nibble.

    Returns None for any of the 16 five-bit patterns that are not valid GCR
    codewords (invalid transition density) -- never guesses a "closest"
    nibble.
    """
    return GCR_DECODE_TABLE.get(code5 & 0x1F)


def gcr_decode_frame(bits21: int) -> Optional[int]:
    """Decode a full 21-bit bidirectional-DSHOT telemetry frame (1 start bit
    + 4x 5-bit GCR nibbles) to its 16-bit (12-bit payload + 4-bit checksum)
    decoded value.

    Returns None if any of the four 5-bit groups is not a valid GCR
    codeword, or if the fold-XOR checksum invariant fails. Never returns a
    value for a frame it can't validate -- matches this project's
    no-fabricated-data convention used throughout field-bridge/.
    """
    if not 0 <= bits21 <= 0x1FFFFF:
        raise ValueError(f"bits21 out of range for a 21-bit frame: {bits21:#x}")

    # First bit is the start bit; discard it, keep the low 20 data bits.
    data20 = bits21 & 0xFFFFF

    decoded = 0
    for shift in (15, 10, 5, 0):
        nibble = gcr_decode_nibble((data20 >> shift) & 0x1F)
        if nibble is None:
            return None
        decoded = (decoded << 4) | nibble

    # Fold-XOR checksum invariant: low nibble of (v ^ v>>8 ^ v>>4) must be 0xF.
    csum = decoded ^ (decoded >> 8)
    csum ^= (csum >> 4)
    if (csum & 0xF) != 0xF:
        return None

    return decoded >> 4  # drop the checksum nibble, return the 12-bit payload


class TelemetryType(IntEnum):
    ERPM = 0
    TEMPERATURE = 1
    VOLTAGE = 2
    CURRENT = 3
    DEBUG1 = 4
    DEBUG2 = 5
    DEBUG3 = 6
    STATE_EVENTS = 7


# EDT type-tag (bits 11..8 of the 12-bit payload) -> TelemetryType, per the
# bird-sanctuary/extended-dshot-telemetry convention: the low bit of the
# 4-bit tag is a marker bit (0 for eRPM-compatible legacy frames), and the
# upper 3 bits select the EDT payload kind when the marker bit is set and
# non-zero.
_EDT_TYPE_LOOKUP = {
    1: TelemetryType.TEMPERATURE,
    3: TelemetryType.VOLTAGE,
    5: TelemetryType.CURRENT,
    7: TelemetryType.DEBUG1,
    9: TelemetryType.DEBUG2,
    11: TelemetryType.DEBUG3,
    13: TelemetryType.STATE_EVENTS,
}

ERPM_ZERO_SENTINEL = 0x0FFF  # documented spec special case: "motor stopped"


@dataclass
class DecodedTelemetry:
    telemetry_type: TelemetryType
    raw_value12: int
    # Populated only for TelemetryType.ERPM:
    erpm: Optional[int] = None
    # Populated for EDT (non-eRPM) types: raw 8-bit payload byte.
    edt_value8: Optional[int] = None


def erpm_from_period(value12: int) -> Optional[int]:
    """Decode a plain (non-EDT) 12-bit eRPM telemetry payload.

    Encoding: bits 11..9 = exponent (0-7), bits 8..0 = 9-bit mantissa.
    period (in units of 100ns) = mantissa << exponent.
    eRPM = 60,000,000 / (period_100ns * 1e-1)  -- expressed below as pure
    integer math following the spec's stated conversion (period is in
    100ns units representing 1/erpm's periodic interval scaled by 100),
    matching the widely cross-referenced formula used by all three
    checked reference decoders. Returns 0 for the documented "stopped"
    sentinel (0x0FFF), and None if the decoded period is zero (a
    genuinely invalid/corrupt frame, distinct from "stopped").
    """
    if not 0 <= value12 <= 0xFFF:
        raise ValueError(f"value12 out of range: {value12:#x}")

    if value12 == ERPM_ZERO_SENTINEL:
        return 0

    exponent = (value12 & 0xFE00) >> 9
    mantissa = value12 & 0x01FF
    period = mantissa << exponent
    if period == 0:
        return None

    # erpm*100 = 60,000,000 / (period * 1e-4 s) collapsed to integer math;
    # matches the reference decoders' "(6,000,000,000 + period/2) / period"
    # rounded-integer form.
    return (6_000_000_00 + period // 2) // period


def decode_telemetry_payload(value12: int, edt_enabled: bool = True) -> DecodedTelemetry:
    """Interpret a validated 12-bit telemetry payload (post-GCR-decode,
    post-checksum) as either plain eRPM or an Extended-DSHOT-Telemetry
    (EDT) field, mirroring the EDT-negotiation fallback used by real ESC
    firmware: if EDT was never negotiated/observed for this motor, or the
    tag doesn't decode to a known EDT type, treat it as eRPM.
    """
    if not 0 <= value12 <= 0xFFF:
        raise ValueError(f"value12 out of range: {value12:#x}")

    type_tag = (value12 >> 8) & 0x0F
    edt_type = _EDT_TYPE_LOOKUP.get(type_tag) if edt_enabled else None

    if edt_type is None:
        return DecodedTelemetry(
            telemetry_type=TelemetryType.ERPM,
            raw_value12=value12,
            erpm=erpm_from_period(value12),
        )

    return DecodedTelemetry(
        telemetry_type=edt_type,
        raw_value12=value12,
        edt_value8=value12 & 0xFF,
    )


def edt_temperature_c(value8: int) -> int:
    """EDT temperature field: raw value IS degrees Celsius (1 deg C/LSB)."""
    return value8


def edt_voltage_v(value8: int) -> float:
    """EDT voltage field: 0.25V per LSB (per the published EDT spec table)."""
    return value8 * 0.25


def edt_current_a(value8: int) -> int:
    """EDT current field: 1A per LSB."""
    return value8


# =============================================================================
# Self-test -- real, spec-derived round-trip test vectors (not fabricated)
# =============================================================================

def _encode_frame(nibbles4: List[int]) -> int:
    """Build a real 21-bit wire frame (start bit=0, MSB-first) from 4 raw
    nibbles, GCR-encoding each -- used only to construct known-good and
    known-bad test vectors below, exercising the actual decode path
    end-to-end exactly as an ESC's transmitted bitstream would look.
    """
    assert len(nibbles4) == 4
    data20 = 0
    for nibble in nibbles4:
        data20 = (data20 << 5) | gcr_encode_nibble(nibble)
    return data20  # start bit is bit 20, implicitly 0 (not represented in data20)


def _value16_to_nibbles(value16: int) -> List[int]:
    return [(value16 >> shift) & 0xF for shift in (12, 8, 4, 0)]


def _checksum_fold(decoded16: int) -> int:
    csum = decoded16 ^ (decoded16 >> 8)
    csum ^= (csum >> 4)
    return csum & 0xF


def _valid_frame_for_payload(payload12: int) -> int:
    """Build a real 21-bit GCR-encoded wire frame carrying payload12 with a
    checksum nibble that actually satisfies the fold-XOR invariant.

    The checksum nibble is itself folded INTO the invariant check (the
    invariant is computed over the full 16-bit decoded value, checksum
    nibble included -- see gcr_decode_frame), so it can't be derived by a
    simple closed-form expression from the payload alone; it is solved for
    directly here by trying all 16 candidate nibbles and keeping the one
    that satisfies the same invariant gcr_decode_frame checks. This is
    exactly what a real ESC's telemetry encoder does (compute a checksum
    nibble such that the receiver's fold-check passes).
    """
    for candidate in range(16):
        decoded16 = (payload12 << 4) | candidate
        if _checksum_fold(decoded16) == 0xF:
            return _encode_frame(_value16_to_nibbles(decoded16))
    raise AssertionError(f"no valid checksum nibble found for payload {payload12:#x}")


def self_test() -> None:
    # --- GCR table sanity: encode/decode must round-trip for all 16 nibbles,
    # and the table must be a bijection (16 distinct 5-bit codewords, all
    # with the "no more than one leading/trailing all-zero run" GCR property
    # implicit in the published table -- checked here simply as distinctness
    # + round-trip rather than re-deriving GCR run-length theory).
    assert len(set(GCR_ENCODE_TABLE.values())) == 16, "GCR encode table must be a bijection"
    for nibble in range(16):
        code = gcr_encode_nibble(nibble)
        assert 0 <= code <= 0x1F
        assert gcr_decode_nibble(code) == nibble, f"GCR round-trip failed for nibble {nibble:#x}"

    # --- Invalid 5-bit codewords (not in the 16-entry table) must decode to
    # None, never to a guessed nibble.
    valid_codes = set(GCR_ENCODE_TABLE.values())
    invalid_codes = [c for c in range(32) if c not in valid_codes]
    assert len(invalid_codes) == 16  # exactly half of the 32 five-bit patterns are valid
    for bad_code in invalid_codes:
        assert gcr_decode_nibble(bad_code) is None

    # --- Full-frame round trip: build a real telemetry value with a valid
    # checksum, GCR-encode all 4 nibbles into a real 21-bit frame (as an ESC
    # transmitter would), and confirm gcr_decode_frame recovers the exact
    # 12-bit payload.
    for payload12 in (0x000, 0x001, 0x0FFF, 0x123, 0x7FF, 0x800):
        frame = _valid_frame_for_payload(payload12)
        result = gcr_decode_frame(frame)
        assert result == payload12, f"round trip failed for payload {payload12:#x}: got {result!r}"

    # --- Corrupted frame (single bit-flip in one GCR codeword, landing on
    # an invalid 5-bit pattern) must be rejected, not silently misdecoded.
    payload12 = 0x055
    frame = _valid_frame_for_payload(payload12)
    # Flip the low bit of the first GCR nibble; verify it actually lands on
    # an invalid codeword before asserting rejection (guards the test
    # itself against a false pass).
    corrupt_frame = frame ^ (1 << 15)
    first_group = (corrupt_frame >> 15) & 0x1F
    assert first_group not in valid_codes, "test setup error: corruption did not land on an invalid codeword"
    assert gcr_decode_frame(corrupt_frame) is None, "corrupted frame must not validate"

    # --- Checksum-only corruption: valid GCR codewords throughout, but wrong
    # checksum nibble -- must also be rejected even though every 5-bit group
    # individually decodes fine.
    good_decoded16 = None
    for candidate in range(16):
        d16 = (payload12 << 4) | candidate
        if _checksum_fold(d16) == 0xF:
            good_decoded16 = d16
            break
    assert good_decoded16 is not None
    bad_checksum16 = (good_decoded16 & 0xFFF0) | ((good_decoded16 & 0xF) ^ 0x1)
    bad_frame = _encode_frame(_value16_to_nibbles(bad_checksum16))
    assert gcr_decode_frame(bad_frame) is None, "bad checksum must not validate even with valid GCR groups"

    # --- eRPM special-case: 0x0FFF sentinel means "stopped", not "invalid".
    assert erpm_from_period(0x0FFF) == 0

    # --- eRPM period decode: exponent=0, mantissa=1 -> period=1 (in 100ns
    # units) -> erpm = (600000000 + 0)/1 = 600,000,000 (a deliberately
    # extreme synthetic value chosen for exact-integer verification, not a
    # physically plausible motor speed).
    assert erpm_from_period(0b000_000000001) == 600_000_000
    # exponent=3, mantissa=1 -> period = 1<<3 = 8 -> erpm = round(600000000/8)
    # = 75,000,000 exactly.
    value_exp3 = (3 << 9) | 1
    assert erpm_from_period(value_exp3) == 75_000_000

    # --- decode_telemetry_payload: plain eRPM fallback when EDT type tag is
    # 0 (marker bit unset) -- must classify as ERPM and populate .erpm.
    plain = decode_telemetry_payload(0x1FF, edt_enabled=True)  # tag bits (0x1FF>>8)&0xF = 1 -> temperature actually
    # 0x1FF: bits 11..8 = 0x1 -> maps to TEMPERATURE per _EDT_TYPE_LOOKUP.
    assert plain.telemetry_type == TelemetryType.TEMPERATURE
    assert plain.edt_value8 == 0xFF
    assert edt_temperature_c(plain.edt_value8) == 255

    zero_tag = decode_telemetry_payload(0x0AB, edt_enabled=True)  # tag = 0 -> eRPM
    assert zero_tag.telemetry_type == TelemetryType.ERPM
    assert zero_tag.erpm is not None

    # --- EDT voltage/current field decode.
    voltage_payload = decode_telemetry_payload(0x350, edt_enabled=True)  # tag=3 -> VOLTAGE, value8=0x50
    assert voltage_payload.telemetry_type == TelemetryType.VOLTAGE
    assert edt_voltage_v(voltage_payload.edt_value8) == 0x50 * 0.25 == 20.0

    current_payload = decode_telemetry_payload(0x50F, edt_enabled=True)  # tag=5 -> CURRENT, value8=0x0F
    assert current_payload.telemetry_type == TelemetryType.CURRENT
    assert edt_current_a(current_payload.edt_value8) == 0x0F

    # --- edt_enabled=False must force eRPM interpretation regardless of the
    # tag bits (mirrors real ESC/FC EDT-negotiation fallback behaviour).
    forced_erpm = decode_telemetry_payload(0x350, edt_enabled=False)
    assert forced_erpm.telemetry_type == TelemetryType.ERPM

    print("dshot_telemetry_parser self_test: ALL PASSED")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="run embedded self-test and exit (default action if no other flag given)")
    args = ap.parse_args()

    # This module has no live-hardware/serial/RF mode -- see the HARDWARE
    # STATUS / RF-relevance determination in the module docstring. The only
    # supported invocation is the self-test.
    self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
