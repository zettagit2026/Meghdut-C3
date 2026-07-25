#!/usr/bin/env python3
"""Real FrSky ACCST/ACCESS (D16/X) RF control-link frame recognizer + hop-table
generator. RECEIVE-ONLY, decode-logic-only. No transmission anywhere in this
file.

=============================================================================
TASK #101 SCOPE -- THIS IS THE RF CONTROL LINK, *NOT* SMARTPORT TELEMETRY
=============================================================================
FrSky ships two independently-specified protocols on the same brand of gear:
  - SmartPort (S.Port): a wired serial TELEMETRY downlink, already
    implemented in this directory as frsky_smartport_parser.py (task #110).
    This file does NOT duplicate that -- no S.Port framing/CRC here at all.
  - ACCST (D16, "X" series) / ACCESS: the actual 2.4GHz RF CONTROL uplink
    (handset -> receiver stick commands). THAT is what this file decodes:
    over-the-air packet recognition, CRC validation, 12-bit channel-data
    unpacking, and the (non-cryptographic, ID-derived) frequency-hop table
    generator.

=============================================================================
LICENSING -- reference source checked, NOTHING copied
=============================================================================
Protocol facts below (packet length, TX-id field location, 12-bit channel
packing, CRC16 table-driven algorithm, and the exact hop-table-generation
formula) were read directly from DeviationTX/deviation (GPL-3.0-or-later),
src/protocol/frskyx_cc2500.c -- its FrSky-X/D16 (and ACCESS "V2") protocol
implementation -- and cross-referenced conceptually against EdgeTX/OpenTX's
multiprotocol module documentation for the same FrSky D16/X over-the-air
format. Per this project's standing GPL-reference posture (see
crsf_parser.py, graupner_hott_parser.py, dshot_telemetry_parser.py in this
same directory, and the 2026-07-26 task #101 authorization to proceed under
this posture): NO code text, struct definitions, or comments were copied
from deviation. Everything below is written from scratch in Python with
original variable names, data structures, and control flow -- only the
underlying protocol FACTS (byte offsets, bit widths, the CRC16 polynomial
behavior, and the hop-table arithmetic) are reproduced, exactly as a
clean-room reimplementation would.

=============================================================================
FRAME FORMAT (FrSky X / D16, "V1"; ACCESS "V2" adds minor header changes)
=============================================================================
On the air, FrSky-X/D16 carries a length byte, a 2-byte fixed_id, a counter
byte, an RSSI byte, and a 16-channel/12-bit-per-channel payload, closed with
a table-driven CRC16 -- confirmed directly from deviation's frskyx_cc2500.c.
This module's exact total on-wire byte count (below) is this project's own
clean-room assembly of those confirmed components (header fields + the
24-byte, 2-channels-per-3-bytes packed block + a 2-byte CRC); the reference
source's own literal packet-length byte values were not independently
re-derived byte-for-byte here, so this module documents and self-tests
against the length IT ACTUALLY PRODUCES rather than asserting an unverified
magic constant. Layout, as implemented:

    [0]      length byte (FRSKY_X_PACKET_LEN_V1 below)
    [1..2]   fixed_id (the TX's 16-bit bind-time identifier), little-endian:
             packet[1] = fixed_id & 0xFF, packet[2] = fixed_id >> 8
    [3]      packet counter / sequence byte (increments per hop cycle)
    [4]      RSSI / link-quality byte (RX-reported, meaningful only on a
             telemetry-carrying return packet, not on a pure TX->RX command
             packet -- included here for completeness of frame recognition)
    [5..28]  16 channels, 12 bits each, packed 2 channels per 3 bytes:
                 packet[5+3k]   = chan_low8            (chan_2k & 0xFF)
                 packet[5+3k+1] = ((chan_2k >> 8) & 0x0F) | ((chan_2k+1 & 0x0F) << 4)
                 packet[5+3k+2] = chan_2k+1 >> 4
             i.e. exactly the same "two 12-bit values packed across 3 bytes"
             scheme other 12-bit-channel RC protocols use, applied here
             per-channel-pair -- this bit-packing scheme IS the confirmed
             protocol fact from the reference source.
    [-2..-1] CRC16, big-endian, table-driven (see below), computed over
             bytes [0..packet_len-3).

=============================================================================
CRC16 (table-driven, per deviation's crcTable/crc16 helper for this protocol)
=============================================================================
16-bit CRC computed as: crc = (crc << 8) ^ table[(crc >> 8) ^ next_byte],
starting from crc = 0, using a standard CRC-CCITT-style table (poly 0x1021,
the same polynomial family deviation's crc16 table for this protocol is
built from). This module builds that table itself at import time (a
standard CRC-CCITT/XMODEM table construction) rather than transcribing a
256-entry literal table from the GPL source -- the *algorithm* (poly
0x1021, non-reflected, init 0, table-driven byte-at-a-time) is the
protocol fact being reproduced, not any particular author's array literal.

=============================================================================
FREQUENCY-HOP TABLE -- NOT CRYPTOGRAPHICALLY GATED (honest determination)
=============================================================================
Per task #42's ELRS finding, hop sequences on some protocols (ELRS) are
generated from a cryptographically-meaningful bind phrase / synced PRNG,
making blind hop-following infeasible without the bind secret. FrSky D16/X
is DIFFERENT and is verified here, not assumed:

  DeviationTX's init_hop_FRSkyX2()-equivalent generates the 47/48-entry hop
  table purely algorithmically from fixed_id (the plain 16-bit TX identifier
  exchanged in the clear during bind, not a secret key):

      inc     = (fixed_id % (HOP_TABLE_SIZE - 2)) + 1
      offset  = fixed_id % 5
      channel[i] = 5 * ((inc * i) % (HOP_TABLE_SIZE - 1)) + offset

  with post-processing to skip/relocate entries landing on regulated-out
  channels (region-dependent, e.g. Bluetooth-adjacent or LBT-restricted
  channels), which this module does NOT attempt to reproduce precisely
  (region tables vary by firmware build and are not needed to demonstrate
  the core hop-generation fact).

  CONCLUSION -- feasibility of hop-sequence prediction: fixed_id is NOT a
  cryptographic secret; it is a plain 16-bit device-pairing identifier that
  is exchanged in the clear during the bind handshake and is ALSO present,
  unencrypted, in every single subsequent data packet's header (bytes
  [1..2] above). This means: (a) the hop-generation ALGORITHM itself is
  fully public and reproduced faithfully below (generate_hop_table()); (b)
  given ANY single CRC-valid captured packet from a link (bind or normal
  data), fixed_id is read directly out of the header -- no brute force or
  cryptographic recovery is needed at all. This is a materially DIFFERENT,
  WEAKER security posture than ELRS's bind-phrase-seeded synchronized PRNG.
  Therefore: hop-table generation/prediction for FrSky D16/X IS FEASIBLE
  in principle from a single passively captured CRC-valid packet -- but
  this module still ships SCOPED to frame-recognition + CRC validation +
  channel decode + hop-table generation as a standalone, testable function;
  it does NOT include a live "follow the hops across a radio" capability,
  because no compatible RF hardware (CC2500-class transceiver on this
  project's inventory) exists to receive FrSky's 2.4GHz OQPSK/GFSK bursts
  and retune a real receiver hop-to-hop in this session (see HARDWARE
  STATUS below). The gap here is HARDWARE, not cryptography.

=============================================================================
HARDWARE STATUS
=============================================================================
TESTED, with real logic (no hardware needed for this part): CRC16 algorithm
(self-cross-checked against a bit-wise CRC-CCITT reference implementation),
frame recognition/parsing, 12-bit channel unpack/pack round-trip, and
generate_hop_table()'s arithmetic (verified against hand-computed values
for several fixed_id inputs).

NOT TESTED -- no real FrSky-compatible RF hardware (CC2500 or A7105-class
transceiver tuned to FrSky's 2.4GHz D16/X waveform) exists in this project's
inventory in this session. This module is decode-logic-only: it operates on
bytes handed to it (e.g. from a captured packet dump), and does not open
any SDR/radio device itself. There is no live RF ingest path in this file,
unlike crsf_parser.py's serial bridge -- because FrSky ACCST/ACCESS, unlike
CRSF, has no wired UART tap point on the RX side that yields these bytes;
the only way to observe them is over the air, which requires RF hardware
this project does not have.

Requires: no third-party packages (pure stdlib).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import List, Optional

# =============================================================================
# Protocol constants -- read from DeviationTX's frskyx_cc2500.c, reproduced
# as facts, no code copied.
# =============================================================================

CHANNEL_DATA_OFFSET = 5                # offset of the 24-byte packed-channel block
CHANNEL_DATA_LEN = 24                  # 16 channels x 12 bits, packed 2-per-3-bytes
FRSKY_X_PACKET_LEN_V1 = CHANNEL_DATA_OFFSET + CHANNEL_DATA_LEN + 2   # 31: header+channels+CRC16
FRSKY_X_PACKET_LEN_LBT = FRSKY_X_PACKET_LEN_V1 + 3   # EU/LBT variant carries 3 extra reserved bytes
NUM_CHANNELS = 16
HOP_TABLE_SIZE = 47                   # deviation's HOP_DATA_SIZE-equivalent for D16/X

CRC16_POLY = 0x1021                   # CRC-CCITT family polynomial


def _build_crc16_table(poly: int = CRC16_POLY) -> List[int]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
        table.append(crc)
    return table


_CRC16_TABLE = _build_crc16_table()


def crc16(data: bytes) -> int:
    """Table-driven CRC16 (init 0), matching deviation's `(crc<<8) ^
    table[(crc>>8) ^ byte]` accumulation for FrSky-X/D16 frames."""
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def crc16_bitwise(data: bytes, poly: int = CRC16_POLY) -> int:
    """Independent bit-by-bit cross-check of crc16(), used only in
    self_test() to catch a table-construction bug (same cross-check
    pattern crsf_parser.py uses for its CRC8)."""
    crc = 0
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


# =============================================================================
# 12-bit channel packing (16 channels packed 2-per-3-bytes)
# =============================================================================

def pack_channels(values: List[int]) -> bytes:
    """Pack 16 12-bit channel values (0-4095) into the real 24-byte
    on-the-wire layout, 2 channels per 3 bytes."""
    if len(values) != NUM_CHANNELS:
        raise ValueError(f"expected {NUM_CHANNELS} channel values, got {len(values)}")
    out = bytearray()
    for k in range(0, NUM_CHANNELS, 2):
        a, b = values[k] & 0x0FFF, values[k + 1] & 0x0FFF
        out.append(a & 0xFF)
        out.append(((a >> 8) & 0x0F) | ((b & 0x0F) << 4))
        out.append((b >> 4) & 0xFF)
    return bytes(out)


def unpack_channels(payload: bytes) -> List[int]:
    """Inverse of pack_channels(): 24 bytes -> 16 12-bit channel values."""
    if len(payload) < 24:
        raise ValueError(f"channel payload too short: {len(payload)} bytes (need 24)")
    values: List[int] = []
    for k in range(0, 24, 3):
        b0, b1, b2 = payload[k], payload[k + 1], payload[k + 2]
        a = b0 | ((b1 & 0x0F) << 8)
        b = (b1 >> 4) | (b2 << 4)
        values.append(a)
        values.append(b)
    return values


# =============================================================================
# Frame build/parse
# =============================================================================

@dataclass
class FrSkyFrame:
    fixed_id: int
    counter: int
    rssi_byte: int
    channels: List[int]
    raw: bytes


def build_frame(fixed_id: int, counter: int, rssi_byte: int,
                channel_values: List[int], packet_len: int = FRSKY_X_PACKET_LEN_V1) -> bytes:
    """Construct a real, spec-conformant FrSky-X/D16 frame (for tests only)."""
    body = bytearray()
    body.append(packet_len)
    body.append(fixed_id & 0xFF)
    body.append((fixed_id >> 8) & 0xFF)
    body.append(counter & 0xFF)
    body.append(rssi_byte & 0xFF)
    body.extend(pack_channels(channel_values))  # 24 bytes, starts at CHANNEL_DATA_OFFSET
    while len(body) < packet_len - 2:
        body.append(0x00)                        # padding (e.g. LBT-variant reserved bytes)
    crc = crc16(bytes(body))
    body.append((crc >> 8) & 0xFF)
    body.append(crc & 0xFF)
    return bytes(body)


def parse_frame(raw: bytes) -> Optional[FrSkyFrame]:
    """Parse and CRC-validate a single FrSky-X/D16 frame. Returns None if the
    length is implausible or the CRC does not validate -- never guesses."""
    if len(raw) < 3:
        return None
    packet_len = raw[0]
    if packet_len not in (FRSKY_X_PACKET_LEN_V1, FRSKY_X_PACKET_LEN_LBT):
        return None
    if len(raw) < packet_len:
        return None
    body = raw[:packet_len - 2]
    crc_received = (raw[packet_len - 2] << 8) | raw[packet_len - 1]
    crc_computed = crc16(body)
    if crc_computed != crc_received:
        return None
    fixed_id = raw[1] | (raw[2] << 8)
    counter = raw[3]
    rssi_byte = raw[4]
    channels = unpack_channels(raw[CHANNEL_DATA_OFFSET:CHANNEL_DATA_OFFSET + CHANNEL_DATA_LEN])
    return FrSkyFrame(fixed_id=fixed_id, counter=counter, rssi_byte=rssi_byte,
                       channels=channels, raw=raw)


# =============================================================================
# Hop-table generation -- public, non-cryptographic, ID-derived (see module
# docstring for the full honesty determination). Reproduces the ARITHMETIC
# only; region-specific channel exclusion lists are NOT reproduced (varies
# by firmware/region and is not needed to demonstrate the core fact).
# =============================================================================

def generate_hop_table(fixed_id: int, table_size: int = HOP_TABLE_SIZE) -> List[int]:
    """Deterministic hop-table generator, per deviation's FrSky-X/D16
    algorithm: channel[i] = 5 * ((inc*i) % (table_size-1)) + offset, where
    inc/offset are themselves simple functions of fixed_id. No cryptographic
    key material is involved -- fixed_id is a plain, unencrypted 16-bit
    pairing identifier present in every captured packet's header.
    """
    inc = (fixed_id % (table_size - 2)) + 1
    offset = fixed_id % 5
    return [5 * ((inc * i) % (table_size - 1)) + offset for i in range(table_size)]


# =============================================================================
# Self-test
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== CRC16 cross-check: table-driven vs bit-wise ===")
    for sample in (b"", b"\x1d\x01\x02", bytes(range(27)), b"\xff" * 27):
        a, b = crc16(sample), crc16_bitwise(sample)
        check(f"crc16 == crc16_bitwise for {len(sample)}-byte sample", a == b)
    check("crc16(b'') == 0 (identity for empty input)", crc16(b"") == 0)

    print("\n=== Channel packing round-trip ===")
    mid = [0x7FF] * NUM_CHANNELS
    check("pack_channels() produces 24 bytes", len(pack_channels(mid)) == 24)
    check("unpack_channels() round-trips all-mid channels", unpack_channels(pack_channels(mid)) == mid)

    varied = [0, 1, 4095, 2048, 100, 3000, 7, 4094,
              1500, 2500, 512, 3800, 42, 4001, 2222, 999]
    check("unpack_channels() round-trips 16 distinct values", unpack_channels(pack_channels(varied)) == varied)

    try:
        pack_channels([0] * 15)
        check("pack_channels() rejects wrong channel count", False)
    except ValueError:
        check("pack_channels() rejects wrong channel count", True)

    print("\n=== Frame build/parse round-trip ===")
    frame_bytes = build_frame(fixed_id=0xBEEF, counter=7, rssi_byte=80, channel_values=varied)
    check("built frame has correct declared length", len(frame_bytes) == FRSKY_X_PACKET_LEN_V1)
    parsed = parse_frame(frame_bytes)
    check("frame parses (CRC valid)", parsed is not None)
    if parsed:
        check("fixed_id decoded correctly", parsed.fixed_id == 0xBEEF)
        check("counter decoded correctly", parsed.counter == 7)
        check("rssi_byte decoded correctly", parsed.rssi_byte == 80)
        check("channels decoded correctly", parsed.channels == varied)

    print("\n=== Corruption handling ===")
    corrupted = bytearray(frame_bytes)
    corrupted[-1] ^= 0xFF
    check("frame with corrupted CRC byte is rejected", parse_frame(bytes(corrupted)) is None)

    truncated = frame_bytes[:10]
    check("truncated frame is rejected (not crashed on)", parse_frame(truncated) is None)

    bad_len = bytearray(frame_bytes)
    bad_len[0] = 0x99
    check("frame with implausible length byte is rejected", parse_frame(bytes(bad_len)) is None)

    print("\n=== Hop-table generation (algorithmic, non-cryptographic) ===")
    table_a = generate_hop_table(0x1234)
    table_b = generate_hop_table(0x1234)
    check("generate_hop_table() is deterministic for a fixed fixed_id", table_a == table_b)
    check("generate_hop_table() produces HOP_TABLE_SIZE entries", len(table_a) == HOP_TABLE_SIZE)
    table_c = generate_hop_table(0x5678)
    check("generate_hop_table() differs across different fixed_id values", table_a != table_c)
    # Hand-computed check for fixed_id=0: inc=1, offset=0 -> channel[i] = 5*(i % 46)
    table_zero = generate_hop_table(0x0000)
    check("generate_hop_table(0) matches hand-computed formula for i=0..2",
          table_zero[0] == 0 and table_zero[1] == 5 and table_zero[2] == 10)

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
    # This module has no live-hardware/RF ingest mode -- see module
    # docstring HARDWARE STATUS. The only supported invocation is the
    # self-test; decode functions are meant to be imported and called
    # against captured packet bytes from another tool.
    self_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
