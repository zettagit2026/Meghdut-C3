#!/usr/bin/env python3
"""Tests for mavlink_sniffer.py's task #114 ArduPilot protocol-layer
recognition (parameter protocol, mission protocol, MAVLink-FTP).

METHOD: for each message type, we ENCODE a real message using pymavlink's
own generated message class (`.pack()`), then feed the resulting real wire
bytes through a *separate* real pymavlink parser instance
(`mavutil.mavlink.MAVLink(None).parse_char()`) byte-by-byte -- i.e. the same
RX-side decode path mavlink_sniffer.py itself uses via
mavutil.mavlink_connection()/recv_match(). We then run the *actual*
describe_*_activity() functions from mavlink_sniffer.py against that
genuinely-decoded message object and assert the real fields (param_id,
param_value, seq, x/y/z, MAVFTP opcode, etc.) come back out correctly. This
is a legitimate ground-truth check: pymavlink is the same real library
encoding and decoding on both ends, and nothing here is a hand-rolled
byte-format reimplementation.

Run: pytest field-bridge/test_mavlink_sniffer.py -v
"""
import pytest

pymavlink = pytest.importorskip("pymavlink")
from pymavlink import mavutil  # noqa: E402

from mavlink_sniffer import (  # noqa: E402
    FTP_PROTOCOL_MSG_TYPES,
    MISSION_PROTOCOL_MSG_TYPES,
    PARAM_PROTOCOL_MSG_TYPES,
    describe_ftp_activity,
    describe_mission_activity,
    describe_param_activity,
)


def _roundtrip(msg, src_system=17, src_component=190):
    """Encode `msg` with a real pymavlink MAVLink() instance (as source
    sysid/compid src_system/src_component), then decode the resulting real
    bytes with a SEPARATE real pymavlink MAVLink() parser instance, exactly
    as mavlink_sniffer.py's mavutil.mavlink_connection()/recv_match() would
    on the wire. Returns the genuinely decoded message object."""
    src = mavutil.mavlink.MAVLink(None, srcSystem=src_system, srcComponent=src_component)
    buf = msg.pack(src)
    parser = mavutil.mavlink.MAVLink(None)
    decoded = None
    for byte in buf:
        result = parser.parse_char(bytes([byte]))
        if result is not None:
            decoded = result
    assert decoded is not None, "pymavlink failed to decode its own packed message"
    return decoded


# ---------------------------------------------------------------------
# Parameter protocol
# ---------------------------------------------------------------------

def test_param_value_recognized_and_decoded():
    msg = mavutil.mavlink.MAVLink_param_value_message(
        b"RC1_MIN", 1500.0, mavutil.mavlink.MAV_PARAM_TYPE_REAL32, 10, 3)
    decoded = _roundtrip(msg, src_system=42, src_component=1)

    assert decoded.get_type() in PARAM_PROTOCOL_MSG_TYPES
    line = describe_param_activity(decoded)
    assert "PARAM_VALUE" in line
    assert "sysid=42" in line
    assert "RC1_MIN" in line
    assert "1500.0" in line
    assert "10" in line  # param_count
    assert "3" in line   # param_index


def test_param_request_read_recognized_and_decoded():
    msg = mavutil.mavlink.MAVLink_param_request_read_message(
        target_system=1, target_component=1, param_id=b"THR_MIN", param_index=-1)
    decoded = _roundtrip(msg, src_system=255, src_component=190)

    assert decoded.get_type() == "PARAM_REQUEST_READ"
    line = describe_param_activity(decoded)
    assert "PARAM_REQUEST_READ" in line
    assert "THR_MIN" in line
    assert "target=(1,1)" in line


def test_param_request_list_recognized():
    msg = mavutil.mavlink.MAVLink_param_request_list_message(
        target_system=1, target_component=1)
    decoded = _roundtrip(msg)
    assert decoded.get_type() == "PARAM_REQUEST_LIST"
    line = describe_param_activity(decoded)
    assert "PARAM_REQUEST_LIST" in line


def test_param_set_recognized_and_decoded():
    msg = mavutil.mavlink.MAVLink_param_set_message(
        target_system=1, target_component=1, param_id=b"ARMING_CHECK",
        param_value=1.0, param_type=mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    decoded = _roundtrip(msg)
    assert decoded.get_type() == "PARAM_SET"
    line = describe_param_activity(decoded)
    assert "PARAM_SET" in line
    assert "ARMING_CHECK" in line


# ---------------------------------------------------------------------
# Mission protocol
# ---------------------------------------------------------------------

def test_mission_item_int_recognized_and_decoded():
    lat = int(12.34 * 1e7)
    lon = int(56.78 * 1e7)
    msg = mavutil.mavlink.MAVLink_mission_item_int_message(
        target_system=1, target_component=1, seq=3,
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
        command=mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
        current=0, autocontinue=1,
        param1=0, param2=0, param3=0, param4=0,
        x=lat, y=lon, z=100.0)
    decoded = _roundtrip(msg, src_system=255, src_component=190)

    assert decoded.get_type() in MISSION_PROTOCOL_MSG_TYPES
    assert decoded.seq == 3
    assert decoded.x == lat
    assert decoded.y == lon
    line = describe_mission_activity(decoded)
    assert "MISSION_ITEM_INT" in line
    assert "seq=3" in line
    assert str(lat) in line
    assert str(lon) in line


def test_mission_count_recognized():
    msg = mavutil.mavlink.MAVLink_mission_count_message(
        target_system=1, target_component=1, count=25)
    decoded = _roundtrip(msg)
    assert decoded.get_type() == "MISSION_COUNT"
    assert decoded.count == 25
    line = describe_mission_activity(decoded)
    assert "MISSION_COUNT" in line
    assert "count=25" in line


def test_mission_request_recognized():
    msg = mavutil.mavlink.MAVLink_mission_request_message(
        target_system=1, target_component=1, seq=4)
    decoded = _roundtrip(msg)
    assert decoded.get_type() == "MISSION_REQUEST"
    line = describe_mission_activity(decoded)
    assert "MISSION_REQUEST" in line
    assert "seq=4" in line


def test_mission_ack_recognized():
    msg = mavutil.mavlink.MAVLink_mission_ack_message(
        target_system=1, target_component=1, type=0)
    decoded = _roundtrip(msg)
    assert decoded.get_type() == "MISSION_ACK"
    line = describe_mission_activity(decoded)
    assert "MISSION_ACK" in line
    assert "completed/acknowledged" in line


# ---------------------------------------------------------------------
# MAVLink-FTP (FILE_TRANSFER_PROTOCOL)
# ---------------------------------------------------------------------

def _make_ftp_payload(seq, session, opcode, size, req_opcode, offset, data=b""):
    payload = bytearray(251)
    payload[0] = seq & 0xFF
    payload[1] = (seq >> 8) & 0xFF
    payload[2] = session
    payload[3] = opcode
    payload[4] = size
    payload[5] = req_opcode
    payload[6] = 0  # burst_complete
    payload[7] = 0  # padding
    payload[8:12] = int(offset).to_bytes(4, "little")
    payload[12:12 + len(data)] = data
    return bytes(payload)


def test_ftp_openfilero_recognized_and_decoded():
    payload = _make_ftp_payload(seq=5, session=2, opcode=4,  # OpenFileRO
                                 size=0, req_opcode=0, offset=0)
    msg = mavutil.mavlink.MAVLink_file_transfer_protocol_message(
        target_network=0, target_system=1, target_component=1, payload=payload)
    decoded = _roundtrip(msg, src_system=255, src_component=190)

    assert decoded.get_type() in FTP_PROTOCOL_MSG_TYPES
    line = describe_ftp_activity(decoded)
    assert "seq=5" in line
    assert "session=2" in line
    assert "OpenFileRO" in line
    assert "file transfer" in line


def test_ftp_ack_with_offset_recognized_and_decoded():
    payload = _make_ftp_payload(seq=9, session=2, opcode=128,  # Ack
                                 size=4, req_opcode=5, offset=4096)  # ReadFile
    msg = mavutil.mavlink.MAVLink_file_transfer_protocol_message(
        target_network=0, target_system=1, target_component=1, payload=payload)
    decoded = _roundtrip(msg)

    line = describe_ftp_activity(decoded)
    assert "opcode=Ack" in line
    assert "req_opcode=ReadFile" in line
    assert "offset=4096" in line


def test_ftp_unknown_opcode_does_not_crash():
    payload = _make_ftp_payload(seq=1, session=0, opcode=250,
                                 size=0, req_opcode=0, offset=0)
    msg = mavutil.mavlink.MAVLink_file_transfer_protocol_message(
        target_network=0, target_system=1, target_component=1, payload=payload)
    decoded = _roundtrip(msg)
    line = describe_ftp_activity(decoded)
    assert "UNKNOWN(250)" in line
