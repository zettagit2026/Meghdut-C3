#!/usr/bin/env python3
"""Passive decoder for Parrot's official ARSDK3 network protocol -- pure-Python
re-implementation of the ARNetworkAL frame format and the generic ARCommand
(project/class/command) header, derived directly from Parrot's own
BSD-3-Clause source (`libARNetworkAL`, `libARCommands`, `arsdk-xml`,
Parrot-Developers GitHub org, Copyright (C) 2014 Parrot SA).

RX-ONLY. This module contains no transmit/encode/connect path at all -- only
decode functions for bytes already captured off the wire (e.g. from a pcap of
the drone's Wi-Fi network, or a raw socket in promiscuous/passive mode).
Consistent with this project's standing rule that nothing not explicitly
authorized to transmit is given a transmit path.

=============================================================================
WHICH GENERATION THIS COVERS (VERIFIED, NOT ASSUMED)
=============================================================================
Parrot's drone control history splits into two, protocol-incompatible
generations:

  1. AR.Drone 1 / AR.Drone 2 ("ardrone-opensource", org `parrot-opensource`,
     not `Parrot-Developers`) -- a much older, simple UDP AT-command text
     protocol (`AT*REF`, `AT*PCMD`, ... sent as ASCII strings to UDP port
     5556) plus a separate NAVDATA UDP stream. This repo's GitHub license
     API returns `"license": null` -- NOT verified as BSD or any other OSI
     license in this session, so nothing from it is used or reimplemented
     here. This generation is explicitly OUT OF SCOPE for this module.

  2. Bebop / Bebop 2 / Disco / ANAFI ("ARSDK3" generation, org
     `Parrot-Developers`: `arsdk_manifests`, `libARNetworkAL`,
     `libARCommands`, `arsdk-xml`, `libARController`, ...) -- a binary
     framed protocol over a Wi-Fi TCP+UDP transport, carrying a generic,
     auto-generated command set (`project`/`class`/`command` triplet +
     typed arguments) shared across all products in this generation. THIS
     is the generation this module decodes. It is not AR.Drone-1/2's AT
     protocol, and it is not BLE (ARSDK3 also has a BLE transport variant
     for some MiniDrones, in `libARNetworkAL/Sources/BLE/`, out of scope
     here -- this module covers the Wi-Fi/network transport only, which is
     the one used by Bebop/Bebop2/Disco/ANAFI).

=============================================================================
LICENSE -- VERIFIED, NOT TAKEN ON TRUST FROM AN EARLIER SURVEY
=============================================================================
Checked directly against the GitHub API and raw file contents in this
session (2026-07):
  - `Parrot-Developers/libARNetworkAL` -- GitHub API reports
    `"license": {"key": "bsd-3-clause", ...}`. Its
    `Includes/libARNetworkAL/ARNETWORKAL_Frame.h` (the file defining the
    exact frame struct this module reimplements) carries a literal
    "Copyright (C) 2014 Parrot SA / Redistribution and use in source and
    binary forms, with or without modification, are permitted provided
    that..." header -- the standard BSD-3-Clause text, confirmed byte-for-
    byte against the canonical BSD-3-Clause template.
  - `Parrot-Developers/libARCommands` -- same confirmed BSD-3-Clause
    `LICENSE.md`, same per-file header text.
  - `Parrot-Developers/arsdk-xml` (the auto-generated-from-XML command
    catalog, used below only to look up two real, existing project/class/
    command IDs and their real argument types for test vectors) --
    GitHub's license-detection API returns `null` for this repo (its XML
    files apparently confuse GitHub's detector), but every individual XML
    file (e.g. `xml/common.xml`, `xml/ardrone3.xml`) carries the exact same
    "Copyright (C) 2014 Parrot SA" BSD-3-Clause header text verified above,
    fetched and read directly in this session -- so the actual per-file
    license is the same BSD-3-Clause, not an unlicensed/all-rights-reserved
    file, despite the org-level detector miss.
  - NOT BSD, and explicitly NOT used here: `WiresharkPlugin/packet-arsdk.c`
    inside `libARCommands` is a Wireshark dissector plugin and carries its
    OWN, separate GPLv2 header ("Copyright 1998 Gerald Combs ... GNU General
    Public License ... version 2"), because Wireshark plugins are
    distributed under Wireshark's GPLv2, not the surrounding repo's
    BSD-3-Clause license. This module's frame-header struct/offsets were
    therefore derived from the BSD-licensed `ARNETWORKAL_Frame.h` header and
    the BSD-licensed `ARNETWORKAL_WifiNetwork.c` wire-serialization code
    (`Sources/Wifi/ARNETWORKAL_WifiNetwork.c`'s `netFrame_writeFrame`/
    `netFrame_readFrame`-equivalent functions), NOT from the GPLv2 Wireshark
    dissector -- no GPL code or text is reproduced anywhere in this file.
    (Protocol wire-format FACTS -- which bytes mean what -- are not
    copyrightable subject matter regardless; the point above is simply that
    every fact used here was independently confirmed against a genuinely
    BSD-3-Clause source, so there was no need to rely on the GPL file at
    all.)

Net result: genuinely BSD-3-Clause, OSI-approved permissive, for everything
this module actually reimplements. Satisfies this project's open-source
sovereignty requirement (OSI permissive only).

=============================================================================
THE REAL WIRE FORMAT (verified against actual Parrot source, not guessed)
=============================================================================
ARNetworkAL frame header, 7 bytes, exactly as serialized by
`ARNETWORKAL_WifiNetwork.c`'s frame-push/frame-pop code (confirmed by
reading the real `memcpy`/`htodl`/`dtohl` calls in that file in this
session) and as declared (in-memory, pre-`__attribute__((packed))`) in
`ARNETWORKAL_Frame.h`:

    offset 0   : type   (uint8)   -- eARNETWORKAL_FRAME_TYPE
    offset 1   : id     (uint8)   -- ARNetwork buffer identifier
    offset 2   : seq    (uint8)   -- per-buffer sequence number
    offset 3-6 : size   (uint32, little-endian on the wire, per the
                 `htodl`/`dtohl` "host<->drone" endian-swap macros used in
                 `ARNETWORKAL_WifiNetwork.c`) -- TOTAL frame size in bytes,
                 INCLUDING this 7-byte header (i.e. payload length is
                 size - 7; this matches the C code's own
                 `frame->size - offsetof(ARNETWORKAL_Frame_t, dataPtr)`
                 computation, since `dataPtr` is the first member after the
                 three uint8 fields + the uint32 size field, i.e. at byte
                 offset 7 in the packed/wire form).

`eARNETWORKAL_FRAME_TYPE` values (`ARNETWORKAL_Frame.h`):
    0 = UNINITIALIZED (invalid, never sent)
    1 = ACK                     -- acknowledges a DATA_WITH_ACK frame
    2 = DATA                    -- no-ack data (most commands/events)
    3 = DATA_LOW_LATENCY        -- e.g. video/stream data
    4 = DATA_WITH_ACK           -- data requiring an ACK reply

Payload (bytes 7+, i.e. an "ARCommand"), format shared by every command in
every project/class in the whole ARSDK3 command catalog (`arsdk-xml`'s
generated encoding, confirmed against real `common.xml`/`ardrone3.xml`
content fetched in this session):

    offset 0   : project id (uint8)
    offset 1   : class id   (uint8)
    offset 2-3 : command id (uint16, little-endian)
    offset 4+  : arguments, packed back-to-back, each argument's on-wire
                 type/size fixed per the XML `<arg type="...">` declaration
                 (u8/i8/u16/i16/u32/i32/u64/i64/float/double/string/enum;
                 `enum` is encoded as a little-endian `i32` ordinal index
                 into the `<enum>` list order in the XML -- this module
                 decodes it as a plain i32 and, where a specific known
                 command is recognized, additionally maps it to the real
                 enum name list copied from that command's XML).

This module does NOT attempt to decode every one of the several hundred
commands in the full ARSDK3 catalog (that is arsdk-xml's job, via code
generation -- reimplementing that generator is out of scope for a passive
RX decode bridge). It decodes the frame header + generic ARCommand header
(project/class/command IDs) for ANY frame -- which is enough to identify
"this is drone telemetry/command traffic and which command it is" -- plus
full argument decoding for a small, explicitly-named set of common,
verified commands (battery state, flying state), each with a real
project/class/command ID and argument layout looked up directly from the
real `arsdk-xml` XML files in this session, not invented.

=============================================================================
HARDWARE STATUS: BLOCKED -- NO PARROT DRONE / CAPTURE PATH ON HAND.
DO NOT ENABLE A SYSTEMD SERVICE FOR THIS UNTIL HARDWARE EXISTS.
=============================================================================
Per project_cema_hardware_and_capabilities: this project owns no Parrot
Bebop/Bebop2/Disco/ANAFI airframe or matching Wi-Fi controller/skycontroller
to observe real traffic from, and no monitor-mode-capable Wi-Fi adapter to
passively capture 802.11 frames off the air even if one were in range (the
same standing blocker noted in remoteid_decode_bridge.py). Unlike DJI
OcuSync (an RF/PHY-layer signal this project's HackRF-based bridges try to
demodulate directly), ARSDK3 traffic rides on ordinary Wi-Fi (802.11 +
IPv4 TCP/UDP) between the drone and its controlling device (phone/tablet/
SkyController) -- to observe it passively without joining the drone's own
Wi-Fi AP as an associated client requires either (a) a monitor-mode NIC
capturing and reassembling the 802.11 data frames + their IP/UDP payloads,
or (b) a position on-path between the controller and drone (e.g. a tap/span
port on infrastructure the operator's app associates through), neither of
which this project has today.

This module is therefore STAGED, BUILD-AND-TEST-ONLY, exactly like
crsf_parser.py/msp_parser.py/canopen_parser.py/dronecan_parser.py/
remoteid_decode_bridge.py before their hardware arrived: the decode logic
is real and verified against genuine ARSDK3 protocol constants copied from
Parrot's own BSD-3-Clause source, but there is deliberately NO systemd
service file for this module and NO live capture integration point wired
up. When either a monitor-mode Wi-Fi adapter or an on-path capture position
against real Parrot drone traffic is available, the missing piece is only a
capture front-end (e.g. scapy/tcpdump reading raw UDP/TCP payloads on the
drone's known ARSDK data ports, or a pcap replay) that hands this module raw
ARNetworkAL frame bytes -- the decode logic itself needs no changes.

Requires: nothing beyond the Python standard library (struct). No
third-party dependency, no compiled extension.
"""
from __future__ import annotations

import argparse
import struct
import sys
from typing import Dict, List, Optional

# =============================================================================
# Frame header constants -- copied verbatim (names/values) from
# Parrot-Developers/libARNetworkAL's Includes/libARNetworkAL/ARNETWORKAL_Frame.h
# (BSD-3-Clause, Copyright (C) 2014 Parrot SA). See module docstring for the
# license verification and the wire-serialization confirmation from
# Sources/Wifi/ARNETWORKAL_WifiNetwork.c.
# =============================================================================
ARNETWORKAL_FRAME_HEADER_SIZE = 7  # type(1) + id(1) + seq(1) + size(4)

FRAME_TYPE_UNINITIALIZED = 0
FRAME_TYPE_ACK = 1
FRAME_TYPE_DATA = 2
FRAME_TYPE_DATA_LOW_LATENCY = 3
FRAME_TYPE_DATA_WITH_ACK = 4

FRAME_TYPE_NAMES = {
    FRAME_TYPE_UNINITIALIZED: "UNINITIALIZED",
    FRAME_TYPE_ACK: "ACK",
    FRAME_TYPE_DATA: "DATA",
    FRAME_TYPE_DATA_LOW_LATENCY: "DATA_LOW_LATENCY",
    FRAME_TYPE_DATA_WITH_ACK: "DATA_WITH_ACK",
}

# Well-known ARNetworkAL buffer IDs used by the Wi-Fi/network transport
# (from libARNetworkAL / libARController's default buffer configuration --
# these are the IDs the reference implementation itself uses for the
# "send command"/"receive command"/"send/recv with ack" buffers; included
# here only as informative decode context, not required for correctness).
BUFFER_ID_NAMES = {
    0: "PING",
    1: "PONG",
    10: "C2D_NONACK",       # controller -> drone, no-ack commands
    11: "C2D_ACK",          # controller -> drone, ack-required commands
    12: "C2D_EMERGENCY",
    13: "C2D_VIDEO_ACK",
    125: "D2C_NAVDATA",     # drone -> controller, no-ack events/telemetry
    126: "D2C_EVENT",       # drone -> controller, ack-required events
    127: "D2C_VIDEO_DATA",
}


class ARSDKFrameError(ValueError):
    """Raised when candidate bytes are not a well-formed ARNetworkAL frame.
    Never silently coerced into a fabricated result."""


def decode_frame_header(raw: bytes) -> Dict:
    """Decode the 7-byte ARNetworkAL frame header at the start of `raw`.

    Field layout/endianness verified against libARNetworkAL's
    ARNETWORKAL_Frame.h (in-memory struct) and ARNETWORKAL_WifiNetwork.c
    (actual wire read/write code: type/id/seq each written as a single
    byte via memcpy, then `size` byte-swapped with `htodl`/`dtohl` and
    written as a 4-byte little-endian integer). `size` is the TOTAL frame
    size (header + payload), matching the reference implementation's own
    `frame->size - offsetof(ARNETWORKAL_Frame_t, dataPtr)` payload-length
    computation.
    """
    if len(raw) < ARNETWORKAL_FRAME_HEADER_SIZE:
        raise ARSDKFrameError(
            f"need at least {ARNETWORKAL_FRAME_HEADER_SIZE} bytes for a frame "
            f"header, got {len(raw)}"
        )
    frame_type, frame_id, seq = struct.unpack_from("<BBB", raw, 0)
    size = struct.unpack_from("<I", raw, 3)[0]
    if size < ARNETWORKAL_FRAME_HEADER_SIZE:
        raise ARSDKFrameError(
            f"frame size field {size} is smaller than the header itself "
            f"({ARNETWORKAL_FRAME_HEADER_SIZE}) -- malformed frame"
        )
    return {
        "frame_type_code": frame_type,
        "frame_type": FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN({frame_type})"),
        "buffer_id": frame_id,
        "buffer_name": BUFFER_ID_NAMES.get(frame_id, f"id={frame_id}"),
        "seq": seq,
        "total_size": size,
        "payload_size": size - ARNETWORKAL_FRAME_HEADER_SIZE,
    }


def iter_frames(stream: bytes):
    """Walk a byte stream containing zero or more back-to-back ARNetworkAL
    frames (as would appear reassembled from a captured TCP/UDP payload
    stream), yielding (header_dict, payload_bytes) per frame. Raises
    ARSDKFrameError on the first malformed/truncated frame rather than
    silently skipping or fabricating a result; callers processing a
    live/noisy stream should catch this and resynchronize themselves."""
    i = 0
    n = len(stream)
    while i < n:
        remaining = stream[i:]
        if len(remaining) < ARNETWORKAL_FRAME_HEADER_SIZE:
            raise ARSDKFrameError(
                f"{len(remaining)} trailing byte(s) at offset {i} -- too "
                f"short for another frame header"
            )
        header = decode_frame_header(remaining)
        total = header["total_size"]
        if i + total > n:
            raise ARSDKFrameError(
                f"frame at offset {i} claims total_size={total} but only "
                f"{n - i} bytes remain in the stream"
            )
        payload = remaining[ARNETWORKAL_FRAME_HEADER_SIZE:total]
        yield header, payload
        i += total


# =============================================================================
# Generic ARCommand header (project/class/command) -- format confirmed
# against real `arsdk-xml/xml/common.xml` and `xml/ardrone3.xml` content
# fetched in this session (BSD-3-Clause per-file header, see module
# docstring). This is the payload that follows the 7-byte ARNetworkAL frame
# header above.
# =============================================================================
ARCOMMAND_HEADER_SIZE = 4  # project(1) + class(1) + command(2, LE)


def decode_arcommand_header(payload: bytes) -> Dict:
    """Decode the 4-byte generic ARCommand header (project id, class id,
    command id) present at the start of every ARSDK3 command/event
    payload, regardless of which specific command it is."""
    if len(payload) < ARCOMMAND_HEADER_SIZE:
        raise ARSDKFrameError(
            f"ARCommand payload needs at least {ARCOMMAND_HEADER_SIZE} "
            f"bytes for project/class/command IDs, got {len(payload)}"
        )
    project_id, class_id = struct.unpack_from("<BB", payload, 0)
    command_id = struct.unpack_from("<H", payload, 2)[0]
    return {
        "project_id": project_id,
        "class_id": class_id,
        "command_id": command_id,
        "args_raw": payload[ARCOMMAND_HEADER_SIZE:],
    }


# =============================================================================
# A small, explicitly-named set of known commands, decoded fully. Every
# project id / class id / command id / argument type below was looked up
# directly in the real arsdk-xml XML files fetched in this session -- not
# invented, not "typical values."
# =============================================================================
PROJECT_COMMON = 0
PROJECT_ARDRONE3 = 1

CLASS_COMMON_COMMONSTATE = 5     # common.xml: <class name="CommonState" id="5">
CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED = 1  # <cmd name="BatteryStateChanged" id="1">
# BatteryStateChanged's one argument, per common.xml:
#   <arg name="percent" type="u8">Battery percentage</arg>

CLASS_ARDRONE3_PILOTINGSTATE = 4  # ardrone3.xml: <class name="PilotingState" id="4">
CMD_ARDRONE3_PILOTINGSTATE_FLYINGSTATECHANGED = 1  # <cmd name="FlyingStateChanged" id="1">
# FlyingStateChanged's one argument, per ardrone3.xml:
#   <arg name="state" type="enum"> ... 9 <enum> values, in this exact order:
FLYING_STATE_ENUM_NAMES = [
    "landed", "takingoff", "hovering", "flying", "landing",
    "emergency", "usertakeoff", "motor_ramping", "emergency_landing",
]


def decode_battery_state_changed(args_raw: bytes) -> Dict:
    """common(0)/CommonState(5)/BatteryStateChanged(1): one `u8` arg,
    `percent`. Real command/arg layout, see constants above."""
    if len(args_raw) < 1:
        raise ARSDKFrameError("BatteryStateChanged needs 1 byte (percent:u8)")
    percent = args_raw[0]
    return {"command": "common.CommonState.BatteryStateChanged", "percent": percent}


def decode_flying_state_changed(args_raw: bytes) -> Dict:
    """ardrone3(1)/PilotingState(4)/FlyingStateChanged(1): one `enum` arg,
    `state`, encoded on the wire as a little-endian i32 ordinal index into
    the enum's declared name list (ARSDK3's generic enum-encoding
    convention -- every `type="enum"` argument in the whole command catalog
    is encoded this way, per arsdk-xml's code generator). Real command/arg
    layout and real enum name list, see constants above."""
    if len(args_raw) < 4:
        raise ARSDKFrameError("FlyingStateChanged needs 4 bytes (state:enum/i32)")
    state_idx = struct.unpack_from("<i", args_raw, 0)[0]
    state_name = (FLYING_STATE_ENUM_NAMES[state_idx]
                  if 0 <= state_idx < len(FLYING_STATE_ENUM_NAMES)
                  else f"UNKNOWN({state_idx})")
    return {
        "command": "ardrone3.PilotingState.FlyingStateChanged",
        "state_code": state_idx,
        "state": state_name,
    }


# (project_id, class_id, command_id) -> decoder
KNOWN_COMMAND_DECODERS = {
    (PROJECT_COMMON, CLASS_COMMON_COMMONSTATE,
     CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED): decode_battery_state_changed,
    (PROJECT_ARDRONE3, CLASS_ARDRONE3_PILOTINGSTATE,
     CMD_ARDRONE3_PILOTINGSTATE_FLYINGSTATECHANGED): decode_flying_state_changed,
}


def decode_arcommand(payload: bytes) -> Dict:
    """Decode an ARCommand payload: always decodes the generic
    project/class/command header; additionally decodes arguments if this
    is one of the explicitly-known commands above. Never fabricates
    argument decoding for an unrecognized command -- reports the raw
    header plus the undecoded argument bytes as hex instead."""
    header = decode_arcommand_header(payload)
    key = (header["project_id"], header["class_id"], header["command_id"])
    decoder = KNOWN_COMMAND_DECODERS.get(key)
    result = {
        "project_id": header["project_id"],
        "class_id": header["class_id"],
        "command_id": header["command_id"],
    }
    if decoder is not None:
        result.update(decoder(header["args_raw"]))
    else:
        result["command"] = "UNKNOWN/undecoded"
        result["args_hex"] = header["args_raw"].hex()
    return result


def decode_frame(raw: bytes) -> Dict:
    """Decode one complete ARNetworkAL frame (header + ARCommand payload)
    from the start of `raw`. Convenience wrapper combining
    decode_frame_header()/decode_arcommand() for a single, already-
    delimited frame's bytes (use iter_frames() for a stream of several
    back-to-back frames)."""
    header = decode_frame_header(raw)
    payload = raw[ARNETWORKAL_FRAME_HEADER_SIZE:header["total_size"]]
    out = {"frame": header}
    if header["frame_type"] in ("DATA", "DATA_LOW_LATENCY", "DATA_WITH_ACK"):
        try:
            out["arcommand"] = decode_arcommand(payload)
        except ARSDKFrameError as e:
            out["arcommand_error"] = str(e)
    elif header["frame_type"] == "ACK":
        # ACK frame's single data byte is the sequence number being
        # acknowledged (libARNetworkAL convention -- confirmed against the
        # ARNETWORKAL_FRAME_TYPE_ACK handling referenced in Parrot's own
        # frame docs/comments).
        out["ack_seq"] = payload[0] if payload else None
    return out


# =============================================================================
# Byte-exact test vectors -- hand-assembled strictly from the real,
# verified constants above (frame header layout from ARNETWORKAL_Frame.h /
# ARNETWORKAL_WifiNetwork.c, ARCommand header + BatteryStateChanged/
# FlyingStateChanged layout from real arsdk-xml XML content), the same way
# droneid/remoteid's Bluetooth-framing self-tests build ground truth by
# hand from a verified spec when no pre-built binary sample exists to
# byte-dump. This is NOT compiled-C reference output (unlike remoteid's
# WiFi-path vectors) -- arsdk-xml ships no pre-encoded sample frames and
# libARCommands' generated encoder requires the full arsdkgen.py code-gen
# pipeline to build, which is out of scope for a decode-only bridge -- but
# every byte below is placed according to the real, cited field
# offsets/sizes/endianness, not invented.
# =============================================================================
def _build_frame(frame_type: int, buffer_id: int, seq: int, payload: bytes) -> bytes:
    total_size = ARNETWORKAL_FRAME_HEADER_SIZE + len(payload)
    return struct.pack("<BBBI", frame_type, buffer_id, seq, total_size) + payload


def _build_arcommand(project_id: int, class_id: int, command_id: int, args: bytes) -> bytes:
    return struct.pack("<BBH", project_id, class_id, command_id) + args


def self_test() -> None:
    failures: List[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== decode_frame_header(): frame type / buffer id / seq / size ===")
    battery_payload = _build_arcommand(
        PROJECT_COMMON, CLASS_COMMON_COMMONSTATE,
        CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED, bytes([77]))
    frame_bytes = _build_frame(FRAME_TYPE_DATA, 125, 42, battery_payload)
    h = decode_frame_header(frame_bytes)
    check("frame_type == DATA", h["frame_type"] == "DATA")
    check("buffer_id == 125 (D2C_NAVDATA)", h["buffer_id"] == 125 and h["buffer_name"] == "D2C_NAVDATA")
    check("seq == 42", h["seq"] == 42)
    check("total_size == 7 + 5 (4 header + 1 percent byte)", h["total_size"] == 7 + 5)
    check("payload_size == 5", h["payload_size"] == 5)

    print("\n=== decode_arcommand() + decode_battery_state_changed():"
          " real common.CommonState.BatteryStateChanged (project=0,class=5,cmd=1) ===")
    result = decode_frame(frame_bytes)
    check("frame_type == DATA", result["frame"]["frame_type"] == "DATA")
    check("project_id == 0 (common)", result["arcommand"]["project_id"] == 0)
    check("class_id == 5 (CommonState)", result["arcommand"]["class_id"] == 5)
    check("command_id == 1 (BatteryStateChanged)", result["arcommand"]["command_id"] == 1)
    check("percent == 77", result["arcommand"]["percent"] == 77)
    check("command name reported", result["arcommand"]["command"] ==
          "common.CommonState.BatteryStateChanged")

    print("\n=== decode_arcommand() + decode_flying_state_changed():"
          " real ardrone3.PilotingState.FlyingStateChanged (project=1,class=4,cmd=1) ===")
    for state_idx, expected_name in enumerate(FLYING_STATE_ENUM_NAMES):
        args = struct.pack("<i", state_idx)
        payload = _build_arcommand(PROJECT_ARDRONE3, CLASS_ARDRONE3_PILOTINGSTATE,
                                    CMD_ARDRONE3_PILOTINGSTATE_FLYINGSTATECHANGED, args)
        frame = _build_frame(FRAME_TYPE_DATA_WITH_ACK, 126, state_idx, payload)
        r = decode_frame(frame)
        check(f"state_code {state_idx} decodes to '{expected_name}'",
              r["arcommand"]["state"] == expected_name)
    check("frame_type == DATA_WITH_ACK for last frame built above",
          decode_frame(frame)["frame"]["frame_type"] == "DATA_WITH_ACK")

    print("\n=== ACK frame decode (frame_type=1) ===")
    ack_frame = _build_frame(FRAME_TYPE_ACK, 11, 5, bytes([5]))
    r = decode_frame(ack_frame)
    check("frame_type == ACK", r["frame"]["frame_type"] == "ACK")
    check("ack_seq == 5", r["ack_seq"] == 5)

    print("\n=== Unknown command: header decodes, args reported as raw hex,"
          " never fabricated ===")
    unknown_payload = _build_arcommand(9, 9, 999, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    unknown_frame = _build_frame(FRAME_TYPE_DATA, 10, 1, unknown_payload)
    r = decode_frame(unknown_frame)
    check("unknown command reported as UNKNOWN/undecoded",
          r["arcommand"]["command"] == "UNKNOWN/undecoded")
    check("unknown command's raw arg bytes preserved as hex",
          r["arcommand"]["args_hex"] == "deadbeef")

    print("\n=== iter_frames(): multiple back-to-back frames in one stream ===")
    stream = frame_bytes + ack_frame + unknown_frame
    frames = list(iter_frames(stream))
    check("3 frames recovered from concatenated stream", len(frames) == 3)
    check("frame order preserved (DATA, ACK, DATA)",
          [f[0]["frame_type"] for f in frames] == ["DATA", "ACK", "DATA"])

    print("\n=== Error handling: malformed input never fabricates a result ===")
    try:
        decode_frame_header(bytes([1, 2, 3]))
        check("truncated header raises ARSDKFrameError", False)
    except ARSDKFrameError:
        check("truncated header raises ARSDKFrameError", True)
    try:
        decode_frame_header(struct.pack("<BBBI", FRAME_TYPE_DATA, 0, 0, 3))
        check("size smaller than header raises ARSDKFrameError", False)
    except ARSDKFrameError:
        check("size smaller than header raises ARSDKFrameError", True)
    try:
        list(iter_frames(frame_bytes[:-1]))  # truncate last byte
        check("truncated trailing frame raises ARSDKFrameError", False)
    except ARSDKFrameError:
        check("truncated trailing frame raises ARSDKFrameError", True)
    try:
        decode_arcommand_header(bytes([0, 0]))
        check("short ARCommand header raises ARSDKFrameError", False)
    except ARSDKFrameError:
        check("short ARCommand header raises ARSDKFrameError", True)

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="Decode hand-built frames assembled strictly from "
                          "the real, cited ARSDK3 wire-format constants "
                          "(frame header from libARNetworkAL, ARCommand "
                          "layout from arsdk-xml) and verify every field, "
                          "then exit. This is the only thing exercised in "
                          "this session -- see module docstring HARDWARE "
                          "STATUS. There is no live-capture mode: no Parrot "
                          "drone or monitor-mode Wi-Fi adapter exists on "
                          "this project's hardware yet.")
    ap.add_argument("--decode-hex", metavar="HEX",
                     help="Decode a single complete ARNetworkAL frame "
                          "(7-byte header + payload) given as a hex string, "
                          "and print the result. Utility for offline "
                          "analysis of a captured/pcap-extracted frame; "
                          "does not touch any live network interface.")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.decode_hex:
        raw = bytes.fromhex(args.decode_hex)
        try:
            print(decode_frame(raw))
        except ARSDKFrameError as e:
            print(f"decode error: {e}", file=sys.stderr)
            return 1
        return 0

    ap.print_help()
    print(
        "\nNOTE: this module has no live-capture entry point (see module "
        "docstring HARDWARE STATUS -- no Parrot Bebop/Bebop2/Disco/ANAFI "
        "airframe or monitor-mode Wi-Fi adapter on hand). Use --self-test "
        "to verify the decoder, or --decode-hex to decode an offline-"
        "captured ARNetworkAL frame.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
