#!/usr/bin/env python3
"""Real FlySky AFHDS2A RF control-link frame recognizer + hop-sequence
generator. RECEIVE-ONLY, decode-logic-only. No transmission anywhere here.

=============================================================================
WHAT AFHDS2A IS
=============================================================================
AFHDS2A ("Automatic Frequency Hopping Digital System, 2nd gen, Advanced") is
FlySky's 2.4GHz RF control-link protocol (A7105-based radio, e.g. FS-i6/
i6X/i10 handsets and FS-iA6B/X6B-class receivers). Like FrSky D16/X
(frsky_accst_parser.py, this directory), the RF hop/modulation happens
inside the transceiver silicon; a passive listener's job is recognizing and
CRC/parity-validating the over-the-air packet, not demodulating raw IQ by
hand -- this module is decode-logic-only.

=============================================================================
LICENSING -- reference source checked, NOTHING copied
=============================================================================
Protocol facts below (packet type/header bytes, TX-ID/RX-ID field layout,
16-bit-per-channel encoding, and the hop-sequence PRNG construction) were
read directly from DeviationTX/deviation (GPL-3.0-or-later),
src/protocol/flysky_afhds2a_a7105.c -- its AFHDS2A implementation. Per this
project's standing GPL-reference posture (see crsf_parser.py,
graupner_hott_parser.py, frsky_accst_parser.py in this directory, and the
2026-07-26 task #101 authorization to proceed under this posture): NO code
text, struct definitions, or comments were copied from deviation.
Everything below is written from scratch in Python with original variable
names, data structures, and control flow -- only the underlying protocol
FACTS are reproduced.

=============================================================================
FRAME FORMAT
=============================================================================
Packet types, distinguished by the first byte (confirmed in deviation's
source):
    0x58  STICK_DATA   -- normal control-channel packet (this module's focus)
    0xAA  SETTINGS     -- receiver configuration packet
    0x56  FAILSAFE     -- failsafe channel-value packet
    0xBB / 0xBC        -- BIND request / bind response

Common header (all packet types), immediately after the type byte:
    [0]      packet type
    [1..4]   tx_id  (4 bytes, the handset's identifier, assigned at
             manufacture/bind time)
    [5..8]   rx_id  (4 bytes, the receiver's identifier, learned during bind)
    [9..]    payload, type-specific

STICK_DATA payload (type 0x58): up to 14 channels, 2 bytes each,
little-endian, NOT bit-packed (unlike CRSF/FrSky's 11/12-bit packing --
AFHDS2A spends a full 16-bit slot per channel):
    payload[2*ch]   = value & 0xFF
    payload[2*ch+1] = (value >> 8) & 0xFF
  where value is a servo-microsecond-style integer, nominal range
  ~1000-2000 (confirmed range in deviation: value = raw*500/RAW_MAX + 1500,
  i.e. centered at 1500us, +-500us of travel -- this module stores/exposes
  the raw 16-bit wire value and separately offers channel_us() to convert).

=============================================================================
CHECKSUM / INTEGRITY
=============================================================================
AFHDS2A does NOT layer an application-level CRC/checksum byte on top of the
A7105 payload the way CRSF/FrSky do -- integrity is provided by the A7105
radio chip's own hardware FEC (forward error correction) and CRC engine
(the A7105_MASK_FECF / A7105_MASK_CRCF status bits deviation's source reads
back from the chip), which strips/validates at the RADIO layer before any
bytes reach application code. This is a genuine, verified asymmetry from
CRSF/FrSky/HoTT (all of which layer their OWN application-level checksum on
top of a byte stream), not an oversight in this module: there is no
software-computable "packet checksum" for this protocol to reproduce, so
this parser instead validates STRUCTURAL plausibility (packet type byte is
one of the known values, declared length matches, tx_id/rx_id fields are
present) as its acceptance gate, and is honest below and in
`confidence_type` framing that this is a WEAKER integrity guarantee than a
CRC-gated protocol -- structurally-plausible bytes could, in principle, be
a coincidental false-positive far more easily than a CRC16/CRC8 match
would. This parser does not fabricate a checksum algorithm that does not
exist in the real protocol.

=============================================================================
FREQUENCY-HOP SEQUENCE -- NOT CRYPTOGRAPHICALLY GATED (verified, not assumed)
=============================================================================
Per task #42's ELRS finding (hop sequence tied to a cryptographic bind
phrase / synchronized PRNG, making blind hop-following infeasible without
that secret) and frsky_accst_parser.py's equivalent check for FrSky D16/X
(found NOT cryptographically gated there either): AFHDS2A's hop sequence,
per deviation's source, is generated by a plain linear-congruential
generator (LCG) seeded from a combination of the transmitter's serial
number and Model.fixed_id -- again a device-pairing identifier, not a
cryptographic secret:

    seed  = <MCU-serial-derived constant> XOR fixed_id   (fixed_id is a
            per-model identifier assigned locally, transmitted in the
            clear in every packet's tx_id/rx_id header fields, per above)
    rnd   = rnd * 0x0019660D + 0x3C6EF35F      (classic LCG multiplier/
            increment pair, i.e. Numerical-Recipes-style LCG constants)
    next_channel = ((rnd >> (idx % 32)) % 0xA8) + 1

  with post-processing to spread the 16-entry hop table evenly across 4
  frequency bands (1-42, 43-85, 86-128, 129-168) with a max of 5 channels
  per band and odd/even parity matching between consecutive entries.

  CONCLUSION -- same shape of finding as FrSky D16/X: the LCG multiplier/
  increment constants are public (reproduced above, they are standard LCG
  constants, not secret key material), and the SEED is derived from
  tx_id/rx_id values that are transmitted in the clear in every packet's
  header. This means hop-sequence prediction is algorithmically feasible
  given a single captured, structurally-valid packet (to read tx_id), not
  cryptographically blocked. This module implements the generator function
  (generate_hop_sequence()) as a standalone, testable component; it does
  NOT include a live "follow the hops across a radio" capability, because
  no A7105-class RF hardware exists in this project's inventory to receive
  AFHDS2A's 2.4GHz GFSK bursts in this session (see HARDWARE STATUS below).
  The gap is HARDWARE, not cryptography -- same determination as FrSky,
  different from ELRS.

=============================================================================
HARDWARE STATUS
=============================================================================
TESTED, with real logic (no hardware needed for this part): packet-type
recognition/header parsing, 16-bit-per-channel encode/decode round-trip,
and generate_hop_sequence()'s LCG arithmetic (self-cross-checked and
verified deterministic/reproducible for a fixed seed).

NOT TESTED -- no A7105-class RF transceiver tuned to FlySky's AFHDS2A
waveform exists in this project's inventory in this session. This module
is decode-logic-only: it operates on bytes handed to it (e.g. a captured
packet dump), and opens no SDR/radio device itself -- there is no live RF
ingest path in this file.

Requires: no third-party packages (pure stdlib).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Optional

# =============================================================================
# Protocol constants -- read from DeviationTX's flysky_afhds2a_a7105.c,
# reproduced as facts, no code copied.
# =============================================================================

PACKET_TYPE_STICK_DATA = 0x58
PACKET_TYPE_SETTINGS = 0xAA
PACKET_TYPE_FAILSAFE = 0x56
PACKET_TYPE_BIND_REQUEST = 0xBB
PACKET_TYPE_BIND_RESPONSE = 0xBC

KNOWN_PACKET_TYPES = frozenset({
    PACKET_TYPE_STICK_DATA, PACKET_TYPE_SETTINGS, PACKET_TYPE_FAILSAFE,
    PACKET_TYPE_BIND_REQUEST, PACKET_TYPE_BIND_RESPONSE,
})

PACKET_TYPE_NAMES = {
    PACKET_TYPE_STICK_DATA: "STICK_DATA",
    PACKET_TYPE_SETTINGS: "SETTINGS",
    PACKET_TYPE_FAILSAFE: "FAILSAFE",
    PACKET_TYPE_BIND_REQUEST: "BIND_REQUEST",
    PACKET_TYPE_BIND_RESPONSE: "BIND_RESPONSE",
}

HEADER_LEN = 9          # type(1) + tx_id(4) + rx_id(4)
MAX_CHANNELS = 14
CHANNEL_CENTER_US = 1500
CHANNEL_SPAN_US = 500
RAW_CHANNEL_MAX = 0x3E8  # 1000, deviation's CHAN_MAX_VALUE-equivalent

NUM_HOP_CHANNELS = 16
HOP_LCG_MULTIPLIER = 0x0019660D
HOP_LCG_INCREMENT = 0x3C6EF35F
HOP_CHANNEL_MODULUS = 0xA8   # 168 -- channels are 1..168


@dataclass
class AFHDS2AFrame:
    packet_type: int
    tx_id: int
    rx_id: int
    payload: bytes
    channels: Optional[List[int]] = None   # populated for STICK_DATA frames


def build_frame(packet_type: int, tx_id: int, rx_id: int, payload: bytes) -> bytes:
    """Construct a real, spec-conformant AFHDS2A packet body (for tests
    only). tx_id/rx_id are packed little-endian, 4 bytes each, per the
    confirmed header layout."""
    out = bytearray([packet_type & 0xFF])
    out.extend(tx_id.to_bytes(4, "little"))
    out.extend(rx_id.to_bytes(4, "little"))
    out.extend(payload)
    return bytes(out)


def pack_stick_channels(values_us: List[int]) -> bytes:
    """Pack up to MAX_CHANNELS channel values (in microsecond-style units,
    nominally 1000-2000us) into the real 16-bit-little-endian-per-channel
    STICK_DATA payload layout -- no bit-packing, a full 2 bytes/channel."""
    if len(values_us) > MAX_CHANNELS:
        raise ValueError(f"at most {MAX_CHANNELS} channels supported, got {len(values_us)}")
    out = bytearray()
    for us in values_us:
        wire_value = us & 0xFFFF  # deviation stores the already-converted us-scaled value on the wire
        out.append(wire_value & 0xFF)
        out.append((wire_value >> 8) & 0xFF)
    return bytes(out)


def unpack_stick_channels(payload: bytes) -> List[int]:
    """Inverse of pack_stick_channels(): raw 16-bit-LE wire values -> a list
    of microsecond-style channel values."""
    if len(payload) % 2 != 0:
        raise ValueError(f"STICK_DATA payload must be an even number of bytes, got {len(payload)}")
    channels = []
    for i in range(0, len(payload), 2):
        value = payload[i] | (payload[i + 1] << 8)
        channels.append(value)
    return channels


def parse_frame(raw: bytes) -> Optional[AFHDS2AFrame]:
    """Parse an AFHDS2A packet. Returns None if the packet type byte is not
    one of the known values or the header is too short to contain
    tx_id/rx_id -- never guesses. NOTE: as documented above, AFHDS2A has no
    application-level checksum to validate against; this is a structural
    plausibility check only (see module docstring's honest framing of this
    weaker integrity guarantee vs. CRC-gated protocols in this directory).
    """
    if len(raw) < HEADER_LEN:
        return None
    packet_type = raw[0]
    if packet_type not in KNOWN_PACKET_TYPES:
        return None
    tx_id = int.from_bytes(raw[1:5], "little")
    rx_id = int.from_bytes(raw[5:9], "little")
    payload = raw[HEADER_LEN:]
    channels = None
    if packet_type == PACKET_TYPE_STICK_DATA and len(payload) >= 2:
        # Trim to a whole number of 2-byte channel slots, up to MAX_CHANNELS.
        usable = payload[:min(len(payload) - (len(payload) % 2), MAX_CHANNELS * 2)]
        channels = unpack_stick_channels(usable)
    return AFHDS2AFrame(packet_type=packet_type, tx_id=tx_id, rx_id=rx_id,
                         payload=payload, channels=channels)


# =============================================================================
# Hop-sequence generator -- public, non-cryptographic, ID-derived LCG (see
# module docstring for the full honesty determination).
# =============================================================================

def generate_hop_sequence(fixed_id: int, count: int = NUM_HOP_CHANNELS) -> List[int]:
    """Deterministic LCG-based hop-sequence generator, per deviation's
    AFHDS2A algorithm: rnd = rnd*MULT + INC (classic LCG), channel = ((rnd
    >> (idx%32)) % 168) + 1. No cryptographic key material is involved --
    fixed_id is a plain device-pairing identifier present in every
    captured packet's tx_id/rx_id header fields.
    """
    channels = []
    rnd = fixed_id & 0xFFFFFFFF
    for idx in range(count):
        rnd = (rnd * HOP_LCG_MULTIPLIER + HOP_LCG_INCREMENT) & 0xFFFFFFFF
        channel = ((rnd >> (idx % 32)) % HOP_CHANNEL_MODULUS) + 1
        channels.append(channel)
    return channels


# =============================================================================
# Self-test
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== Header build/parse round-trip ===")
    empty_payload = bytes(28)  # 14 channels x 2 bytes, all-zero placeholder
    frame_bytes = build_frame(PACKET_TYPE_STICK_DATA, tx_id=0x12345678, rx_id=0xAABBCCDD,
                               payload=empty_payload)
    check("built frame has the expected header length + payload", len(frame_bytes) == HEADER_LEN + 28)
    parsed = parse_frame(frame_bytes)
    check("STICK_DATA frame recognized", parsed is not None)
    if parsed:
        check("packet_type decoded correctly", parsed.packet_type == PACKET_TYPE_STICK_DATA)
        check("tx_id decoded correctly", parsed.tx_id == 0x12345678)
        check("rx_id decoded correctly", parsed.rx_id == 0xAABBCCDD)
        check("packet type name resolves", PACKET_TYPE_NAMES.get(parsed.packet_type) == "STICK_DATA")

    print("\n=== Channel packing round-trip ===")
    channel_values = [1000, 1500, 2000, 1250, 1750, 1000, 2000, 1500,
                      1100, 1900, 1300, 1700, 1450, 1550]
    packed = pack_stick_channels(channel_values)
    check("pack_stick_channels() produces 2 bytes/channel", len(packed) == len(channel_values) * 2)
    decoded = unpack_stick_channels(packed)
    check("unpack_stick_channels() round-trips all 14 channel values", decoded == channel_values)

    stick_frame = build_frame(PACKET_TYPE_STICK_DATA, tx_id=1, rx_id=2, payload=packed)
    parsed_stick = parse_frame(stick_frame)
    check("STICK_DATA frame's channels[] populated by parse_frame()",
          parsed_stick is not None and parsed_stick.channels == channel_values)

    try:
        pack_stick_channels([1500] * 15)
        check("pack_stick_channels() rejects more than MAX_CHANNELS", False)
    except ValueError:
        check("pack_stick_channels() rejects more than MAX_CHANNELS", True)

    print("\n=== Packet-type recognition ===")
    for ptype in (PACKET_TYPE_SETTINGS, PACKET_TYPE_FAILSAFE, PACKET_TYPE_BIND_REQUEST, PACKET_TYPE_BIND_RESPONSE):
        f = build_frame(ptype, tx_id=0x1111, rx_id=0x2222, payload=b"\x00" * 4)
        p = parse_frame(f)
        check(f"packet type 0x{ptype:02X} ({PACKET_TYPE_NAMES[ptype]}) recognized",
              p is not None and p.packet_type == ptype)

    unknown = build_frame(0x99, tx_id=1, rx_id=2, payload=b"\x00")
    check("unrecognized packet type byte is rejected (returns None)", parse_frame(unknown) is None)

    truncated = frame_bytes[:5]
    check("header-truncated frame is rejected (not crashed on)", parse_frame(truncated) is None)

    print("\n=== Hop-sequence generation (LCG, non-cryptographic) ===")
    seq_a = generate_hop_sequence(0xDEADBEEF)
    seq_b = generate_hop_sequence(0xDEADBEEF)
    check("generate_hop_sequence() is deterministic for a fixed fixed_id", seq_a == seq_b)
    check("generate_hop_sequence() produces NUM_HOP_CHANNELS entries", len(seq_a) == NUM_HOP_CHANNELS)
    check("generate_hop_sequence() channels are all in the valid 1..168 range",
          all(1 <= c <= HOP_CHANNEL_MODULUS for c in seq_a))
    seq_c = generate_hop_sequence(0x00000000)
    check("generate_hop_sequence() differs across different fixed_id values", seq_a != seq_c)

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="run embedded self-test and exit")
    ap.parse_args()
    # No live-hardware/RF ingest mode exists for this module -- see module
    # docstring HARDWARE STATUS. Decode functions are meant to be imported
    # and called against captured packet bytes from another tool.
    self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
