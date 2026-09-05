#!/usr/bin/env python3
"""Unit tests for wifi_drone_bridge.py -- the Wi-Fi drone SSID/OUI fingerprint
wiring. No hardware, no network.

The Kismet device records here mirror Kismet's REAL documented device-JSON
field-name schema (kismet.device.base.*), the same schema kismet_bridge.py's
build_test_fixture() uses. HONEST bar under test: a match is a make/model
CANDIDATE from a SPOOFABLE SSID/OUI -- never a serial or an exact-confirmed ID.
"""
import wifi_drone_bridge as b


def _wifi_device(mac, ssid=None, manuf=None, channel="6", signal=-55):
    dev = {
        "kismet.device.base.macaddr": mac,
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.channel": channel,
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": signal},
    }
    if ssid is not None:
        dev["kismet.device.base.name"] = ssid
    if manuf is not None:
        dev["kismet.device.base.manuf"] = manuf
    return dev


# --- SSID pattern matching ---------------------------------------------------
def test_tello_ssid_is_candidate_not_serial():
    dev = _wifi_device("AA:BB:CC:11:22:33", ssid="TELLO-9F6C21")
    body = b.scan_device(dev)
    assert body is not None
    # HONEST: a make/model CANDIDATE, NOT a serial or exact-confirmed identity.
    assert body["make_candidate"] == "DJI/Ryze Tello"
    assert "ssid" in body["match_basis"]
    # nothing here is a serial -- the fields are SSID/OUI/manuf/candidate only.
    assert "serial" not in body
    assert body["ssid"] == "TELLO-9F6C21"
    assert body["channel"] == 6
    assert body["signal_dbm"] == -55.0
    assert body["caveats"]  # spoofable/candidate caveats always attached


def test_anafi_ssid_candidate():
    body = b.scan_device(_wifi_device("11:22:33:44:55:66", ssid="ANAFI-123456"))
    assert body is not None
    assert body["make_candidate"] == "Parrot Anafi"


def test_autel_ssid_candidate():
    body = b.scan_device(_wifi_device("11:22:33:44:55:66", ssid="Autel-EVO-2"))
    assert body is not None
    assert body["make_candidate"] == "Autel"


# --- OUI matching (reuses kismet_bridge.DRONE_MANUFACTURER_OUIS) --------------
def test_dji_oui_match_without_droney_ssid():
    # A DJI-OUI MAC with a non-drone SSID still flags on OUI alone.
    dev = _wifi_device("60:60:1F:AA:BB:CC", ssid="somebodys-hotspot")
    body = b.scan_device(dev)
    assert body is not None
    assert body["make_candidate"] == "DJI"
    assert "oui" in body["match_basis"]
    assert body["oui"] == "60:60:1F"


def test_parrot_added_oui_matches():
    # The two Parrot OUIs added for this work must resolve.
    body = b.scan_device(_wifi_device("00:26:7E:00:11:22", ssid="x"))
    assert body is not None and body["make_candidate"] == "Parrot"


# --- generic Wi-Fi Direct softAP (weak, honest) ------------------------------
def test_generic_direct_softap_is_weak_null_make():
    # A bare DIRECT- softAP with a non-drone OUI is a WEAK generic signal:
    # flagged (so an operator sees it) but make_candidate stays null.
    dev = _wifi_device("3C:5A:B4:11:22:33", ssid="DIRECT-7a-HP OfficeJet")
    body = b.scan_device(dev)
    assert body is not None
    assert body["make_candidate"] is None
    assert "generic softAP" in body["match_basis"]


def test_generic_direct_with_drone_oui_gets_make():
    # DIRECT- softAP AND a drone OUI -> the OUI vendor becomes the candidate.
    dev = _wifi_device("34:D2:62:11:22:33", ssid="DIRECT-xy-Mavic")
    body = b.scan_device(dev)
    assert body is not None
    assert body["make_candidate"] == "DJI"
    assert "oui" in body["match_basis"]


# --- manufacturer-string hint ------------------------------------------------
def test_manuf_string_hint_matches_drone_vendor():
    # Kismet-resolved manufacturer names a drone vendor, no droney SSID/OUI.
    dev = _wifi_device("AA:BB:CC:11:22:33", ssid="myssid", manuf="Skydio Inc")
    body = b.scan_device(dev)
    assert body is not None
    assert body["make_candidate"] == "Skydio Inc"
    assert "manuf" in body["match_basis"]


# --- negatives: nothing fabricated -------------------------------------------
def test_plain_phone_is_not_a_drone():
    dev = _wifi_device("3C:5A:B4:11:22:33", ssid="Pixel-8", manuf="Google")
    assert b.scan_device(dev) is None


def test_non_wifi_phy_ignored():
    # A Bluetooth device is not considered by the Wi-Fi fingerprint bridge.
    dev = {"kismet.device.base.macaddr": "60:60:1F:00:00:00",
           "kismet.device.base.phyname": "Bluetooth",
           "kismet.device.base.name": "TELLO-x"}
    assert b.scan_device(dev) is None


def test_match_basis_combines_ssid_and_oui():
    dev = _wifi_device("60:60:1F:AA:BB:CC", ssid="TELLO-abc123")
    body = b.scan_device(dev)
    assert body["match_basis"] == "ssid+oui"


def test_ssid_extracted_from_dot11_beaconed_key():
    # SSID surfaced under a dot11 last-beaconed key (schema-robust extraction),
    # not kismet.device.base.name.
    dev = {
        "kismet.device.base.macaddr": "11:22:33:44:55:66",
        "kismet.device.base.phyname": "IEEE802.11",
        "dot11.device": {"dot11.device.last_beaconed_ssid": "TELLO-ZZ99"},
    }
    body = b.scan_device(dev)
    assert body is not None and body["make_candidate"] == "DJI/Ryze Tello"


def test_channel_parses_leading_int_from_ht_string():
    dev = _wifi_device("60:60:1F:AA:BB:CC", ssid="x", channel="149HT40+")
    body = b.scan_device(dev)
    assert body["channel"] == 149
