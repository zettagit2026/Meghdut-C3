#!/usr/bin/env python3
"""Unit tests for remoteid_decode_bridge.py's Bluetooth 4/5 AD-framing
support (task #89 follow-up).

As documented in remoteid_decode_bridge.py's module docstring "BLUETOOTH
FRAMING" section: the local opendroneid-core-c checkout has NO Bluetooth
encode/decode source at all (verified by exhaustive grep), so these tests
do NOT claim to be validated against compiled-C reference output the way
the WiFi-path message-struct tests are. Instead they:
  - construct BLE AD/GAP frames by hand per the ASTM F3411 / Bluetooth SIG
    Assigned Numbers framing (AD Type 0x16 Service Data, UUID 0xFFFA,
    AppCode 0x0D, then message_counter + payload), and
  - wrap the SAME byte-exact TEST_VECTORS_HEX payloads already verified
    against the real compiled opendroneid-core-c encoder,
so what's actually being tested here is the new outer-framing parser
(iter_ble_ad_structures / parse_bluetooth_ad_frame /
decode_bluetooth_service_data), not the message-struct decode logic, which
needed no changes and is already covered by remoteid_decode_bridge.py's own
--self-test.

Run: pytest field-bridge/test_remoteid_bluetooth.py -v
"""
import pytest

from remoteid_decode_bridge import (
    BLE_AD_TYPE_SERVICE_DATA_16BIT_UUID,
    BLE_ODID_APPCODE,
    BluetoothFrameError,
    ODID_MESSAGE_SIZE,
    TEST_VECTORS_HEX,
    decode_basic_id,
    decode_bluetooth_service_data,
    iter_ble_ad_structures,
    parse_bluetooth_ad_frame,
)

FLAGS_AD = bytes([0x02, 0x01, 0x06])  # unrelated leading AD structure, must be skipped


def _wrap_service_data(inner: bytes, message_counter: int = 0) -> bytes:
    service_data = bytes([0xFA, 0xFF, BLE_ODID_APPCODE, message_counter]) + inner
    ad_struct = bytes([1 + len(service_data), BLE_AD_TYPE_SERVICE_DATA_16BIT_UUID]) + service_data
    return FLAGS_AD + ad_struct


def test_iter_ble_ad_structures_skips_unrelated_ad():
    structs = list(iter_ble_ad_structures(FLAGS_AD))
    assert structs == [(0x01, bytes([0x06]))]


def test_iter_ble_ad_structures_handles_multiple():
    payload = FLAGS_AD + bytes([0x03, 0x09, 0x41, 0x42])  # Complete Local Name "AB"
    structs = list(iter_ble_ad_structures(payload))
    assert len(structs) == 2
    assert structs[1] == (0x09, b"AB")


def test_parse_bluetooth_ad_frame_finds_service_data_after_other_ad():
    basic_id_bytes = bytes.fromhex(TEST_VECTORS_HEX["BasicID"])
    payload = _wrap_service_data(basic_id_bytes)
    inner = parse_bluetooth_ad_frame(payload)
    assert inner == bytes([0x00]) + basic_id_bytes


def test_parse_bluetooth_ad_frame_no_service_data_raises():
    with pytest.raises(BluetoothFrameError):
        parse_bluetooth_ad_frame(FLAGS_AD)


def test_parse_bluetooth_ad_frame_wrong_uuid_raises():
    payload = bytes([0x04, 0x16, 0xAB, 0xCD, BLE_ODID_APPCODE])
    with pytest.raises(BluetoothFrameError):
        parse_bluetooth_ad_frame(payload)


def test_parse_bluetooth_ad_frame_wrong_appcode_raises():
    payload = bytes([0x04, 0x16, 0xFA, 0xFF, 0x99])
    with pytest.raises(BluetoothFrameError):
        parse_bluetooth_ad_frame(payload)


def test_parse_bluetooth_ad_frame_truncated_raises():
    payload = bytes([0x05, 0x16, 0xFA, 0xFF])  # claims 5 bytes follow, only 2 present
    with pytest.raises(BluetoothFrameError):
        parse_bluetooth_ad_frame(payload)


def test_decode_bluetooth_service_data_single_basic_id_matches_wifi_path():
    basic_id_bytes = bytes.fromhex(TEST_VECTORS_HEX["BasicID"])
    payload = _wrap_service_data(basic_id_bytes)
    results = decode_bluetooth_service_data(payload)
    assert len(results) == 1
    assert results[0]["message_type"] == "BASIC_ID"
    assert results[0]["uas_id"] == decode_basic_id(basic_id_bytes)["uas_id"]


def test_decode_bluetooth_service_data_message_pack_matches_wifi_path():
    pack_body = b"".join(
        bytes.fromhex(TEST_VECTORS_HEX[k])
        for k in ("BasicID", "Location", "SelfID", "System", "OperatorID")
    )
    pack_header = bytes([0xF0, ODID_MESSAGE_SIZE, 5])
    payload = _wrap_service_data(pack_header + pack_body)
    results = decode_bluetooth_service_data(payload)
    assert [r.get("message_type") for r in results] == [
        "BASIC_ID", "LOCATION", "SELF_ID", "SYSTEM", "OPERATOR_ID",
    ]
    assert all("error" not in r for r in results)


def test_decode_bluetooth_service_data_empty_payload_after_counter_raises():
    payload = _wrap_service_data(b"")
    with pytest.raises(BluetoothFrameError):
        decode_bluetooth_service_data(payload)
