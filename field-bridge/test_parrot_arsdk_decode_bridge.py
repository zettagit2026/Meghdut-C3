#!/usr/bin/env python3
"""Unit tests for parrot_arsdk_decode_bridge.py (task #109).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 parrot_arsdk_decode_bridge.py --self-test`),
ported into discrete pytest functions so each assertion group runs and
reports independently under the standard field-bridge suite.

Run: pytest field-bridge/test_parrot_arsdk_decode_bridge.py -v
"""
import struct

import pytest

from parrot_arsdk_decode_bridge import (
    ARSDKFrameError,
    CLASS_ARDRONE3_PILOTINGSTATE,
    CLASS_COMMON_COMMONSTATE,
    CMD_ARDRONE3_PILOTINGSTATE_FLYINGSTATECHANGED,
    CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED,
    FLYING_STATE_ENUM_NAMES,
    FRAME_TYPE_ACK,
    FRAME_TYPE_DATA,
    FRAME_TYPE_DATA_WITH_ACK,
    PROJECT_ARDRONE3,
    PROJECT_COMMON,
    _build_arcommand,
    _build_frame,
    decode_arcommand_header,
    decode_frame,
    decode_frame_header,
    iter_frames,
    self_test,
)


def test_embedded_self_test_passes():
    # The module's own self_test() is the authoritative correctness check
    # (real, hand-computed test vectors built strictly from the cited
    # ARSDK3 wire-format constants); run it under pytest too so a
    # regression here fails CI, not just manual invocation.
    self_test()


@pytest.fixture
def battery_frame():
    battery_payload = _build_arcommand(
        PROJECT_COMMON, CLASS_COMMON_COMMONSTATE,
        CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED, bytes([77]))
    return _build_frame(FRAME_TYPE_DATA, 125, 42, battery_payload)


def test_decode_frame_header_fields(battery_frame):
    h = decode_frame_header(battery_frame)
    assert h["frame_type"] == "DATA"
    assert h["buffer_id"] == 125 and h["buffer_name"] == "D2C_NAVDATA"
    assert h["seq"] == 42
    assert h["total_size"] == 7 + 5  # 4 header + 1 percent byte
    assert h["payload_size"] == 5


def test_decode_battery_state_changed(battery_frame):
    result = decode_frame(battery_frame)
    assert result["frame"]["frame_type"] == "DATA"
    assert result["arcommand"]["project_id"] == 0
    assert result["arcommand"]["class_id"] == 5
    assert result["arcommand"]["command_id"] == 1
    assert result["arcommand"]["percent"] == 77
    assert result["arcommand"]["command"] == "common.CommonState.BatteryStateChanged"


@pytest.mark.parametrize("state_idx,expected_name", list(enumerate(FLYING_STATE_ENUM_NAMES)))
def test_decode_flying_state_changed(state_idx, expected_name):
    args = struct.pack("<i", state_idx)
    payload = _build_arcommand(PROJECT_ARDRONE3, CLASS_ARDRONE3_PILOTINGSTATE,
                                CMD_ARDRONE3_PILOTINGSTATE_FLYINGSTATECHANGED, args)
    frame = _build_frame(FRAME_TYPE_DATA_WITH_ACK, 126, state_idx, payload)
    r = decode_frame(frame)
    assert r["arcommand"]["state"] == expected_name
    assert r["frame"]["frame_type"] == "DATA_WITH_ACK"


def test_ack_frame_decode():
    ack_frame = _build_frame(FRAME_TYPE_ACK, 11, 5, bytes([5]))
    r = decode_frame(ack_frame)
    assert r["frame"]["frame_type"] == "ACK"
    assert r["ack_seq"] == 5


def test_unknown_command_reports_raw_hex_not_fabricated():
    unknown_payload = _build_arcommand(9, 9, 999, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    unknown_frame = _build_frame(FRAME_TYPE_DATA, 10, 1, unknown_payload)
    r = decode_frame(unknown_frame)
    assert r["arcommand"]["command"] == "UNKNOWN/undecoded"
    assert r["arcommand"]["args_hex"] == "deadbeef"


def test_iter_frames_multiple_back_to_back(battery_frame):
    ack_frame = _build_frame(FRAME_TYPE_ACK, 11, 5, bytes([5]))
    unknown_payload = _build_arcommand(9, 9, 999, bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    unknown_frame = _build_frame(FRAME_TYPE_DATA, 10, 1, unknown_payload)
    stream = battery_frame + ack_frame + unknown_frame
    frames = list(iter_frames(stream))
    assert len(frames) == 3
    assert [f[0]["frame_type"] for f in frames] == ["DATA", "ACK", "DATA"]


def test_truncated_header_raises():
    with pytest.raises(ARSDKFrameError):
        decode_frame_header(bytes([1, 2, 3]))


def test_size_smaller_than_header_raises():
    with pytest.raises(ARSDKFrameError):
        decode_frame_header(struct.pack("<BBBI", FRAME_TYPE_DATA, 0, 0, 3))


def test_truncated_trailing_frame_raises(battery_frame):
    with pytest.raises(ARSDKFrameError):
        list(iter_frames(battery_frame[:-1]))  # truncate last byte


def test_short_arcommand_header_raises():
    with pytest.raises(ARSDKFrameError):
        decode_arcommand_header(bytes([0, 0]))
