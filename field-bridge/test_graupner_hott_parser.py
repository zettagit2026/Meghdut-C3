#!/usr/bin/env python3
"""Unit tests for graupner_hott_parser.py (task #111).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 graupner_hott_parser.py --self-test`), ported
into discrete pytest functions so each assertion group runs and reports
independently under the standard field-bridge suite.

Run: pytest field-bridge/test_graupner_hott_parser.py -v
"""
from typing import Dict, List

import pytest

from graupner_hott_parser import (
    HOTT_BINARY_BODY_LEN,
    HOTT_BINARY_FRAME_LEN,
    HOTT_BINARY_MODE_REQUEST_ID,
    HOTT_EAM_OFFSET_HEIGHT,
    HOTT_GPS_ALTITUDE_OFFSET,
    HOTT_EAM_SENSOR_TEXT_ID,
    HOTT_SENSOR_ID_AIRESC,
    HOTT_SENSOR_ID_EAM,
    HOTT_SENSOR_ID_GAM,
    HOTT_SENSOR_ID_GPS,
    HOTT_SENSOR_ID_VARIO,
    HOTT_TELEMETRY_AIRESC_SENSOR_ID,
    HOTT_TELEMETRY_EAM_SENSOR_ID,
    HOTT_TELEMETRY_GAM_SENSOR_ID,
    HOTT_TELEMETRY_GPS_SENSOR_ID,
    HOTT_TELEMETRY_NO_SENSOR_ID,
    HOTT_TELEMETRY_VARIO_SENSOR_ID,
    HOTT_TEXT_FRAME_LEN,
    HOTT_TEXT_MODE_REQUEST_ID,
    HoTTParser,
    HoTTRequestFrame,
    HoTTResponseFrame,
    SENSOR_TYPE_NAMES,
    build_binary_frame,
    build_text_frame,
    decode_text_rows,
    hott_checksum_accumulate,
    hott_checksum_sum,
    parse_airesc,
    parse_eam,
    parse_gam,
    parse_gps,
    parse_vario,
    self_test,
)


def test_embedded_self_test_passes():
    # The module's own self_test() is the authoritative correctness check
    # (real, hand-computed test vectors built strictly from the cited
    # betaflight/inav HoTT struct layouts); run it under pytest too so a
    # regression here fails CI, not just manual invocation.
    self_test()


@pytest.mark.parametrize("sample", [
    b"", bytes([0x7C, 0x8E, 0x00]), bytes(range(256)), b"\xff" * 44,
])
def test_checksum_cross_check(sample):
    assert hott_checksum_accumulate(sample) == hott_checksum_sum(sample)


def test_checksum_identity_empty_input():
    assert hott_checksum_accumulate(b"") == 0


def test_checksum_hand_computed():
    # sum of [0x7C, 0x8E, 0x01] = 0x7C+0x8E+0x01 = 0x10B, mod 256 = 0x0B.
    assert hott_checksum_accumulate(bytes([0x7C, 0x8E, 0x01])) == 0x0B


def _fill_from_body_indices(assignments: Dict[int, int]) -> bytes:
    fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
    for idx, val in assignments.items():
        if idx == 2:
            fill[0] = val & 0xFF
        elif 4 <= idx <= 42:
            fill[idx - 3] = val & 0xFF
        else:
            raise ValueError(f"unsupported body index {idx} in test fill data")
    return bytes(fill)


def test_eam_frame_round_trip():
    # battery 1 = 12.6V (raw 126), current = 5.0A (raw 50), main voltage =
    # 11.1V (raw 111), altitude = 120m (raw 620 = 120+500 offset),
    # climbrate = +2.50 m/s (raw 30000+250=30250), speed = 45 km/h.
    batt1_raw = 126
    alt_raw = 120 + HOTT_EAM_OFFSET_HEIGHT
    current_raw = 50
    mainv_raw = 111
    battcap_raw = 150
    climb_raw = 30000 + 250
    speed_raw = 45
    eam_assignments = {
        2: 0x00,
        20: batt1_raw & 0xFF, 21: batt1_raw >> 8,
        24: 20 + 25,
        25: 20 + 0,
        26: alt_raw & 0xFF, 27: alt_raw >> 8,
        28: current_raw & 0xFF, 29: current_raw >> 8,
        30: mainv_raw & 0xFF, 31: mainv_raw >> 8,
        32: battcap_raw & 0xFF, 33: battcap_raw >> 8,
        34: climb_raw & 0xFF, 35: climb_raw >> 8,
        36: 120,
        41: speed_raw & 0xFF, 42: speed_raw >> 8,
    }
    eam_fill = _fill_from_body_indices(eam_assignments)
    eam_frame_bytes = build_binary_frame(HOTT_TELEMETRY_EAM_SENSOR_ID, HOTT_SENSOR_ID_EAM, eam_fill)
    assert len(eam_frame_bytes) == HOTT_BINARY_FRAME_LEN

    parser = HoTTParser()
    frames = parser.feed_bytes(eam_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    assert isinstance(f, HoTTResponseFrame)
    assert f.sensor_type_id == HOTT_TELEMETRY_EAM_SENSOR_ID
    eam = parse_eam(f.body)
    assert abs(eam["batt1_voltage_v"] - 12.6) < 1e-9
    assert eam["temp1_c"] == 25
    assert eam["altitude_m"] == 120
    assert abs(eam["current_a"] - 5.0) < 1e-9
    assert abs(eam["main_voltage_v"] - 11.1) < 1e-9
    assert abs(eam["climbrate_ms"] - 2.5) < 1e-9
    assert eam["speed_kmh"] == 45
    assert SENSOR_TYPE_NAMES.get(f.sensor_type_id) == "EAM"


def test_gps_frame_round_trip():
    # lat 12deg 30.5000min N, lon 77deg 15.2500min E, 10 satellites, 3D fix.
    gps_speed_raw = 36
    ns_dm = 12 * 100 + 30
    ns_sec = 5000
    ew_dm = 77 * 100 + 15
    ew_sec = 2500
    home_dist_raw = 42
    gps_alt_raw = 100 + HOTT_GPS_ALTITUDE_OFFSET
    gps_climb_raw = 30000
    gps_assignments = {
        2: 0x00,
        6: 0,
        7: gps_speed_raw & 0xFF, 8: gps_speed_raw >> 8,
        9: 0,
        10: ns_dm & 0xFF, 11: ns_dm >> 8,
        12: ns_sec & 0xFF, 13: ns_sec >> 8,
        14: 0,
        15: ew_dm & 0xFF, 16: ew_dm >> 8,
        17: ew_sec & 0xFF, 18: ew_sec >> 8,
        19: home_dist_raw & 0xFF, 20: home_dist_raw >> 8,
        21: gps_alt_raw & 0xFF, 22: gps_alt_raw >> 8,
        23: gps_climb_raw & 0xFF, 24: gps_climb_raw >> 8,
        25: 120,
        26: 10,
        27: ord('3'),
        28: 0,
    }
    gps_fill = _fill_from_body_indices(gps_assignments)
    gps_frame_bytes = build_binary_frame(HOTT_TELEMETRY_GPS_SENSOR_ID, HOTT_SENSOR_ID_GPS, gps_fill)
    frames = HoTTParser().feed_bytes(gps_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    assert f.sensor_type_id == HOTT_TELEMETRY_GPS_SENSOR_ID
    gps = parse_gps(f.body)
    assert abs(gps["latitude"] - (12 + 30.5 / 60.0)) < 1e-6
    assert abs(gps["longitude"] - (77 + 15.25 / 60.0)) < 1e-6
    assert gps["num_satellites"] == 10
    assert gps["gps_fix_char"] == "3"
    assert gps["altitude_m"] == 100
    assert gps["home_distance_m"] == 42


def test_gam_frame_round_trip():
    # fuel 60%, RPM 3000 (raw 300), altitude 200m.
    rpm_raw = 300
    gam_alt_raw = 200 + HOTT_EAM_OFFSET_HEIGHT
    gam_climb_raw = 30000
    gam_assignments = {
        2: 0x00,
        16: 20,
        17: 20,
        18: 60,
        21: rpm_raw & 0xFF, 22: rpm_raw >> 8,
        23: gam_alt_raw & 0xFF, 24: gam_alt_raw >> 8,
        25: gam_climb_raw & 0xFF, 26: gam_climb_raw >> 8,
        27: 120,
    }
    gam_fill = _fill_from_body_indices(gam_assignments)
    gam_frame_bytes = build_binary_frame(HOTT_TELEMETRY_GAM_SENSOR_ID, HOTT_SENSOR_ID_GAM, gam_fill)
    frames = HoTTParser().feed_bytes(gam_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    assert f.sensor_type_id == HOTT_TELEMETRY_GAM_SENSOR_ID
    gam = parse_gam(f.body)
    assert gam["fuel_pct"] == 60
    assert gam["rpm"] == 3000
    assert gam["altitude_m"] == 200


def test_vario_frame_round_trip():
    # altitude 50m, max 80m, min 10m, climbrate +1.00 m/s.
    vario_fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
    vario_fill[0] = 0x00
    vario_fill[1] = 0x00
    alt_raw = 50 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[2] = alt_raw & 0xFF
    vario_fill[3] = alt_raw >> 8
    alt_max_raw = 80 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[4] = alt_max_raw & 0xFF
    vario_fill[5] = alt_max_raw >> 8
    alt_min_raw = 10 + HOTT_GPS_ALTITUDE_OFFSET
    vario_fill[6] = alt_min_raw & 0xFF
    vario_fill[7] = alt_min_raw >> 8
    climb_raw = 30000 + 100
    vario_fill[8] = climb_raw & 0xFF
    vario_fill[9] = climb_raw >> 8

    vario_frame_bytes = build_binary_frame(HOTT_TELEMETRY_VARIO_SENSOR_ID, HOTT_SENSOR_ID_VARIO, bytes(vario_fill))
    frames = HoTTParser().feed_bytes(vario_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    vario = parse_vario(f.body)
    assert vario["altitude_m"] == 50
    assert vario["altitude_max_m"] == 80
    assert vario["altitude_min_m"] == 10
    assert abs(vario["climbrate_ms"] - 1.0) < 1e-9


def test_airesc_frame_round_trip():
    # input voltage 16.8V, current 12.0A, RPM 8000, throttle 75%.
    airesc_fill = bytearray(HOTT_BINARY_BODY_LEN - 4)
    airesc_fill[0] = 0x00
    airesc_fill[1] = 0x00
    airesc_fill[2] = 0x00
    inv_raw = 168
    airesc_fill[3] = inv_raw & 0xFF
    airesc_fill[4] = inv_raw >> 8
    airesc_fill[5] = 0
    airesc_fill[6] = 0
    airesc_fill[7] = 0
    airesc_fill[8] = 0
    airesc_fill[9] = 45
    airesc_fill[10] = 60
    cur_raw = 120
    airesc_fill[11] = cur_raw & 0xFF
    airesc_fill[12] = cur_raw >> 8
    airesc_fill[13] = 0
    airesc_fill[14] = 0
    rpm_raw = 800
    airesc_fill[15] = rpm_raw & 0xFF
    airesc_fill[16] = rpm_raw >> 8
    airesc_fill[17] = 0
    airesc_fill[18] = 0
    airesc_fill[19] = 75

    airesc_frame_bytes = build_binary_frame(HOTT_TELEMETRY_AIRESC_SENSOR_ID, HOTT_SENSOR_ID_AIRESC, bytes(airesc_fill))
    frames = HoTTParser().feed_bytes(airesc_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    airesc = parse_airesc(f.body)
    assert abs(airesc["input_voltage_v"] - 16.8) < 1e-9
    assert abs(airesc["current_a"] - 12.0) < 1e-9
    assert airesc["rpm"] == 8000
    assert airesc["throttle_pct"] == 75


def test_text_mode_frame():
    text_frame_bytes = build_text_frame(HOTT_EAM_SENSOR_TEXT_ID, 0, ["HELLO WORLD", "ROW TWO"])
    assert len(text_frame_bytes) == HOTT_TEXT_FRAME_LEN
    frames = HoTTParser().feed_bytes(text_frame_bytes)
    assert len(frames) == 1
    f = frames[0]
    assert f.is_text_mode is True
    rows = decode_text_rows(f)
    assert rows[0].rstrip(".").rstrip() == "HELLO WORLD" or rows[0][:11] == "HELLO WORLD"
    assert rows[1][:7] == "ROW TWO"


def test_binary_mode_request_frame():
    binary_request = bytes([HOTT_BINARY_MODE_REQUEST_ID, HOTT_TELEMETRY_EAM_SENSOR_ID])
    frames = HoTTParser().feed_bytes(binary_request)
    assert len(frames) == 1
    f = frames[0]
    assert isinstance(f, HoTTRequestFrame)
    assert f.address == HOTT_TELEMETRY_EAM_SENSOR_ID
    assert f.is_text_mode is False


def test_text_mode_request_frame():
    text_request = bytes([HOTT_TEXT_MODE_REQUEST_ID, HOTT_EAM_SENSOR_TEXT_ID])
    frames = HoTTParser().feed_bytes(text_request)
    assert len(frames) == 1
    assert frames[0].is_text_mode is True


def test_no_sensor_present_request_frame():
    no_sensor_request = bytes([HOTT_BINARY_MODE_REQUEST_ID, HOTT_TELEMETRY_NO_SENSOR_ID])
    frames = HoTTParser().feed_bytes(no_sensor_request)
    assert len(frames) == 1


def _eam_frame_bytes():
    eam_assignments = {2: 0x00}
    eam_fill = _fill_from_body_indices(eam_assignments)
    return build_binary_frame(HOTT_TELEMETRY_EAM_SENSOR_ID, HOTT_SENSOR_ID_EAM, eam_fill)


def test_corrupted_checksum_rejected():
    eam_frame_bytes = _eam_frame_bytes()
    corrupted = bytearray(eam_frame_bytes)
    corrupted[-1] ^= 0xFF  # flip every bit of the checksum byte
    frames = HoTTParser().feed_bytes(bytes(corrupted))
    assert len(frames) == 0


def test_corrupted_body_byte_rejected():
    eam_frame_bytes = _eam_frame_bytes()
    corrupted2 = bytearray(eam_frame_bytes)
    corrupted2[10] ^= 0x01  # flip a body byte; checksum no longer matches
    frames = HoTTParser().feed_bytes(bytes(corrupted2))
    assert len(frames) == 0


def test_resync_past_garbage():
    eam_frame_bytes = _eam_frame_bytes()
    garbage_then_real = b"\x00\x01\x02\xff\xff" + eam_frame_bytes
    frames = HoTTParser().feed_bytes(garbage_then_real)
    assert len(frames) == 1


def test_byte_at_a_time_streaming_matches_whole_buffer():
    gam_frame_bytes = build_binary_frame(
        HOTT_TELEMETRY_GAM_SENSOR_ID, HOTT_SENSOR_ID_GAM,
        _fill_from_body_indices({2: 0x00}))
    parser = HoTTParser()
    streamed: List = []
    for b in gam_frame_bytes:
        streamed.extend(parser.feed_bytes(bytes([b])))
    assert len(streamed) == 1 and streamed[0].raw == gam_frame_bytes


def test_mixed_request_and_response_stream():
    binary_request = bytes([HOTT_BINARY_MODE_REQUEST_ID, HOTT_TELEMETRY_EAM_SENSOR_ID])
    eam_frame_bytes = _eam_frame_bytes()
    mixed = binary_request + eam_frame_bytes
    frames = HoTTParser().feed_bytes(mixed)
    assert len(frames) == 2
    assert isinstance(frames[0], HoTTRequestFrame)
    assert isinstance(frames[1], HoTTResponseFrame)
