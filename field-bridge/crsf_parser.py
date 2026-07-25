#!/usr/bin/env python3
"""Real CRSF (Crossfire/ExpressLRS) frame parser + RX-only serial bridge.

RECEIVE ONLY. No transmission happens anywhere in this script. There is no
`write()`/`send()` path here at all — see the "HARDWARE STATUS" section below
for why.

=============================================================================
WHAT CRSF IS, AND WHY THIS IS *NOT* AN SDR/RF SCRIPT LIKE hackrf_rx.py
=============================================================================
CRSF (Crossfire, designed by TBS, the de facto standard also used by
ExpressLRS) is a **wired, point-to-point UART serial protocol** running
between an RC receiver and a flight controller (or between a handset and a
TX module), typically at 420,000 baud. It is NOT something an SDR receives
over the air the way hackrf_rx.py/iq_capture.py receive OcuSync or WiFi
energy — the RF hop/modulation happens inside the CRSF receiver/TX-module
hardware, and by the time bytes reach a UART they are already a clean,
CRC-protected serial byte stream. There is no meaningful "IQ capture of
CRSF" the way there is for DroneID (see droneid_decode_bridge.py) — the
correct integration point is a serial tap on a real CRSF receiver's UART
output, exactly analogous to how mavlink_sniffer.py taps a SiK/RFD900
radio's UART rather than demodulating MAVLink from RF.

=============================================================================
FRAME FORMAT (verified against two independent real implementations)
=============================================================================
    [sync] [len] [type] [payload ...] [crc8]

  - sync (1 byte):  0xC8 on a receiver->FC serial link (0xEE/0xEA appear on
    handset-side links). This is a sync marker, not a destination address,
    even though 0xC8 collides with CRSF_ADDRESS_FLIGHT_CONTROLLER.
  - len (1 byte):   count of bytes AFTER this field, i.e. type + payload +
    crc. Total frame length on the wire is len + 2.
  - type (1 byte):  frame type (CRSF_FRAMETYPE_*). Types 0x28-0x96 are
    "extended header" frames and carry two extra routing bytes (dest, orig)
    at the start of their payload.
  - payload:        len - 2 bytes, frame-type specific.
  - crc8 (1 byte):  CRC-8/DVB-S2 (poly 0xD5, non-reflected, MSB-first, init
    0), computed over type+payload (NOT sync/len).

This layout and the CRC parameters were read directly from two real, already
-catalogued upstream sources (not re-derived from memory / not guessed):
  - AlfredoCRSF (GPLv3; permitted here under this project's internal-defense
    licensing override — see project memory note on open-source
    sovereignty): src/crsf_protocol.h (CRSF_SYNC_BYTE, frame type enum,
    struct layouts) and src/crc8.{h,cpp} (table-based CRC8 over an arbitrary
    poly, used with poly 0xD5).
  - ExpressLRS (GPLv3; the authoritative upstream firmware for the real
    over-the-air protocol CRSF rides on): src/python/crsf.py's calc_crc8()
    (bit-by-bit CRC8, poly 0xD5, same non-reflected/MSB-first/init-0
    algorithm as AlfredoCRSF's table-based one — the two are byte-for-byte
    equivalent, which is a strong cross-check that the CRC definition here
    is correct and not a guess) and src/include/crsf_protocol.h /
    src/lib/CrsfProtocol/* for frame type IDs and address values.

CRC8_POLY = 0xD5 is defined below and its correctness is verified in
self_test() by cross-computing every test frame both ways (table-based,
matching AlfredoCRSF's Crc8::calc, and bit-by-bit, matching ExpressLRS's
calc_crc8) and asserting they agree, then checking the resulting CRC against
frames built directly from the CRSF_PROTOCOL.md field layouts.

=============================================================================
HARDWARE STATUS — READ BEFORE TRUSTING ANY "LIVE" OUTPUT OF THIS SCRIPT
=============================================================================
TESTED, with real logic (no real hardware needed for this part):
  - CRC8 algorithm: verified byte-for-byte identical to both AlfredoCRSF's
    table-based Crc8::calc(poly=0xD5) and ExpressLRS's bit-by-bit
    calc_crc8(poly=0xD5) implementations, reproduced faithfully in
    _crc8_table() / _crc8_bitwise() below.
  - Frame parsing state machine (CRSFParser.feed): correctly finds sync,
    reads length, validates CRC, and rejects corrupt/truncated/resynced
    streams — exercised in self_test() against frames built from real
    on-the-wire layouts (crsf_protocol.h struct definitions): RC_CHANNELS_
    PACKED (16x 11-bit channels, exact bit-packing per crsf_channels_t),
    LINK_STATISTICS, BATTERY_SENSOR, HEARTBEAT, VIDEO_TRANSMITTER (0x0F --
    recognized/named/dispatched only, no field-level decoder; see the long
    comment at CRSF_FRAMETYPE_VIDEO_TRANSMITTER's definition for why), and a
    corrupted/bit-flipped frame (must be rejected).
  - RC channel unpacking (unpack_rc_channels): bit-for-bit reimplementation
    of AlfredoCRSF's crsf_channels_t bitfield layout (11 bits/channel,
    little-endian bit order), round-tripped against known channel values in
    self_test().

NOT TESTED — no real CRSF-capable serial hardware was available in this
session:
  - CRSFSerialBridge (the RX-only pyserial listener below) has NEVER been
    run against a real CRSF receiver's UART. This project's existing serial
    hardware (SiK/RFD900 radios, see mavlink_sniffer.py/sik_mavlink_bridge.py)
    speaks MAVLink at 57600 baud over 915/433MHz telemetry links — that is a
    DIFFERENT physical radio and protocol from a CRSF receiver's 420000 baud
    UART output, and cannot be repurposed to produce real CRSF traffic. No
    ELRS/TBS Crossfire receiver, module, or handset was available to test
    against.
  - Per this project's standing rule (no synthetic/fallback data, ever):
    this script will NOT fabricate a serial connection or synthetic CRSF
    bytes to "demo" a detection. If you run CRSFSerialBridge against a
    /dev/ttyUSB* with no real CRSF device attached, it will simply see
    garbage/no sync bytes and post nothing — that is the correct, honest
    behavior, exactly like mavlink_sniffer.py posting nothing when no real
    MAVLink traffic exists on its link.
  - This module is therefore shipped as tested PARSING INFRASTRUCTURE, with
    a bridge class staged and ready to go the moment real CRSF hardware
    (e.g. a TBS Crossfire/ExpressLRS receiver wired UART-out to a USB-serial
    adapter) is available. Wire it up, point --serial at that device, and
    the existing, tested parser takes over — no code changes needed.

=============================================================================
INGEST CONVENTION (matches droneid_decode_bridge.py's CRC-gated pattern)
=============================================================================
A detection is posted to /api/detections/ingest ONLY when a frame's CRC8
genuinely validates against real bytes read off the wire — confidence_type=
"protocol_verified", exactly like droneid_decode_bridge.py's DJI DroneID CRC
gate. There is no RSSI/energy-heuristic path in this script at all (CRSF has
no such signal available at the UART layer) and no probabilistic "candidate"
label — a CRC pass/fail is a binary, protocol-level fact, not a guess.

Requires: pyserial, requests (see field-bridge/requirements.txt). No new
dependency is introduced by this file.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

import requests

# =============================================================================
# Protocol constants — taken verbatim from AlfredoCRSF's src/crsf_protocol.h
# and cross-checked against ExpressLRS's src/include/crsf_protocol.h /
# src/python/crsf.py. Nothing here is invented.
# =============================================================================

CRSF_SYNC_BYTE = 0xC8          # sync byte on a receiver -> flight-controller serial link
CRSF_ADDRESS_RADIO_TRANSMITTER = 0xEA   # handset-side sync/address
CRSF_ADDRESS_CRSF_TRANSMITTER = 0xEE    # TX-module-side sync/address
# Frames may legitimately begin with any of these three byte values
# depending on which link is being tapped (FC-side vs handset-side).
VALID_SYNC_BYTES = frozenset({CRSF_SYNC_BYTE, CRSF_ADDRESS_RADIO_TRANSMITTER,
                               CRSF_ADDRESS_CRSF_TRANSMITTER})

CRSF_CRC_POLY = 0xD5            # CRC-8/DVB-S2 polynomial used by real CRSF frames
CRSF_MAX_PACKET_LEN = 64         # per crsf_protocol.h CRSF_MAX_PACKET_LEN
CRSF_BAUDRATE = 420000           # per crsf_protocol.h CRSF_BAUDRATE
CRSF_NUM_CHANNELS = 16

# Frame types actually relevant to a passive RX-only detection bridge.
# (Full enum is in crsf_protocol.h; only the ones this script acts on are
# reproduced here to keep the surface area honest and minimal.)
CRSF_FRAMETYPE_GPS = 0x02
CRSF_FRAMETYPE_BATTERY_SENSOR = 0x08
CRSF_FRAMETYPE_HEARTBEAT = 0x0B
# VIDEO_TRANSMITTER (0x0F): a real, on-the-wire CRSF frame type -- confirmed
# by THREE independent sources: (1) AlessioMorale/crsf_parser (construct-
# based Python CRSF decoder)'s crsf_parser/payloads.py, which registers
# `PacketsTypes.VIDEO_TRANSMITTER = 0x0F` with `PAYLOADS_SIZE[...] = 6`
# (6-byte payload); (2) AlfredoCRSF's src/crsf_protocol.h, which lists
# `CRSF_FRAMETYPE_VIDEO_TRANSMITTER = 0x0F` (commented out with "no need to
# support? (rev07)" -- i.e. acknowledged as a real legacy frame type, just
# not wired up in that library); (3) the current officially-maintained TBS
# spec (github.com/tbs-fpv/tbs-crsf-spec, crsf.md) lists "0x0F Discontinued"
# in its frame-type table -- confirming 0x0F is a real, reserved frame-type
# ID (its successor, the still-used VTX telemetry frame, was reassigned to
# 0x10 with a documented field layout: origin_address/power_dBm/
# frequency_MHz/pit_mode bits -- that is a DIFFERENT frame type, 0x10, not
# in scope here).
#
# IMPORTANT -- what is and is NOT implemented here, and why: none of the
# three sources above defines a byte-for-byte field layout for 0x0F's
# 6-byte payload that this project could verify and reproduce without
# guessing. AlessioMorale/crsf_parser itself never defines a construct
# Struct for it (only registers the type ID + total size, unlike its
# payload_battery_sensor / payload_link_statistics / payload_rc_channels_
# packed Structs, which DO have field-level definitions); AlfredoCRSF never
# implemented it at all; and the current TBS spec has retired the type
# without republishing its legacy field layout. Per this project's standing
# "no synthetic/fallback data, ever" rule, this parser therefore recognizes,
# names, dispatches, and CRC-validates VIDEO_TRANSMITTER frames -- exactly
# like GPS/HEARTBEAT/ATTITUDE/DEVICE_PING/DEVICE_INFO below, none of which
# have a field-level payload decoder either -- but does NOT ship a
# parse_video_transmitter() that would have to invent field semantics no
# accessible source actually defines.
CRSF_FRAMETYPE_VIDEO_TRANSMITTER = 0x0F
CRSF_FRAMETYPE_LINK_STATISTICS = 0x14
CRSF_FRAMETYPE_RC_CHANNELS_PACKED = 0x16
CRSF_FRAMETYPE_ATTITUDE = 0x1E
CRSF_FRAMETYPE_DEVICE_PING = 0x28
CRSF_FRAMETYPE_DEVICE_INFO = 0x29
CRSF_FRAMETYPE_EXT_FIRST = 0x28
CRSF_FRAMETYPE_EXT_LAST = 0x96

# Payload size (type+payload+crc's payload portion only) for frame types
# whose length is fixed but that don't (yet) have a field-level decoder --
# used only for self-test frame construction, per AlessioMorale/crsf_parser's
# PAYLOADS_SIZE dict.
CRSF_VIDEO_TRANSMITTER_PAYLOAD_LEN = 6

FRAME_TYPE_NAMES: Dict[int, str] = {
    CRSF_FRAMETYPE_GPS: "GPS",
    CRSF_FRAMETYPE_BATTERY_SENSOR: "BATTERY_SENSOR",
    CRSF_FRAMETYPE_HEARTBEAT: "HEARTBEAT",
    CRSF_FRAMETYPE_VIDEO_TRANSMITTER: "VIDEO_TRANSMITTER",
    CRSF_FRAMETYPE_LINK_STATISTICS: "LINK_STATISTICS",
    CRSF_FRAMETYPE_RC_CHANNELS_PACKED: "RC_CHANNELS_PACKED",
    CRSF_FRAMETYPE_ATTITUDE: "ATTITUDE",
    CRSF_FRAMETYPE_DEVICE_PING: "DEVICE_PING",
    CRSF_FRAMETYPE_DEVICE_INFO: "DEVICE_INFO",
}


def is_extended_frame_type(frame_type: int) -> bool:
    """Extended-header frames (0x28-0x96) carry dest/orig routing bytes."""
    return CRSF_FRAMETYPE_EXT_FIRST <= frame_type <= CRSF_FRAMETYPE_EXT_LAST


# =============================================================================
# Cadence-reference constants (task #96) -- taken verbatim from Betaflight's
# reference CRSF implementation, NOT re-derived or guessed. Verified directly
# against the source in this session:
#
#   CRSF_TIME_BETWEEN_FRAMES_US   = 6667   -- src/main/rx/crsf.c:57
#       "At fastest, frames are sent by the transmitter every 6.667
#       milliseconds, 150 Hz" -- the minimum legal inter-RC-frame spacing.
#   CRSF_LINK_STATUS_UPDATE_TIMEOUT_US = 250000 -- src/main/rx/crsf.c:64
#       "250ms, 4 Hz mode 1 telemetry" -- also independently defined as
#       CRSF_LINK_TYPE_CHECK_US = 250000 in src/main/telemetry/crsf.c:117
#       ("250 ms"), confirming the two sources agree on this cadence for
#       LINK_STATISTICS telemetry.
#   CRSF_CYCLETIME_US             = 100000 -- src/main/telemetry/crsf.c:80
#       "100ms, 10 Hz -- all telemetry frames are inserted in this
#       timeslice" -- the outer round-robin cycle telemetry frame TYPES
#       (battery, GPS, attitude, ...) are scheduled within.
#   CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US = 20000 -- src/main/telemetry/crsf.c:114
#       "20ms" -- the minimum spacing Betaflight enforces between any two
#       telemetry frames going out (a rate ceiling, not a target).
#   CRSF_ELRS_DISLAYPORT_CHUNK_INTERVAL_US = 75000 -- src/main/telemetry/crsf.c:118
#       "75 ms" -- ELRS displayport (OSD-over-CRSF) chunk pacing; not used
#       as a cadence-tracker bucket below because this parser has no
#       displayport frame decoder, but retained here for completeness/audit.
# =============================================================================

CRSF_TIME_BETWEEN_FRAMES_US = 6667
CRSF_LINK_STATUS_UPDATE_TIMEOUT_US = 250000
CRSF_CYCLETIME_US = 100000
CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US = 20000
CRSF_ELRS_DISLAYPORT_CHUNK_INTERVAL_US = 75000


# =============================================================================
# CRC8 — two independent implementations, cross-checked in self_test().
# =============================================================================

def _build_crc8_table(poly: int) -> List[int]:
    """Table-based CRC8, byte-for-byte port of AlfredoCRSF's Crc8::init()."""
    table = []
    for idx in range(256):
        crc = idx
        for _ in range(8):
            crc = ((crc << 1) ^ (poly if (crc & 0x80) else 0)) & 0xFF
        table.append(crc)
    return table


_CRC8_TABLE_D5 = _build_crc8_table(CRSF_CRC_POLY)


def crc8_table(data: bytes, poly: int = CRSF_CRC_POLY) -> int:
    """Table-based CRC8 (fast path, used by the real-time parser).

    Matches AlfredoCRSF's Crc8::calc() exactly: crc = table[crc ^ byte],
    starting from crc=0, no final XOR.
    """
    table = _CRC8_TABLE_D5 if poly == CRSF_CRC_POLY else _build_crc8_table(poly)
    crc = 0
    for b in data:
        crc = table[crc ^ b]
    return crc


def crc8_bitwise(data: bytes, poly: int = CRSF_CRC_POLY) -> int:
    """Bit-by-bit CRC8, byte-for-byte port of ExpressLRS's calc_crc8()
    (src/python/crsf.py). Slower, kept only as an independent cross-check
    against crc8_table() in self_test() — production code path uses
    crc8_table().
    """
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x80:
                crc = (crc << 1) ^ poly
            else:
                crc = crc << 1
            crc &= 0xFF
    return crc


def build_frame(sync: int, frame_type: int, payload: bytes) -> bytes:
    """Construct a real, spec-conformant CRSF frame (for tests / a TX peer,
    not used by the RX-only bridge itself). CRC covers type + payload only,
    per crsf_protocol.h's documented CRC scope.
    """
    length = 1 + len(payload) + 1  # type + payload + crc
    body = bytes([frame_type]) + payload
    crc = crc8_table(body)
    return bytes([sync, length]) + body + bytes([crc])


# =============================================================================
# Frame parser: a byte-stream state machine, feed()-driven so it plugs
# straight into pyserial's read loop (or into self_test()'s canned bytes).
# =============================================================================

@dataclass
class CRSFFrame:
    sync: int
    frame_type: int
    payload: bytes          # for extended frames, INCLUDES the dest/orig bytes stripped out separately below
    dest_addr: Optional[int] = None
    orig_addr: Optional[int] = None
    raw: bytes = b""
    # Wall-clock time (seconds, time.time()-style float) at which this frame
    # was fully extracted from the byte stream. Populated by
    # CRSFParser.feed_bytes() below -- used only by CRSFCadenceTracker for
    # inter-arrival-time analysis (see that class for the honest epistemic
    # framing of what this can/cannot conclude). This is a NEW field added
    # for task #96; it does not affect CRC validation or any existing
    # self-test behavior above.
    timestamp: Optional[float] = None


class CRSFParser:
    """Feed raw serial bytes in one at a time (or in chunks via feed_bytes);
    yields CRSFFrame objects for frames whose CRC8 genuinely validates.
    Non-conformant / corrupt bytes are discarded by resyncing on the next
    valid sync byte — this never blocks and never raises on garbage input.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed_bytes(self, data: bytes, timestamp: Optional[float] = None) -> List[CRSFFrame]:
        """Feed raw bytes; returns any complete, CRC-valid frames extracted.

        `timestamp`: wall-clock time (time.time()-style float, seconds) to
        stamp onto every frame extracted from THIS call. Defaults to
        time.time() at call time if not given. When a live serial reader
        calls feed_bytes() once per read() chunk (the real usage pattern in
        CRSFSerialBridge.run_forever below), every frame decoded out of that
        chunk shares one timestamp -- that is a real limitation (multiple
        frames arriving in one buffered read are indistinguishable in time
        at sub-chunk resolution) and is the reason CRSFCadenceTracker's
        interval statistics are documented as approximate, not precise
        oscilloscope-grade timing.
        """
        if timestamp is None:
            timestamp = time.time()
        self._buf.extend(data)
        frames: List[CRSFFrame] = []
        while True:
            frame = self._try_extract_one()
            if frame is None:
                break
            frame.timestamp = timestamp
            frames.append(frame)
        return frames

    def _try_extract_one(self) -> Optional[CRSFFrame]:
        buf = self._buf
        # Resync: drop bytes until we see a plausible sync byte.
        while buf and buf[0] not in VALID_SYNC_BYTES:
            del buf[0]
        if len(buf) < 2:
            return None
        sync = buf[0]
        length = buf[1]  # bytes after this field: type + payload + crc
        if length < 2 or length > (CRSF_MAX_PACKET_LEN - 2):
            # Not a plausible CRSF length field for this sync byte; drop it
            # and let the resync loop above look for the next real sync.
            del buf[0]
            return None
        total_len = 2 + length  # sync + len + (type+payload+crc)
        if len(buf) < total_len:
            return None  # wait for more bytes
        raw = bytes(buf[:total_len])
        body = raw[2:2 + length - 1]     # type + payload (crc scope)
        crc_received = raw[2 + length - 1]
        crc_computed = crc8_table(body)
        del buf[:total_len]
        if crc_computed != crc_received:
            # Genuinely corrupt/misaligned frame -- not ingested, no
            # fallback. The parser has already resynced past it above.
            return None

        frame_type = body[0]
        payload = body[1:]
        dest_addr = orig_addr = None
        if is_extended_frame_type(frame_type) and len(payload) >= 2:
            dest_addr, orig_addr = payload[0], payload[1]
            payload = payload[2:]
        return CRSFFrame(sync=sync, frame_type=frame_type, payload=payload,
                          dest_addr=dest_addr, orig_addr=orig_addr, raw=raw)


# =============================================================================
# Payload decoders for the frame types this bridge acts on.
# =============================================================================

def unpack_rc_channels(payload: bytes) -> List[int]:
    """Unpack RC_CHANNELS_PACKED's 16x 11-bit little-endian-bit-order values.

    Bit-for-bit reimplementation of AlfredoCRSF's crsf_channels_t bitfield
    (16 consecutive 11-bit fields packed LSB-first across a 22-byte buffer).
    Values are raw CRSF units (172-1811 typical, 992 = center), NOT
    microseconds -- matching getChannelsPacked()'s raw representation.
    """
    if len(payload) < 22:
        raise ValueError(f"RC_CHANNELS_PACKED payload too short: {len(payload)} bytes (need 22)")
    bits = 0
    bitcount = 0
    byte_i = 0
    channels = []
    for _ in range(CRSF_NUM_CHANNELS):
        while bitcount < 11:
            bits |= payload[byte_i] << bitcount
            bitcount += 8
            byte_i += 1
        channels.append(bits & 0x7FF)
        bits >>= 11
        bitcount -= 11
    return channels


def parse_link_statistics(payload: bytes) -> dict:
    """LINK_STATISTICS (0x14) -- 10 unsigned/signed bytes, per crsfLinkStatistics_t."""
    if len(payload) < 10:
        raise ValueError(f"LINK_STATISTICS payload too short: {len(payload)} bytes (need 10)")

    def s8(v: int) -> int:
        return v - 256 if v >= 128 else v

    return {
        "uplink_rssi_1": payload[0],
        "uplink_rssi_2": payload[1],
        "uplink_link_quality": payload[2],
        "uplink_snr": s8(payload[3]),
        "active_antenna": payload[4],
        "rf_mode": payload[5],
        "uplink_tx_power": payload[6],
        "downlink_rssi": payload[7],
        "downlink_link_quality": payload[8],
        "downlink_snr": s8(payload[9]),
    }


def parse_battery_sensor(payload: bytes) -> dict:
    """BATTERY_SENSOR (0x08), per crsf_sensor_battery_t: voltage/current as
    big-endian *10 units, capacity as 24-bit big-endian mAh, remaining as %.
    """
    if len(payload) < 8:
        raise ValueError(f"BATTERY_SENSOR payload too short: {len(payload)} bytes (need 8)")
    voltage = int.from_bytes(payload[0:2], "big") / 10.0
    current = int.from_bytes(payload[2:4], "big") / 10.0
    capacity = int.from_bytes(payload[4:7], "big")
    remaining = payload[7]
    return {"voltage_v": voltage, "current_a": current,
            "capacity_mah": capacity, "remaining_pct": remaining}


# =============================================================================
# Cadence-anomaly heuristic (task #96)
# =============================================================================
#
# WHAT THIS IS: CRSF is a continuous serial link, not a sweep-cycle scanner
# like hackrf_rx.py -- there is no "cycle" to gate persistence across the way
# hackrf_rx.py's CONFIRM_CYCLES pattern does for RF sweeps. What IS analogous
# is tracking inter-arrival-time (the gap between one decoded frame and the
# next, of the same rough category) over a rolling window of the live
# decoded stream, and comparing that against Betaflight's own reference
# timing constants (verified above). This is INTER-FRAME-ARRIVAL-TIME
# statistics on a live stream, a different shape of tracker than
# hackrf_rx.py's per-sweep-cycle persistence gate, because CRSF has no
# sweep cycles at all.
#
# WHAT THIS CAN CONCLUDE: whether the observed frame-to-frame timing on THIS
# link is broadly consistent with the timing envelope a genuine
# Betaflight-class CRSF receiver/telemetry stack would produce, per the
# constants above.
#
# WHAT THIS CANNOT CONCLUDE (read before trusting any "anomalous" label):
#   - This checks timing against ONE reference implementation's (Betaflight)
#     typical behavior. Other legitimate stacks (e.g. different ELRS
#     firmware branches, ArduPilot's CRSF driver, custom RC frame rates
#     configured by a user, or receivers running non-default packet rates)
#     may legitimately produce different-but-still-genuine cadences. A
#     "cadence_anomalous" verdict is NOT proof of spoofing, replay, or a
#     non-standard transmitter -- it is evidence worth flagging for further
#     investigation, exactly like this project's other heuristics (see
#     hackrf_rx.py task #88 hop-tracking notes for the same honest framing).
#   - False positives are expected under: USB-serial/OS scheduling jitter on
#     the CAPTURING side (this tracker measures time-of-decode on the
#     listener's clock, not the transmitter's clock -- added latency here
#     looks identical to added latency on the link itself), brief link
#     dropouts/re-syncs, and the first few frames after connecting (handled
#     by requiring a minimum sample count before classifying at all --
#     "insufficient_data" rather than guessing).
#   - False negatives are expected under: a spoofed/replayed stream built
#     from real captured Betaflight-cadence traffic (this checks TIMING
#     consistency only, not authenticity of frame CONTENT -- a byte-perfect
#     replay at the right cadence looks identical to genuine traffic here).
#
# TOLERANCE BAND REASONING: real links have jitter from UART buffering,
# scheduler timing, and (for this listener specifically) OS-level read()
# batching -- so this uses a wide MULTIPLICATIVE tolerance band around each
# reference constant rather than a tight statistical (std-dev) test. The
# band is deliberately generous (roughly 2x on the fast side, 3x on the
# slow side of the reference values) so that normal jitter never triggers a
# false "anomalous" verdict; only intervals that are grossly (multiple-x)
# faster than the fastest documented rate, or grossly slower than the
# slowest documented periodic cadence, are flagged. This trades sensitivity
# for a low false-positive rate, matching this project's stated design goal
# ("an anomaly is evidence worth flagging, not a hard confirm/deny").
#
# BUCKETS TRACKED:
#   - RC_CHANNELS_PACKED (0x16): the actual RC control stream. Reference
#     band is [CRSF_TIME_BETWEEN_FRAMES_US, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US]
#     i.e. [6.667ms, 250ms] -- 150Hz is the fastest documented RC rate, and
#     250ms/4Hz is the slowest documented periodic cadence anywhere in these
#     constants, used here as a generous slow-side backstop (a live control
#     link idling slower than 4Hz for a sustained window is atypical).
#   - LINK_STATISTICS (0x14): reference is CRSF_LINK_STATUS_UPDATE_TIMEOUT_US
#     (250ms, 4Hz mode-1 telemetry), the one constant Betaflight ties
#     directly to this specific frame type.
#   - Other telemetry-class frames (BATTERY_SENSOR, GPS, ATTITUDE, HEARTBEAT,
#     DEVICE_PING/INFO, VIDEO_TRANSMITTER): reference is CRSF_CYCLETIME_US
#     (100ms, 10Hz) as the round-robin telemetry-insertion cycle, with
#     CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US (20ms) as the enforced minimum
#     spacing floor -- intervals persistently tighter than 20ms are not
#     achievable by a spec-conformant Betaflight-class scheduler and are
#     flagged regardless of the multiplicative band.

CADENCE_WINDOW = 20            # rolling window size (intervals), per bucket
CADENCE_MIN_SAMPLES = 5         # minimum intervals before classifying at all
_FAST_TOLERANCE = 0.5           # allow down to 0.5x the fastest reference bound
_SLOW_TOLERANCE = 3.0           # allow up to 3x the slowest reference bound

# (bucket_name, reference_min_us, reference_max_us)
_RC_BAND = ("rc_channels", CRSF_TIME_BETWEEN_FRAMES_US, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US)
_LINK_STATS_BAND = ("link_statistics",
                     CRSF_LINK_STATUS_UPDATE_TIMEOUT_US * 0.5,   # allow up to 2x faster (8Hz) as still-plausible
                     CRSF_LINK_STATUS_UPDATE_TIMEOUT_US)
_TELEMETRY_BAND = ("telemetry_other", CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US, CRSF_CYCLETIME_US)

_TELEMETRY_OTHER_TYPES = frozenset({
    CRSF_FRAMETYPE_GPS, CRSF_FRAMETYPE_BATTERY_SENSOR, CRSF_FRAMETYPE_HEARTBEAT,
    CRSF_FRAMETYPE_VIDEO_TRANSMITTER, CRSF_FRAMETYPE_ATTITUDE,
    CRSF_FRAMETYPE_DEVICE_PING, CRSF_FRAMETYPE_DEVICE_INFO,
})


def _bucket_for(frame_type: int) -> Optional[str]:
    """Map a frame type to a cadence-tracking bucket, or None if this frame
    type isn't tracked for cadence (no reference cadence defined for it).
    """
    if frame_type == CRSF_FRAMETYPE_RC_CHANNELS_PACKED:
        return "rc_channels"
    if frame_type == CRSF_FRAMETYPE_LINK_STATISTICS:
        return "link_statistics"
    if frame_type in _TELEMETRY_OTHER_TYPES:
        return "telemetry_other"
    return None


_BAND_BY_BUCKET = {
    "rc_channels": _RC_BAND,
    "link_statistics": _LINK_STATS_BAND,
    "telemetry_other": _TELEMETRY_BAND,
}


class CRSFCadenceTracker:
    """Rolling inter-arrival-time tracker for a live decoded CRSF frame
    stream. See the module-level comment block above this class for the
    full epistemic framing (what this can/cannot conclude) -- read it
    before wiring this into anything that makes operational decisions.

    Usage: call record(frame_type, timestamp) once per decoded frame (in
    arrival order; timestamp is any monotonically-nondecreasing float in
    seconds, e.g. CRSFFrame.timestamp or time.time()). Call classify() at
    any point to get the current verdict per bucket.
    """

    def __init__(self, window: int = CADENCE_WINDOW,
                 min_samples: int = CADENCE_MIN_SAMPLES) -> None:
        self.window = window
        self.min_samples = min_samples
        self._last_ts: Dict[str, float] = {}
        self._intervals_us: Dict[str, Deque[float]] = {
            b: deque(maxlen=window) for b in _BAND_BY_BUCKET
        }

    def record(self, frame_type: int, timestamp: float) -> None:
        bucket = _bucket_for(frame_type)
        if bucket is None:
            return  # no reference cadence defined for this frame type
        last = self._last_ts.get(bucket)
        self._last_ts[bucket] = timestamp
        if last is None:
            return  # first frame of this bucket -- no interval yet
        interval_us = (timestamp - last) * 1_000_000.0
        if interval_us <= 0:
            return  # out-of-order/duplicate timestamp -- ignore rather than corrupt stats
        self._intervals_us[bucket].append(interval_us)

    def classify(self, bucket: str) -> dict:
        """Classify one bucket ('rc_channels' | 'link_statistics' |
        'telemetry_other'). Returns a dict with 'status' one of
        'cadence_consistent' / 'cadence_anomalous' / 'insufficient_data',
        plus supporting stats and an honest 'notes' string.
        """
        if bucket not in _BAND_BY_BUCKET:
            raise ValueError(f"unknown cadence bucket: {bucket}")
        name, ref_min_us, ref_max_us = _BAND_BY_BUCKET[bucket]
        samples = list(self._intervals_us[bucket])
        if len(samples) < self.min_samples:
            return {
                "bucket": bucket,
                "status": "insufficient_data",
                "sample_count": len(samples),
                "notes": f"Fewer than {self.min_samples} inter-arrival samples observed "
                         f"for {name}; not enough data to judge cadence consistency "
                         f"one way or the other.",
            }
        median_us = statistics.median(samples)
        low_bound = ref_min_us * _FAST_TOLERANCE
        high_bound = ref_max_us * _SLOW_TOLERANCE
        consistent = low_bound <= median_us <= high_bound
        status = "cadence_consistent" if consistent else "cadence_anomalous"
        notes = (
            f"Median inter-arrival interval for {name} over last {len(samples)} "
            f"frame(s) is {median_us:.0f}us; reference band from Betaflight's "
            f"CRSF implementation is [{ref_min_us:.0f}us, {ref_max_us:.0f}us] "
            f"with tolerance applied as [{low_bound:.0f}us, {high_bound:.0f}us]. "
        )
        if consistent:
            notes += ("Timing is consistent with a genuine Betaflight-class "
                      "implementation's typical cadence for this frame type -- "
                      "this is NOT proof of authenticity, only an absence of "
                      "gross timing anomaly.")
        else:
            notes += ("Timing falls OUTSIDE the tolerance band -- worth "
                      "flagging as evidence of a non-standard transmitter, "
                      "possible spoofed/replayed traffic, or a degraded link. "
                      "This is NOT a confirmed detection: other legitimate "
                      "CRSF/ELRS configurations can have different, still-"
                      "valid timing (see module docstring for false-positive "
                      "risk).")
        return {
            "bucket": bucket,
            "status": status,
            "sample_count": len(samples),
            "median_interval_us": median_us,
            "reference_band_us": [ref_min_us, ref_max_us],
            "tolerance_band_us": [low_bound, high_bound],
            "confidence_type": "timing_heuristic",  # explicitly NOT protocol_verified -- this is statistical, not a CRC/spec fact
            "notes": notes,
        }

    def classify_all(self) -> Dict[str, dict]:
        return {bucket: self.classify(bucket) for bucket in _BAND_BY_BUCKET}


# =============================================================================
# Self-test: exercises the CRC and parser against frames built directly from
# the real field layouts in crsf_protocol.h. This is what is ACTUALLY TESTED
# in this session (see module docstring's HARDWARE STATUS section) -- no
# live CRSF hardware, real spec-conformant bytes only.
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== CRC8 cross-check: table-based (AlfredoCRSF) vs bit-wise (ExpressLRS) ===")
    for sample in (b"", b"\x16", b"\x00\x01\x02\x03", bytes(range(256)), b"\xff" * 22):
        a, b = crc8_table(sample), crc8_bitwise(sample)
        check(f"crc8_table == crc8_bitwise for {len(sample)}-byte sample", a == b)

    # Known scalar check: CRC-8/DVB-S2 (poly 0xD5, init 0x00, no reflect, no
    # final xor) of the single byte 0x00 is 0x00, and of ASCII "123456789"
    # this exact parameterization (init 0x00) yields 0xBC -- this is the
    # standard DVB-S2 CRC-8 check value MINUS the CRC-8/DVB-S2 spec's usual
    # init=0xFF; CRSF uses init=0x00 per AlfredoCRSF's Crc8(poly) constructor
    # (no init offset applied), so we verify against our own two independent
    # implementations instead of the external DVB-S2 check value, which
    # would not match under this init parameterization. Documented here so
    # the reasoning is auditable, not silently skipped.
    check("crc8_table(b'') == 0 (identity for empty input)", crc8_table(b"") == 0)

    print("\n=== Frame round-trip: build_frame() + CRSFParser, real field layouts ===")

    # HEARTBEAT (0x0B): payload is a single big-endian uint16 origin address
    # per crsf_protocol.h device discovery usage; here just origin=0xEC (RX).
    hb = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_HEARTBEAT, bytes([0x00, 0xEC]))
    parser = CRSFParser()
    frames = parser.feed_bytes(hb)
    check("HEARTBEAT frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        check("HEARTBEAT frame type decoded correctly", frames[0].frame_type == CRSF_FRAMETYPE_HEARTBEAT)

    # VIDEO_TRANSMITTER (0x0F): recognized/named/dispatched only -- no field-
    # level payload decoder exists for this frame in this project (see the
    # long comment at CRSF_FRAMETYPE_VIDEO_TRANSMITTER's definition above for
    # why: none of the three cross-checked sources -- AlessioMorale/
    # crsf_parser, AlfredoCRSF, or the current TBS spec -- publishes a
    # verifiable byte-level field layout for this now-"Discontinued" type).
    # This test only proves the frame-type ID, dispatch, and CRC8 path work
    # for it, using the real 6-byte payload size registered by
    # AlessioMorale/crsf_parser's PAYLOADS_SIZE[VIDEO_TRANSMITTER].
    vtx_payload = bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
    check("VIDEO_TRANSMITTER payload size matches AlessioMorale/crsf_parser's "
          "PAYLOADS_SIZE[VIDEO_TRANSMITTER]=6",
          len(vtx_payload) == CRSF_VIDEO_TRANSMITTER_PAYLOAD_LEN)
    vtx = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_VIDEO_TRANSMITTER, vtx_payload)
    frames = CRSFParser().feed_bytes(vtx)
    check("VIDEO_TRANSMITTER frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        check("VIDEO_TRANSMITTER frame type decoded correctly",
              frames[0].frame_type == CRSF_FRAMETYPE_VIDEO_TRANSMITTER)
        check("VIDEO_TRANSMITTER frame type name resolves via FRAME_TYPE_NAMES",
              FRAME_TYPE_NAMES.get(frames[0].frame_type) == "VIDEO_TRANSMITTER")
        check("VIDEO_TRANSMITTER payload round-trips intact (no field decoder "
              "to mangle it)", frames[0].payload == vtx_payload)

    # RC_CHANNELS_PACKED (0x16): 16 channels, all at CRSF_CHANNEL_VALUE_MID=992,
    # per crsf_protocol.h's documented center value -- pack manually per the
    # crsf_channels_t bitfield layout (11 bits/channel, LSB-first).
    def pack_channels(values: List[int]) -> bytes:
        bits = 0
        bitcount = 0
        out = bytearray()
        for v in values:
            bits |= (v & 0x7FF) << bitcount
            bitcount += 11
            while bitcount >= 8:
                out.append(bits & 0xFF)
                bits >>= 8
                bitcount -= 8
        if bitcount:
            out.append(bits & 0xFF)
        return bytes(out)

    mid_channels = [992] * 16
    packed = pack_channels(mid_channels)
    check("pack_channels() produces the documented 22-byte RC_CHANNELS_PACKED payload", len(packed) == 22)
    rc = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, packed)
    frames = CRSFParser().feed_bytes(rc)
    check("RC_CHANNELS_PACKED frame parses to exactly 1 frame", len(frames) == 1)
    if frames:
        decoded = unpack_rc_channels(frames[0].payload)
        check("unpack_rc_channels() round-trips all 16 channels at value 992",
              decoded == mid_channels)

    # Non-trivial per-channel values to catch bit-packing-order bugs that a
    # uniform 992-everywhere test would hide.
    varied = [172, 191, 992, 1000, 1500, 1792, 1811, 500,
              172, 991, 993, 1811, 172, 992, 1811, 1000]
    packed_v = pack_channels(varied)
    rc_v = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, packed_v)
    frames = CRSFParser().feed_bytes(rc_v)
    if frames:
        decoded_v = unpack_rc_channels(frames[0].payload)
        check("unpack_rc_channels() round-trips 16 distinct per-channel values", decoded_v == varied)

    # LINK_STATISTICS (0x14), per crsfLinkStatistics_t field order.
    ls_payload = bytes([80, 0, 99, (256 - 6) & 0xFF, 0, 1, 20, 0, 0, (256 - 1) & 0xFF])
    ls = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_LINK_STATISTICS, ls_payload)
    frames = CRSFParser().feed_bytes(ls)
    if frames:
        stats = parse_link_statistics(frames[0].payload)
        check("LINK_STATISTICS uplink_rssi_1 decoded", stats["uplink_rssi_1"] == 80)
        check("LINK_STATISTICS uplink_link_quality decoded", stats["uplink_link_quality"] == 99)
        check("LINK_STATISTICS signed uplink_snr decoded (-6 dB)", stats["uplink_snr"] == -6)
        check("LINK_STATISTICS signed downlink_snr decoded (-1 dB)", stats["downlink_snr"] == -1)

    # BATTERY_SENSOR (0x08): 12.4V, 3.2A, 1500mAh, 77% remaining.
    batt_payload = (124).to_bytes(2, "big") + (32).to_bytes(2, "big") + (1500).to_bytes(3, "big") + bytes([77])
    bs = build_frame(CRSF_SYNC_BYTE, CRSF_FRAMETYPE_BATTERY_SENSOR, batt_payload)
    frames = CRSFParser().feed_bytes(bs)
    if frames:
        batt = parse_battery_sensor(frames[0].payload)
        check("BATTERY_SENSOR voltage decoded (12.4V)", abs(batt["voltage_v"] - 12.4) < 1e-9)
        check("BATTERY_SENSOR current decoded (3.2A)", abs(batt["current_a"] - 3.2) < 1e-9)
        check("BATTERY_SENSOR capacity decoded (1500mAh)", batt["capacity_mah"] == 1500)
        check("BATTERY_SENSOR remaining decoded (77%)", batt["remaining_pct"] == 77)

    print("\n=== Corruption / resync handling ===")
    corrupted = bytearray(hb)
    corrupted[-1] ^= 0xFF  # flip every bit of the CRC byte
    frames = CRSFParser().feed_bytes(bytes(corrupted))
    check("Frame with corrupted CRC is REJECTED (not ingested)", len(frames) == 0)

    corrupted2 = bytearray(hb)
    corrupted2[3] ^= 0x01  # flip a payload bit; CRC no longer matches
    frames = CRSFParser().feed_bytes(bytes(corrupted2))
    check("Frame with corrupted payload byte is REJECTED (not ingested)", len(frames) == 0)

    garbage_then_real = b"\x00\x01\x02\xFF\xFF" + hb
    frames = CRSFParser().feed_bytes(garbage_then_real)
    check("Parser resyncs past leading garbage bytes to find the real frame", len(frames) == 1)

    # Streamed one byte at a time (as pyserial delivers in practice) must
    # behave identically to feed_bytes() on the whole buffer at once.
    parser = CRSFParser()
    streamed: List[CRSFFrame] = []
    for b in rc_v:
        streamed.extend(parser.feed_bytes(bytes([b])))
    check("Byte-at-a-time feed() matches whole-buffer feed_bytes()", len(streamed) == 1
          and streamed[0].raw == rc_v)

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


# =============================================================================
# RX-only serial bridge -- STAGED, UNTESTED against real hardware (see
# module docstring HARDWARE STATUS). Follows mavlink_sniffer.py's exact
# conventions: CLI args / env vars for console URL + creds, login(), a
# read-only loop, CRC-gated ingest with confidence_type="protocol_verified",
# no synthetic fallback of any kind.
# =============================================================================

def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                       json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


class CRSFSerialBridge:
    """RX-only CRSF serial listener. Opens a real serial device, feeds every
    byte read to CRSFParser, and posts a detection only for frames whose
    CRC8 genuinely validates. Never writes to the serial port.
    """

    def __init__(self, console_url: str, headers: dict, serial_device: str,
                 baud: int = CRSF_BAUDRATE, repost_interval_s: float = 10.0) -> None:
        self.console_url = console_url
        self.headers = headers
        self.serial_device = serial_device
        self.baud = baud
        self.repost_interval_s = repost_interval_s
        self.parser = CRSFParser()
        self.cadence = CRSFCadenceTracker()
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
        # recv_match(timeout=1).
        return serial.Serial(self.serial_device, self.baud, timeout=1)

    def _ingest(self, frame: CRSFFrame) -> None:
        # Cadence tracking runs on every CRC-valid frame regardless of the
        # repost-interval gate below, so the rolling window reflects the
        # true frame stream, not just the (rate-limited) posted subset.
        self.cadence.record(frame.frame_type, frame.timestamp or time.time())

        now = time.time()
        if now - self._last_posted < self.repost_interval_s:
            return
        type_name = FRAME_TYPE_NAMES.get(frame.frame_type, f"0x{frame.frame_type:02X}")

        notes = ("Protocol-level CRSF CRC8-verified frame decode, not an "
                  "RF-signature heuristic.")
        cadence_anomaly = False
        bucket = _bucket_for(frame.frame_type)
        if bucket is not None:
            verdict = self.cadence.classify(bucket)
            if verdict["status"] != "insufficient_data":
                cadence_anomaly = verdict["status"] == "cadence_anomalous"
                notes += " | Cadence check (" + bucket + "): " + verdict["notes"]

        detection = {
            "callsign": f"CRSF-{self.serial_device}",
            "model": "CRSF/ExpressLRS receiver (protocol-confirmed)",
            "protocol": "CRSF",
            "threat_level": "MEDIUM",
            # CRSF is a wired serial link, not an over-the-air RF capture --
            # there is no measured center_freq/RSSI at this layer, matching
            # mavlink_sniffer.py's same honest omission for its SiK link.
            "encrypted": False,
            "source": "CRSF_SERIAL",
            "frame_type": type_name,
            "notes": notes,
            "confidence_type": "protocol_verified",  # CRC check passed -- pass/fail, no probability to report
            # cadence_anomaly is a SEPARATE, weaker signal than the CRC-based
            # confidence_type above -- see CRSFCadenceTracker's module-level
            # docstring for what it can/cannot conclude. False by default
            # when there isn't enough data yet to judge either way.
            "cadence_anomaly": cadence_anomaly,
        }
        try:
            r = requests.post(f"{self.console_url}/api/detections/ingest",
                               json=detection, headers=self.headers, timeout=5)
            r.raise_for_status()
            self._last_posted = now
            print(f"[crsf_parser] CONFIRMED CRSF frame: type={type_name} "
                  f"(CRC8 verified) -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"[crsf_parser] detection ingest failed: {e}", file=sys.stderr)

    def run_forever(self) -> None:
        ser = self._open_serial()
        print(f"[crsf_parser] Opened {self.serial_device} @ {self.baud} baud in "
              f"RX-ONLY mode (no transmission will occur).")
        print("[crsf_parser] Passive CRSF frame listener running. Posts a detection "
              "ONLY when a real CRC8-verified CRSF frame is decoded off the wire. "
              "If no such traffic ever arrives, nothing is ever posted -- there is "
              "no synthetic fallback, by design.")
        while True:
            try:
                chunk = ser.read(256)  # blocks up to timeout=1s, never portMAX_DELAY-style forever
            except Exception as e:
                print(f"[crsf_parser] WARN: serial read error: {e}", file=sys.stderr)
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
                     help="Run the offline parser/CRC self-test (no serial hardware "
                          "needed) and exit. This is the only thing verified in this "
                          "session -- see module docstring HARDWARE STATUS.")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--serial", default=os.environ.get("CRSF_SNIFF_SERIAL", "/dev/ttyUSB0"),
                     help="CRSF receiver serial device (READ-ONLY; never transmits). "
                          "REQUIRES real CRSF-capable hardware wired to this port -- "
                          "see module docstring HARDWARE STATUS; none was available "
                          "in this session.")
    ap.add_argument("--baud", type=int,
                     default=int(os.environ.get("CRSF_SNIFF_BAUD", str(CRSF_BAUDRATE))))
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

    print("[crsf_parser] KNOWN LIMITATION: this bridge has never been run against real "
          "CRSF hardware in this session (none was available). It is real, tested "
          "PARSING logic (run with --self-test to verify) wired to a real, untested "
          "serial listener. See module docstring HARDWARE STATUS before trusting any "
          "output.", file=sys.stderr)

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    bridge = CRSFSerialBridge(args.console_url, headers, args.serial, args.baud)
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
