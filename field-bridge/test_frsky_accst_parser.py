#!/usr/bin/env python3
"""Unit tests for frsky_accst_parser.py (task #101).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 frsky_accst_parser.py --self-test`), plus a few
extra edge cases exercised here under pytest.

NOTE ON SCOPE: FrSky ACCST/ACCESS D16/X is the RF control uplink, distinct
from SmartPort telemetry (frsky_smartport_parser.py, task #110) -- see the
module docstring for the full scope note. No live RF hardware exists in
this project to validate against real captured traffic (see module
docstring HARDWARE STATUS) -- these tests exercise protocol-decode
correctness against spec-conformant constructed frames only.

Run: pytest field-bridge/test_frsky_accst_parser.py -v
"""
import pytest

from frsky_accst_parser import (
    FRSKY_X_PACKET_LEN_V1,
    HOP_TABLE_SIZE,
    NUM_CHANNELS,
    build_frame,
    crc16,
    crc16_bitwise,
    generate_hop_table,
    pack_channels,
    parse_frame,
    self_test,
    unpack_channels,
)


def test_embedded_self_test_passes():
    self_test()


def test_crc16_matches_bitwise_reference():
    for sample in (b"", b"\x01\x02\x03", bytes(range(50))):
        assert crc16(sample) == crc16_bitwise(sample)


def test_pack_unpack_channels_round_trip():
    values = [0, 4095, 2048, 1, 3000, 500, 4094, 2, 100, 200, 300, 400, 500, 600, 700, 800]
    assert unpack_channels(pack_channels(values)) == values


def test_pack_channels_rejects_wrong_count():
    with pytest.raises(ValueError):
        pack_channels([0] * NUM_CHANNELS + [1])


def test_build_and_parse_frame_round_trip():
    values = [1000] * NUM_CHANNELS
    frame = build_frame(fixed_id=0x4321, counter=1, rssi_byte=90, channel_values=values)
    assert len(frame) == FRSKY_X_PACKET_LEN_V1
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed.fixed_id == 0x4321
    assert parsed.counter == 1
    assert parsed.rssi_byte == 90
    assert parsed.channels == values


def test_parse_frame_rejects_corrupted_crc():
    frame = bytearray(build_frame(fixed_id=1, counter=0, rssi_byte=0, channel_values=[0] * NUM_CHANNELS))
    frame[-1] ^= 0xFF
    assert parse_frame(bytes(frame)) is None


def test_parse_frame_rejects_truncated_input():
    assert parse_frame(b"\x1d\x01") is None


def test_generate_hop_table_deterministic_and_id_dependent():
    a = generate_hop_table(0xABCD)
    b = generate_hop_table(0xABCD)
    c = generate_hop_table(0x1234)
    assert a == b
    assert a != c
    assert len(a) == HOP_TABLE_SIZE
