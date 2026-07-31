#!/usr/bin/env python3
"""Unit tests for ltm_parser.py (task #112).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 ltm_parser.py --self-test`), ported into
discrete pytest functions so each assertion group runs and reports
independently under the standard field-bridge suite.

Run: pytest field-bridge/test_ltm_parser.py -v
"""
import struct

import pytest

import ltm_parser
from ltm_parser import (
    LTMFrame,
    LTMParser,
    LTMSerialBridge,
    LTM_PAYLOAD_LEN,
    build_frame,
    ltm_checksum,
    parse_attitude,
    parse_extended_nav,
    parse_gps,
    parse_navigation,
    parse_origin,
    parse_status,
    self_test,
)


def test_embedded_self_test_passes():
    # The module's own self_test() is the authoritative correctness check
    # (real, cross-checked test vectors); run it under pytest too so a
    # regression here fails CI, not just manual invocation.
    self_test()


@pytest.mark.parametrize("ft,expected_total", [
    ("G", 18), ("A", 10), ("S", 11), ("O", 18), ("N", 10), ("X", 10),
])
def test_frame_size_table(ft, expected_total):
    actual_total = 2 + 1 + LTM_PAYLOAD_LEN[ft] + 1
    assert actual_total == expected_total


def test_xor_checksum_identity_empty():
    assert ltm_checksum(b"") == 0


def test_xor_checksum_self_cancelling():
    assert ltm_checksum(bytes([0xAA, 0xAA])) == 0


def test_xor_checksum_simple():
    assert ltm_checksum(bytes([0x01, 0x02, 0x04])) == 0x07


def test_g_frame_round_trip():
    lat_raw = int(28.6139389 * 1e7)
    lon_raw = int(77.2090212 * 1e7)
    g_payload = struct.pack("<iiBiB", lat_raw, lon_raw, 12, 15000, (11 << 2) | 2)
    g_frame_bytes = build_frame("G", g_payload)
    assert len(g_frame_bytes) == 18
    frames = LTMParser().feed_bytes(g_frame_bytes)
    assert len(frames) == 1
    assert frames[0].frame_type == "G"
    gps = parse_gps(frames[0].payload)
    assert abs(gps["lat_deg"] - 28.6139389) < 1e-6
    assert abs(gps["lon_deg"] - 77.2090212) < 1e-6
    assert gps["ground_speed_ms"] == 12
    assert gps["altitude_cm"] == 15000
    assert gps["fix_type"] == 2
    assert gps["satellite_count"] == 11


def test_a_frame_round_trip():
    a_payload = struct.pack("<hhh", -15, 30, 270)
    a_frame_bytes = build_frame("A", a_payload)
    assert len(a_frame_bytes) == 10
    frames = LTMParser().feed_bytes(a_frame_bytes)
    assert len(frames) == 1
    att = parse_attitude(frames[0].payload)
    assert att["pitch_deg"] == -15
    assert att["roll_deg"] == 30
    assert att["heading_deg"] == 270


def test_s_frame_round_trip():
    status_byte = 0x01 | (5 << 2)  # armed=1, failsafe=0, mode=5
    s_payload = struct.pack("<HHBBB", 11100, 850, 90, 8, status_byte)
    s_frame_bytes = build_frame("S", s_payload)
    assert len(s_frame_bytes) == 11
    frames = LTMParser().feed_bytes(s_frame_bytes)
    assert len(frames) == 1
    stat = parse_status(frames[0].payload)
    assert stat["vbat_mv"] == 11100
    assert stat["current_ma"] == 850
    assert stat["rssi"] == 90
    assert stat["airspeed_ms"] == 8
    assert stat["armed"] is True
    assert stat["failsafe"] is False
    assert stat["flight_mode_code"] == 5


def test_o_frame_round_trip():
    o_lat_raw = int(28.6100000 * 1e7)
    o_lon_raw = int(77.2000000 * 1e7)
    o_payload = struct.pack("<iiiBB", o_lat_raw, o_lon_raw, 500, 1, 2)
    o_frame_bytes = build_frame("O", o_payload)
    assert len(o_frame_bytes) == 18
    frames = LTMParser().feed_bytes(o_frame_bytes)
    assert len(frames) == 1
    origin = parse_origin(frames[0].payload)
    assert abs(origin["home_lat_deg"] - 28.61) < 1e-6
    assert abs(origin["home_lon_deg"] - 77.20) < 1e-6
    assert origin["home_altitude_cm"] == 500


def test_n_frame_round_trip():
    n_payload = struct.pack("<BBBBBB", 1, 2, 3, 4, 0, 9)
    n_frame_bytes = build_frame("N", n_payload)
    assert len(n_frame_bytes) == 10
    frames = LTMParser().feed_bytes(n_frame_bytes)
    assert len(frames) == 1
    nav = parse_navigation(frames[0].payload)
    assert nav["waypoint_number"] == 4
    assert nav["flags"] == 9


def test_x_frame_round_trip():
    x_payload = struct.pack("<H", 120) + bytes(4)
    x_frame_bytes = build_frame("X", x_payload)
    assert len(x_frame_bytes) == 10
    frames = LTMParser().feed_bytes(x_frame_bytes)
    assert len(frames) == 1
    xnav = parse_extended_nav(frames[0].payload)
    assert xnav["hdop"] == 120


def test_corrupted_checksum_rejected():
    lat_raw = int(28.6139389 * 1e7)
    lon_raw = int(77.2090212 * 1e7)
    g_payload = struct.pack("<iiBiB", lat_raw, lon_raw, 12, 15000, (11 << 2) | 2)
    g_frame_bytes = build_frame("G", g_payload)
    corrupted = bytearray(g_frame_bytes)
    corrupted[-1] ^= 0xFF  # flip every bit of the checksum byte
    frames = LTMParser().feed_bytes(bytes(corrupted))
    assert len(frames) == 0


def test_corrupted_payload_byte_rejected():
    a_payload = struct.pack("<hhh", -15, 30, 270)
    a_frame_bytes = build_frame("A", a_payload)
    corrupted2 = bytearray(a_frame_bytes)
    corrupted2[3] ^= 0x01  # flip a payload bit; checksum no longer matches
    frames = LTMParser().feed_bytes(bytes(corrupted2))
    assert len(frames) == 0


def test_resync_past_leading_garbage():
    status_byte = 0x01 | (5 << 2)
    s_payload = struct.pack("<HHBBB", 11100, 850, 90, 8, status_byte)
    s_frame_bytes = build_frame("S", s_payload)
    garbage_then_real = b"\x00\x01\x02\xFF\xFF" + s_frame_bytes
    frames = LTMParser().feed_bytes(garbage_then_real)
    assert len(frames) == 1


def test_skip_coincidental_fake_header():
    lat_raw = int(28.6139389 * 1e7)
    lon_raw = int(77.2090212 * 1e7)
    g_payload = struct.pack("<iiBiB", lat_raw, lon_raw, 12, 15000, (11 << 2) | 2)
    g_frame_bytes = build_frame("G", g_payload)
    # A coincidental '$','T' pair followed by a non-type-letter byte must be
    # skipped without ever blocking (regression test for the resync loop's
    # "not a recognized type letter" branch).
    fake_header_then_real = b"$T\x00\x00" + g_frame_bytes
    frames = LTMParser().feed_bytes(fake_header_then_real)
    assert len(frames) == 1


def test_byte_at_a_time_streaming_matches_whole_buffer():
    lat_raw = int(28.6139389 * 1e7)
    lon_raw = int(77.2090212 * 1e7)
    g_payload = struct.pack("<iiBiB", lat_raw, lon_raw, 12, 15000, (11 << 2) | 2)
    g_frame_bytes = build_frame("G", g_payload)
    parser = LTMParser()
    streamed = []
    for b in g_frame_bytes:
        streamed.extend(parser.feed_bytes(bytes([b])))
    assert len(streamed) == 1 and streamed[0].raw == g_frame_bytes


def test_two_back_to_back_frames_in_one_chunk():
    a_payload = struct.pack("<hhh", -15, 30, 270)
    a_frame_bytes = build_frame("A", a_payload)
    status_byte = 0x01 | (5 << 2)
    s_payload = struct.pack("<HHBBB", 11100, 850, 90, 8, status_byte)
    s_frame_bytes = build_frame("S", s_payload)
    two_frames = a_frame_bytes + s_frame_bytes
    frames = LTMParser().feed_bytes(two_frames)
    assert len(frames) == 2
    assert frames[0].frame_type == "A"
    assert frames[1].frame_type == "S"


# -----------------------------------------------------------------------
# TASK #151: run_forever() idle-loop liveness heartbeat (same pattern as
# mavlink_sniffer.py's IDLE_HEARTBEAT_INTERVAL_S, task #139). Verifies the
# "still listening" print fires on the fixed cadence when the serial link
# is genuinely idle (empty reads), and not more often than that cadence.
# -----------------------------------------------------------------------

class _StopLoop(BaseException):
    """Sentinel used to break run_forever()'s `while True` after a fixed
    number of iterations, so the test doesn't hang. Deliberately derives
    from BaseException (like KeyboardInterrupt), NOT Exception -- the read
    loop's `except Exception` (for real serial I/O errors) would otherwise
    swallow this sentinel too and loop forever instead of propagating out."""


class _FakeIdleSerial:
    """Fake serial.Serial-like object whose .read() always returns an empty
    chunk (genuinely idle link), and raises _StopLoop once the caller has
    exhausted the number of reads the test wants to observe."""

    def __init__(self, max_reads: int):
        self.max_reads = max_reads
        self.n = 0

    def read(self, size):
        self.n += 1
        if self.n > self.max_reads:
            raise _StopLoop()
        return b""


def test_run_forever_prints_idle_heartbeat_on_cadence(monkeypatch, capsys):
    bridge = LTMSerialBridge("http://console.example", {}, "/dev/fake-ltm", 115200,
                              email="e@example.com", password="pw")
    fake_serial = _FakeIdleSerial(max_reads=3)
    monkeypatch.setattr(bridge, "_open_serial", lambda: fake_serial)

    # Fake clock: three idle reads at t=1000, t=1061, t=1062 (61s then 1s
    # apart) so exactly two of the three should cross the 60s cadence.
    fake_times = iter([1000.0, 1061.0, 1062.0])
    monkeypatch.setattr(ltm_parser.time, "time", lambda: next(fake_times))

    with pytest.raises(_StopLoop):
        bridge.run_forever()

    out = capsys.readouterr().out
    heartbeat_lines = [line for line in out.splitlines() if "[heartbeat]" in line]
    assert len(heartbeat_lines) == 2
    assert "still listening" in heartbeat_lines[0]
    assert "/dev/fake-ltm" in heartbeat_lines[0]
