#!/usr/bin/env python3
"""Real Graupner HoTT telemetry frame decoder + RX-only serial bridge.

RECEIVE ONLY. No transmission happens anywhere in this script. There is no
`write()`/`send()` path here at all — see the "HARDWARE STATUS" section below
for why.

=============================================================================
WHAT HoTT IS, AND WHY THIS IS A SERIAL PARSER, NOT AN SDR/RF SCRIPT
=============================================================================
Graupner HoTT ("Hop-on-Telemetry") is a **half-duplex, single-wire, bi-
directional UART serial protocol** running between a Graupner RC receiver
(GR-12/16/24 etc, or a HoTT telemetry-capable module) and a sensor/flight
controller, at 19200 baud. Exactly like CRSF (see crsf_parser.py's module
docstring for the same reasoning), the RF hop/modulation happens inside the
Graupner receiver hardware; by the time bytes reach a UART they are already
a clean, checksum-protected serial byte stream on a single shared wire
(hence the diode-OR'd TX/RX wiring real implementations use — see
betaflight/src/main/telemetry/hott.c's header comment for the exact
diagram). There is no meaningful "IQ capture of HoTT" the way there is for
DroneID — the correct integration point is a serial tap on a real HoTT
receiver's telemetry wire, exactly analogous to crsf_parser.py's CRSF tap
and mavlink_sniffer.py's SiK/RFD900 tap.

=============================================================================
PROTOCOL SHAPE: A REQUEST/RESPONSE POLL, NOT A FREE-RUNNING STREAM
=============================================================================
Unlike CRSF (continuous downlink stream) or MAVLink (continuous bidirectional
stream), HoTT is a strict master/slave POLL protocol driven by the RECEIVER:

    receiver -> sensor: [request_id] [sensor_address]   (2 bytes, no checksum)
    sensor   -> receiver: [full sensor telemetry frame] [checksum]

  - request_id is either 0x80 (binary mode request) or 0x7F (text mode
    request, used by a Graupner transmitter's built-in telemetry display /
    CMS menu system).
  - sensor_address selects which sensor on the bus should answer (0x8A=GPS,
    0x8D=General Air Module, 0x8E=Electric Air Module, 0x89=Vario,
    0x8C=Air ESC). A sensor only answers if its own ID matches; 0x80 as the
    address byte means "no sensor found" / bus idle.
  - The request itself carries NO checksum in the real protocol (see
    betaflight's hottCheckSerialData(): it reads exactly 2 raw bytes and
    acts on them with no CRC check at all) — only the SENSOR's RESPONSE
    frame is checksum-protected. This decoder reflects that asymmetry
    honestly: request frames are recognized/named but never "CRC-verified"
    (there is nothing to verify), while response frames are only accepted
    when their checksum genuinely matches.

A passive listener tapping this single wire sees an interleaved stream of
2-byte requests and (when a sensor is present and addressed) full response
frames — this parser's byte-stream state machine (HoTTParser.feed_bytes)
handles both shapes.

=============================================================================
FRAME FORMAT (verified against a real, GPL-3.0 reference implementation)
=============================================================================
Binary-mode SENSOR RESPONSE frame (fixed 44 bytes + 1 checksum byte = 45
bytes on the wire, identical length for every one of the 5 binary sensor
types below):

    [start=0x7C] [sensor_id byte #2] [warning_beeps] [sensor_id_dup byte #4]
    ... sensor-specific fields (bytes #5..#43) ...
    [stop=0x7D] [checksum]

  - start_byte: always 0x7C for every binary sensor response.
  - byte #2 ("<type>_sensor_id"): the sensor TYPE ID also used as the
    request's address byte: 0x89=Vario, 0x8A=GPS, 0x8C=AirESC, 0x8D=GAM
    (General Air Module), 0x8E=EAM (Electric Air Module).
  - byte #4 ("sensor_id"): a SECOND, different constant per sensor type,
    used by the receiver's display/menu logic to pick an icon/text-mode
    page: 0x90=Vario, 0xA0=GPS, 0xC0=AirESC, 0xD0=GAM, 0xE0=EAM. This is
    NOT a duplicate of byte #2 — it is a distinct constant per real
    reference struct (see e.g. HOTT_GPS_MSG_s.sensor_id vs .gps_sensor_id
    in betaflight/src/main/telemetry/hott.h).
  - stop_byte: always 0x7D, the last byte of the fixed 44-byte body.
  - checksum: a SIMPLE ARITHMETIC SUM (not CRC/polynomial) of every byte
    from start_byte through stop_byte inclusive, truncated to 8 bits (mod
    256). Verified directly from betaflight's hottSendTelemetryData():
    `hottMsgCrc += *hottMsg` accumulates every body byte as it's
    transmitted (hottMsgCrc is declared `static uint8_t`, so the addition
    silently wraps mod 256 in C), and the crc byte sent last is exactly
    that running (mod-256) sum — no init offset, no final XOR, no table.

Text-mode frame (Graupner transmitter CMS/menu display, fixed 173 bytes on
the wire = 172-byte body + 1 checksum byte):

    [start=0x7B] [esc: sensor-text-ID or 0x01] [warning] [8 rows x 21 cols
    ASCII text] [stop=0x7D] [checksum]

  Same mod-256 sum checksum rule as binary frames, computed over the full
  172-byte body (start through stop inclusive).

This layout, every sensor-ID constant, every field offset, and the checksum
algorithm were read directly from a real, already-catalogued local
reference (not re-derived from memory / not guessed):
  - betaflight/src/main/telemetry/hott.h (struct layouts: HOTT_GPS_MSG_s,
    HOTT_EAM_MSG_s, HOTT_GAM (OTT_GAM_MSG_s), HOTT_VARIO_MSG_s,
    HOTT_AIRESC_MSG_s, hottTextModeMsg_s; all the HOTT_*_SENSOR_ID /
    HOTT_*_TEXT_ID / HOTT_TEXTMODE_* constants) at
    ~/Desktop/Zettawise/PMO Suraj/tool/betaflight/src/main/telemetry/hott.h
  - betaflight/src/main/telemetry/hott.c (request/response state machine
    in hottCheckSerialData()/processBinaryModeRequest(), and the checksum
    accumulation in hottSendTelemetryData()) at the sibling hott.c in the
    same directory. inav/src/main/telemetry/hott.{c,h} is a near-identical
    fork of the same code (both descend from the original Cleanflight HoTT
    driver credited in the file header to Dominic Clifton/Hydra, Carsten
    Giesen, Oliver Bayer, and Adam Majerczyk's HoTT-for-ardupilot reverse
    engineering) — cross-checking the two confirms none of the constants
    or the checksum rule is a betaflight-only quirk.

=============================================================================
LICENSE — READ BEFORE REUSING/REDISTRIBUTING THIS FILE
=============================================================================
betaflight/src/main/telemetry/hott.{c,h} (and inav's near-identical fork)
are GPL-3.0-or-later (see the file header reproduced verbatim: "Cleanflight
and Betaflight are free software... GNU General Public License... version 3
... or any later version"). NOTHING in that GPL source was copied into this
file — no struct definitions, no function bodies, no verbatim comment text.
What is reproduced here is the PROTOCOL ITSELF: sensor-ID byte values,
field offsets/order, and the mod-256-sum checksum algorithm. These are
factual, unprotectable elements of a communications protocol (the same
category of fact as "HTTP requests start with a verb and a path"), not
expressive/creative code — this project reimplements them from scratch in
Python with entirely new variable names, data structures (dataclasses),
and control flow, exactly as crsf_parser.py did for CRSF's frame layout
(citing AlfredoCRSF/ExpressLRS, both also GPLv3) and as msp_parser.py
already established for a sibling protocol in this same directory.

Per this project's standing "internal-defense licensing override" precedent
(see crsf_parser.py's module docstring, and this project's memory note on
open-source sovereignty / task #101's legacy-RC-protocol GPL precedent):
GPL-licensed flight-stack sources are permitted as a REFERENCE for protocol
facts in this internal-defense tool, with the underlying code
reimplemented rather than copied, and the reference cited explicitly here
for audit. This is flagged for explicit Reality Checker sign-off before any
deployment, consistent with that precedent — this script is NOT to be
deployed without that review (see task instructions).

=============================================================================
HARDWARE STATUS — READ BEFORE TRUSTING ANY "LIVE" OUTPUT OF THIS SCRIPT
=============================================================================
TESTED, with real logic (no real hardware needed for this part):
  - Checksum algorithm: mod-256 running sum, verified in self_test() with
    two independently-written computation paths (byte-by-byte accumulation
    matching betaflight's `hottMsgCrc += *hottMsg` loop exactly, and
    Python's built-in sum() mod 256 as an independent cross-check) that
    must agree on every test frame.
  - Frame parsing state machine (HoTTParser.feed_bytes): finds the real
    0x7C/0x7B start bytes, reads the fixed-length body for the addressed
    sensor type, validates the checksum, and rejects corrupt/truncated/
    resynced streams — exercised in self_test() against frames built
    directly from the real field layouts above: GPS, EAM (Electric Air
    Module), GAM (General Air Module), Vario, AirESC, text-mode, a 2-byte
    binary-mode REQUEST frame, a 2-byte text-mode REQUEST frame, and a
    checksum-corrupted response frame (must be rejected).
  - Field-level decoders (parse_gps/parse_eam/parse_gam/parse_vario/
    parse_airesc): reproduce the real offset-based encodings (e.g. GPS
    lat/lon as degree-minutes + 1/10000-minute seconds split across
    pos_NS_dm/pos_NS_sec, altitude with the +500m offset, cell voltages in
    0.02V/2mV steps, current/voltage in 0.1-unit steps) and are round-
    tripped in self_test() against known, hand-computed values.

NOT TESTED — no real HoTT-capable receiver/sensor hardware was available in
this session:
  - HoTTSerialBridge (the RX-only pyserial listener below) has NEVER been
    run against a real Graupner HoTT receiver's single-wire telemetry
    output. This project's existing serial hardware (SiK/RFD900 radios,
    see mavlink_sniffer.py) speaks MAVLink at 57600 baud over 915/433MHz
    telemetry links — a different physical radio, protocol, AND wiring
    topology (bidirectional two-wire vs HoTT's shared single-wire) from a
    Graupner GR-series receiver's HoTT output, and cannot be repurposed to
    produce real HoTT traffic. No Graupner receiver, HoTT sensor module, or
    HoTT-capable transmitter was available to test against.
  - Per this project's standing rule (no synthetic/fallback data, ever):
    this script will NOT fabricate a serial connection or synthetic HoTT
    bytes to "demo" a detection. If you run HoTTSerialBridge against a
    /dev/ttyUSB* with no real HoTT device attached, it will simply see
    garbage/no start bytes and post nothing — that is the correct, honest
    behavior, identical to crsf_parser.py's and mavlink_sniffer.py's
    stated behavior under the same condition.
  - This module is therefore shipped as tested PARSING INFRASTRUCTURE, with
    a bridge class staged and ready to go the moment real HoTT hardware
    (a Graupner GR-12/16/24-class receiver, or a HoTT sensor such as the
    #33620 Electric Air Module or #33600 GPS Module, wired single-wire via
    a diode-OR to a USB-serial adapter per the wiring note above) is
    available. Wire it up, point --serial at that device, and the existing,
    tested parser takes over — no code changes needed.
  - STAGED RX-ONLY, HARDWARE-BLOCKED per task instructions: no compatible
    HoTT receiver/sensor hardware exists in this session's inventory.

=============================================================================
INGEST CONVENTION (matches crsf_parser.py's / droneid_decode_bridge.py's
checksum-gated pattern)
=============================================================================
A detection is posted to /api/detections/ingest ONLY when a sensor RESPONSE
frame's mod-256 checksum genuinely validates against real bytes read off
the wire — confidence_type="protocol_verified". Bare 2-byte REQUEST frames
(receiver polling for a sensor) are recognized and counted, but since the
real protocol carries no checksum on the request itself (see above), a
request-only observation is never itself posted as a checksum-verified
detection — it only updates internal state (mirroring the honest asymmetry
in the real protocol).

Requires: pyserial, requests (see field-bridge/requirements.txt). No new
dependency is introduced by this file.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

# =============================================================================
# Protocol constants — taken from betaflight/src/main/telemetry/hott.h
# (cross-checked against inav's near-identical fork). Nothing here is
# invented; see module docstring for exact source paths.
# =============================================================================

HOTT_BAUDRATE = 19200

HOTT_START_BYTE_BINARY = 0x7C
HOTT_STOP_BYTE_BINARY = 0x7D
HOTT_START_BYTE_TEXT = 0x7B
HOTT_STOP_BYTE_TEXT = 0x7D
HOTT_TEXTMODE_ESC = 0x01

HOTT_BINARY_MODE_REQUEST_ID = 0x80
HOTT_TEXT_MODE_REQUEST_ID = 0x7F
HOTT_TELEMETRY_NO_SENSOR_ID = 0x80   # address byte value meaning "no sensor on bus answered"

# Sensor TYPE IDs (byte #2 of every binary response frame; also the
# request's address byte selecting which sensor should answer).
HOTT_TELEMETRY_VARIO_SENSOR_ID = 0x89   # Graupner #33601 Vario Module
HOTT_TELEMETRY_GPS_SENSOR_ID = 0x8A     # Graupner #33600 GPS Module
HOTT_TELEMETRY_AIRESC_SENSOR_ID = 0x8C  # Graupner #337xx Air ESC
HOTT_TELEMETRY_GAM_SENSOR_ID = 0x8D     # Graupner #33611 General Air Module
HOTT_TELEMETRY_EAM_SENSOR_ID = 0x8E     # Graupner #33620 Electric Air Module

# Byte #4 constants (distinct per-type "display/menu" sensor ID, per the
# real structs' second `sensor_id` field — NOT a duplicate of the type ID
# above).
HOTT_SENSOR_ID_VARIO = 0x90
HOTT_SENSOR_ID_GPS = 0xA0
HOTT_SENSOR_ID_AIRESC = 0xC0
HOTT_SENSOR_ID_GAM = 0xD0
HOTT_SENSOR_ID_EAM = 0xE0

# Text-mode per-sensor display IDs used in the `esc` byte.
HOTT_EAM_SENSOR_TEXT_ID = 0xE0
HOTT_GPS_SENSOR_TEXT_ID = 0xA0

HOTT_TEXTMODE_DISPLAY_ROWS = 8
HOTT_TEXTMODE_DISPLAY_COLUMNS = 21

# Fixed on-the-wire BODY lengths (start_byte..stop_byte inclusive, i.e. the
# struct sizeof() in the real C source) for each binary sensor response —
# every one of the 5 binary structs is exactly 44 bytes in the real source.
HOTT_BINARY_BODY_LEN = 44
# Total bytes on the wire = body + 1 checksum byte.
HOTT_BINARY_FRAME_LEN = HOTT_BINARY_BODY_LEN + 1

# Text-mode body length: start(1) + esc(1) + warning(1) + 8*21 txt(168) +
# stop(1) = 172, + 1 checksum byte = 173 total on the wire.
HOTT_TEXT_BODY_LEN = 3 + (HOTT_TEXTMODE_DISPLAY_ROWS * HOTT_TEXTMODE_DISPLAY_COLUMNS) + 1
HOTT_TEXT_FRAME_LEN = HOTT_TEXT_BODY_LEN + 1

# Offset conventions shared by GPS/EAM/GAM/Vario altitude & related fields.
HOTT_EAM_OFFSET_HEIGHT = 500
HOTT_GPS_ALTITUDE_OFFSET = 500
HOTT_EAM_OFFSET_TEMPERATURE = 20

SENSOR_TYPE_NAMES: Dict[int, str] = {
    HOTT_TELEMETRY_VARIO_SENSOR_ID: "VARIO",
    HOTT_TELEMETRY_GPS_SENSOR_ID: "GPS",
    HOTT_TELEMETRY_AIRESC_SENSOR_ID: "AIRESC",
    HOTT_TELEMETRY_GAM_SENSOR_ID: "GAM",
    HOTT_TELEMETRY_EAM_SENSOR_ID: "EAM",
}


def is_binary_sensor_address(address: int) -> bool:
    return address in SENSOR_TYPE_NAMES


# =============================================================================
# Checksum — mod-256 running sum, two independent implementations
# cross-checked in self_test(). NOT a CRC/polynomial (unlike CRSF's CRC8) —
# reproduces betaflight's `hottMsgCrc += *hottMsg` accumulation over an
# implicit-uint8_t-wraparound running total, verified against the file's
# actual C source.
# =============================================================================

def hott_checksum_accumulate(body: bytes) -> int:
    """Byte-by-byte accumulation, matching hottSendTelemetryData()'s
    `hottMsgCrc += *hottMsg` loop exactly (each add truncated mod 256, as a
    C `uint8_t` would silently wrap)."""
    crc = 0
    for b in body:
        crc = (crc + b) & 0xFF
    return crc


def hott_checksum_sum(body: bytes) -> int:
    """Independent cross-check: Python's built-in sum(), reduced mod 256 in
    one shot rather than incrementally. Must always agree with
    hott_checksum_accumulate() since addition mod 256 is associative —
    used purely as a second, differently-written implementation to catch
    a transcription bug in either one (same spirit as crsf_parser.py's
    table-based vs bit-wise CRC8 cross-check)."""
    return sum(body) & 0xFF


def build_binary_frame(sensor_type_id: int, sensor_display_id: int, body_fill: bytes) -> bytes:
    """Construct a real, spec-conformant HoTT binary sensor response frame
    (for tests only, not used by the RX-only bridge). `body_fill` is the
    sensor-specific bytes #3..#43 (warning_beeps through the last field
    before stop_byte); this function affixes start/type/display bytes,
    stop_byte, and the correct checksum.
    """
    body = bytearray()
    body.append(HOTT_START_BYTE_BINARY)
    body.append(sensor_type_id)
    body.append(body_fill[0])          # warning_beeps
    body.append(sensor_display_id)
    body.extend(body_fill[1:])
    # Pad/truncate to the fixed 44-byte body length minus the stop byte.
    while len(body) < HOTT_BINARY_BODY_LEN - 1:
        body.append(0)
    body = body[:HOTT_BINARY_BODY_LEN - 1]
    body.append(HOTT_STOP_BYTE_BINARY)
    crc = hott_checksum_accumulate(bytes(body))
    return bytes(body) + bytes([crc])


def build_text_frame(esc: int, warning: int, rows: List[str]) -> bytes:
    """Construct a real, spec-conformant HoTT text-mode frame (for tests
    only). `rows` is up to 8 strings, each padded/truncated to 21 chars."""
    body = bytearray()
    body.append(HOTT_START_BYTE_TEXT)
    body.append(esc)
    body.append(warning)
    for r in range(HOTT_TEXTMODE_DISPLAY_ROWS):
        text = rows[r] if r < len(rows) else ""
        text = text[:HOTT_TEXTMODE_DISPLAY_COLUMNS].ljust(HOTT_TEXTMODE_DISPLAY_COLUMNS)
        body.extend(text.encode("ascii", errors="replace"))
    body.append(HOTT_STOP_BYTE_TEXT)
    crc = hott_checksum_accumulate(bytes(body))
    return bytes(body) + bytes([crc])


# =============================================================================
# Frame parser: byte-stream state machine, feed_bytes()-driven so it plugs
# straight into pyserial's read loop (or into self_test()'s canned bytes).
# Handles BOTH shapes seen on a real HoTT wire: 2-byte requests and fixed-
# length checksummed sensor responses.
# =============================================================================

@dataclass
class HoTTRequestFrame:
    """A 2-byte receiver->sensor poll. No checksum exists on this frame in
    the real protocol (see module docstring) — there is nothing to verify,
    so this is never itself checksum-gated."""
    request_id: int
    address: int
    is_text_mode: bool
    raw: bytes
    timestamp: Optional[float] = None


@dataclass
class HoTTResponseFrame:
    """A checksum-validated sensor response (binary or text mode)."""
    is_text_mode: bool
    sensor_type_id: int          # 0 for text-mode frames (no per-type ID byte there)
    body: bytes                  # start_byte..stop_byte inclusive
    checksum: int
    raw: bytes
    timestamp: Optional[float] = None


class HoTTParser:
    """Feed raw serial bytes in one at a time (or in chunks via
    feed_bytes); yields HoTTRequestFrame / HoTTResponseFrame objects.
    Response frames are yielded ONLY when their checksum genuinely
    validates. Non-conformant bytes are discarded by resyncing on the next
    plausible start byte — this never blocks and never raises on garbage
    input, exactly like crsf_parser.py's CRSFParser.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed_bytes(self, data: bytes, timestamp: Optional[float] = None):
        if timestamp is None:
            timestamp = time.time()
        self._buf.extend(data)
        frames = []
        while True:
            frame = self._try_extract_one()
            if frame is None:
                break
            frame.timestamp = timestamp
            frames.append(frame)
        return frames

    def _try_extract_one(self):
        buf = self._buf

        # A binary/text START byte is unambiguous (0x7C or 0x7B); a REQUEST
        # frame's first byte is 0x80 or 0x7F. Resync past anything that is
        # none of these four plausible leading values.
        plausible_leaders = {HOTT_START_BYTE_BINARY, HOTT_START_BYTE_TEXT,
                              HOTT_BINARY_MODE_REQUEST_ID, HOTT_TEXT_MODE_REQUEST_ID}
        while buf and buf[0] not in plausible_leaders:
            del buf[0]
        if not buf:
            return None

        leader = buf[0]

        # --- 2-byte REQUEST frame (receiver polling a sensor) ---
        if leader in (HOTT_BINARY_MODE_REQUEST_ID, HOTT_TEXT_MODE_REQUEST_ID):
            if len(buf) < 2:
                return None
            # Disambiguate from a genuine sensor response frame: a request's
            # second byte is a sensor address (or HOTT_TELEMETRY_NO_SENSOR_ID);
            # it is never itself a plausible start byte of the NEXT frame in a
            # well-formed stream, so simply consume 2 bytes and hand back a
            # request frame. This mirrors betaflight's own hottCheckSerialData(),
            # which reads exactly 2 raw bytes and dispatches on them with no
            # length ambiguity because the wire is half-duplex/one-poll-at-a-time.
            request_id = buf[0]
            address = buf[1]
            raw = bytes(buf[:2])
            del buf[:2]
            return HoTTRequestFrame(
                request_id=request_id, address=address,
                is_text_mode=(request_id == HOTT_TEXT_MODE_REQUEST_ID),
                raw=raw,
            )

        # --- Binary sensor RESPONSE frame (fixed 44-byte body + checksum) ---
        if leader == HOTT_START_BYTE_BINARY:
            total_len = HOTT_BINARY_FRAME_LEN
            if len(buf) < total_len:
                return None
            body = bytes(buf[:HOTT_BINARY_BODY_LEN])
            checksum_received = buf[HOTT_BINARY_BODY_LEN]
            # A genuine binary response's body must end with the real stop
            # byte at the expected fixed offset; if not, this 0x7C was a
            # false-positive resync point (e.g. mid-payload data byte that
            # happens to equal 0x7C) -- drop just the leader and keep looking.
            if body[-1] != HOTT_STOP_BYTE_BINARY:
                del buf[0]
                return None
            checksum_computed = hott_checksum_accumulate(body)
            raw = bytes(buf[:total_len])
            del buf[:total_len]
            if checksum_computed != checksum_received:
                return None  # genuinely corrupt -- not ingested, no fallback
            sensor_type_id = body[1]
            return HoTTResponseFrame(
                is_text_mode=False, sensor_type_id=sensor_type_id,
                body=body, checksum=checksum_received, raw=raw,
            )

        # --- Text-mode RESPONSE frame (fixed 172-byte body + checksum) ---
        if leader == HOTT_START_BYTE_TEXT:
            total_len = HOTT_TEXT_FRAME_LEN
            if len(buf) < total_len:
                return None
            body = bytes(buf[:HOTT_TEXT_BODY_LEN])
            checksum_received = buf[HOTT_TEXT_BODY_LEN]
            if body[-1] != HOTT_STOP_BYTE_TEXT:
                del buf[0]
                return None
            checksum_computed = hott_checksum_accumulate(body)
            raw = bytes(buf[:total_len])
            del buf[:total_len]
            if checksum_computed != checksum_received:
                return None
            return HoTTResponseFrame(
                is_text_mode=True, sensor_type_id=0,
                body=body, checksum=checksum_received, raw=raw,
            )

        # Unreachable given plausible_leaders above.
        del buf[0]
        return None


# =============================================================================
# Field-level decoders — one per binary sensor type, offsets taken directly
# from betaflight/src/main/telemetry/hott.h's struct definitions (see module
# docstring for the exact source path). All multi-byte fields are
# little-endian low-byte-then-high-byte, per the real *_L/*_H field pairs.
# =============================================================================

def _u16le(body: bytes, lo_offset: int) -> int:
    return body[lo_offset] | (body[lo_offset + 1] << 8)


def _degmin_to_decimal(dm: int, sec: int) -> float:
    """HoTT GPS coordinate decode: dm is packed as (degrees*100 + minutes),
    sec is minutes-seconds in 1/10000-minute units (per addGPSCoordinates()'s
    inverse in betaflight: `degMin = deg*100 + min`, `sec` in 1e-4 minute
    steps). Returns signed-magnitude decimal degrees (sign applied by
    caller from the NS/EW flag byte)."""
    deg = dm // 100
    minutes = (dm % 100) + (sec / 10000.0)
    return deg + minutes / 60.0


def parse_gps(body: bytes) -> dict:
    """HOTT_GPS_MSG_s, per betaflight/src/main/telemetry/hott.h."""
    if len(body) < HOTT_BINARY_BODY_LEN:
        raise ValueError(f"GPS body too short: {len(body)} bytes (need {HOTT_BINARY_BODY_LEN})")
    flight_direction = body[6]              # #07, 2deg/step
    gps_speed_kmh = _u16le(body, 7)         # #08-09
    pos_ns_sign = body[9]                   # #10: 0=N, 1=S
    ns_dm = _u16le(body, 10)                # #11-12
    ns_sec = _u16le(body, 12)               # #13-14
    pos_ew_sign = body[14]                  # #15: 0=E, 1=W
    ew_dm = _u16le(body, 15)                # #16-17
    ew_sec = _u16le(body, 17)               # #18-19
    home_distance_m = _u16le(body, 19)      # #20-21
    altitude_m = _u16le(body, 21) - HOTT_GPS_ALTITUDE_OFFSET   # #22-23
    climbrate_ms = (_u16le(body, 23) - 30000) / 100.0          # #24-25
    climbrate3s_ms = body[25] - 120                            # #26
    num_satellites = body[26]               # #27
    gps_fix_char = chr(body[27]) if 32 <= body[27] < 127 else "?"  # #28
    home_direction = body[28]                # #29, 2deg/step
    lat = _degmin_to_decimal(ns_dm, ns_sec) * (-1 if pos_ns_sign else 1)
    lon = _degmin_to_decimal(ew_dm, ew_sec) * (-1 if pos_ew_sign else 1)
    return {
        "latitude": lat,
        "longitude": lon,
        "flight_direction_deg": flight_direction * 2,
        "gps_speed_kmh": gps_speed_kmh,
        "home_distance_m": home_distance_m,
        "altitude_m": altitude_m,
        "climbrate_ms": climbrate_ms,
        "climbrate3s_ms": climbrate3s_ms,
        "num_satellites": num_satellites,
        "gps_fix_char": gps_fix_char,
        "home_direction_deg": home_direction * 2,
    }


def parse_eam(body: bytes) -> dict:
    """HOTT_EAM_MSG_s (Electric Air Module) — cell voltages, battery,
    current, altitude, climb rate."""
    if len(body) < HOTT_BINARY_BODY_LEN:
        raise ValueError(f"EAM body too short: {len(body)} bytes (need {HOTT_BINARY_BODY_LEN})")
    cells_l = body[6:13]     # #07-13
    cells_h = body[13:20]    # #14-20
    cell_volts = [((cells_h[i] << 8) | cells_l[i]) * 0.02 for i in range(7)]
    batt1_v = _u16le(body, 20) * 0.1   # #21-22
    batt2_v = _u16le(body, 22) * 0.1   # #23-24
    temp1_c = body[24] - HOTT_EAM_OFFSET_TEMPERATURE   # #25
    temp2_c = body[25] - HOTT_EAM_OFFSET_TEMPERATURE   # #26
    altitude_m = _u16le(body, 26) - HOTT_EAM_OFFSET_HEIGHT  # #27-28
    current_a = _u16le(body, 28) * 0.1  # #29-30
    main_voltage_v = _u16le(body, 30) * 0.1  # #31-32
    batt_cap_mah = _u16le(body, 32) * 10  # #33-34
    climbrate_ms = (_u16le(body, 34) - 30000) / 100.0  # #35-36
    climbrate3s_ms = body[36] - 120  # #37
    rpm = _u16le(body, 37) * 10  # #38-39
    electric_min = body[39]  # #40
    electric_sec = body[40]  # #41
    speed_kmh = _u16le(body, 41)  # #42-43
    return {
        "cell_voltages": cell_volts,
        "batt1_voltage_v": batt1_v,
        "batt2_voltage_v": batt2_v,
        "temp1_c": temp1_c,
        "temp2_c": temp2_c,
        "altitude_m": altitude_m,
        "current_a": current_a,
        "main_voltage_v": main_voltage_v,
        "batt_cap_mah": batt_cap_mah,
        "climbrate_ms": climbrate_ms,
        "climbrate3s_ms": climbrate3s_ms,
        "rpm": rpm,
        "electric_min": electric_min,
        "electric_sec": electric_sec,
        "speed_kmh": speed_kmh,
    }


def parse_gam(body: bytes) -> dict:
    """OTT_GAM_MSG_t / General Air Module."""
    if len(body) < HOTT_BINARY_BODY_LEN:
        raise ValueError(f"GAM body too short: {len(body)} bytes (need {HOTT_BINARY_BODY_LEN})")
    cells = [body[6 + i] * 0.02 for i in range(6)]   # #07-12
    batt1_v = _u16le(body, 12) * 0.1   # #13-14
    batt2_v = _u16le(body, 14) * 0.1   # #15-16
    temp1_c = body[16] - HOTT_EAM_OFFSET_TEMPERATURE  # #17
    temp2_c = body[17] - HOTT_EAM_OFFSET_TEMPERATURE  # #18
    fuel_pct = body[18]  # #19
    fuel_ml = _u16le(body, 19)  # #20-21
    rpm = _u16le(body, 21) * 10  # #22-23
    altitude_m = _u16le(body, 23) - HOTT_EAM_OFFSET_HEIGHT  # #24-25
    climbrate_ms = (_u16le(body, 25) - 30000) / 100.0  # #26-27
    climbrate3s_ms = body[27] - 120  # #28
    current_a = _u16le(body, 28) * 0.1  # #29-30
    main_voltage_v = _u16le(body, 30) * 0.1  # #31-32
    batt_cap_mah = _u16le(body, 32) * 10  # #33-34
    speed_kmh = _u16le(body, 34)  # #35-36
    return {
        "cell_voltages": cells,
        "batt1_voltage_v": batt1_v,
        "batt2_voltage_v": batt2_v,
        "temp1_c": temp1_c,
        "temp2_c": temp2_c,
        "fuel_pct": fuel_pct,
        "fuel_ml": fuel_ml,
        "rpm": rpm,
        "altitude_m": altitude_m,
        "climbrate_ms": climbrate_ms,
        "climbrate3s_ms": climbrate3s_ms,
        "current_a": current_a,
        "main_voltage_v": main_voltage_v,
        "batt_cap_mah": batt_cap_mah,
        "speed_kmh": speed_kmh,
    }


def parse_vario(body: bytes) -> dict:
    """HOTT_VARIO_MSG_s."""
    if len(body) < HOTT_BINARY_BODY_LEN:
        raise ValueError(f"VARIO body too short: {len(body)} bytes (need {HOTT_BINARY_BODY_LEN})")
    altitude_m = _u16le(body, 5) - HOTT_GPS_ALTITUDE_OFFSET       # #06-07
    altitude_max_m = _u16le(body, 7) - HOTT_GPS_ALTITUDE_OFFSET   # #08-09
    altitude_min_m = _u16le(body, 9) - HOTT_GPS_ALTITUDE_OFFSET   # #10-11
    climbrate_ms = (_u16le(body, 11) - 30000) / 100.0             # #12-13
    climbrate3s_ms = (_u16le(body, 13) - 30000) / 100.0           # #14-15
    climbrate10s_ms = (_u16le(body, 15) - 30000) / 100.0          # #16-17
    return {
        "altitude_m": altitude_m,
        "altitude_max_m": altitude_max_m,
        "altitude_min_m": altitude_min_m,
        "climbrate_ms": climbrate_ms,
        "climbrate3s_ms": climbrate3s_ms,
        "climbrate10s_ms": climbrate10s_ms,
    }


def parse_airesc(body: bytes) -> dict:
    """HOTT_AIRESC_MSG_s."""
    if len(body) < HOTT_BINARY_BODY_LEN:
        raise ValueError(f"AIRESC body too short: {len(body)} bytes (need {HOTT_BINARY_BODY_LEN})")
    input_v = _u16le(body, 6) * 0.1        # #07-08
    input_v_min = _u16le(body, 8) * 0.1    # #09-10
    batt_cap_mah = _u16le(body, 10) * 10   # #11-12
    esc_temp_c = body[12]                  # #13
    esc_max_temp_c = body[13]              # #14
    current_a = _u16le(body, 14) * 0.1     # #15-16
    current_max_a = _u16le(body, 16) * 0.1  # #17-18
    rpm = _u16le(body, 18) * 10            # #19-20
    rpm_max = _u16le(body, 20) * 10        # #21-22
    throttle_pct = body[22]                # #23
    return {
        "input_voltage_v": input_v,
        "input_voltage_min_v": input_v_min,
        "batt_cap_mah": batt_cap_mah,
        "esc_temp_c": esc_temp_c,
        "esc_max_temp_c": esc_max_temp_c,
        "current_a": current_a,
        "current_max_a": current_max_a,
        "rpm": rpm,
        "rpm_max": rpm_max,
        "throttle_pct": throttle_pct,
    }


_PARSER_BY_SENSOR_TYPE = {
    HOTT_TELEMETRY_GPS_SENSOR_ID: parse_gps,
    HOTT_TELEMETRY_EAM_SENSOR_ID: parse_eam,
    HOTT_TELEMETRY_GAM_SENSOR_ID: parse_gam,
    HOTT_TELEMETRY_VARIO_SENSOR_ID: parse_vario,
    HOTT_TELEMETRY_AIRESC_SENSOR_ID: parse_airesc,
}


def parse_response_fields(frame: HoTTResponseFrame) -> dict:
    """Dispatch to the correct field decoder for a binary response frame's
    sensor type. Raises ValueError for an unrecognized sensor type (never
    silently returns fabricated/guessed fields)."""
    if frame.is_text_mode:
        raise ValueError("parse_response_fields() is for binary frames; "
                          "text-mode frames carry raw ASCII text only")
    decoder = _PARSER_BY_SENSOR_TYPE.get(frame.sensor_type_id)
    if decoder is None:
        raise ValueError(f"unrecognized HoTT sensor type id 0x{frame.sensor_type_id:02X}")
    return decoder(frame.body)


def decode_text_rows(frame: HoTTResponseFrame) -> List[str]:
    """Extract the 8x21 ASCII display rows from a text-mode response frame,
    per hottTextModeMsg_s's txt[8][21] layout (offset 3, right after
    start/esc/warning)."""
    if not frame.is_text_mode:
        raise ValueError("decode_text_rows() is for text-mode frames only")
    rows = []
    off = 3
    for _ in range(HOTT_TEXTMODE_DISPLAY_ROWS):
        raw_row = frame.body[off:off + HOTT_TEXTMODE_DISPLAY_COLUMNS]
        # Bit 7 = inverse-display flag per hottTextModeMsg_s's txt[] comment;
        # strip it for plain ASCII decode (inverse-display state is a
        # display concern, not part of the character's identity).
        chars = "".join(chr(b & 0x7F) if 32 <= (b & 0x7F) < 127 else "." for b in raw_row)
        rows.append(chars)
        off += HOTT_TEXTMODE_DISPLAY_COLUMNS
    return rows


# =============================================================================
# Self-test: exercises the checksum and parser against frames built directly
# from the real field layouts above. This is what is ACTUALLY TESTED in this
# session (see module docstring HARDWARE STATUS) -- no live HoTT hardware,
# real spec-conformant bytes only.
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== Checksum cross-check: byte-accumulate vs sum()-mod-256 ===")
    for sample in (b"", bytes([0x7C, 0x8E, 0x00]), bytes(range(256)), b"\xff" * 44):
        a, b = hott_checksum_accumulate(sample), hott_checksum_sum(sample)
        check(f"hott_checksum_accumulate == hott_checksum_sum for {len(sample)}-byte sample", a == b)
    check("hott_checksum_accumulate(b'') == 0 (identity for empty input)",
          hott_checksum_accumulate(b"") == 0)
    # Known scalar check: sum of [0x7C, 0x8E, 0x01] = 0x7C+0x8E+0x01 = 0x10B,
    # mod 256 = 0x0B.
    check("hott_checksum_accumulate([0x7C,0x8E,0x01]) == 0x0B (hand-computed)",
          hott_checksum_accumulate(bytes([0x7C, 0x8E, 0x01])) == 0x0B)

    print("\n=== Binary sensor response frames: build + parse round-trip ===")

    def fill_from_body_indices(assignments: Dict[int, int]) -> bytes:
        """Build the `body_fill` array build_binary_frame() expects, from a
        dict of {0-based body byte index (per the real struct's #NN field
        numbering, index = NN-1) -> value}. build_binary_frame() places
        body_fill[0] at body index 2 (warning_beeps) and body_fill[1:] at
        body indices 4..42 (i.e. body[j] = body_fill[j-3] for j>=4) -- this
        helper hides that mapping so per-field test data can be written
        directly against the real struct's documented byte offsets instead
        of hand-translated indices (the earlier, error-prone approach)."""
        fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
        for idx, val in assignments.items():
            if idx == 2:
                fill[0] = val & 0xFF
            elif 4 <= idx <= 42:
                fill[idx - 3] = val & 0xFF
            else:
                raise ValueError(f"unsupported body index {idx} in test fill data")
        return bytes(fill)

    # EAM (Electric Air Module): battery 1 = 12.6V (raw 126), current = 5.0A
    # (raw 50), main voltage = 11.1V (raw 111), altitude = 120m (raw 620 =
    # 120+500 offset), climbrate = +2.50 m/s (raw 30000+250=30250), speed =
    # 45 km/h. Body indices per HOTT_EAM_MSG_s's #NN comments (index = NN-1).
    batt1_raw = 126
    alt_raw = 120 + HOTT_EAM_OFFSET_HEIGHT
    current_raw = 50
    mainv_raw = 111
    battcap_raw = 150
    climb_raw = 30000 + 250
    speed_raw = 45
    eam_assignments = {
        2: 0x00,                                            # #03 warning_beeps
        20: batt1_raw & 0xFF, 21: batt1_raw >> 8,            # #21-22 batt1_voltage_L/H
        24: 20 + 25,                                         # #25 temp1 = 25C
        25: 20 + 0,                                          # #26 temp2 = 0C
        26: alt_raw & 0xFF, 27: alt_raw >> 8,                # #27-28 altitude_L/H
        28: current_raw & 0xFF, 29: current_raw >> 8,        # #29-30 current_L/H
        30: mainv_raw & 0xFF, 31: mainv_raw >> 8,             # #31-32 main_voltage_L/H
        32: battcap_raw & 0xFF, 33: battcap_raw >> 8,         # #33-34 batt_cap_L/H
        34: climb_raw & 0xFF, 35: climb_raw >> 8,             # #35-36 climbrate_L/H
        36: 120,                                              # #37 climbrate3s = 0
        41: speed_raw & 0xFF, 42: speed_raw >> 8,             # #42-43 speed_L/H
    }
    eam_fill = fill_from_body_indices(eam_assignments)

    eam_frame_bytes = build_binary_frame(HOTT_TELEMETRY_EAM_SENSOR_ID, HOTT_SENSOR_ID_EAM, eam_fill)
    check("EAM frame is exactly HOTT_BINARY_FRAME_LEN bytes", len(eam_frame_bytes) == HOTT_BINARY_FRAME_LEN)

    parser = HoTTParser()
    frames = parser.feed_bytes(eam_frame_bytes)
    check("EAM frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("EAM frame is a HoTTResponseFrame", isinstance(f, HoTTResponseFrame))
        check("EAM sensor_type_id decoded correctly", f.sensor_type_id == HOTT_TELEMETRY_EAM_SENSOR_ID)
        eam = parse_eam(f.body)
        check("EAM batt1_voltage_v decoded (12.6V)", abs(eam["batt1_voltage_v"] - 12.6) < 1e-9)
        check("EAM temp1_c decoded (25C)", eam["temp1_c"] == 25)
        check("EAM altitude_m decoded (120m)", eam["altitude_m"] == 120)
        check("EAM current_a decoded (5.0A)", abs(eam["current_a"] - 5.0) < 1e-9)
        check("EAM main_voltage_v decoded (11.1V)", abs(eam["main_voltage_v"] - 11.1) < 1e-9)
        check("EAM climbrate_ms decoded (+2.50 m/s)", abs(eam["climbrate_ms"] - 2.5) < 1e-9)
        check("EAM speed_kmh decoded (45)", eam["speed_kmh"] == 45)
        check("EAM sensor type name resolves", SENSOR_TYPE_NAMES.get(f.sensor_type_id) == "EAM")

    # GPS: lat 12deg 30.5000min N, lon 77deg 15.2500min E, 10 satellites, 3D
    # fix. Body indices per HOTT_GPS_MSG_s's #NN comments (index = NN-1).
    gps_speed_raw = 36
    ns_dm = 12 * 100 + 30
    ns_sec = 5000
    ew_dm = 77 * 100 + 15
    ew_sec = 2500
    home_dist_raw = 42
    gps_alt_raw = 100 + HOTT_GPS_ALTITUDE_OFFSET
    gps_climb_raw = 30000
    gps_assignments = {
        2: 0x00,                                              # #03 warning_beeps
        6: 0,                                                 # #07 flight_direction
        7: gps_speed_raw & 0xFF, 8: gps_speed_raw >> 8,       # #08-09 gps_speed_L/H
        9: 0,                                                 # #10 pos_NS = N
        10: ns_dm & 0xFF, 11: ns_dm >> 8,                     # #11-12 pos_NS_dm_L/H
        12: ns_sec & 0xFF, 13: ns_sec >> 8,                   # #13-14 pos_NS_sec_L/H
        14: 0,                                                # #15 pos_EW = E
        15: ew_dm & 0xFF, 16: ew_dm >> 8,                     # #16-17 pos_EW_dm_L/H
        17: ew_sec & 0xFF, 18: ew_sec >> 8,                   # #18-19 pos_EW_sec_L/H
        19: home_dist_raw & 0xFF, 20: home_dist_raw >> 8,     # #20-21 home_distance_L/H
        21: gps_alt_raw & 0xFF, 22: gps_alt_raw >> 8,         # #22-23 altitude_L/H
        23: gps_climb_raw & 0xFF, 24: gps_climb_raw >> 8,     # #24-25 climbrate_L/H
        25: 120,                                              # #26 climbrate3s = 0
        26: 10,                                               # #27 gps_satelites
        27: ord('3'),                                         # #28 gps_fix_char
        28: 0,                                                # #29 home_direction
    }
    gps_fill = fill_from_body_indices(gps_assignments)

    gps_frame_bytes = build_binary_frame(HOTT_TELEMETRY_GPS_SENSOR_ID, HOTT_SENSOR_ID_GPS, gps_fill)
    frames = HoTTParser().feed_bytes(gps_frame_bytes)
    check("GPS frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("GPS sensor_type_id decoded correctly", f.sensor_type_id == HOTT_TELEMETRY_GPS_SENSOR_ID)
        gps = parse_gps(f.body)
        check("GPS latitude decoded (~12.5083 deg N)", abs(gps["latitude"] - (12 + 30.5 / 60.0)) < 1e-6)
        check("GPS longitude decoded (~77.2542 deg E)", abs(gps["longitude"] - (77 + 15.25 / 60.0)) < 1e-6)
        check("GPS num_satellites decoded (10)", gps["num_satellites"] == 10)
        check("GPS fix char decoded ('3')", gps["gps_fix_char"] == "3")
        check("GPS altitude_m decoded (100m)", gps["altitude_m"] == 100)
        check("GPS home_distance_m decoded (42m)", gps["home_distance_m"] == 42)

    # GAM: fuel 60%, RPM 3000 (raw 300), altitude 200m. Body indices per
    # OTT_GAM_MSG_t's #NN comments (index = NN-1).
    rpm_raw = 300
    gam_alt_raw = 200 + HOTT_EAM_OFFSET_HEIGHT
    gam_climb_raw = 30000
    gam_assignments = {
        2: 0x00,                                          # #03 warning_beeps
        16: 20,                                           # #17 temperature1 = 0C
        17: 20,                                           # #18 temperature2 = 0C
        18: 60,                                           # #19 fuel_procent = 60%
        21: rpm_raw & 0xFF, 22: rpm_raw >> 8,             # #22-23 rpm_L/H
        23: gam_alt_raw & 0xFF, 24: gam_alt_raw >> 8,     # #24-25 altitude_L/H
        25: gam_climb_raw & 0xFF, 26: gam_climb_raw >> 8,  # #26-27 climbrate_L/H
        27: 120,                                          # #28 climbrate3s = 0
    }
    gam_fill = fill_from_body_indices(gam_assignments)

    gam_frame_bytes = build_binary_frame(HOTT_TELEMETRY_GAM_SENSOR_ID, HOTT_SENSOR_ID_GAM, gam_fill)
    frames = HoTTParser().feed_bytes(gam_frame_bytes)
    check("GAM frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("GAM sensor_type_id decoded correctly", f.sensor_type_id == HOTT_TELEMETRY_GAM_SENSOR_ID)
        gam = parse_gam(f.body)
        check("GAM fuel_pct decoded (60%)", gam["fuel_pct"] == 60)
        check("GAM rpm decoded (3000)", gam["rpm"] == 3000)
        check("GAM altitude_m decoded (200m)", gam["altitude_m"] == 200)

    # VARIO: altitude 50m, max 80m, min 10m, climbrate +1.00 m/s.
    vario_fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
    vario_fill[0] = 0x00  # warning_beeps
    vario_fill[1] = 0x00  # alarm_invers1
    alt_raw = 50 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[2] = alt_raw & 0xFF        # body idx 6
    vario_fill[3] = alt_raw >> 8          # body idx 7
    alt_max_raw = 80 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[4] = alt_max_raw & 0xFF    # body idx 8
    vario_fill[5] = alt_max_raw >> 8      # body idx 9
    alt_min_raw = 10 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[6] = alt_min_raw & 0xFF    # body idx 10
    vario_fill[7] = alt_min_raw >> 8      # body idx 11
    climb_raw = 30000 + 100
    vario_fill[8] = climb_raw & 0xFF      # body idx 12
    vario_fill[9] = climb_raw >> 8        # body idx 13

    vario_frame_bytes = build_binary_frame(HOTT_TELEMETRY_VARIO_SENSOR_ID, HOTT_SENSOR_ID_VARIO, bytes(vario_fill))
    frames = HoTTParser().feed_bytes(vario_frame_bytes)
    check("VARIO frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        vario = parse_vario(f.body)
        check("VARIO altitude_m decoded (50m)", vario["altitude_m"] == 50)
        check("VARIO altitude_max_m decoded (80m)", vario["altitude_max_m"] == 80)
        check("VARIO altitude_min_m decoded (10m)", vario["altitude_min_m"] == 10)
        check("VARIO climbrate_ms decoded (+1.00 m/s)", abs(vario["climbrate_ms"] - 1.0) < 1e-9)

    # AirESC: input voltage 16.8V, current 12.0A, RPM 8000, throttle 75%.
    airesc_fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
    airesc_fill[0] = 0x00  # warning_beeps
    airesc_fill[1] = 0x00  # alarm_invers1
    airesc_fill[2] = 0x00  # alarm_invers2
    inv_raw = 168
    airesc_fill[3] = inv_raw & 0xFF       # body idx 7
    airesc_fill[4] = inv_raw >> 8         # body idx 8
    airesc_fill[5] = 0                    # input_v_min_L    (body idx 9)
    airesc_fill[6] = 0                    # input_v_min_H    (body idx 10)
    airesc_fill[7] = 0                    # batt_cap_L       (body idx 11)
    airesc_fill[8] = 0                    # batt_cap_H       (body idx 12)
    airesc_fill[9] = 45                   # esc_temp         (body idx 13)
    airesc_fill[10] = 60                  # esc_max_temp     (body idx 14)
    cur_raw = 120
    airesc_fill[11] = cur_raw & 0xFF      # current_L        (body idx 15)
    airesc_fill[12] = cur_raw >> 8        # current_H        (body idx 16)
    airesc_fill[13] = 0                   # current_max_L    (body idx 17)
    airesc_fill[14] = 0                   # current_max_H    (body idx 18)
    rpm_raw = 800
    airesc_fill[15] = rpm_raw & 0xFF      # rpm_L            (body idx 19)
    airesc_fill[16] = rpm_raw >> 8        # rpm_H            (body idx 20)
    airesc_fill[17] = 0                   # rpm_max_L        (body idx 21)
    airesc_fill[18] = 0                   # rpm_max_H        (body idx 22)
    airesc_fill[19] = 75                  # throttle         (body idx 23)

    airesc_frame_bytes = build_binary_frame(HOTT_TELEMETRY_AIRESC_SENSOR_ID, HOTT_SENSOR_ID_AIRESC, bytes(airesc_fill))
    frames = HoTTParser().feed_bytes(airesc_frame_bytes)
    check("AIRESC frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        airesc = parse_airesc(f.body)
        check("AIRESC input_voltage_v decoded (16.8V)", abs(airesc["input_voltage_v"] - 16.8) < 1e-9)
        check("AIRESC current_a decoded (12.0A)", abs(airesc["current_a"] - 12.0) < 1e-9)
        check("AIRESC rpm decoded (8000)", airesc["rpm"] == 8000)
        check("AIRESC throttle_pct decoded (75%)", airesc["throttle_pct"] == 75)

    print("\n=== Text-mode frame ===")
    text_frame_bytes = build_text_frame(HOTT_EAM_SENSOR_TEXT_ID, 0, ["HELLO WORLD", "ROW TWO"])
    check("Text frame is exactly HOTT_TEXT_FRAME_LEN bytes", len(text_frame_bytes) == HOTT_TEXT_FRAME_LEN)
    frames = HoTTParser().feed_bytes(text_frame_bytes)
    check("Text-mode frame parses to exactly 1 response frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("Text-mode frame flagged is_text_mode", f.is_text_mode is True)
        rows = decode_text_rows(f)
        check("Text-mode row 0 decodes as 'HELLO WORLD' (padded to 21 cols)",
              rows[0].rstrip(".") .rstrip() == "HELLO WORLD" or rows[0][:11] == "HELLO WORLD")
        check("Text-mode row 1 decodes as 'ROW TWO' (padded to 21 cols)",
              rows[1][:7] == "ROW TWO")

    print("\n=== Request frames (2-byte, no checksum in the real protocol) ===")
    binary_request = bytes([HOTT_BINARY_MODE_REQUEST_ID, HOTT_TELEMETRY_EAM_SENSOR_ID])
    frames = HoTTParser().feed_bytes(binary_request)
    check("Binary-mode request frame parses to exactly 1 request frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("Binary-mode request frame decoded as HoTTRequestFrame", isinstance(f, HoTTRequestFrame))
        check("Binary-mode request address decoded (EAM)", f.address == HOTT_TELEMETRY_EAM_SENSOR_ID)
        check("Binary-mode request is_text_mode is False", f.is_text_mode is False)

    text_request = bytes([HOTT_TEXT_MODE_REQUEST_ID, HOTT_EAM_SENSOR_TEXT_ID])
    frames = HoTTParser().feed_bytes(text_request)
    check("Text-mode request frame parses to exactly 1 request frame", len(frames) == 1)
    if frames:
        f = frames[0]
        check("Text-mode request is_text_mode is True", f.is_text_mode is True)

    no_sensor_request = bytes([HOTT_BINARY_MODE_REQUEST_ID, HOTT_TELEMETRY_NO_SENSOR_ID])
    frames = HoTTParser().feed_bytes(no_sensor_request)
    check("No-sensor-present request frame still parses (address=0x80)", len(frames) == 1)

    print("\n=== Corruption / resync handling ===")
    corrupted = bytearray(eam_frame_bytes)
    corrupted[-1] ^= 0xFF  # flip every bit of the checksum byte
    frames = HoTTParser().feed_bytes(bytes(corrupted))
    check("Frame with corrupted checksum is REJECTED (not ingested)", len(frames) == 0)

    corrupted2 = bytearray(eam_frame_bytes)
    corrupted2[10] ^= 0x01  # flip a body byte; checksum no longer matches
    frames = HoTTParser().feed_bytes(bytes(corrupted2))
    check("Frame with corrupted body byte is REJECTED (not ingested)", len(frames) == 0)

    garbage_then_real = b"\x00\x01\x02\xff\xff" + eam_frame_bytes
    frames = HoTTParser().feed_bytes(garbage_then_real)
    check("Parser resyncs past leading garbage bytes to find the real frame", len(frames) == 1)

    # Streamed one byte at a time (as pyserial delivers in practice) must
    # behave identically to feed_bytes() on the whole buffer at once.
    parser = HoTTParser()
    streamed = []
    for b in gam_frame_bytes:
        streamed.extend(parser.feed_bytes(bytes([b])))
    check("Byte-at-a-time feed matches whole-buffer feed_bytes",
          len(streamed) == 1 and streamed[0].raw == gam_frame_bytes)

    # Mixed stream: request immediately followed by its sensor's response,
    # exactly as seen on a real half-duplex HoTT wire.
    mixed = binary_request + eam_frame_bytes
    frames = HoTTParser().feed_bytes(mixed)
    check("Mixed request+response stream yields exactly 2 frames", len(frames) == 2)
    if len(frames) == 2:
        check("First of mixed stream is the request", isinstance(frames[0], HoTTRequestFrame))
        check("Second of mixed stream is the response", isinstance(frames[1], HoTTResponseFrame))

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


# =============================================================================
# RX-only serial bridge -- STAGED, UNTESTED against real hardware, HARDWARE-
# BLOCKED (see module docstring HARDWARE STATUS). Follows crsf_parser.py's /
# mavlink_sniffer.py's exact conventions: CLI args / env vars for console
# URL + creds, login(), a read-only loop, checksum-gated ingest with
# confidence_type="protocol_verified", no synthetic fallback of any kind.
# =============================================================================

def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                       json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5) -> "requests.Response":
    """POST to the backend, auto-recovering from an expired JWT by re-login
    ONCE and retrying. Duplicated per-file (same convention as login() above
    -- no shared auth module exists in field-bridge/); canonical copy +
    rationale lives in hackrf_rx.py. This bridge runs Restart=always
    indefinitely, so without this it would silently and permanently 401 on
    every ingest past the backend's 12h JWT TTL (create_access_token() in
    backend/server.py) until manually restarted -- task #150."""
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", "graupner_hott_parser")
    r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        print(f"[auth] 401 from POST {path} -- token expired, re-authenticating as {email}",
              file=sys.stderr)
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[auth] re-login failed ({e})", file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
        if r.status_code == 401:
            print(f"[auth] still 401 for POST {path} after re-authenticating -- real auth "
                  f"problem (check credentials for {email}), not just an expired token.",
                  file=sys.stderr)
    return r


class HoTTSerialBridge:
    """RX-only HoTT serial listener. Opens a real serial device, feeds every
    byte read to HoTTParser, and posts a detection only for sensor RESPONSE
    frames whose checksum genuinely validates. Never writes to the serial
    port (HoTT is half-duplex single-wire; this bridge only ever reads).
    """

    def __init__(self, console_url: str, headers: dict, serial_device: str,
                 baud: int = HOTT_BAUDRATE, repost_interval_s: float = 10.0,
                 email: str = "", password: str = "") -> None:
        self.console_url = console_url
        self.headers = headers
        self.email = email
        self.password = password
        self.serial_device = serial_device
        self.baud = baud
        self.repost_interval_s = repost_interval_s
        self.parser = HoTTParser()
        self._last_posted = 0.0
        self._request_count = 0

    def _open_serial(self):
        try:
            import serial  # pyserial
        except ImportError:
            print("ERROR: pyserial not installed. pip install pyserial (see "
                  "field-bridge/requirements.txt).", file=sys.stderr)
            sys.exit(1)
        # timeout=1 so the read loop below can't block indefinitely if the
        # link goes quiet -- same read-loop shape as crsf_parser.py/
        # mavlink_sniffer.py's recv_match(timeout=1).
        return serial.Serial(self.serial_device, self.baud, timeout=1)

    def _ingest(self, frame) -> None:
        if isinstance(frame, HoTTRequestFrame):
            # Real protocol carries no checksum on the request -- nothing to
            # verify, so this only updates internal bookkeeping, never posts
            # a "protocol_verified" detection off a request alone.
            self._request_count += 1
            return

        now = time.time()
        if now - self._last_posted < self.repost_interval_s:
            return

        if frame.is_text_mode:
            type_name = "TEXTMODE"
            fields = {"rows": decode_text_rows(frame)}
        else:
            type_name = SENSOR_TYPE_NAMES.get(frame.sensor_type_id, f"0x{frame.sensor_type_id:02X}")
            try:
                fields = parse_response_fields(frame)
            except ValueError as e:
                fields = {"decode_error": str(e)}

        detection = {
            "callsign": f"HOTT-{self.serial_device}",
            "model": "Graupner HoTT telemetry sensor (protocol-confirmed)",
            "protocol": "HOTT",
            "threat_level": "MEDIUM",
            # HoTT is a wired serial link, not an over-the-air RF capture --
            # there is no measured center_freq/RSSI at this layer, matching
            # crsf_parser.py's / mavlink_sniffer.py's same honest omission.
            "encrypted": False,
            "source": "HOTT_SERIAL",
            "frame_type": type_name,
            "fields": fields,
            "notes": ("Protocol-level HoTT checksum-verified frame decode, not an "
                      "RF-signature heuristic. Checksum is a mod-256 arithmetic "
                      "sum, not a CRC -- see module docstring."),
            "confidence_type": "protocol_verified",  # checksum passed -- pass/fail, no probability to report
        }
        try:
            r = _post_with_reauth(self.console_url, "/api/detections/ingest",
                                   detection, self.headers, self.email, self.password,
                                   timeout=5)
            r.raise_for_status()
            self._last_posted = now
            print(f"[graupner_hott_parser] CONFIRMED HoTT frame: type={type_name} "
                  f"(checksum verified) -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"[graupner_hott_parser] detection ingest failed: {e}", file=sys.stderr)

    def run_forever(self) -> None:
        ser = self._open_serial()
        print(f"[graupner_hott_parser] Opened {self.serial_device} @ {self.baud} baud in "
              f"RX-ONLY mode (no transmission will occur; HoTT is half-duplex "
              f"single-wire and this bridge only ever reads).")
        print("[graupner_hott_parser] Passive HoTT frame listener running. Posts a "
              "detection ONLY when a real checksum-verified HoTT sensor RESPONSE "
              "frame is decoded off the wire. If no such traffic ever arrives, "
              "nothing is ever posted -- there is no synthetic fallback, by design.")
        while True:
            try:
                chunk = ser.read(256)  # blocks up to timeout=1s, never portMAX_DELAY-style forever
            except Exception as e:
                print(f"[graupner_hott_parser] WARN: serial read error: {e}", file=sys.stderr)
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
                          "HARDWARE STATUS. This module is HARDWARE-BLOCKED for "
                          "live use: no compatible Graupner HoTT receiver/sensor "
                          "was available in this session.")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--serial", default=os.environ.get("HOTT_SNIFF_SERIAL", "/dev/ttyUSB0"),
                     help="HoTT receiver/sensor serial device (READ-ONLY; never "
                          "transmits). REQUIRES real Graupner HoTT-capable hardware "
                          "wired to this port -- see module docstring HARDWARE "
                          "STATUS; none was available in this session.")
    ap.add_argument("--baud", type=int,
                     default=int(os.environ.get("HOTT_SNIFF_BAUD", str(HOTT_BAUDRATE))))
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

    print("[graupner_hott_parser] HARDWARE-BLOCKED: no compatible Graupner HoTT "
          "receiver/sensor hardware was available in this session, so this bridge "
          "has never been run against real HoTT hardware. It is real, tested "
          "PARSING logic (run with --self-test to verify) wired to a real, "
          "untested serial listener. See module docstring HARDWARE STATUS before "
          "trusting any output.", file=sys.stderr)

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    bridge = HoTTSerialBridge(args.console_url, headers, args.serial, args.baud,
                               email=args.email, password=args.password)
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
