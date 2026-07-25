#!/usr/bin/env python3
"""Passive ASTM F3411 / ASD-STAN Remote ID message decoder -- pure-Python
re-implementation of the message-unpack logic from the official OpenDroneID
reference implementation (`opendroneid-core-c`, Intel/Linux Foundation, Apache
2.0, https://github.com/opendroneid/opendroneid-core-c), local checkout at
`~/Desktop/Zettawise/PMO Suraj/tool/opendroneid-core-c`.

RX-ONLY. This module contains no transmit/broadcast/encode path at all --
only decode functions for bytes already captured off the air. Consistent with
this project's standing rule that nothing not explicitly authorized to
transmit is given a transmit path.

=============================================================================
READ THIS FIRST -- WHAT REMOTE ID ACTUALLY TELLS YOU (AND WHAT IT DOESN'T)
=============================================================================
Remote ID is a REGULATORY broadcast standard (FAA Part 89 in the US, the EASA/
EU "Direct Remote ID" equivalent per ASD-STAN prEN 4709-002, and similar
rules elsewhere). Compliant drones broadcast their own ID/position/velocity
over Wi-Fi Beacon/NAN frames or Bluetooth 4/5 Advertising/LE Extended
Advertising because the LAW requires it, not because of anything this system
does to detect or provoke them.

This is fundamentally different from:
  - DJI's proprietary DroneID (handled by droneid_decode_bridge.py in this
    same directory) -- a vendor protocol, not a regulatory one, and DJI
    drones broadcast it regardless of jurisdiction or registration status.
  - Detecting a hostile emitter's RF signature (hackrf_rx.py, ml_classify_
    bridge.py, gamutrf_backend_adapter.py) -- inferring "something is
    transmitting here" from energy/spectral features alone, with no
    protocol-level identity claim at all.

THE SINGLE MOST IMPORTANT LIMITATION, STATED PLAINLY: a hostile or
non-compliant drone -- exactly the adversarial case this counter-UAS system
cares about most -- has no obligation to transmit Remote ID at all, and an
adversary actively evading detection has every incentive not to. Such a
drone will simply not broadcast anything this module can decode. This module
can therefore NEVER be used to detect a threat by its absence of Remote ID
traffic, and a decoded Remote ID message is evidence only that SOME broadcast
-compliant transmitter is nearby -- it is not proof the airframe emitting it
is benign (a compliant ID could, in principle, be transmitted by a malicious
actor spoofing/replaying a legitimate one; this decoder does not attempt any
cryptographic authentication of Remote ID content beyond what the standard
itself provides via the optional Authentication message, which this module
decodes as opaque bytes only).

The one thing this module IS useful for: airspace deconfliction / positive
identification of KNOWN, LEGAL traffic -- "that broadcast is a registered,
compliant drone, not necessarily a threat" -- which lets an operator spend
attention on unidentified/non-broadcasting contacts instead. It is a
DECONFLICTION aid, not a THREAT DETECTOR.

=============================================================================
WHY A PYTHON RE-IMPLEMENTATION INSTEAD OF A C BINDING
=============================================================================
opendroneid-core-c's actual message format is a small set of fixed 25-byte,
bitfield-packed structs (see libopendroneid/opendroneid.h's `__attribute__
((packed))` ODID_*_encoded structs) with a handful of linear/enum decode
functions (decodeLatLon, decodeAltitude, decodeSpeedHorizontal, etc. in
libopendroneid/opendroneid.c). There is no parser state machine, no
variable-length framing beyond the outer message-pack header, and no
external dependency graph to speak of -- unlike DroneCAN/CANopen (real DSDL/
EDS ecosystems worth wrapping via their upstream Python libraries) or the DJI
DroneID decode chain (a genuinely complex OFDM/QPSK/turbo-decode signal
pipeline worth reusing wholesale from DroneSecurity), this is fundamentally
"unpack N fixed bitfields per struct." A ctypes/cffi binding would add a
build step (this project's field-bridge is all-Python, no other module here
compiles or ships a .so) for something that is more maintainable, more
auditable, and just as correct as a direct, carefully-verified Python
transliteration. Every field offset, bit width, and scaling constant below
was copied from the real C source (opendroneid.h struct layouts + the
decodeXxx()/SPEED_DIV/VSPEED_DIV/LATLON_MULT/ALT_DIV/ALT_ADDER constants in
opendroneid.c), not guessed or re-derived from the spec text.

=============================================================================
TEST VECTORS -- HOW BYTE-EXACT GROUND TRUTH WAS OBTAINED (NOT FABRICATED)
=============================================================================
opendroneid-core-c ships no pre-encoded binary sample messages in its repo
(test/test_inout.c only exercises encode->decode round-trip in-process and
prints human-readable field values, not a byte dump). Rather than invent
plausible-looking bytes by hand, this module's test vectors were generated by
compiling the REAL, UNMODIFIED reference library (gcc -I libopendroneid
opendroneid.c) together with a small harness that calls test_inout.c's own
encodeBasicIDMessage/encodeLocationMessage/encodeSelfIDMessage/
encodeSystemMessage/encodeOperatorIDMessage functions with test_inout.c's own
literal input values (same UASID string, same lat/lon, same speeds/
altitudes/timestamps as the upstream test), and printing the resulting
encoded byte arrays as hex. Those hex strings (see TEST_VECTORS_HEX below)
are the actual output of the real C reference encoder -- this Python decoder
is verified against genuine reference-implementation output, not
self-consistent fabricated bytes.

=============================================================================
HARDWARE STATUS: BLOCKED -- NO LIVE CAPTURE PATH EXISTS TODAY. DO NOT ENABLE
A SYSTEMD SERVICE FOR THIS UNTIL HARDWARE EXISTS.
=============================================================================
Per project_cema_hardware_and_capabilities / task #70: this project has no
monitor-mode-capable Wi-Fi adapter (the same blocker that stalled the earlier
WiFi MAC-OUI drone-detection evaluation), and no Bluetooth 5 Long Range /
Extended Advertising capture path either. Remote ID over Wi-Fi is carried in
802.11 Beacon frames or the Neighbor Awareness Networking (NAN) Service
Discovery Frame (see opendroneid-core-c's libopendroneid/odid_wifi.h /
wifi.c, which build/parse those frames but still require a monitor-mode NIC
to actually capture them off the air); Remote ID over Bluetooth needs a BLE
scanner capable of Bluetooth 4 Legacy Advertising or Bluetooth 5 Long Range
Extended Advertising. This project owns neither today.

This module is therefore STAGED, BUILD-AND-TEST-ONLY, exactly like
crsf_parser.py/msp_parser.py/canopen_parser.py/dronecan_parser.py before
their hardware arrived: the decode logic is real, verified, and correct
against reference-implementation bytes, but there is deliberately NO systemd
service file for this module and NO live capture integration point wired up.
When a monitor-mode Wi-Fi adapter or BLE 5 scanner is procured, the missing
piece is only a capture front-end (scapy/tcpdump on a monitor-mode interface
for the Wi-Fi Beacon/NAN path, or bleak/bluepy for the BLE path) that hands
this module raw 25-byte ODID message payloads -- the decode logic itself
needs no changes.

Requires: nothing beyond the Python standard library (struct). No
third-party dependency, no compiled extension.
"""
from __future__ import annotations

import argparse
import struct
import sys
from typing import Dict, List, Optional

# =============================================================================
# Constants copied verbatim from libopendroneid/opendroneid.c (scaling) and
# libopendroneid/opendroneid.h (sizes/enums) -- see module docstring.
# =============================================================================
ODID_MESSAGE_SIZE = 25
ODID_ID_SIZE = 20
ODID_STR_SIZE = 23
ODID_AUTH_PAGE_ZERO_DATA_SIZE = 17
ODID_AUTH_PAGE_NONZERO_DATA_SIZE = 23

SPEED_DIV = (0.25, 0.75)   # opendroneid.c: const float SPEED_DIV[2]
VSPEED_DIV = 0.5           # opendroneid.c: const float VSPEED_DIV
LATLON_MULT = 10000000     # opendroneid.c: const int32_t LATLON_MULT
ALT_DIV = 0.5              # opendroneid.c: const float ALT_DIV
ALT_ADDER = 1000           # opendroneid.c: const int ALT_ADDER
INV_TIMESTAMP = 0xFFFF     # opendroneid.h: #define INV_TIMESTAMP

MESSAGETYPE_BASIC_ID = 0
MESSAGETYPE_LOCATION = 1
MESSAGETYPE_AUTH = 2
MESSAGETYPE_SELF_ID = 3
MESSAGETYPE_SYSTEM = 4
MESSAGETYPE_OPERATOR_ID = 5
MESSAGETYPE_PACKED = 0xF

MESSAGE_TYPE_NAMES = {
    MESSAGETYPE_BASIC_ID: "BASIC_ID",
    MESSAGETYPE_LOCATION: "LOCATION",
    MESSAGETYPE_AUTH: "AUTH",
    MESSAGETYPE_SELF_ID: "SELF_ID",
    MESSAGETYPE_SYSTEM: "SYSTEM",
    MESSAGETYPE_OPERATOR_ID: "OPERATOR_ID",
    MESSAGETYPE_PACKED: "PACKED",
}

ODID_IDTYPE_NAMES = {
    0: "NONE", 1: "SERIAL_NUMBER", 2: "CAA_REGISTRATION_ID",
    3: "UTM_ASSIGNED_UUID", 4: "SPECIFIC_SESSION_ID",
}
ODID_UATYPE_NAMES = {
    0: "NONE", 1: "AEROPLANE", 2: "HELICOPTER_OR_MULTIROTOR", 3: "GYROPLANE",
    4: "HYBRID_LIFT", 5: "ORNITHOPTER", 6: "GLIDER", 7: "KITE",
    8: "FREE_BALLOON", 9: "CAPTIVE_BALLOON", 10: "AIRSHIP",
    11: "FREE_FALL_PARACHUTE", 12: "ROCKET", 13: "TETHERED_POWERED_AIRCRAFT",
    14: "GROUND_OBSTACLE", 15: "OTHER",
}
ODID_STATUS_NAMES = {
    0: "UNDECLARED", 1: "GROUND", 2: "AIRBORNE", 3: "EMERGENCY",
    4: "REMOTE_ID_SYSTEM_FAILURE",
}


def _strip_nul(b: bytes) -> str:
    """opendroneid.c's safe_dec_copyfill(): copy up to the first NUL,
    decode as ASCII/latin-1 (Remote ID string fields are not necessarily
    valid UTF-8), ignore trailing NUL padding."""
    idx = b.find(b"\x00")
    if idx != -1:
        b = b[:idx]
    return b.decode("latin-1", errors="replace")


def _decode_direction(direction_enc: int, ew_direction: int) -> float:
    # opendroneid.c: decodeDirection()
    return float(direction_enc) + 180.0 if ew_direction else float(direction_enc)


def _decode_speed_horizontal(speed_enc: int, mult: int) -> float:
    # opendroneid.c: decodeSpeedHorizontal()
    if mult:
        return (speed_enc * SPEED_DIV[1]) + (255 * SPEED_DIV[0])
    return speed_enc * SPEED_DIV[0]


def _decode_speed_vertical(speed_vertical_enc: int) -> float:
    # opendroneid.c: decodeSpeedVertical()
    return speed_vertical_enc * VSPEED_DIV


def _decode_lat_lon(latlon_enc: int) -> float:
    # opendroneid.c: decodeLatLon()
    return latlon_enc / LATLON_MULT


def _decode_altitude(alt_enc: int) -> float:
    # opendroneid.c: decodeAltitude()
    return alt_enc * ALT_DIV - ALT_ADDER


def _decode_timestamp(seconds_enc: int) -> float:
    # opendroneid.c: decodeTimeStamp()
    if seconds_enc == INV_TIMESTAMP:
        return float(INV_TIMESTAMP)
    return seconds_enc / 10.0


def _decode_area_radius(radius_enc: int) -> int:
    # opendroneid.c: decodeAreaRadius()
    return radius_enc * 10


class RemoteIDDecodeError(ValueError):
    """Raised when a candidate 25-byte payload is not a valid/recognized
    ODID message -- never silently coerced into a fabricated result."""


def _require_len(raw: bytes) -> None:
    if len(raw) != ODID_MESSAGE_SIZE:
        raise RemoteIDDecodeError(
            f"expected exactly {ODID_MESSAGE_SIZE} bytes, got {len(raw)}"
        )


def peek_message_type(raw: bytes) -> int:
    """Top nibble of byte 0 across every ODID_*_encoded struct is
    MessageType (opendroneid.h: every struct's first field pair is
    ProtoVersion:4 / MessageType:4, LSb-first -- so MessageType is bits 4-7
    of byte 0)."""
    if not raw:
        raise RemoteIDDecodeError("empty payload")
    return (raw[0] >> 4) & 0x0F


def peek_protocol_version(raw: bytes) -> int:
    if not raw:
        raise RemoteIDDecodeError("empty payload")
    return raw[0] & 0x0F


def decode_basic_id(raw: bytes) -> Dict:
    """ODID_BasicID_encoded (opendroneid.h lines ~423-437):
    byte0: ProtoVersion:4(lo) MessageType:4(hi)
    byte1: UAType:4(lo) IDType:4(hi)
    bytes2-21: UASID[20]
    bytes22-24: Reserved[3]
    """
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_BASIC_ID:
        raise RemoteIDDecodeError(f"not a Basic ID message (type={msg_type})")

    proto_version = peek_protocol_version(raw)
    ua_type = raw[1] & 0x0F
    id_type = (raw[1] >> 4) & 0x0F
    uasid_raw = raw[2:22]

    return {
        "message_type": "BASIC_ID",
        "protocol_version": proto_version,
        "id_type_code": id_type,
        "id_type": ODID_IDTYPE_NAMES.get(id_type, f"UNKNOWN({id_type})"),
        "ua_type_code": ua_type,
        "ua_type": ODID_UATYPE_NAMES.get(ua_type, f"UNKNOWN({ua_type})"),
        # Serial Number / CAA Registration ID are ASCII text (safe_dec_copyfill
        # in the C source); other ID types are opaque bytes managed by ICAO.
        "uas_id": (_strip_nul(uasid_raw) if id_type in (1, 2)
                   else uasid_raw.hex()),
    }


def decode_location(raw: bytes) -> Dict:
    """ODID_Location_encoded (opendroneid.h lines ~439-478):
    byte0: ProtoVersion:4 MessageType:4
    byte1: SpeedMult:1(lsb) EWDirection:1 HeightType:1 Reserved:1 Status:4(msb)
    byte2: Direction (uint8)
    byte3: SpeedHorizontal (uint8)
    byte4: SpeedVertical (int8)
    bytes5-8: Latitude (int32 LE)
    bytes9-12: Longitude (int32 LE)
    bytes13-14: AltitudeBaro (uint16 LE)
    bytes15-16: AltitudeGeo (uint16 LE)
    bytes17-18: Height (uint16 LE)
    byte19: HorizAccuracy:4(lsb) VertAccuracy:4(msb)
    byte20: SpeedAccuracy:4(lsb) BaroAccuracy:4(msb)
    bytes21-22: TimeStamp (uint16 LE)
    byte23: TSAccuracy:4(lsb) Reserved2:4(msb)
    byte24: Reserved3
    """
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_LOCATION:
        raise RemoteIDDecodeError(f"not a Location message (type={msg_type})")

    b1 = raw[1]
    speed_mult = b1 & 0x01
    ew_direction = (b1 >> 1) & 0x01
    height_type = (b1 >> 2) & 0x01
    status = (b1 >> 4) & 0x0F

    direction_enc = raw[2]
    speed_h_enc = raw[3]
    speed_v_enc = struct.unpack_from("<b", raw, 4)[0]
    latitude_enc = struct.unpack_from("<i", raw, 5)[0]
    longitude_enc = struct.unpack_from("<i", raw, 9)[0]
    alt_baro_enc = struct.unpack_from("<H", raw, 13)[0]
    alt_geo_enc = struct.unpack_from("<H", raw, 15)[0]
    height_enc = struct.unpack_from("<H", raw, 17)[0]
    b19 = raw[19]
    horiz_accuracy = b19 & 0x0F
    vert_accuracy = (b19 >> 4) & 0x0F
    b20 = raw[20]
    speed_accuracy = b20 & 0x0F
    baro_accuracy = (b20 >> 4) & 0x0F
    timestamp_enc = struct.unpack_from("<H", raw, 21)[0]
    ts_accuracy = raw[23] & 0x0F

    return {
        "message_type": "LOCATION",
        "protocol_version": peek_protocol_version(raw),
        "status_code": status,
        "status": ODID_STATUS_NAMES.get(status, f"UNKNOWN({status})"),
        "direction_deg": _decode_direction(direction_enc, ew_direction),
        "speed_horizontal_mps": _decode_speed_horizontal(speed_h_enc, speed_mult),
        "speed_vertical_mps": _decode_speed_vertical(speed_v_enc),
        "latitude_deg": _decode_lat_lon(latitude_enc),
        "longitude_deg": _decode_lat_lon(longitude_enc),
        "altitude_baro_m": _decode_altitude(alt_baro_enc),
        "altitude_geo_m": _decode_altitude(alt_geo_enc),
        "height_type": "OVER_GROUND" if height_type else "OVER_TAKEOFF",
        "height_m": _decode_altitude(height_enc),
        "horiz_accuracy_code": horiz_accuracy,
        "vert_accuracy_code": vert_accuracy,
        "baro_accuracy_code": baro_accuracy,
        "speed_accuracy_code": speed_accuracy,
        "timestamp_accuracy_code": ts_accuracy,
        "timestamp_s_after_hour": _decode_timestamp(timestamp_enc),
    }


def decode_self_id(raw: bytes) -> Dict:
    """ODID_SelfID_encoded (opendroneid.h lines ~523-533):
    byte0: ProtoVersion:4 MessageType:4
    byte1: DescType (uint8)
    bytes2-24: Desc[23]
    """
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_SELF_ID:
        raise RemoteIDDecodeError(f"not a Self-ID message (type={msg_type})")

    desc_type = raw[1]
    desc_raw = raw[2:25]
    return {
        "message_type": "SELF_ID",
        "protocol_version": peek_protocol_version(raw),
        "desc_type_code": desc_type,
        "description": _strip_nul(desc_raw),
    }


def decode_system(raw: bytes) -> Dict:
    """ODID_System_encoded (opendroneid.h lines ~535-565):
    byte0: ProtoVersion:4 MessageType:4
    byte1: OperatorLocationType:2(lsb) ClassificationType:3 Reserved:3(msb)
    bytes2-5: OperatorLatitude (int32 LE)
    bytes6-9: OperatorLongitude (int32 LE)
    bytes10-11: AreaCount (uint16 LE)
    byte12: AreaRadius (uint8)
    bytes13-14: AreaCeiling (uint16 LE)
    bytes15-16: AreaFloor (uint16 LE)
    byte17: ClassEU:4(lsb) CategoryEU:4(msb)
    bytes18-19: OperatorAltitudeGeo (uint16 LE)
    bytes20-23: Timestamp (uint32 LE)
    byte24: Reserved2
    """
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_SYSTEM:
        raise RemoteIDDecodeError(f"not a System message (type={msg_type})")

    b1 = raw[1]
    operator_location_type = b1 & 0x03
    classification_type = (b1 >> 2) & 0x07

    operator_lat_enc = struct.unpack_from("<i", raw, 2)[0]
    operator_lon_enc = struct.unpack_from("<i", raw, 6)[0]
    area_count = struct.unpack_from("<H", raw, 10)[0]
    area_radius_enc = raw[12]
    area_ceiling_enc = struct.unpack_from("<H", raw, 13)[0]
    area_floor_enc = struct.unpack_from("<H", raw, 15)[0]
    b17 = raw[17]
    class_eu = b17 & 0x0F
    category_eu = (b17 >> 4) & 0x0F
    operator_alt_geo_enc = struct.unpack_from("<H", raw, 18)[0]
    timestamp = struct.unpack_from("<I", raw, 20)[0]

    return {
        "message_type": "SYSTEM",
        "protocol_version": peek_protocol_version(raw),
        "operator_location_type_code": operator_location_type,
        "classification_type_code": classification_type,
        "operator_latitude_deg": _decode_lat_lon(operator_lat_enc),
        "operator_longitude_deg": _decode_lat_lon(operator_lon_enc),
        "area_count": area_count,
        "area_radius_m": _decode_area_radius(area_radius_enc),
        "area_ceiling_m": _decode_altitude(area_ceiling_enc),
        "area_floor_m": _decode_altitude(area_floor_enc),
        "category_eu_code": category_eu,
        "class_eu_code": class_eu,
        "operator_altitude_geo_m": _decode_altitude(operator_alt_geo_enc),
        "timestamp_s_since_2019": timestamp,
    }


def decode_operator_id(raw: bytes) -> Dict:
    """ODID_OperatorID_encoded (opendroneid.h lines ~567-580):
    byte0: ProtoVersion:4 MessageType:4
    byte1: OperatorIdType (uint8)
    bytes2-21: OperatorId[20]
    bytes22-24: Reserved[3]
    """
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_OPERATOR_ID:
        raise RemoteIDDecodeError(f"not an Operator ID message (type={msg_type})")

    operator_id_type = raw[1]
    operator_id_raw = raw[2:22]
    return {
        "message_type": "OPERATOR_ID",
        "protocol_version": peek_protocol_version(raw),
        "operator_id_type_code": operator_id_type,
        "operator_id": _strip_nul(operator_id_raw),
    }


DECODERS = {
    MESSAGETYPE_BASIC_ID: decode_basic_id,
    MESSAGETYPE_LOCATION: decode_location,
    MESSAGETYPE_SELF_ID: decode_self_id,
    MESSAGETYPE_SYSTEM: decode_system,
    MESSAGETYPE_OPERATOR_ID: decode_operator_id,
    # Auth messages are intentionally left as opaque bytes (see decode_auth
    # below) -- there is no further structure this module interprets, and no
    # authentication/signature VERIFICATION is implemented anywhere here.
}


def decode_auth(raw: bytes) -> Dict:
    """ODID_Auth_encoded_page_zero / page_non_zero (opendroneid.h lines
    ~480-509). Returned as opaque AuthData bytes -- this module does NOT
    implement any signature verification; see module docstring's note that
    Remote ID content is not cryptographically authenticated by this
    decoder."""
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_AUTH:
        raise RemoteIDDecodeError(f"not an Auth message (type={msg_type})")

    b1 = raw[1]
    data_page = b1 & 0x0F
    auth_type = (b1 >> 4) & 0x0F

    if data_page == 0:
        last_page_index = raw[2]
        length = raw[3]
        timestamp = struct.unpack_from("<I", raw, 4)[0]
        auth_data = raw[8:25]
        return {
            "message_type": "AUTH",
            "protocol_version": peek_protocol_version(raw),
            "data_page": data_page,
            "auth_type_code": auth_type,
            "last_page_index": last_page_index,
            "length": length,
            "timestamp": timestamp,
            "auth_data_hex": auth_data.hex(),
        }

    auth_data = raw[2:25]
    return {
        "message_type": "AUTH",
        "protocol_version": peek_protocol_version(raw),
        "data_page": data_page,
        "auth_type_code": auth_type,
        "auth_data_hex": auth_data.hex(),
    }


def decode_message(raw: bytes) -> Dict:
    """Decode a single 25-byte ODID message of any type. Raises
    RemoteIDDecodeError for anything malformed or unrecognized -- never
    guesses/fills in a fabricated result."""
    _require_len(raw)
    msg_type = peek_message_type(raw)
    if msg_type == MESSAGETYPE_AUTH:
        return decode_auth(raw)
    decoder = DECODERS.get(msg_type)
    if decoder is None:
        raise RemoteIDDecodeError(
            f"unrecognized/unsupported message type {msg_type} "
            f"(known: {sorted(MESSAGE_TYPE_NAMES)})"
        )
    return decoder(raw)


def decode_message_pack(raw: bytes) -> List[Dict]:
    """ODID_MessagePack_encoded (opendroneid.h lines ~599+):
    byte0: ProtoVersion:4 MessageType:4 (MessageType must be 0xF/PACKED)
    byte1: SingleMessageSize (uint8, must equal ODID_MESSAGE_SIZE)
    byte2: MsgPackSize (uint8, number of embedded messages)
    bytes3+: MsgPackSize * ODID_MESSAGE_SIZE raw embedded messages
    Returns a list of decoded dicts, one per embedded message that decoded
    successfully; embedded messages that fail to decode are reported with an
    "error" key rather than silently dropped or fabricated.
    """
    if len(raw) < 3:
        raise RemoteIDDecodeError("message pack too short for header")
    msg_type = peek_message_type(raw)
    if msg_type != MESSAGETYPE_PACKED:
        raise RemoteIDDecodeError(f"not a Message Pack (type={msg_type})")

    single_message_size = raw[1]
    msg_pack_size = raw[2]
    if single_message_size != ODID_MESSAGE_SIZE:
        raise RemoteIDDecodeError(
            f"unexpected SingleMessageSize {single_message_size} "
            f"(expected {ODID_MESSAGE_SIZE})"
        )
    expected_len = 3 + msg_pack_size * ODID_MESSAGE_SIZE
    if len(raw) < expected_len:
        raise RemoteIDDecodeError(
            f"message pack truncated: need {expected_len} bytes, got {len(raw)}"
        )

    results = []
    for i in range(msg_pack_size):
        start = 3 + i * ODID_MESSAGE_SIZE
        chunk = raw[start:start + ODID_MESSAGE_SIZE]
        try:
            results.append(decode_message(chunk))
        except RemoteIDDecodeError as e:
            results.append({"error": str(e), "raw_hex": chunk.hex()})
    return results


# =============================================================================
# Byte-exact test vectors -- real output of the actual reference C library.
# See module docstring "TEST VECTORS" section for exactly how these were
# produced (compiled opendroneid-core-c's own opendroneid.c with a harness
# calling encodeBasicIDMessage/encodeLocationMessage/encodeSelfIDMessage/
# encodeSystemMessage/encodeOperatorIDMessage using test_inout.c's own
# literal input values, then hex-dumped the resulting encoded structs).
# =============================================================================
TEST_VECTORS_HEX = {
    "BasicID": "02223132333435363738393031323334353637383930000000",
    "Location": "122624160b42bf241b6ed1b4b69808ac0870086b53150e0200",
    "SelfID": "320044726f6e65735255533a205265616c2045737461746500",
    "System": "4204a6bf241bd2d1b4b62300073209230824f907003fab0100",
    "OperatorID": "52003938373635343332313030313233343536373839000000",
}


def self_test() -> None:
    failures: List[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== decode_basic_id() vs real opendroneid-core-c encoder output ===")
    raw = bytes.fromhex(TEST_VECTORS_HEX["BasicID"])
    d = decode_basic_id(raw)
    check("id_type == CAA_REGISTRATION_ID", d["id_type"] == "CAA_REGISTRATION_ID")
    check("ua_type == HELICOPTER_OR_MULTIROTOR", d["ua_type"] == "HELICOPTER_OR_MULTIROTOR")
    check("uas_id round-trips (test_inout.c literal '12345678901234567890')",
          d["uas_id"] == "12345678901234567890")

    print("\n=== decode_location() vs real opendroneid-core-c encoder output ===")
    raw = bytes.fromhex(TEST_VECTORS_HEX["Location"])
    d = decode_location(raw)
    check("status == AIRBORNE", d["status"] == "AIRBORNE")
    check("direction ~= 215.7 deg (quantized to 1 deg)", abs(d["direction_deg"] - 216.0) < 1.0)
    check("speed_horizontal ~= 5.4 m/s (quantized to 0.25 m/s)",
          abs(d["speed_horizontal_mps"] - 5.4) < 0.3)
    check("speed_vertical ~= 5.25 m/s (quantized to 0.5 m/s)",
          abs(d["speed_vertical_mps"] - 5.25) < 0.3)
    check("latitude ~= 45.539309 deg (LATLON_MULT=1e7 quantization)",
          abs(d["latitude_deg"] - 45.539309) < 1e-6)
    check("longitude ~= -122.966389 deg", abs(d["longitude_deg"] - (-122.966389)) < 1e-6)
    check("altitude_baro ~= 100 m (ALT_DIV=0.5 quantization)",
          abs(d["altitude_baro_m"] - 100.0) < 0.5)
    check("altitude_geo ~= 110 m", abs(d["altitude_geo_m"] - 110.0) < 0.5)
    check("height_type == OVER_GROUND", d["height_type"] == "OVER_GROUND")
    check("height ~= 80 m", abs(d["height_m"] - 80.0) < 0.5)

    print("\n=== decode_self_id() vs real opendroneid-core-c encoder output ===")
    raw = bytes.fromhex(TEST_VECTORS_HEX["SelfID"])
    d = decode_self_id(raw)
    check("description round-trips (test_inout.c literal 'DronesRUS: Real Estate')",
          d["description"] == "DronesRUS: Real Estate")

    print("\n=== decode_system() vs real opendroneid-core-c encoder output ===")
    raw = bytes.fromhex(TEST_VECTORS_HEX["System"])
    d = decode_system(raw)
    check("operator_latitude ~= 45.539319 deg (Location.Latitude + 0.00001)",
          abs(d["operator_latitude_deg"] - 45.539319) < 1e-5)
    check("operator_longitude ~= -122.966379 deg (Location.Longitude + 0.00001)",
          abs(d["operator_longitude_deg"] - (-122.966379)) < 1e-5)
    check("area_count == 35", d["area_count"] == 35)
    check("area_radius ~= 75 m (10 m quantization -> nearest multiple of 10)",
          abs(d["area_radius_m"] - 75) <= 10)
    check("area_ceiling ~= 176.9 m", abs(d["area_ceiling_m"] - 176.9) < 0.5)
    check("area_floor ~= 41.7 m", abs(d["area_floor_m"] - 41.7) < 0.5)
    check("category_eu_code == 2 (ODID_CATEGORY_EU_SPECIFIC)", d["category_eu_code"] == 2)
    check("class_eu_code == 4 (ODID_CLASS_EU_CLASS_3)", d["class_eu_code"] == 4)
    check("operator_altitude_geo ~= 20.5 m", abs(d["operator_altitude_geo_m"] - 20.5) < 0.5)
    check("timestamp == 28000000 (raw seconds-since-2019 counter)",
          d["timestamp_s_since_2019"] == 28000000)

    print("\n=== decode_operator_id() vs real opendroneid-core-c encoder output ===")
    raw = bytes.fromhex(TEST_VECTORS_HEX["OperatorID"])
    d = decode_operator_id(raw)
    check("operator_id round-trips (test_inout.c literal '98765432100123456789')",
          d["operator_id"] == "98765432100123456789")

    print("\n=== decode_message_pack() over all five real encoded messages ===")
    pack_body = b"".join(bytes.fromhex(h) for h in (
        TEST_VECTORS_HEX["BasicID"], TEST_VECTORS_HEX["Location"],
        TEST_VECTORS_HEX["SelfID"], TEST_VECTORS_HEX["System"],
        TEST_VECTORS_HEX["OperatorID"],
    ))
    pack_header = bytes([0xF0 | 0x00, ODID_MESSAGE_SIZE, 5])  # MessageType=PACKED(0xF), ProtoVersion=0
    results = decode_message_pack(pack_header + pack_body)
    check("message pack decodes exactly 5 embedded messages", len(results) == 5)
    check("no embedded message reported an error",
          all("error" not in r for r in results))
    check("embedded message order preserved (BASIC_ID, LOCATION, SELF_ID, SYSTEM, OPERATOR_ID)",
          [r.get("message_type") for r in results] ==
          ["BASIC_ID", "LOCATION", "SELF_ID", "SYSTEM", "OPERATOR_ID"])

    print("\n=== Error handling: malformed input never fabricates a result ===")
    try:
        decode_basic_id(b"\x00" * 10)
        check("wrong-length payload raises RemoteIDDecodeError", False)
    except RemoteIDDecodeError:
        check("wrong-length payload raises RemoteIDDecodeError", True)
    try:
        decode_location(bytes.fromhex(TEST_VECTORS_HEX["BasicID"]))
        check("wrong-message-type payload raises RemoteIDDecodeError", False)
    except RemoteIDDecodeError:
        check("wrong-message-type payload raises RemoteIDDecodeError", True)

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="Decode the byte-exact TEST_VECTORS_HEX (real "
                          "opendroneid-core-c encoder output, see module "
                          "docstring) and verify every field, then exit. "
                          "This is the only thing exercised in this session -- "
                          "see module docstring HARDWARE STATUS. There is no "
                          "live-capture mode: no monitor-mode Wi-Fi adapter or "
                          "BLE 5 scanner exists on this project's hardware yet.")
    ap.add_argument("--decode-hex", metavar="HEX",
                     help="Decode a single 25-byte ODID message given as a hex "
                          "string (49-50 hex chars) and print the result. "
                          "Utility for offline analysis of a captured payload; "
                          "does not touch any live radio.")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    if args.decode_hex:
        raw = bytes.fromhex(args.decode_hex)
        try:
            print(decode_message(raw))
        except RemoteIDDecodeError as e:
            print(f"decode error: {e}", file=sys.stderr)
            return 1
        return 0

    ap.print_help()
    print(
        "\nNOTE: this module has no live-capture entry point (see module "
        "docstring HARDWARE STATUS -- no monitor-mode Wi-Fi adapter or BLE 5 "
        "scanner on hand). Use --self-test to verify the decoder, or "
        "--decode-hex to decode an offline-captured 25-byte ODID payload.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
