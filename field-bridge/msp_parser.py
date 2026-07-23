#!/usr/bin/env python3
"""Real MSP (MultiWii Serial Protocol) v2 frame parser -- backlog C8.

STATUS AS OF 2026-07-23: TESTED PARSING INFRASTRUCTURE. NO INGEST BRIDGE.
NO MSP-CAPABLE SERIAL HARDWARE IN THIS PROJECT. See "HARDWARE STATUS" below
before assuming this is wired into the live detection pipeline -- it is not.

=============================================================================
WHAT THIS IS
=============================================================================
MSP is the serial protocol Betaflight/Cleanflight/iNAV flight controllers
speak to a GCS, OSD, or companion computer (goggles, telemetry radio, Lua
scripts on the TX, etc.). It runs over a plain UART, not RF -- there is no
"sniffing MSP off the air" the way there is for MAVLink-over-SiK-radio or
DroneID-over-OcuSync. To ever see a real MSP frame, this bridge would need
a physical wired serial tap into an FC's UART (or a USB-serial adapter on
an FC/OSD's MSP port).

This module implements the real MSP v2 ("MSPv2 native") frame format, one
byte at a time, as an explicit state machine -- the same architecture as
serialport-parser-msp-v2's MspParser.ts (host-side Node/TS reference,
MIT-licensed, ~/Desktop/Zettawise/PMO Suraj/tool/serialport-parser-msp-v2),
ported to Python rather than reimplemented from a vague description. The
frame format implemented here:

    '$' 'X' dir  flag(1)  function(2, LE)  size(2, LE)  payload(size)  crc8(1)

  - '$' 'X'      : MSPv2 native preamble (as opposed to legacy MSPv1's
                   '$' 'M', which this module does NOT implement -- see
                   "WHAT WAS NOT PORTED" below).
  - dir          : '<' (0x3C) = request/IN message (GCS/OSD -> FC),
                   '>' (0x3E) = response/OUT message (FC -> GCS/OSD),
                   '!' (0x21) = FC-reported error frame (no payload/CRC
                   after it -- MSP_ERROR_RECEIVED state below).
  - flag         : 1 byte, MSPv2 flag field (reserved, typically 0).
  - function     : 2 bytes little-endian, the MSP command/function code.
  - size         : 2 bytes little-endian, payload length in bytes.
  - payload      : `size` raw bytes.
  - crc8         : CRC-8/DVB-S2 (poly 0xD5, per the official MSPv2 spec:
                   https://github.com/betaflight/betaflight/wiki/MSP-V2-Native)
                   computed over flag+function+size+payload -- NOT over the
                   '$','X','<'/'>' preamble bytes. This matches
                   serialport-parser-msp-v2/src/MspParser.ts exactly: its
                   checksum only starts accumulating in MSP_HEADER_V2_NATIVE
                   (i.e. after the 3-byte preamble has already been consumed
                   in MSP_HEADER_START/MSP_HEADER_X).

=============================================================================
CRC-8/DVB-S2 -- verified bit-identical to the TypeScript reference
=============================================================================
serialport-parser-msp-v2/src/Msp.ts:

    export const crc8_dvb_s2 = (crc, num) => {
      crc = (crc ^ num) & 0xFF
      for (let i = 0; i < 8; i++)
        crc = ((crc & 0x80 & 0xFF) != 0) ? ((crc << 1) ^ 0xD5) & 0xFF : (crc << 1) & 0xFF
      return crc
    }

`crc8_dvb_s2()` below is a direct, byte-for-byte port of this (not a
from-scratch reimplementation of "CRC-8/DVB-S2" from a generic spec table,
to avoid any bit-order/reflection mismatch). This is the same polynomial
Betaflight's own MSP implementation uses (src/main/msp/msp_serial.c,
`crc8_dvb_s2` in src/main/common/crc.c).

=============================================================================
REAL TEST VECTORS -- taken verbatim from the reference repo's own test
suite, NOT fabricated
=============================================================================
serialport-parser-msp-v2/src/MspDecoder.test.ts contains hand-verified
byte sequences with correct, working CRCs (these round-trip through the
library's own MspEncoder.checksum() and are asserted against in
MspEncoder.test.ts too, so they are cross-validated within the reference
project, not just asserted once):

  Frame A (MSP_IDENT, empty payload):
    24 58 3e 00 64 00 00 00 8f
    = '$' 'X' '>' flag=0x00 fn=0x0064(100=MSP_IDENT) size=0x0000 crc=0x8f

  Frame B (fn=0x4242, 19-byte ASCII payload "Hello flying world"):
    24 58 3e a5 42 42 12 00
    48 65 6c 6c 6f 20 66 6c 79 69 6e 67 20 77 6f 72 6c 64
    82
    = '$' 'X' '>' flag=0xa5 fn=0x4242 size=0x0012(18) payload="Hello flying world" crc=0x82

  NOTE: frame B's declared size is 0x12=18 bytes, and "Hello flying world"
  is indeed 18 ASCII characters (H-e-l-l-o- -f-l-y-i-n-g- -w-o-r-l-d) --
  double-checked, not assumed.

  Negative vectors (MspDecoder.test.ts "Invalid message" cases -- must be
  REJECTED, not decoded):
    24 58 3c 00 64 00 00 00 8f   -- '<' direction after '$X': this
                                    decoder implementation only accepts
                                    '>' or '!' after '$X' (see MspParser.ts
                                    MSP_HEADER_X state); '<' falls through
                                    to MSP_IDLE, i.e. "not a frame at all"
                                    from this decoder's point of view.
    24 58 3e 00 64 00 00 00 80   -- same as Frame A but with the CRC byte
                                    corrupted (0x80 instead of 0x8f) --
                                    must fail checksum verification.

  This module's own test harness (`_run_self_tests()` at the bottom of
  this file, executed when run as `python3 msp_parser.py --selftest`)
  replays exactly these vectors and asserts against them. No test vector
  in this file was invented; all came from the cited .test.ts files.

=============================================================================
WHAT WAS NOT PORTED (explicitly out of scope, not an oversight)
=============================================================================
  - MSPv1 legacy framing ('$' 'M' dir len cmd payload crc, XOR checksum).
    serialport-parser-msp-v2 is v2-only; so is this module. If real MSPv1
    hardware is ever the actual field target, that is separate work.
  - MSPv2-over-MSPv1 jumbo-frame encapsulation (function 255 / MSP_V2_FRAME
    wrapping an MSPv2 frame inside an MSPv1 envelope). Not implemented
    here; not present in the TS reference either.
  - Encoding/request-side (MspEncoder.ts equivalent). This module only
    DECODES -- consistent with every other field-bridge script in this
    repo being RX-only (mavlink_sniffer.py, droneid_decode_bridge.py):
    there is no reason for a passive detection bridge to ever transmit
    MSP requests, and the standing project rule is RX-only where the
    real use case doesn't require TX.

=============================================================================
HARDWARE STATUS -- READ BEFORE ASSUMING THIS IS DEPLOYED
=============================================================================
MSP is a point-to-point UART protocol between an FC and whatever is wired
to its MSP-capable UART (a GCS, an OSD, a telemetry radio doing MSP
passthrough, DJI/HD-VTX goggles, etc.) -- it is NOT broadcast over RF like
MAVLink-over-SiK or DroneID-over-OcuSync, so there is no RF capture path
that could ever yield an MSP frame. The only way this project could ever
see a real MSP frame is a literal wired serial tap: a USB-to-UART adapter
(or a spare UART on a Pi/companion computer) physically connected to a
Betaflight/iNAV/Cleanflight FC's MSP TX/RX pins, OR to an OSD/goggle
module's MSP passthrough port.

No such hardware exists in this project as of 2026-07-23:
  - grep across the repo for flight-controller/Betaflight/iNAV/OSD
    hardware references turned up nothing (unlike the SiK/RFD900 radios
    documented for mavlink_sniffer.py/sik_mavlink_bridge.py, or the
    HackRF One documented throughout field-bridge/).
  - No serial device, baud rate, or FC model has ever been specified for
    an MSP link in this project's docs, .env conventions, or systemd
    units.

Consequently this pass delivers ONLY the parser (this file), tested
against the real vectors above. It deliberately does NOT deliver:
  - a msp_bridge.py ingest script posting to /api/detections/ingest, or
  - a cema-msp-bridge.service unit,
because building either would require inventing a serial device path,
baud rate, and confidence semantics with no real hardware or spec context
to ground them in -- exactly the kind of fabrication this project's
standing rule (real protocol logic only, no fabricated hardware claims)
prohibits. If/when real MSP-capable serial hardware (an FC, OSD, or
MSP-passthrough goggle module) is identified for this project, the bridge
script is a small addition on top of this parser: open the serial port
(pyserial, same pattern as mavlink_sniffer.py), feed bytes through
`MspFrameParser.feed()`, and post an ingest event of confidence_type
"protocol_verified" whenever `feed()` yields a CRC-valid MspFrame --
mirroring droneid_decode_bridge.py's "only ingest on CRC-verified decode"
discipline. That wiring is NOT written speculatively here.

Requires: no third-party dependencies (stdlib only).
"""
from __future__ import annotations

import argparse
import enum
import sys
from dataclasses import dataclass, field
from typing import List, Optional


# ---------------------------------------------------------------------------
# CRC-8/DVB-S2, ported bit-for-bit from serialport-parser-msp-v2/src/Msp.ts
# crc8_dvb_s2() (poly 0xD5, non-reflected, initial value carried in by the
# caller starting from 0 for a fresh frame). This is also the exact
# algorithm Betaflight's own firmware uses for MSPv2 checksums.
# ---------------------------------------------------------------------------
def crc8_dvb_s2(crc: int, byte: int) -> int:
    crc = (crc ^ byte) & 0xFF
    for _ in range(8):
        if crc & 0x80:
            crc = ((crc << 1) ^ 0xD5) & 0xFF
        else:
            crc = (crc << 1) & 0xFF
    return crc


class MspState(enum.Enum):
    IDLE = "IDLE"
    HEADER_START = "HEADER_START"      # saw '$'
    HEADER_X = "HEADER_X"              # saw '$X'
    HEADER_V2_NATIVE = "HEADER_V2"     # saw '$X>' (or '$X!' -> ERROR), reading flag+fn+size
    PAYLOAD_V2_NATIVE = "PAYLOAD_V2"   # reading `size` payload bytes
    CHECKSUM_V2_NATIVE = "CHECKSUM_V2"  # reading the trailing CRC byte
    ERROR_RECEIVED = "ERROR_RECEIVED"  # saw '$X!' -- FC-reported protocol error, no payload/CRC


@dataclass
class MspFrame:
    """A fully decoded, CRC-verified MSPv2 native frame."""
    flag: int
    function: int
    payload: bytes
    direction: str  # '>' (FC->host response) is the only direction this decoder accepts as a full frame


@dataclass
class _ParseState:
    state: MspState = MspState.IDLE
    direction: str = ""
    flag: int = 0
    function: int = 0
    length: int = 0
    header_buf: List[int] = field(default_factory=list)  # accumulates flag+fn_lo+fn_hi+len_lo+len_hi
    payload_buf: List[int] = field(default_factory=list)
    checksum: int = 0


class MspFrameParser:
    """Byte-at-a-time MSPv2 native frame decoder.

    Direct port of serialport-parser-msp-v2/src/MspParser.ts's
    parseNextCharCode() state machine (MSP_IDLE -> HEADER_START -> HEADER_X
    -> HEADER_V2_NATIVE -> PAYLOAD_V2_NATIVE -> CHECKSUM_V2_NATIVE), with
    one behavioral addition needed for a Python byte-stream API: `feed()`
    returns a completed MspFrame the instant the CRC byte is consumed and
    verified good, or None otherwise (including on CRC failure -- a bad
    checksum yields no frame, mirroring droneid_decode_bridge.py's
    "CRC fail -- not a genuine confirmed decode, do not report" discipline).
    Errors ('$X!' frames) are consumed and silently dropped back to IDLE,
    same as the TS reference's MSP_ERROR_RECEIVED state.

    Only the '>' (response/OUT, FC->host) direction is accepted as end-to-end
    decodable, matching the TS reference decoder (see MSP_HEADER_X state
    there: only '>' or '!' are accepted after '$X'; '<' falls back to IDLE).
    """

    def __init__(self) -> None:
        self._s = _ParseState()

    def reset(self) -> None:
        self._s = _ParseState()

    def feed(self, byte: int) -> Optional[MspFrame]:
        """Feed one byte (0-255). Returns a verified MspFrame, or None."""
        s = self._s
        st = s.state

        if st == MspState.IDLE:
            if byte == ord('$'):
                s.state = MspState.HEADER_START

        elif st == MspState.HEADER_START:
            s.header_buf = []
            s.payload_buf = []
            s.checksum = 0
            if byte == ord('X'):
                s.state = MspState.HEADER_X
            else:
                s.state = MspState.IDLE

        elif st == MspState.HEADER_X:
            if byte == ord('>'):
                s.direction = '>'
                s.state = MspState.HEADER_V2_NATIVE
            elif byte == ord('!'):
                s.direction = '!'
                s.state = MspState.ERROR_RECEIVED
            else:
                s.state = MspState.IDLE

        elif st == MspState.HEADER_V2_NATIVE:
            s.header_buf.append(byte & 0xFF)
            s.checksum = crc8_dvb_s2(s.checksum, byte)
            if len(s.header_buf) == 5:
                s.flag = s.header_buf[0]
                s.function = s.header_buf[1] + (s.header_buf[2] << 8)
                s.length = s.header_buf[3] + (s.header_buf[4] << 8)
                s.header_buf = []
                if s.length > 0:
                    s.state = MspState.PAYLOAD_V2_NATIVE
                else:
                    s.state = MspState.CHECKSUM_V2_NATIVE

        elif st == MspState.PAYLOAD_V2_NATIVE:
            s.payload_buf.append(byte & 0xFF)
            s.checksum = crc8_dvb_s2(s.checksum, byte)
            s.length -= 1
            if s.length == 0:
                s.state = MspState.CHECKSUM_V2_NATIVE

        elif st == MspState.CHECKSUM_V2_NATIVE:
            frame = None
            if s.checksum == (byte & 0xFF):
                frame = MspFrame(
                    flag=s.flag,
                    function=s.function,
                    payload=bytes(s.payload_buf),
                    direction=s.direction,
                )
            # Whether CRC matched or not, this frame attempt is over --
            # go back to IDLE either way (same as the TS reference: both
            # MSP_COMMAND_RECEIVED and the CRC-mismatch branch return to
            # IDLE on the next byte; we return to IDLE immediately since
            # this module yields the result synchronously).
            s.state = MspState.IDLE
            return frame

        elif st == MspState.ERROR_RECEIVED:
            s.state = MspState.IDLE

        return None

    def feed_bytes(self, data: bytes) -> List[MspFrame]:
        """Feed a chunk of bytes, return all frames completed within it."""
        out = []
        for b in data:
            f = self.feed(b)
            if f is not None:
                out.append(f)
        return out


# ---------------------------------------------------------------------------
# Self-test: real vectors from serialport-parser-msp-v2/src/MspDecoder.test.ts
# (cross-validated there against MspEncoder.test.ts's checksum() output).
# Nothing below is a fabricated/derived-and-hoped-for vector.
# ---------------------------------------------------------------------------
_VECTOR_IDENT = bytes([0x24, 0x58, 0x3e, 0x00, 0x64, 0x00, 0x00, 0x00, 0x8f])
_VECTOR_HELLO = bytes([
    0x24, 0x58, 0x3e, 0xa5, 0x42, 0x42, 0x12, 0x00,
    0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x20, 0x66, 0x6c, 0x79, 0x69, 0x6e, 0x67,
    0x20, 0x77, 0x6f, 0x72, 0x6c, 0x64,
    0x82,
])
_VECTOR_BAD_DIRECTION = bytes([0x24, 0x58, 0x3c, 0x00, 0x64, 0x00, 0x00, 0x00, 0x8f])
_VECTOR_BAD_CRC = bytes([0x24, 0x58, 0x3e, 0x00, 0x64, 0x00, 0x00, 0x00, 0x80])


def _run_self_tests() -> bool:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[msp_parser selftest] {status}: {name}")

    # Frame A: MSP_IDENT, empty payload, flag 0.
    p = MspFrameParser()
    frames = p.feed_bytes(_VECTOR_IDENT)
    check("Frame A (MSP_IDENT) decodes to exactly 1 frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("Frame A function == 100 (MSP_IDENT)", f.function == 100)
        check("Frame A flag == 0", f.flag == 0)
        check("Frame A payload == b''", f.payload == b"")
        check("Frame A direction == '>'", f.direction == '>')

    # Frame B: fn=0x4242, flag=0xa5, 18-byte "Hello flying world" payload.
    p.reset()
    frames = p.feed_bytes(_VECTOR_HELLO)
    check("Frame B decodes to exactly 1 frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("Frame B function == 0x4242", f.function == 0x4242)
        check("Frame B flag == 0xa5", f.flag == 0xa5)
        check("Frame B payload == b'Hello flying world'", f.payload == b"Hello flying world")

    # Negative vector: '<' direction after '$X' must NOT produce a frame.
    p.reset()
    frames = p.feed_bytes(_VECTOR_BAD_DIRECTION)
    check("'<' direction after $X yields 0 frames (rejected, not decoded)", len(frames) == 0)

    # Negative vector: corrupted CRC byte must NOT produce a frame.
    p.reset()
    frames = p.feed_bytes(_VECTOR_BAD_CRC)
    check("Corrupted CRC byte yields 0 frames (checksum verification works)", len(frames) == 0)

    # Direct CRC function check against the two known-good frames' trailing
    # CRC bytes, computed over flag+fn+size+payload (NOT the '$','X','>' preamble).
    crc = 0
    for b in _VECTOR_IDENT[3:-1]:
        crc = crc8_dvb_s2(crc, b)
    check("crc8_dvb_s2 over Frame A body == 0x8f", crc == 0x8f)

    crc = 0
    for b in _VECTOR_HELLO[3:-1]:
        crc = crc8_dvb_s2(crc, b)
    check("crc8_dvb_s2 over Frame B body == 0x82", crc == 0x82)

    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true",
                     help="Run the real-vector self-tests (from serialport-parser-msp-v2's "
                          "own MspDecoder.test.ts) and exit.")
    args = ap.parse_args()

    if args.selftest:
        ok = _run_self_tests()
        print("[msp_parser selftest] " + ("ALL PASS" if ok else "FAILURES ABOVE"))
        sys.exit(0 if ok else 1)

    print(__doc__)
    print("\nThis module is parsing infrastructure only -- see the "
          "'HARDWARE STATUS' section in its docstring above. There is no "
          "MSP-capable serial hardware in this project, so there is no "
          "bridge/ingest entry point here (unlike mavlink_sniffer.py or "
          "droneid_decode_bridge.py). Run with --selftest to verify the "
          "parser against real vectors from serialport-parser-msp-v2's own "
          "test suite.")


if __name__ == "__main__":
    main()
