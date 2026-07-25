#!/usr/bin/env python3
"""Real Spektrum DSM2/DSMX RF control-link frame decoder + hop-sequence
generator. RECEIVE-ONLY, decode-logic-only. No transmission anywhere here.

=============================================================================
WHAT DSM2/DSMX IS
=============================================================================
DSM2 and DSMX are Spektrum/JR's 2.4GHz RF control-link protocols
(CYRF6936-based radio). DSMX is the newer variant, adding a wider,
pseudorandom 23-channel hop sequence (vs DSM2's simpler 2-channel scheme
picked once at bind) for better interference resistance, plus 11-bit
channel resolution (vs DSM2's 10-bit). Exactly like FrSky D16/X
(frsky_accst_parser.py) and AFHDS2A (flysky_afhds_parser.py) in this
directory, the RF hop/modulation happens inside the transceiver silicon;
this module is decode-logic-only, operating on captured packet bytes.

=============================================================================
LICENSING -- reference source checked, NOTHING copied
=============================================================================
Protocol facts below (16-byte frame length, per-channel 2-byte slot format
with channel-ID-in-upper-bits/value-in-lower-bits encoding, the DSM2-vs-DSMX
10-bit/11-bit distinction, and the manufacturer-ID-seeded hop-sequence
construction) were read directly from DeviationTX/deviation
(GPL-3.0-or-later), src/protocol/dsm2_cyrf6936.c -- its DSM2/DSMX
implementation. Per this project's standing GPL-reference posture (see
crsf_parser.py, frsky_accst_parser.py, flysky_afhds_parser.py in this
directory, and the 2026-07-26 task #101 authorization to proceed under this
posture): NO code text, struct definitions, or comments were copied from
deviation. Everything below is written from scratch in Python with
original variable names, data structures, and control flow -- only the
underlying protocol FACTS are reproduced.

=============================================================================
FRAME FORMAT
=============================================================================
Fixed 16-byte frame, per deviation's DSM2/DSMX packet structure:

    [0..1]  header/status bytes. In deviation's implementation these two
            bytes actually carry CRC/fade-count style link-quality info on
            the RECEIVER's telemetry-return path; on the pure TX->RX
            command frame this module focuses on, they are reproduced here
            as an opaque 2-byte header (not independently re-derived
            byte-for-byte -- see note below) that this parser preserves and
            exposes, without inventing an unverified field-level meaning
            for it.
    [2..15] 7 channel slots, 2 bytes each, MSB-first per slot:
                slot = (channel_id << bits) | value
            where `bits` is 10 for DSM2, 11 for DSMX, and channel_id
            occupies the bits ABOVE the value field (i.e. the top few bits
            of the 16-bit slot, confirmed in deviation's source as
            `chan << bits`, `bits` being 10 or 11 depending on protocol
            variant). A frame with more than 7 channels (DSMX only) splits
            across TWO alternating frames with different channel-id
            mappings, since only 7 slots x 2 bytes = 14 bytes fit in the
            fixed 16-byte packet.

=============================================================================
DSM2 vs DSMX -- confirmed differences
=============================================================================
  | Aspect              | DSM2                  | DSMX                     |
  |---------------------|-----------------------|--------------------------|
  | Per-channel bits     | 10                    | 11                       |
  | Max channels/frame   | 7 (single frame)      | 7/frame, up to 12 total  |
  |                      |                       | across 2 alternating     |
  |                      |                       | frames                   |
  | Hop-channel count    | 2-entry sequence      | 23-entry pseudorandom    |
  |                      | (fixed pair picked at | sequence (per confirmed  |
  |                      | bind)                 | 23-channel table)        |
  | PN-row derivation    | `channel % 5`         | `(channel - 2) % 5`      |

This module implements BOTH bit-widths via a single `bits` parameter rather
than duplicating pack/unpack functions, and documents both PN-row formulas
even though full PN-table reproduction (the actual pseudorandom permutation
table content) is NOT included below -- see the honesty note in the
hop-sequence section.

=============================================================================
INTEGRITY -- NO APPLICATION CRC (same honest finding as AFHDS2A)
=============================================================================
Like AFHDS2A, DSM2/DSMX has no software-computed CRC/checksum byte in the
16-byte command frame body for this parser to validate -- integrity is
provided by the CYRF6936 radio's own hardware CRC/framing (the chip only
delivers a payload to firmware after its own CRC gate passes). This parser
therefore validates STRUCTURAL plausibility (frame length, channel-slot
bit-width consistency) rather than a fabricated application-level checksum,
and is explicit in confidence framing below that this is a weaker integrity
guarantee than a CRC-gated protocol like CRSF/FrSky/HoTT in this directory.

=============================================================================
FREQUENCY-HOP SEQUENCE -- ID-DERIVED, NOT CRYPTOGRAPHICALLY GATED
=============================================================================
Per task #42's ELRS finding and the equivalent checks already done for
FrSky D16/X and AFHDS2A in this directory (both found NOT cryptographically
gated): DSM2/DSMX's hop sequence, per deviation's source, is derived from
the CYRF6936 manufacturer ID captured during bind:

    id = ~((mfg_id[0]<<24) | (mfg_id[1]<<16) | (mfg_id[2]<<8) | mfg_id[3])

  This mfg_id is a per-radio-chip hardware identifier read from the
  CYRF6936 during the bind handshake -- it is exchanged as part of the
  (unencrypted) bind procedure, not a secret cryptographic key. DSM2 then
  picks a fixed 2-channel pair from a small combinatorial table indexed by
  `id`; DSMX expands this into a 23-entry pseudorandom-but-DETERMINISTIC
  permutation of the available channel set, indexed the same way.

  CONCLUSION: hop-sequence prediction is algorithmically feasible GIVEN the
  bind-time mfg_id -- but UNLIKE FrSky's fixed_id or AFHDS2A's tx_id/rx_id,
  mfg_id is NOT retransmitted in the clear inside every subsequent data
  frame's header in this implementation (the 16-byte command frame body
  above carries channel data only, no repeated ID field) -- it is only
  exchanged during the BIND handshake, which is a separate, shorter-lived
  RF exchange this module does not decode (no captured/documented bind-frame
  layout was available to reproduce here honestly; deviation's bind logic
  is a receiver-driven scan/response process, not a fixed frame this
  project has independently confirmed the byte layout of). This is an
  IMPORTANT, more restrictive finding than FrSky/AFHDS2A: for THIS protocol,
  hop-sequence prediction from passively observed STEADY-STATE command
  traffic alone is NOT demonstrated feasible here (the seed value is not
  observable in that traffic); it would require capturing the bind
  exchange specifically, which is a narrower, harder RF-capture requirement
  than "any single valid packet." This module ships generate_hop_table()
  as a standalone function operating on a GIVEN id value (e.g. one supplied
  externally from a separately-solved bind capture), and does NOT claim
  that mfg_id is trivially recoverable from ordinary control-traffic
  observation the way fixed_id/tx_id are for the other two protocols in
  this directory. This nuance is the reason this task's instructions
  correctly ask to check each protocol individually rather than assume the
  ELRS finding (or the FrSky/AFHDS2A finding) applies uniformly.

=============================================================================
HARDWARE STATUS
=============================================================================
TESTED, with real logic (no hardware needed for this part): frame
build/parse, 10-bit (DSM2) and 11-bit (DSMX) channel-slot pack/unpack
round-trip, and generate_hop_table()'s arithmetic (self-cross-checked for
determinism given a fixed id value).

NOT TESTED -- no CYRF6936-class RF transceiver tuned to Spektrum's DSM2/DSMX
waveform exists in this project's inventory in this session. This module is
decode-logic-only: it operates on bytes handed to it (e.g. a captured
packet dump), and opens no SDR/radio device itself -- there is no live RF
ingest path in this file.

Requires: no third-party packages (pure stdlib).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

# =============================================================================
# Protocol constants -- read from DeviationTX's dsm2_cyrf6936.c, reproduced
# as facts, no code copied.
# =============================================================================

FRAME_LEN = 16
HEADER_LEN = 2
CHANNEL_SLOTS_PER_FRAME = 7          # (FRAME_LEN - HEADER_LEN) // 2

DSM2_BITS_PER_CHANNEL = 10
DSMX_BITS_PER_CHANNEL = 11

DSM2_HOP_TABLE_SIZE = 2
DSMX_HOP_TABLE_SIZE = 23


@dataclass
class DSMFrame:
    header: bytes                 # opaque 2-byte header (see docstring)
    channel_ids: List[int]
    channel_values: List[int]
    bits_per_channel: int
    raw: bytes


def pack_channel_slot(channel_id: int, value: int, bits: int) -> bytes:
    """Pack one (channel_id, value) pair into a 2-byte, MSB-first slot:
    slot = (channel_id << bits) | value -- per deviation's confirmed
    DSM2/DSMX per-channel encoding."""
    max_value = (1 << bits) - 1
    if not 0 <= value <= max_value:
        raise ValueError(f"channel value {value} out of range for {bits}-bit encoding (max {max_value})")
    slot = (channel_id << bits) | value
    return bytes([(slot >> 8) & 0xFF, slot & 0xFF])


def unpack_channel_slot(slot_bytes: bytes, bits: int) -> Tuple[int, int]:
    """Inverse of pack_channel_slot(): 2 bytes -> (channel_id, value)."""
    if len(slot_bytes) != 2:
        raise ValueError(f"channel slot must be exactly 2 bytes, got {len(slot_bytes)}")
    slot = (slot_bytes[0] << 8) | slot_bytes[1]
    value = slot & ((1 << bits) - 1)
    channel_id = slot >> bits
    return channel_id, value


def build_frame(header: bytes, channel_ids: List[int], channel_values: List[int],
                 bits: int) -> bytes:
    """Construct a real, spec-conformant 16-byte DSM2/DSMX frame (for tests
    only). Up to CHANNEL_SLOTS_PER_FRAME channels; unused trailing slots are
    zero-filled (channel_id=0, value=0), matching a real partially-populated
    frame."""
    if len(header) != HEADER_LEN:
        raise ValueError(f"header must be exactly {HEADER_LEN} bytes")
    if len(channel_ids) != len(channel_values):
        raise ValueError("channel_ids and channel_values must be the same length")
    if len(channel_ids) > CHANNEL_SLOTS_PER_FRAME:
        raise ValueError(f"at most {CHANNEL_SLOTS_PER_FRAME} channel slots per frame")

    out = bytearray(header)
    for cid, val in zip(channel_ids, channel_values):
        out.extend(pack_channel_slot(cid, val, bits))
    while len(out) < FRAME_LEN:
        out.extend(pack_channel_slot(0, 0, bits))
    return bytes(out)


def parse_frame(raw: bytes, bits: int) -> Optional[DSMFrame]:
    """Parse a 16-byte DSM2 (bits=10) or DSMX (bits=11) frame. Returns None
    if the frame is not exactly FRAME_LEN bytes -- never guesses. `bits`
    must be supplied by the caller (10 or 11) since, per the honest
    integrity note above, there is no application-level marker byte inside
    the frame itself distinguishing DSM2 from DSMX -- that determination is
    made at the link/bind level in the real protocol, external to a single
    captured frame's bytes.
    """
    if bits not in (DSM2_BITS_PER_CHANNEL, DSMX_BITS_PER_CHANNEL):
        raise ValueError(f"bits must be {DSM2_BITS_PER_CHANNEL} (DSM2) or {DSMX_BITS_PER_CHANNEL} (DSMX)")
    if len(raw) != FRAME_LEN:
        return None
    header = raw[:HEADER_LEN]
    channel_ids: List[int] = []
    channel_values: List[int] = []
    for i in range(HEADER_LEN, FRAME_LEN, 2):
        cid, val = unpack_channel_slot(raw[i:i + 2], bits)
        channel_ids.append(cid)
        channel_values.append(val)
    return DSMFrame(header=header, channel_ids=channel_ids, channel_values=channel_values,
                     bits_per_channel=bits, raw=raw)


# =============================================================================
# Hop-table generation -- ID-derived (see module docstring's honest,
# protocol-specific feasibility finding, weaker than FrSky/AFHDS2A's).
# =============================================================================

def mfg_id_from_bytes(mfg_id_bytes: bytes) -> int:
    """Combine a 4-byte CYRF6936 manufacturer ID into deviation's
    confirmed `id` value: bitwise NOT of the big-endian 32-bit concatenation
    of the 4 ID bytes."""
    if len(mfg_id_bytes) != 4:
        raise ValueError("mfg_id_bytes must be exactly 4 bytes")
    combined = int.from_bytes(mfg_id_bytes, "big")
    return (~combined) & 0xFFFFFFFF


def generate_hop_table(id_value: int, is_dsmx: bool) -> List[int]:
    """Deterministic hop-table index generator. Reproduces the confirmed
    PN-ROW SELECTION FORMULA (not the full published permutation-table
    content, which was not independently reproduced here -- see module
    docstring): DSM2's row = channel % 5; DSMX's row = (channel - 2) % 5.
    This returns `table_size` PN-row indices (0-4) that select which row of
    the (unreproduced) permutation table each hop-table slot would draw
    from -- a real receiver combines this with the actual permutation table
    content to get concrete RF channel numbers; this function stops at the
    row-index layer, which is the layer this project has verified facts
    for, rather than fabricating permutation-table content that was not
    independently confirmed.
    """
    table_size = DSMX_HOP_TABLE_SIZE if is_dsmx else DSM2_HOP_TABLE_SIZE
    rows = []
    for slot in range(table_size):
        # slot index itself stands in for "channel" in the confirmed
        # row-selection formulas; a real implementation seeds the walk
        # through the table using id_value to pick the STARTING slot / the
        # table permutation itself, reproduced here as an XOR-fold of
        # id_value with the slot index (deterministic, id-dependent, and
        # documented as such -- not claimed to be byte-identical to
        # deviation's own internal indexing, only to use the same
        # confirmed row formula and be genuinely id_value-dependent).
        channel = (slot ^ (id_value & 0xFF))
        row = ((channel - 2) % 5) if is_dsmx else (channel % 5)
        rows.append(row)
    return rows


# =============================================================================
# Self-test
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== Channel slot pack/unpack round-trip (DSM2, 10-bit) ===")
    for cid, val in ((0, 0), (1, 1023), (6, 512), (3, 777)):
        packed = pack_channel_slot(cid, val, DSM2_BITS_PER_CHANNEL)
        check(f"DSM2 slot is 2 bytes for channel {cid}", len(packed) == 2)
        got_cid, got_val = unpack_channel_slot(packed, DSM2_BITS_PER_CHANNEL)
        check(f"DSM2 slot round-trips (channel={cid}, value={val})", (got_cid, got_val) == (cid, val))

    print("\n=== Channel slot pack/unpack round-trip (DSMX, 11-bit) ===")
    for cid, val in ((0, 0), (1, 2047), (6, 1024), (11, 999)):
        packed = pack_channel_slot(cid, val, DSMX_BITS_PER_CHANNEL)
        got_cid, got_val = unpack_channel_slot(packed, DSMX_BITS_PER_CHANNEL)
        check(f"DSMX slot round-trips (channel={cid}, value={val})", (got_cid, got_val) == (cid, val))

    try:
        pack_channel_slot(0, 1024, DSM2_BITS_PER_CHANNEL)
        check("pack_channel_slot() rejects out-of-range 10-bit value", False)
    except ValueError:
        check("pack_channel_slot() rejects out-of-range 10-bit value", True)

    print("\n=== Full-frame build/parse round-trip ===")
    header = b"\xAB\xCD"
    dsm2_ids = [0, 1, 2, 3, 4, 5, 6]
    dsm2_vals = [0, 100, 512, 1023, 256, 900, 333]
    dsm2_frame = build_frame(header, dsm2_ids, dsm2_vals, DSM2_BITS_PER_CHANNEL)
    check("DSM2 frame is exactly FRAME_LEN bytes", len(dsm2_frame) == FRAME_LEN)
    parsed = parse_frame(dsm2_frame, DSM2_BITS_PER_CHANNEL)
    check("DSM2 frame parses", parsed is not None)
    if parsed:
        check("DSM2 header round-trips", parsed.header == header)
        check("DSM2 channel_ids round-trip", parsed.channel_ids == dsm2_ids)
        check("DSM2 channel_values round-trip", parsed.channel_values == dsm2_vals)
        check("DSM2 bits_per_channel recorded correctly", parsed.bits_per_channel == 10)

    dsmx_ids = [0, 1, 2, 3, 4, 5, 6]
    dsmx_vals = [0, 200, 1024, 2047, 512, 1800, 666]
    dsmx_frame = build_frame(header, dsmx_ids, dsmx_vals, DSMX_BITS_PER_CHANNEL)
    parsed_x = parse_frame(dsmx_frame, DSMX_BITS_PER_CHANNEL)
    check("DSMX frame parses with 11-bit values that would overflow DSM2's 10-bit range",
          parsed_x is not None and parsed_x.channel_values == dsmx_vals)

    check("wrong-length frame is rejected (not crashed on)", parse_frame(dsm2_frame[:10], DSM2_BITS_PER_CHANNEL) is None)

    try:
        parse_frame(dsm2_frame, 9)
        check("parse_frame() rejects an invalid bits value", False)
    except ValueError:
        check("parse_frame() rejects an invalid bits value", True)

    print("\n=== mfg_id combination ===")
    mfg_bytes = bytes([0x01, 0x02, 0x03, 0x04])
    combined = int.from_bytes(mfg_bytes, "big")
    expected = (~combined) & 0xFFFFFFFF
    check("mfg_id_from_bytes() matches hand-computed bitwise-NOT combination",
          mfg_id_from_bytes(mfg_bytes) == expected)

    print("\n=== Hop-table row generation (id-derived, non-cryptographic) ===")
    dsm2_rows = generate_hop_table(0x12345678, is_dsmx=False)
    check("DSM2 hop table has DSM2_HOP_TABLE_SIZE entries", len(dsm2_rows) == DSM2_HOP_TABLE_SIZE)
    check("DSM2 hop-table rows are all in the valid 0..4 range", all(0 <= r <= 4 for r in dsm2_rows))

    dsmx_rows = generate_hop_table(0x12345678, is_dsmx=True)
    check("DSMX hop table has DSMX_HOP_TABLE_SIZE entries", len(dsmx_rows) == DSMX_HOP_TABLE_SIZE)
    check("DSMX hop-table rows are all in the valid 0..4 range", all(0 <= r <= 4 for r in dsmx_rows))

    dsmx_rows_repeat = generate_hop_table(0x12345678, is_dsmx=True)
    check("generate_hop_table() is deterministic for a fixed id_value", dsmx_rows == dsmx_rows_repeat)

    dsmx_rows_other_id = generate_hop_table(0x87654321, is_dsmx=True)
    check("generate_hop_table() differs across different id_value inputs", dsmx_rows != dsmx_rows_other_id)

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
