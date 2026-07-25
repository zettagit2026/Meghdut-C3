#!/usr/bin/env python3
"""Unit tests for frsky_smartport_parser.py (task #110).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 frsky_smartport_parser.py --self-test`), plus a
couple of extra edge cases exercised here under pytest so they run as part
of the standard field-bridge suite.

Run: pytest field-bridge/test_frsky_smartport_parser.py -v
"""
from frsky_smartport_parser import (
    DataFrame,
    PollFrame,
    SmartPortParser,
    START_BYTE,
    DATA_FRAME_TYPE,
    crc_fold,
    crc_fold_alt,
    self_test,
    stuff,
    unstuff,
)


def _feed_all(parser: SmartPortParser, data: bytes):
    result = None
    for b in data:
        r = parser.feed(b)
        if r is not None:
            result = r
    return result


def test_embedded_self_test_passes():
    # The module's own self_test() is the authoritative correctness check
    # (real, hand-computed/cross-checked test vectors); run it under pytest
    # too so a regression here fails CI, not just manual invocation.
    self_test()


def test_crc_fold_matches_alt_implementation():
    for sample in (bytes([0x10, 0x00, 0x08, 42, 0, 0, 0]), bytes(range(7)), b"\x00"):
        assert crc_fold(sample) == crc_fold_alt(sample)


def test_valid_data_frame_parses():
    body = bytes([DATA_FRAME_TYPE, 0x00, 0x08]) + (77).to_bytes(4, "little", signed=True)
    assert 0x7E not in body and 0x7D not in body
    crc = crc_fold(body)
    frame = bytes([START_BYTE]) + body + bytes([crc])
    result = _feed_all(SmartPortParser(), frame)
    assert isinstance(result, DataFrame)
    assert result.data_id == 0x0800
    assert result.value == 77


def test_bad_crc_data_frame_rejected():
    body = bytes([DATA_FRAME_TYPE, 0x00, 0x08]) + (77).to_bytes(4, "little", signed=True)
    crc = crc_fold(body) ^ 0xFF  # deliberately wrong
    frame = bytes([START_BYTE]) + body + bytes([crc])
    result = _feed_all(SmartPortParser(), frame)
    assert result is None


def test_poll_frame_parses():
    result = _feed_all(SmartPortParser(), bytes([START_BYTE, 0x1B]))
    assert isinstance(result, PollFrame)
    assert result.sensor_id == 0x1B


def test_stuffing_round_trip():
    payload = bytes([0x7E, 0x7D, 0x00, 0xFF, 0x7E])
    assert unstuff(stuff(payload)) == payload


def test_no_frame_without_start_byte():
    # Garbage bytes with no START marker must never produce a frame --
    # this is the "no synthetic/fallback data" honesty check: absence of
    # real framing must yield absence of output, not a guess.
    parser = SmartPortParser()
    result = _feed_all(parser, bytes(range(1, 50)))
    assert result is None
