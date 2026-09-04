#!/usr/bin/env python3
"""Unit tests for remoteid_kismet_bridge.py -- the LIVE Kismet -> ODID decode
wiring. No hardware, no network.

The ODID payloads used here are built from remoteid_decode_bridge.TEST_VECTORS_HEX,
which are the REAL byte output of the unmodified opendroneid-core-c reference
encoder (see that module's docstring), so a decode success is genuine, not a
self-consistent fabrication.
"""
import remoteid_kismet_bridge as b
import remoteid_decode_bridge as rid


def _message_pack(keys):
    """Build a real ODID message pack from named test vectors."""
    msgs = [bytes.fromhex(rid.TEST_VECTORS_HEX[k]) for k in keys]
    return bytes([0xF0 | 2, rid.ODID_MESSAGE_SIZE, len(msgs)]) + b"".join(msgs)


def _wifi_device_with_pack(pack_hex):
    return {
        "kismet.device.base.macaddr": "60:60:1F:AA:BB:CC",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": -55},
        # ODID payload surfaced under an OpenDroneID-hinted key (schema-agnostic
        # extraction, so the exact key path does not matter).
        "dot11.device": {"dot11.device.opendroneid.raw": pack_hex},
    }


def test_full_pack_decodes_and_aggregates():
    dev = _wifi_device_with_pack(
        _message_pack(["BasicID", "Location", "SelfID", "System", "OperatorID"]).hex()
    )
    decoded = b.decode_device_odid(dev)
    assert [m["message_type"] for m in decoded] == \
        ["BASIC_ID", "LOCATION", "SELF_ID", "SYSTEM", "OPERATOR_ID"]

    body = b.aggregate_messages(decoded, source_mac=b._device_mac(dev),
                                transport=b._device_transport(dev),
                                rssi_dbm=b._device_rssi(dev))
    assert body is not None
    assert body["uas_id"] == "12345678901234567890"
    assert abs(body["latitude_deg"] - 45.539309) < 1e-6
    assert abs(body["longitude_deg"] - (-122.966389)) < 1e-6
    assert body["operator_id"] == "98765432100123456789"
    assert body["description"] == "DronesRUS: Real Estate"
    assert body["transport"] == "wifi"
    assert body["source_mac"] == "60:60:1F:AA:BB:CC"
    assert body["rssi_dbm"] == -55.0
    assert set(body["message_types"]) == {"BASIC_ID", "LOCATION", "SELF_ID",
                                          "SYSTEM", "OPERATOR_ID"}
    assert body["caveats"]  # honesty caveats always attached


def test_lone_basic_id_message():
    # A single 25-byte message (not a pack) under an ODID-hinted key.
    dev = {"kismet.device.base.macaddr": "AA:BB:CC:DD:EE:FF",
           "kismet.device.base.phyname": "IEEE802.11",
           "remoteid": rid.TEST_VECTORS_HEX["BasicID"]}
    decoded = b.decode_device_odid(dev)
    assert len(decoded) == 1 and decoded[0]["message_type"] == "BASIC_ID"
    body = b.aggregate_messages(decoded, source_mac="AA:BB:CC:DD:EE:FF",
                                transport="wifi")
    assert body is not None and body["uas_id"] == "12345678901234567890"


def test_non_odid_device_decodes_nothing():
    # A plain phone/AP device with no ODID-hinted payload -> zero decodes, and
    # nothing is fabricated.
    dev = {"kismet.device.base.macaddr": "3C:5A:B4:11:22:33",
           "kismet.device.base.phyname": "IEEE802.11",
           "kismet.device.base.name": "Galaxy-S23",
           "kismet.device.base.manuf": "Samsung"}
    assert b.decode_device_odid(dev) == []
    assert b.aggregate_messages([]) is None


def test_odid_hinted_but_garbage_bytes_do_not_fabricate():
    # A key that LOOKS like ODID but carries non-ODID bytes must not produce a
    # fake decode (the decoder validates message type + version and raises).
    dev = {"opendroneid_raw": "deadbeef" * 8}
    assert b.decode_device_odid(dev) == []


def test_bluetooth_service_data_path():
    # Wrap a real ODID BasicID message in an ASTM BLE Service Data AD structure
    # (AD type 0x16, UUID 0xFFFA LE, AppCode 0x0D, then message_counter + msg),
    # exactly the framing decode_bluetooth_service_data() expects.
    inner_msg = bytes.fromhex(rid.TEST_VECTORS_HEX["BasicID"])
    service_data = bytes([0xFA, 0xFF, 0x0D, 0x00]) + inner_msg  # UUID LE + appcode + msg_counter=0
    ad = bytes([len(service_data) + 1, 0x16]) + service_data
    dev = {"kismet.device.base.macaddr": "11:22:33:44:55:66",
           "kismet.device.base.phyname": "Bluetooth",
           "bluetooth.device.servicedata": ad.hex()}
    decoded = b.decode_device_odid(dev)
    assert len(decoded) == 1 and decoded[0]["uas_id"] == "12345678901234567890"
    assert b._device_transport(dev) == "bluetooth"


def test_position_only_is_still_ingestable():
    # A sender broadcasting only Location (no BasicID/OperatorID) still yields a
    # useful ingest body (position is worth reporting).
    dev = _wifi_device_with_pack(_message_pack(["Location"]).hex())
    body = b.aggregate_messages(b.decode_device_odid(dev))
    assert body is not None
    assert body["uas_id"] is None
    assert body["latitude_deg"] is not None


def test_hex_coercion_tolerates_separators():
    assert b._hex_to_bytes("0x60:60:1f") == bytes([0x60, 0x60, 0x1f])
    assert b._hex_to_bytes("not hex!!") is None
    assert b._hex_to_bytes("abc") is None  # odd length
