#!/usr/bin/env python3
"""Unit tests for flysky_afhds_parser.py (task #101).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 flysky_afhds_parser.py --self-test`), plus a
few extra edge cases exercised here under pytest. No live A7105-class RF
hardware exists in this project to validate against real captured traffic
(see module docstring HARDWARE STATUS) -- these tests exercise
protocol-decode correctness against spec-conformant constructed frames only.

Run: pytest field-bridge/test_flysky_afhds_parser.py -v
"""
import pytest

from flysky_afhds_parser import (
    HEADER_LEN,
    MAX_CHANNELS,
    NUM_HOP_CHANNELS,
    PACKET_TYPE_BIND_REQUEST,
    PACKET_TYPE_FAILSAFE,
    PACKET_TYPE_SETTINGS,
    PACKET_TYPE_STICK_DATA,
    build_frame,
    generate_hop_sequence,
    pack_stick_channels,
    parse_frame,
    self_test,
    unpack_stick_channels,
)


def test_embedded_self_test_passes():
    self_test()


def test_pack_unpack_channels_round_trip():
    values = [1000, 1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900, 2000, 1050, 1950, 1500]
    assert unpack_stick_channels(pack_stick_channels(values)) == values


def test_pack_stick_channels_rejects_too_many_channels():
    with pytest.raises(ValueError):
        pack_stick_channels([1500] * (MAX_CHANNELS + 1))


def test_build_and_parse_stick_data_frame():
    values = [1500] * 10
    payload = pack_stick_channels(values)
    frame = build_frame(PACKET_TYPE_STICK_DATA, tx_id=0x11223344, rx_id=0x55667788, payload=payload)
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed.packet_type == PACKET_TYPE_STICK_DATA
    assert parsed.tx_id == 0x11223344
    assert parsed.rx_id == 0x55667788
    assert parsed.channels == values


@pytest.mark.parametrize("packet_type", [PACKET_TYPE_SETTINGS, PACKET_TYPE_FAILSAFE, PACKET_TYPE_BIND_REQUEST])
def test_non_stick_data_packet_types_recognized(packet_type):
    frame = build_frame(packet_type, tx_id=1, rx_id=2, payload=b"\x00\x00")
    parsed = parse_frame(frame)
    assert parsed is not None
    assert parsed.packet_type == packet_type
    assert parsed.channels is None


def test_parse_frame_rejects_unknown_packet_type():
    frame = build_frame(0xEE, tx_id=1, rx_id=2, payload=b"\x00")
    assert parse_frame(frame) is None


def test_parse_frame_rejects_short_header():
    assert parse_frame(b"\x58\x01\x02") is None


def test_generate_hop_sequence_deterministic_and_id_dependent():
    a = generate_hop_sequence(0x11111111)
    b = generate_hop_sequence(0x11111111)
    c = generate_hop_sequence(0x22222222)
    assert a == b
    assert a != c
    assert len(a) == NUM_HOP_CHANNELS
    assert all(1 <= ch <= 168 for ch in a)
