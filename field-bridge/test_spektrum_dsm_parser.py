#!/usr/bin/env python3
"""Unit tests for spektrum_dsm_parser.py (task #101).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 spektrum_dsm_parser.py --self-test`), plus a
few extra edge cases exercised here under pytest. No live CYRF6936-class RF
hardware exists in this project to validate against real captured traffic
(see module docstring HARDWARE STATUS) -- these tests exercise
protocol-decode correctness against spec-conformant constructed frames only.

Run: pytest field-bridge/test_spektrum_dsm_parser.py -v
"""
import pytest

from spektrum_dsm_parser import (
    DSM2_BITS_PER_CHANNEL,
    DSM2_HOP_TABLE_SIZE,
    DSMX_BITS_PER_CHANNEL,
    DSMX_HOP_TABLE_SIZE,
    FRAME_LEN,
    build_frame,
    generate_hop_table,
    mfg_id_from_bytes,
    pack_channel_slot,
    parse_frame,
    self_test,
    unpack_channel_slot,
)


def test_embedded_self_test_passes():
    self_test()


def test_channel_slot_round_trip_dsm2():
    for cid, val in ((0, 0), (2, 512), (6, 1023)):
        packed = pack_channel_slot(cid, val, DSM2_BITS_PER_CHANNEL)
        assert unpack_channel_slot(packed, DSM2_BITS_PER_CHANNEL) == (cid, val)


def test_channel_slot_round_trip_dsmx():
    for cid, val in ((0, 0), (3, 1024), (11, 2047)):
        packed = pack_channel_slot(cid, val, DSMX_BITS_PER_CHANNEL)
        assert unpack_channel_slot(packed, DSMX_BITS_PER_CHANNEL) == (cid, val)


def test_pack_channel_slot_rejects_dsm2_overflow_value():
    with pytest.raises(ValueError):
        pack_channel_slot(0, 1024, DSM2_BITS_PER_CHANNEL)


def test_build_and_parse_frame_round_trip():
    header = b"\x00\x01"
    ids = [0, 1, 2, 3, 4, 5, 6]
    values = [10, 20, 30, 40, 50, 60, 70]
    frame = build_frame(header, ids, values, DSM2_BITS_PER_CHANNEL)
    assert len(frame) == FRAME_LEN
    parsed = parse_frame(frame, DSM2_BITS_PER_CHANNEL)
    assert parsed is not None
    assert parsed.header == header
    assert parsed.channel_ids == ids
    assert parsed.channel_values == values


def test_parse_frame_rejects_wrong_length():
    assert parse_frame(b"\x00" * 10, DSM2_BITS_PER_CHANNEL) is None


def test_parse_frame_rejects_invalid_bits():
    frame = build_frame(b"\x00\x00", [0], [1], DSM2_BITS_PER_CHANNEL)
    with pytest.raises(ValueError):
        parse_frame(frame, 12)


def test_mfg_id_from_bytes_is_bitwise_not_of_be_concat():
    mfg = bytes([0xAA, 0xBB, 0xCC, 0xDD])
    expected = (~int.from_bytes(mfg, "big")) & 0xFFFFFFFF
    assert mfg_id_from_bytes(mfg) == expected


def test_generate_hop_table_sizes_and_id_dependence():
    dsm2 = generate_hop_table(0x1000, is_dsmx=False)
    dsmx = generate_hop_table(0x1000, is_dsmx=True)
    assert len(dsm2) == DSM2_HOP_TABLE_SIZE
    assert len(dsmx) == DSMX_HOP_TABLE_SIZE
    assert all(0 <= r <= 4 for r in dsm2 + dsmx)
    assert generate_hop_table(0x1000, is_dsmx=True) == dsmx
    assert generate_hop_table(0x2042, is_dsmx=True) != dsmx
