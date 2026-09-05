#!/usr/bin/env python3
"""Byte-exact tests for wifi_arsdk_encode.py (MEGHDUT C3 Phase 1b).

Asserts the encoder emits the exact frames a real unencrypted Parrot / Tello
would accept, using the source-VERIFIED command IDs (ardrone3 Piloting
Landing=3, Emergency=4; Tello ASCII land/emergency/command), and that the
encoder REFUSES any command it cannot verify (no guessed frames, no skip/xfail).
"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wifi_arsdk_encode as enc
import parrot_arsdk_decode_bridge as dec


# ---------------------------------------------------------------------------
# ARSDK3: byte-exact frames, verified command IDs
# ---------------------------------------------------------------------------
def test_land_byte_exact():
    # ARNetworkAL(7): type=4(DATA_WITH_ACK) buf=11(C2D_ACK) seq=0 size=11(LE)
    # ARCommand(4)  : project=1 class=0 command=3(LE)  -> Landing, no args
    assert enc.encode_land(seq=0) == bytes.fromhex("040b000b00000001000300")


def test_emergency_byte_exact():
    # type=4 buf=12(C2D_EMERGENCY) seq=0 size=11 ; project=1 class=0 command=4
    assert enc.encode_emergency(seq=0) == bytes.fromhex("040c000b00000001000400")


def test_land_header_fields():
    frame = enc.encode_land(seq=7)
    frame_type, buffer_id, seq = struct.unpack_from("<BBB", frame, 0)
    total_size = struct.unpack_from("<I", frame, 3)[0]
    project, class_id = struct.unpack_from("<BB", frame, 7)
    command_id = struct.unpack_from("<H", frame, 9)[0]
    assert frame_type == dec.FRAME_TYPE_DATA_WITH_ACK == 4
    assert buffer_id == enc.C2D_ACK == 11
    assert seq == 7
    assert total_size == 11               # 7 header + 4 arcommand, no args
    assert len(frame) == total_size
    assert (project, class_id, command_id) == (1, 0, 3)  # ardrone3/Piloting/Landing


def test_emergency_header_fields():
    frame = enc.encode_emergency(seq=255)
    frame_type, buffer_id, seq = struct.unpack_from("<BBB", frame, 0)
    total_size = struct.unpack_from("<I", frame, 3)[0]
    project, class_id = struct.unpack_from("<BB", frame, 7)
    command_id = struct.unpack_from("<H", frame, 9)[0]
    assert frame_type == 4
    assert buffer_id == enc.C2D_EMERGENCY == 12
    assert seq == 255
    assert total_size == 11
    assert (project, class_id, command_id) == (1, 0, 4)  # ardrone3/Piloting/Emergency


def test_verified_command_ids_match_registry():
    # Guard against silent drift of the source-verified IDs.
    assert enc.PROJECT_ARDRONE3 == 1
    assert enc.CLASS_ARDRONE3_PILOTING == 0
    assert enc.ARDRONE3_PILOTING_COMMANDS["land"].command_id == 3
    assert enc.ARDRONE3_PILOTING_COMMANDS["emergency"].command_id == 4
    assert enc.ARDRONE3_PILOTING_COMMANDS["land"].verified is True
    assert enc.ARDRONE3_PILOTING_COMMANDS["emergency"].verified is True
    # Every registered command carries a source citation.
    for spec in enc.ARDRONE3_PILOTING_COMMANDS.values():
        assert "arsdk-xml" in spec.citation


def test_seq_out_of_range_rejected():
    with pytest.raises(ValueError):
        enc.encode_land(seq=256)


# ---------------------------------------------------------------------------
# Round-trip through the real RX decoder: the emitted frame must decode back
# to exactly the intended command (independent correctness check).
# ---------------------------------------------------------------------------
def test_land_roundtrips_through_decoder():
    decoded = dec.decode_frame(enc.encode_land(seq=3))
    assert decoded["frame"]["frame_type"] == "DATA_WITH_ACK"
    assert decoded["frame"]["buffer_name"] == "C2D_ACK"
    assert decoded["arcommand"]["project_id"] == 1
    assert decoded["arcommand"]["class_id"] == 0
    assert decoded["arcommand"]["command_id"] == 3


def test_emergency_roundtrips_through_decoder():
    decoded = dec.decode_frame(enc.encode_emergency(seq=3))
    assert decoded["frame"]["frame_type"] == "DATA_WITH_ACK"
    assert decoded["frame"]["buffer_name"] == "C2D_EMERGENCY"
    assert decoded["arcommand"]["project_id"] == 1
    assert decoded["arcommand"]["class_id"] == 0
    assert decoded["arcommand"]["command_id"] == 4


# ---------------------------------------------------------------------------
# HONESTY GATE: unverified / unknown commands are REFUSED, never guessed.
# ---------------------------------------------------------------------------
def test_unknown_ardrone3_command_refused():
    with pytest.raises(enc.UnverifiedCommandError):
        enc.encode_ardrone3_piloting("takeoff")   # not verified/registered here
    with pytest.raises(enc.UnverifiedCommandError):
        enc.encode_ardrone3_piloting("flip")


def test_explicitly_unverified_spec_refused():
    # A command PRESENT in the registry but flagged unverified must still be
    # refused -- the encoder never emits a guessed frame.
    enc.ARDRONE3_PILOTING_COMMANDS["_unverified_probe"] = enc.ArdronePilotingCommand(
        xml_name="ProbeOnly", command_id=None, default_buffer_id=enc.C2D_ACK,
        verified=False, citation="none -- deliberately unverified test probe",
    )
    try:
        with pytest.raises(enc.UnverifiedCommandError):
            enc.encode_ardrone3_piloting("_unverified_probe")
    finally:
        del enc.ARDRONE3_PILOTING_COMMANDS["_unverified_probe"]


# ---------------------------------------------------------------------------
# Tello: literal ASCII tokens to 192.168.10.1:8889 (NOT ARSDK)
# ---------------------------------------------------------------------------
def test_tello_land_ascii():
    payload, addr = enc.tello_land()
    assert payload == b"land"
    assert addr == ("192.168.10.1", 8889)


def test_tello_emergency_ascii():
    payload, addr = enc.tello_emergency()
    assert payload == b"emergency"
    assert addr == ("192.168.10.1", 8889)


def test_tello_enter_sdk_ascii():
    payload, addr = enc.tello_enter_sdk()
    assert payload == b"command"
    assert addr == ("192.168.10.1", 8889)


def test_tello_no_terminator():
    # ASCII token only -- no trailing newline/null (Tello SDK 2.0 wire format).
    for payload, _ in (enc.tello_land(), enc.tello_emergency(), enc.tello_enter_sdk()):
        assert not payload.endswith(b"\n")
        assert not payload.endswith(b"\x00")


def test_tello_unknown_token_refused():
    with pytest.raises(enc.TelloCommandError):
        enc.encode_tello("selfdestruct")


# ---------------------------------------------------------------------------
# Tello and ARSDK are DISTINCT protocol families, not conflated.
# ---------------------------------------------------------------------------
def test_tello_and_arsdk_not_conflated():
    arsdk_emergency = enc.encode_emergency()
    tello_emergency_payload, _ = enc.tello_emergency()
    assert arsdk_emergency != tello_emergency_payload
    assert b"emergency" not in arsdk_emergency   # ARSDK is binary, not ASCII
    assert tello_emergency_payload == b"emergency"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
