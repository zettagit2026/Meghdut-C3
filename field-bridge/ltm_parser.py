#!/usr/bin/env python3
"""Real LTM (Lightweight Telemetry) frame parser + RX-only serial bridge.

RECEIVE ONLY. No transmission happens anywhere in this script — see the
"HARDWARE STATUS" section below for why.

=============================================================================
WHAT LTM IS, AND WHY THIS IS A SERIAL PARSER, NOT AN SDR/RF SCRIPT
=============================================================================
LTM (Lightweight Telemetry, sometimes called "Light Telemetry") is a very
simple, low-bandwidth, ONE-WAY UART serial telemetry protocol, originally
from Ghettostation/MultiWii and now carried forward by iNav, Cleanflight,
ArduPilot (SERIALx_PROTOCOL = 25), and used by OpenHD as one of its
supported downlink telemetry formats. It typically runs at 2400 baud
(sometimes 1200 or higher on OpenHD-class links) between a flight
controller and a ground-side OSD/tracker/telemetry display. Exactly like
crsf_parser.py's CRSF and mavlink_sniffer.py's MAVLink, the RF hop happens
inside whatever radio carries the UART bytes (a SiK/RFD900 link, an OpenHD
digital link, a simple FSK telemetry radio, etc.) — by the time bytes reach
a UART they are already a plain, checksum-protected serial byte stream.
There is no meaningful "IQ capture of LTM"; the correct integration point is
a serial tap on the ground-side receiver's UART output.

=============================================================================
FRAME FORMAT (verified against THREE independent real sources)
=============================================================================
    ['$']['T'][frame-type][payload ...][checksum]

  - header (2 bytes): ASCII '$' 'T' (0x24 0x54) — fixed sync bytes.
  - frame-type (1 byte): ASCII letter identifying the frame: 'G' (GPS),
    'A' (Attitude), 'S' (Status/Sensors), 'O' (Origin/home, a.k.a. "OSD
    home"), 'N' (Navigation, iNav extension), 'X' (Extended Navigation,
    iNav extension).
  - payload: frame-type-specific fixed-size little-endian fields (sizes
    below).
  - checksum (1 byte): XOR of every payload byte only (NOT the '$', 'T',
    or frame-type byte) — i.e. XOR over frame[3 : total_len-1].

Per-frame-type payload sizes and total on-the-wire frame sizes (header +
type + payload + checksum):
    G (GPS):           14-byte payload, 18 bytes total
    A (Attitude):        6-byte payload, 10 bytes total
    S (Status):          7-byte payload, 11 bytes total
    O (Origin/home):   14-byte payload, 18 bytes total
    N (Navigation):      6-byte payload, 10 bytes total
    X (Extended Nav):    6-byte payload, 10 bytes total

This layout, the per-type sizes, and the checksum SCOPE (payload-only, type
byte excluded) were read directly from real, already-catalogued upstream
sources (not re-derived from memory / not guessed), cross-checked three
ways:
  1. KipK/Ghettostation (unspecified/permissive-leaning hobby license; the
     ORIGINAL LTM reference implementation), src/GhettoStation/
     LightTelemetry.cpp: the actual checksum loop
     `for (i = 3; i < LTPacket_size-1; i++) LTCrc ^= LTPacket[i];` — byte
     index 3 is the first PAYLOAD byte (0='$', 1='T', 2=type), which is the
     authoritative source for "checksum excludes the type byte" used below.
     Also the byte-exact G/A/S/O frame layouts (field order, sizes,
     endianness) come from this file's LTsend*() functions.
  2. DzikuVx/ltm_telemetry_reader (GPL-3.0; an Arduino LTM decoder built
     from the same lineage), ltm_telemetry_reader.ino: confirms the exact
     total on-the-wire frame lengths per type (G/O=18, A/N/X=10, S=11) via
     its receiver state machine's `frameLength` table, independently
     matching Ghettostation's sizes.
  3. mwptools' ltm-definition.md (stronnag/mwptools) and the current
     ArduPilot LTM telemetry docs (ardupilot.org/plane/docs/
     common-ltm-telemetry.html): both independently describe the same
     field semantics/units (lat/lon as int32 deg*1e7, altitude as
     int32/uint32 cm, vbat/current as uint16 mV/mA, etc.) and the same
     total frame sizes (G-Frame "18 bytes", A-Frame "10 bytes", S-Frame
     "11 bytes" per ArduPilot's own docs) — a strong independent
     cross-check that the sizes/layout above are correct and not guessed.

N-frame and X-frame (iNav extensions) are NOT implemented in Ghettostation
(the original reference) — their field layout below is taken ONLY from the
mwptools/ArduPilot documentation sources (source #3 above), not from a
reviewed C implementation. Per this project's standing "no synthetic data,
but no false modesty when a real spec exists" stance (same treatment as
crsf_parser.py's VIDEO_TRANSMITTER), N/X frames are recognized, CRC-gated,
and given a best-effort field decode sourced from two independently-written
docs that agree with each other, but this is flagged as "spec-derived, not
source-code-verified" everywhere it matters (self_test() included).

=============================================================================
LICENSE NOTE (per this project's open-source-sovereignty standing rule)
=============================================================================
- DzikuVx/ltm_telemetry_reader is GPL-3.0 — flagged per this project's
  standing precedent on copyleft references (same as AlfredoCRSF/
  ExpressLRS in crsf_parser.py), permitted here under this project's
  internal-defense licensing override.
- KipK/Ghettostation does not carry an explicit repo-level LICENSE file at
  the reviewed revision; treated conservatively as "all rights reserved
  unless stated" and NOT vendored — only the documented byte offsets/sizes
  and the checksum-scope FACT (an unpatentable, unoriginal arithmetic
  detail) were read from it, not its code.
- Regardless of upstream licensing, LTM itself is a trivial, widely
  published, one-way telemetry format (a 2-byte sync + 1-byte type +
  fixed-size little-endian fields + 1-byte XOR checksum). The
  implementation below is a clean-room reimplementation from the DOCUMENTED
  SPEC (field names, sizes, units, checksum scope — all facts, not
  expressive code), not a port or translation of any GPL source file's
  actual code text. This is the same "trivial protocol, low reimplementation
  risk regardless of upstream license" reasoning called out in this task's
  own briefing.

=============================================================================
HARDWARE STATUS — READ BEFORE TRUSTING ANY "LIVE" OUTPUT OF THIS SCRIPT
=============================================================================
TESTED, with real logic (no real hardware needed for this part):
  - XOR checksum: reproduced exactly per Ghettostation's documented scope
    (payload-only XOR), verified in self_test() against frames built from
    the real per-type field layouts above.
  - Frame parsing state machine (LTMParser.feed_bytes): finds the '$','T'
    sync pair, reads the frame-type letter, determines payload length from
    the type-to-length table, validates the XOR checksum, and rejects
    corrupt/truncated/resynced streams.
  - Payload decoders for G/A/S/O (byte-exact per Ghettostation's real
    LTsend*() functions) and N/X (spec-derived only, see caveat above),
    round-tripped against known field values in self_test().

NOT TESTED — no real LTM-capable serial hardware was available in this
session:
  - LTMSerialBridge (the RX-only pyserial listener below) has NEVER been
    run against a real LTM-emitting flight controller or OpenHD ground
    unit's UART. This project's existing serial hardware (SiK/RFD900
    radios, see mavlink_sniffer.py/sik_mavlink_bridge.py) speaks MAVLink,
    not LTM, and cannot be repurposed to produce real LTM traffic. No
    iNav/Cleanflight/ArduPilot flight controller configured for LTM output,
    nor an OpenHD ground station emitting LTM, was available to test
    against.
  - Per this project's standing rule (no synthetic/fallback data, ever):
    this script will NOT fabricate a serial connection or synthetic LTM
    bytes to "demo" a detection. If you run LTMSerialBridge against a
    /dev/ttyUSB* with no real LTM source attached, it will simply see
    garbage/no sync bytes and post nothing — that is the correct, honest
    behavior, exactly like crsf_parser.py's and mavlink_sniffer.py's
    identical treatment of their own untested serial bridges.
  - This module is therefore shipped as tested PARSING INFRASTRUCTURE, with
    a bridge class staged and ready to go the moment real LTM-capable
    hardware (e.g. an iNav FC's telemetry UART, or an OpenHD ground unit's
    LTM output, wired to a USB-serial adapter) is available. Wire it up,
    point --serial at that device, and the existing, tested parser takes
    over — no code changes needed.

=============================================================================
INGEST CONVENTION (matches crsf_parser.py's / droneid_decode_bridge.py's
CRC-gated pattern)
=============================================================================
A detection is posted to /api/detections/ingest ONLY when a frame's XOR
checksum genuinely validates against real bytes read off the wire —
confidence_type="protocol_verified". There is no RSSI/energy-heuristic path
in this script (LTM has no such signal available at the UART layer) and no
probabilistic "candidate" label — a checksum pass/fail is a binary,
protocol-level fact, not a guess.

Requires: pyserial, requests (see field-bridge/requirements.txt). No new
dependency is introduced by this file.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

# =============================================================================
# Protocol constants — taken from Ghettostation's LightTelemetry.cpp (byte
# layout / checksum scope) and cross-checked against DzikuVx/
# ltm_telemetry_reader (frame lengths) and mwptools/ArduPilot docs (field
# semantics/units). Nothing here is invented.
# =============================================================================

LTM_HEADER = b"$T"          # fixed 2-byte sync, ASCII '$' 'T' (0x24 0x54)
LTM_DEFAULT_BAUD = 2400      # per ArduPilot docs: "usually at 2400 bauds"

# frame-type letter -> payload length in bytes (i.e. bytes strictly between
# the type letter and the checksum byte). Total on-the-wire frame length is
# 2 (header) + 1 (type) + payload_len + 1 (checksum).
LTM_PAYLOAD_LEN: Dict[str, int] = {
    "G": 14,   # GPS -- byte-exact per Ghettostation LTsendGFrame()
    "A": 6,    # Attitude -- byte-exact per Ghettostation LTsendAFrame()
    "S": 7,    # Status/sensors -- byte-exact per Ghettostation LTsendSFrame()
    "O": 14,   # Origin/home -- byte-exact per Ghettostation LTsendOFrame()
    "N": 6,    # Navigation (iNav ext.) -- SPEC-DERIVED ONLY, see module docstring
    "X": 6,    # Extended Nav (iNav ext.) -- SPEC-DERIVED ONLY, see module docstring
}

# Frame types whose payload layout comes from an actual reviewed C
# implementation (Ghettostation) rather than documentation alone. Used only
# to keep the "spec-derived vs source-verified" distinction honest in
# self_test() output and docstrings -- does not change parsing/CRC logic,
# which is identical (and equally trustworthy) for every frame type, since
# the CRC is a structural fact independent of field semantics.
LTM_SOURCE_VERIFIED_TYPES = frozenset({"G", "A", "S", "O"})
LTM_SPEC_DERIVED_ONLY_TYPES = frozenset({"N", "X"})

FRAME_TYPE_NAMES: Dict[str, str] = {
    "G": "GPS",
    "A": "ATTITUDE",
    "S": "STATUS",
    "O": "ORIGIN",
    "N": "NAVIGATION",
    "X": "EXTENDED_NAV",
}

LTM_MIN_TOTAL_LEN = 2 + 1 + min(LTM_PAYLOAD_LEN.values()) + 1
LTM_MAX_TOTAL_LEN = 2 + 1 + max(LTM_PAYLOAD_LEN.values()) + 1


# =============================================================================
# XOR checksum — matches Ghettostation's exact scope: XOR of payload bytes
# only (frame[3:-1], i.e. everything AFTER '$','T',type and BEFORE the
# checksum byte itself). The type byte and header are explicitly excluded.
# =============================================================================

def ltm_checksum(payload: bytes) -> int:
    """XOR checksum over payload bytes only, per Ghettostation's
    `for (i = 3; i < LTPacket_size-1; i++) LTCrc ^= LTPacket[i];` loop
    (index 3 == first payload byte, since 0='$',1='T',2=type)."""
    crc = 0
    for b in payload:
        crc ^= b
    return crc & 0xFF


def build_frame(frame_type: str, payload: bytes) -> bytes:
    """Construct a real, spec-conformant LTM frame (for tests / a TX peer,
    not used by the RX-only bridge itself)."""
    if frame_type not in LTM_PAYLOAD_LEN:
        raise ValueError(f"unknown LTM frame type: {frame_type!r}")
    expected = LTM_PAYLOAD_LEN[frame_type]
    if len(payload) != expected:
        raise ValueError(f"{frame_type}-frame payload must be {expected} bytes, got {len(payload)}")
    crc = ltm_checksum(payload)
    return LTM_HEADER + frame_type.encode("ascii") + payload + bytes([crc])


# =============================================================================
# Frame parser: a byte-stream state machine, feed_bytes()-driven so it plugs
# straight into pyserial's read loop (or into self_test()'s canned bytes).
# =============================================================================

@dataclass
class LTMFrame:
    frame_type: str      # single ASCII letter: G/A/S/O/N/X
    payload: bytes
    raw: bytes = b""
    timestamp: Optional[float] = None


class LTMParser:
    """Feed raw serial bytes in one at a time (or in chunks via feed_bytes);
    yields LTMFrame objects for frames whose XOR checksum genuinely
    validates. Non-conformant / corrupt bytes are discarded by resyncing on
    the next '$','T' pair -- this never blocks and never raises on garbage
    input."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed_bytes(self, data: bytes, timestamp: Optional[float] = None) -> List[LTMFrame]:
        if timestamp is None:
            timestamp = time.time()
        self._buf.extend(data)
        frames: List[LTMFrame] = []
        while True:
            frame = self._try_extract_one()
            if frame is None:
                break
            frame.timestamp = timestamp
            frames.append(frame)
        return frames

    def _try_extract_one(self) -> Optional[LTMFrame]:
        # Looping internally (rather than returning None after advancing a
        # single byte) is required here: a coincidental '$','T' pair
        # followed by an unrecognized type byte, or a real-looking frame
        # whose checksum fails, must not stop the scan for THIS feed_bytes()
        # call -- the caller's outer loop treats any None return as "wait
        # for more bytes" and stops trying, so all resync progress has to
        # happen inside a single call to actually reach a real frame later
        # in an already-buffered chunk.
        buf = self._buf
        while True:
            # Resync: drop bytes until we see the '$','T' sync pair.
            while len(buf) >= 2 and not (buf[0] == LTM_HEADER[0] and buf[1] == LTM_HEADER[1]):
                del buf[0]
            if len(buf) < 3:
                return None  # need header + at least the type byte
            frame_type = chr(buf[2])
            if frame_type not in LTM_PAYLOAD_LEN:
                # Not a recognized type letter right after a '$','T' pair --
                # this is very likely a coincidental '$','T' inside unrelated
                # bytes, not a real LTM frame. Drop just the leading '$' and
                # keep scanning for the next candidate pair.
                del buf[0]
                continue
            payload_len = LTM_PAYLOAD_LEN[frame_type]
            total_len = 2 + 1 + payload_len + 1  # header + type + payload + checksum
            if len(buf) < total_len:
                return None  # wait for more bytes
            raw = bytes(buf[:total_len])
            payload = raw[3:3 + payload_len]
            crc_received = raw[3 + payload_len]
            crc_computed = ltm_checksum(payload)
            del buf[:total_len]
            if crc_computed != crc_received:
                # Genuinely corrupt/misaligned frame -- not ingested, no
                # fallback. Keep scanning the rest of this chunk for a real
                # frame rather than stopping here.
                continue
            return LTMFrame(frame_type=frame_type, payload=payload, raw=raw)


# =============================================================================
# Payload decoders — G/A/S/O byte-exact per Ghettostation's LTsend*()
# functions; N/X spec-derived only from mwptools/ArduPilot docs (see module
# docstring). All little-endian per the cross-checked sources.
# =============================================================================

def parse_gps(payload: bytes) -> dict:
    """G-frame (GPS), 14 bytes: lat(i32) lon(i32) groundspeed(u8) alt(i32) sats(u8).
    sats byte: bits 0-1 = fix type (0=none,1=2D,2=3D), bits 2-7 = sat count."""
    if len(payload) != 14:
        raise ValueError(f"G-frame payload must be 14 bytes, got {len(payload)}")
    lat, lon, gspeed, alt, sats_byte = struct.unpack("<iiBiB", payload)
    return {
        "lat_deg": lat / 1e7,
        "lon_deg": lon / 1e7,
        "ground_speed_ms": gspeed,
        "altitude_cm": alt,
        "fix_type": sats_byte & 0x03,
        "satellite_count": sats_byte >> 2,
    }


def parse_attitude(payload: bytes) -> dict:
    """A-frame (Attitude), 6 bytes: pitch(i16) roll(i16) heading(i16), degrees."""
    if len(payload) != 6:
        raise ValueError(f"A-frame payload must be 6 bytes, got {len(payload)}")
    pitch, roll, heading = struct.unpack("<hhh", payload)
    return {"pitch_deg": pitch, "roll_deg": roll, "heading_deg": heading}


def parse_status(payload: bytes) -> dict:
    """S-frame (Status), 7 bytes: vbat(u16,mV) current(u16,mA) rssi(u8) airspeed(u8,m/s) status(u8).
    status byte: bit0=armed, bit1=failsafe, bits2-6=flight mode code."""
    if len(payload) != 7:
        raise ValueError(f"S-frame payload must be 7 bytes, got {len(payload)}")
    vbat, current, rssi, airspeed, status = struct.unpack("<HHBBB", payload)
    return {
        "vbat_mv": vbat,
        "current_ma": current,
        "rssi": rssi,
        "airspeed_ms": airspeed,
        "armed": bool(status & 0x01),
        "failsafe": bool(status & 0x02),
        "flight_mode_code": (status >> 2) & 0x1F,
    }


def parse_origin(payload: bytes) -> dict:
    """O-frame (Origin/home), 14 bytes: lat(i32) lon(i32) alt(i32,cm) osd_on(u8) fix(u8).
    Byte-exact field order/sizes per Ghettostation LTsendOFrame(); the final
    two flag bytes' precise semantics (OSD-enabled vs. home-fix-status vs.
    home-bearing) vary slightly across secondary docs -- reported here as
    raw uint8 fields rather than over-interpreted, per this project's "don't
    guess field semantics past what the byte-exact source actually shows"
    stance (same treatment as VIDEO_TRANSMITTER in crsf_parser.py)."""
    if len(payload) != 14:
        raise ValueError(f"O-frame payload must be 14 bytes, got {len(payload)}")
    lat, lon, alt, flag_a, flag_b = struct.unpack("<iiiBB", payload)
    return {
        "home_lat_deg": lat / 1e7,
        "home_lon_deg": lon / 1e7,
        "home_altitude_cm": alt,
        "flag_byte_a_raw": flag_a,   # OSD-on flag per some docs
        "flag_byte_b_raw": flag_b,   # home-fix-status / bearing per some docs
    }


def parse_navigation(payload: bytes) -> dict:
    """N-frame (Navigation, iNav extension), 6 bytes, ALL FIELDS SPEC-DERIVED
    ONLY (mwptools/ArduPilot docs) -- not verified against a reviewed C
    implementation, since Ghettostation (the original reference) does not
    implement this iNav-only extension. See module docstring caveat."""
    if len(payload) != 6:
        raise ValueError(f"N-frame payload must be 6 bytes, got {len(payload)}")
    gps_mode, nav_mode, nav_action, wp_number, nav_error, flags = struct.unpack("<BBBBBB", payload)
    return {
        "gps_mode": gps_mode,
        "nav_mode": nav_mode,
        "nav_action": nav_action,
        "waypoint_number": wp_number,
        "nav_error": nav_error,
        "flags": flags,
    }


def parse_extended_nav(payload: bytes) -> dict:
    """X-frame (Extended Navigation, iNav extension), 6 bytes: hdop(u16) +
    4 reserved/unused bytes. SPEC-DERIVED ONLY -- see parse_navigation()'s
    caveat, identical reasoning applies here."""
    if len(payload) != 6:
        raise ValueError(f"X-frame payload must be 6 bytes, got {len(payload)}")
    hdop, = struct.unpack("<H", payload[:2])
    return {"hdop": hdop, "reserved": payload[2:6].hex()}


_PAYLOAD_DECODERS = {
    "G": parse_gps,
    "A": parse_attitude,
    "S": parse_status,
    "O": parse_origin,
    "N": parse_navigation,
    "X": parse_extended_nav,
}


def decode_frame(frame: LTMFrame) -> dict:
    """Dispatch a validated LTMFrame to its payload decoder."""
    decoder = _PAYLOAD_DECODERS.get(frame.frame_type)
    if decoder is None:
        raise ValueError(f"no decoder registered for LTM frame type {frame.frame_type!r}")
    return decoder(frame.payload)


# =============================================================================
# Self-test: exercises the checksum and parser against frames built directly
# from the real (Ghettostation-sourced, for G/A/S/O) field layouts. This is
# what is ACTUALLY TESTED in this session (see module docstring HARDWARE
# STATUS section) -- no live LTM hardware, real spec-conformant bytes only.
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== Frame size table cross-check (Ghettostation vs ltm_telemetry_reader) ===")
    # Total on-the-wire lengths per DzikuVx/ltm_telemetry_reader's receiver
    # state machine: G/O=18, A/N/X=10, S=11.
    expected_totals = {"G": 18, "A": 10, "S": 11, "O": 18, "N": 10, "X": 10}
    for ft, expected_total in expected_totals.items():
        actual_total = 2 + 1 + LTM_PAYLOAD_LEN[ft] + 1
        check(f"{ft}-frame total length == {expected_total} bytes (header+type+payload+checksum)",
              actual_total == expected_total)

    print("\n=== XOR checksum sanity ===")
    check("ltm_checksum(b'') == 0 (identity for empty input)", ltm_checksum(b"") == 0)
    check("ltm_checksum(bytes([0xAA, 0xAA])) == 0 (self-cancelling XOR)",
          ltm_checksum(bytes([0xAA, 0xAA])) == 0)
    check("ltm_checksum(bytes([0x01, 0x02, 0x04])) == 0x07 (simple XOR)",
          ltm_checksum(bytes([0x01, 0x02, 0x04])) == 0x07)

    print("\n=== Frame round-trip: build_frame() + LTMParser, real field layouts ===")

    # G-frame: lat=28.6139389 (New Delhi-ish), lon=77.2090212, gspeed=12m/s,
    # alt=15000cm (150m), fix=3D(2) + 11 sats -> sats_byte = (11<<2)|2 = 46.
    lat_raw = int(28.6139389 * 1e7)
    lon_raw = int(77.2090212 * 1e7)
    g_payload = struct.pack("<iiBiB", lat_raw, lon_raw, 12, 15000, (11 << 2) | 2)
    g_frame_bytes = build_frame("G", g_payload)
    check("G-frame build_frame() produces 18 total bytes", len(g_frame_bytes) == 18)
    parser = LTMParser()
    frames = parser.feed_bytes(g_frame_bytes)
    check("G-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        check("G-frame type decoded correctly", frames[0].frame_type == "G")
        gps = parse_gps(frames[0].payload)
        check("G-frame lat round-trips", abs(gps["lat_deg"] - 28.6139389) < 1e-6)
        check("G-frame lon round-trips", abs(gps["lon_deg"] - 77.2090212) < 1e-6)
        check("G-frame ground_speed_ms round-trips (12)", gps["ground_speed_ms"] == 12)
        check("G-frame altitude_cm round-trips (15000)", gps["altitude_cm"] == 15000)
        check("G-frame fix_type decoded (3D=2)", gps["fix_type"] == 2)
        check("G-frame satellite_count decoded (11)", gps["satellite_count"] == 11)

    # A-frame: pitch=-15deg, roll=30deg, heading=270deg.
    a_payload = struct.pack("<hhh", -15, 30, 270)
    a_frame_bytes = build_frame("A", a_payload)
    check("A-frame build_frame() produces 10 total bytes", len(a_frame_bytes) == 10)
    frames = LTMParser().feed_bytes(a_frame_bytes)
    check("A-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        att = parse_attitude(frames[0].payload)
        check("A-frame pitch_deg decoded (-15)", att["pitch_deg"] == -15)
        check("A-frame roll_deg decoded (30)", att["roll_deg"] == 30)
        check("A-frame heading_deg decoded (270)", att["heading_deg"] == 270)

    # S-frame: vbat=1260mV*... wait vbat is per-cell-summed mV typically;
    # use 11100mV (3S at 3.7V), current=850mA, rssi=90, airspeed=8m/s,
    # armed=1, failsafe=0, flight_mode_code=5.
    status_byte = 0x01 | (5 << 2)  # armed=1, failsafe=0, mode=5
    s_payload = struct.pack("<HHBBB", 11100, 850, 90, 8, status_byte)
    s_frame_bytes = build_frame("S", s_payload)
    check("S-frame build_frame() produces 11 total bytes", len(s_frame_bytes) == 11)
    frames = LTMParser().feed_bytes(s_frame_bytes)
    check("S-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        stat = parse_status(frames[0].payload)
        check("S-frame vbat_mv decoded (11100)", stat["vbat_mv"] == 11100)
        check("S-frame current_ma decoded (850)", stat["current_ma"] == 850)
        check("S-frame rssi decoded (90)", stat["rssi"] == 90)
        check("S-frame airspeed_ms decoded (8)", stat["airspeed_ms"] == 8)
        check("S-frame armed decoded (True)", stat["armed"] is True)
        check("S-frame failsafe decoded (False)", stat["failsafe"] is False)
        check("S-frame flight_mode_code decoded (5)", stat["flight_mode_code"] == 5)

    # O-frame: home lat/lon/alt.
    o_lat_raw = int(28.6100000 * 1e7)
    o_lon_raw = int(77.2000000 * 1e7)
    o_payload = struct.pack("<iiiBB", o_lat_raw, o_lon_raw, 500, 1, 2)
    o_frame_bytes = build_frame("O", o_payload)
    check("O-frame build_frame() produces 18 total bytes", len(o_frame_bytes) == 18)
    frames = LTMParser().feed_bytes(o_frame_bytes)
    check("O-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        origin = parse_origin(frames[0].payload)
        check("O-frame home_lat_deg round-trips", abs(origin["home_lat_deg"] - 28.61) < 1e-6)
        check("O-frame home_lon_deg round-trips", abs(origin["home_lon_deg"] - 77.20) < 1e-6)
        check("O-frame home_altitude_cm round-trips (500)", origin["home_altitude_cm"] == 500)

    # N-frame (spec-derived only): gps_mode=1, nav_mode=2, nav_action=3, wp=4, err=0, flags=9.
    n_payload = struct.pack("<BBBBBB", 1, 2, 3, 4, 0, 9)
    n_frame_bytes = build_frame("N", n_payload)
    check("N-frame build_frame() produces 10 total bytes", len(n_frame_bytes) == 10)
    frames = LTMParser().feed_bytes(n_frame_bytes)
    check("N-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        nav = parse_navigation(frames[0].payload)
        check("N-frame waypoint_number decoded (4)", nav["waypoint_number"] == 4)
        check("N-frame flags decoded (9)", nav["flags"] == 9)

    # X-frame (spec-derived only): hdop=120 (1.2 in typical *100 hdop units).
    x_payload = struct.pack("<H", 120) + bytes(4)
    x_frame_bytes = build_frame("X", x_payload)
    check("X-frame build_frame() produces 10 total bytes", len(x_frame_bytes) == 10)
    frames = LTMParser().feed_bytes(x_frame_bytes)
    check("X-frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        xnav = parse_extended_nav(frames[0].payload)
        check("X-frame hdop decoded (120)", xnav["hdop"] == 120)

    print("\n=== Corruption / resync handling ===")
    corrupted = bytearray(g_frame_bytes)
    corrupted[-1] ^= 0xFF  # flip every bit of the checksum byte
    frames = LTMParser().feed_bytes(bytes(corrupted))
    check("Frame with corrupted checksum is REJECTED (not ingested)", len(frames) == 0)

    corrupted2 = bytearray(a_frame_bytes)
    corrupted2[3] ^= 0x01  # flip a payload bit; checksum no longer matches
    frames = LTMParser().feed_bytes(bytes(corrupted2))
    check("Frame with corrupted payload byte is REJECTED (not ingested)", len(frames) == 0)

    garbage_then_real = b"\x00\x01\x02\xFF\xFF" + s_frame_bytes
    frames = LTMParser().feed_bytes(garbage_then_real)
    check("Parser resyncs past leading garbage bytes to find the real frame", len(frames) == 1)

    # A coincidental '$','T' pair followed by a non-type-letter byte must be
    # skipped without ever blocking (regression test for the resync loop's
    # "not a recognized type letter" branch).
    fake_header_then_real = b"$T\x00\x00" + g_frame_bytes
    frames = LTMParser().feed_bytes(fake_header_then_real)
    check("Parser skips a coincidental '$T' + unrecognized type byte and "
          "still finds the real frame after it", len(frames) == 1)

    # Byte-at-a-time streaming (as pyserial delivers in practice) must behave
    # identically to feed_bytes() on the whole buffer at once.
    parser = LTMParser()
    streamed: List[LTMFrame] = []
    for b in g_frame_bytes:
        streamed.extend(parser.feed_bytes(bytes([b])))
    check("Byte-at-a-time feed matches whole-buffer feed_bytes()",
          len(streamed) == 1 and streamed[0].raw == g_frame_bytes)

    # Two back-to-back frames in one chunk (typical pyserial read() batching).
    two_frames = a_frame_bytes + s_frame_bytes
    frames = LTMParser().feed_bytes(two_frames)
    check("Two back-to-back frames in one chunk both parse", len(frames) == 2)
    if len(frames) == 2:
        check("First of two back-to-back frames is A", frames[0].frame_type == "A")
        check("Second of two back-to-back frames is S", frames[1].frame_type == "S")

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


# =============================================================================
# RX-only serial bridge -- STAGED, UNTESTED against real hardware (see
# module docstring HARDWARE STATUS). Follows crsf_parser.py's / mavlink_
# sniffer.py's exact conventions: CLI args / env vars for console URL +
# creds, login(), a read-only loop, checksum-gated ingest with
# confidence_type="protocol_verified", no synthetic fallback of any kind.
# =============================================================================

def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                       json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


class LTMSerialBridge:
    """RX-only LTM serial listener. Opens a real serial device, feeds every
    byte read to LTMParser, and posts a detection only for frames whose XOR
    checksum genuinely validates. Never writes to the serial port."""

    def __init__(self, console_url: str, headers: dict, serial_device: str,
                 baud: int = LTM_DEFAULT_BAUD, repost_interval_s: float = 10.0) -> None:
        self.console_url = console_url
        self.headers = headers
        self.serial_device = serial_device
        self.baud = baud
        self.repost_interval_s = repost_interval_s
        self.parser = LTMParser()
        self._last_posted = 0.0

    def _open_serial(self):
        try:
            import serial  # pyserial
        except ImportError:
            print("ERROR: pyserial not installed. pip install pyserial (see "
                  "field-bridge/requirements.txt).", file=sys.stderr)
            sys.exit(1)
        # timeout=1 so the read loop below can't block indefinitely if the
        # link goes quiet -- same read-loop shape as mavlink_sniffer.py's
        # recv_match(timeout=1) and crsf_parser.py's serial.Serial(timeout=1).
        return serial.Serial(self.serial_device, self.baud, timeout=1)

    def _ingest(self, frame: LTMFrame) -> None:
        now = time.time()
        if now - self._last_posted < self.repost_interval_s:
            return
        type_name = FRAME_TYPE_NAMES.get(frame.frame_type, frame.frame_type)
        try:
            fields = decode_frame(frame)
        except ValueError as e:
            fields = {"decode_error": str(e)}

        source_note = ("byte-exact per Ghettostation reference implementation"
                        if frame.frame_type in LTM_SOURCE_VERIFIED_TYPES
                        else "field layout is spec-derived only (mwptools/ArduPilot "
                             "docs), not verified against a reviewed C implementation")
        notes = (f"Protocol-level LTM XOR-checksum-verified frame decode, not an "
                 f"RF-signature heuristic. Frame type {type_name} ({source_note}).")

        detection = {
            "callsign": f"LTM-{self.serial_device}",
            "model": "LTM (Lightweight Telemetry) receiver (protocol-confirmed)",
            "protocol": "LTM",
            "threat_level": "MEDIUM",
            # LTM is a wired serial link, not an over-the-air RF capture --
            # there is no measured center_freq/RSSI at this layer, matching
            # crsf_parser.py's / mavlink_sniffer.py's same honest omission.
            "encrypted": False,
            "source": "LTM_SERIAL",
            "frame_type": type_name,
            "fields": fields,
            "notes": notes,
            "confidence_type": "protocol_verified",  # checksum check passed -- pass/fail, no probability to report
        }
        try:
            r = requests.post(f"{self.console_url}/api/detections/ingest",
                               json=detection, headers=self.headers, timeout=5)
            r.raise_for_status()
            self._last_posted = now
            print(f"[ltm_parser] CONFIRMED LTM frame: type={type_name} "
                  f"(XOR checksum verified) -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"[ltm_parser] detection ingest failed: {e}", file=sys.stderr)

    def run_forever(self) -> None:
        ser = self._open_serial()
        print(f"[ltm_parser] Opened {self.serial_device} @ {self.baud} baud in "
              f"RX-ONLY mode (no transmission will occur).")
        print("[ltm_parser] Passive LTM frame listener running. Posts a detection "
              "ONLY when a real XOR-checksum-verified LTM frame is decoded off the "
              "wire. If no such traffic ever arrives, nothing is ever posted -- "
              "there is no synthetic fallback, by design.")
        while True:
            try:
                chunk = ser.read(256)  # blocks up to timeout=1s, never blocks forever
            except Exception as e:
                print(f"[ltm_parser] WARN: serial read error: {e}", file=sys.stderr)
                time.sleep(0.5)
                continue
            if not chunk:
                continue  # genuinely nothing on the link this second -- expected, not an error
            for frame in self.parser.feed_bytes(chunk):
                self._ingest(frame)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="Run the offline parser/checksum self-test (no serial "
                          "hardware needed) and exit. This is the only thing "
                          "verified in this session -- see module docstring "
                          "HARDWARE STATUS.")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--serial", default=os.environ.get("LTM_SNIFF_SERIAL", "/dev/ttyUSB0"),
                     help="LTM source serial device (READ-ONLY; never transmits). "
                          "REQUIRES a real LTM-emitting flight controller or OpenHD "
                          "ground unit wired to this port -- see module docstring "
                          "HARDWARE STATUS; none was available in this session.")
    ap.add_argument("--baud", type=int,
                     default=int(os.environ.get("LTM_SNIFF_BAUD", str(LTM_DEFAULT_BAUD))))
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    print("[ltm_parser] KNOWN LIMITATION: this bridge has never been run against real "
          "LTM hardware in this session (none was available). It is real, tested "
          "PARSING logic (run with --self-test to verify) wired to a real, untested "
          "serial listener. See module docstring HARDWARE STATUS before trusting any "
          "output.", file=sys.stderr)

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    bridge = LTMSerialBridge(args.console_url, headers, args.serial, args.baud)
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
