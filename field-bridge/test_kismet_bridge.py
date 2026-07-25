#!/usr/bin/env python3
"""Real unit tests for field-bridge/kismet_bridge.py's translation logic.

These test the actual shipped code: OUI matching, Kismet-device-JSON ->
detection-ingest translation, and the forward-all vs drone-OUI-only
filtering decision in poll_once(). No live Kismet server or backend is
required -- build_test_fixture() reproduces Kismet's real documented
device-JSON field-name schema (verified against the local Kismet source
checkout; see kismet_bridge.py's module docstring), it is not fabricated
drone data.

_post_with_reauth 401-retry behaviour itself is not re-tested here since
its control flow is identical to (and already covered by) the shared tests
in test_reauth_on_401.py for hackrf_rx.py/mavlink_sniffer.py/
sik_mavlink_bridge.py -- kismet_bridge.py's copy is the same duplicated
convention, not new logic.
"""
from __future__ import annotations

import unittest
from unittest import mock

import kismet_bridge


class OuiMatchTests(unittest.TestCase):
    def test_dji_oui_matches(self):
        self.assertEqual(kismet_bridge.match_drone_oui("60:60:1F:44:55:66"), "DJI")

    def test_dji_oui_matches_lowercase(self):
        self.assertEqual(kismet_bridge.match_drone_oui("60:60:1f:44:55:66"), "DJI")

    def test_parrot_oui_matches(self):
        self.assertEqual(kismet_bridge.match_drone_oui("90:3A:E6:01:02:03"), "Parrot")

    def test_autel_oui_matches(self):
        # task #97: the old "A0:14:3D:00" entry was a malformed 4-octet key
        # and was silently filtered out, so Autel devices never matched.
        # EC:5B:CD is a real, verified 3-octet OUI (IEEE MA-M registry:
        # "Autel Robotics USA LLC") -- confirm it now matches.
        self.assertEqual(kismet_bridge.match_drone_oui("EC:5B:CD:11:22:33"), "Autel")

    def test_autel_oui_matches_lowercase(self):
        self.assertEqual(kismet_bridge.match_drone_oui("ec:5b:cd:11:22:33"), "Autel")

    def test_non_drone_oui_does_not_match(self):
        self.assertIsNone(kismet_bridge.match_drone_oui("3C:5A:B4:11:22:33"))

    def test_all_registered_ouis_are_exactly_three_octets(self):
        # Guards against the placeholder-length entry regressing back in
        # (see kismet_bridge.py's normalization comment).
        for k in kismet_bridge.DRONE_MANUFACTURER_OUIS:
            self.assertEqual(len(k.split(":")), 3, f"malformed OUI key: {k}")


class ToDetectionTests(unittest.TestCase):
    def setUp(self):
        self.devices = kismet_bridge.build_test_fixture()
        self.phone = self.devices[0]
        self.bt = self.devices[1]
        self.dji = self.devices[2]

    def test_non_drone_device_is_advisory_only(self):
        d = kismet_bridge.to_detection(self.phone, None)
        self.assertEqual(d["confidence_type"], "advisory_only")
        self.assertEqual(d["threat_level"], "LOW")
        self.assertEqual(d["source"], "KISMET")
        self.assertFalse(d["protocol_confirmed"])

    def test_drone_oui_device_is_heuristic_binary(self):
        d = kismet_bridge.to_detection(self.dji, "DJI")
        self.assertEqual(d["confidence_type"], "heuristic_binary")
        self.assertEqual(d["threat_level"], "MEDIUM")
        self.assertIn("DJI", d["model"])

    def test_frequency_khz_converted_to_ghz(self):
        d = kismet_bridge.to_detection(self.phone, None)
        # fixture: kismet.device.base.frequency = 2437000 (kHz) -> 2.437 GHz
        self.assertAlmostEqual(d["center_freq_ghz"], 2.437, places=3)

    def test_missing_frequency_falls_back_to_ism_default(self):
        device = dict(self.bt)
        device.pop("kismet.device.base.frequency", None)
        d = kismet_bridge.to_detection(device, None)
        self.assertAlmostEqual(d["center_freq_ghz"], 2.437, places=3)

    def test_signal_dbm_passed_through(self):
        d = kismet_bridge.to_detection(self.phone, None)
        self.assertEqual(d["rssi_dbm"], -62.0)

    def test_missing_signal_uses_documented_default(self):
        device = dict(self.phone)
        device["kismet.device.base.signal"] = {}
        d = kismet_bridge.to_detection(device, None)
        self.assertEqual(d["rssi_dbm"], -90.0)

    def test_callsign_includes_mac(self):
        d = kismet_bridge.to_detection(self.phone, None)
        self.assertIn("3C:5A:B4:11:22:33", d["callsign"])


class PollOnceFilteringTests(unittest.TestCase):
    """Exercises poll_once()'s forward_all vs drone-OUI-only gating and its
    per-MAC repost throttle, mocking _post_with_reauth to avoid any network
    call."""

    def _fake_response(self):
        r = mock.Mock()
        r.raise_for_status.return_value = None
        r.json.return_value = {"callsign": "X"}
        return r

    def test_default_only_forwards_drone_oui_matches(self):
        devices = kismet_bridge.build_test_fixture()
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            posted = kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=False, seen_macs={}, repost_interval_s=60.0)
        self.assertEqual(posted, 1)  # only the DJI-OUI fixture device
        self.assertEqual(post.call_count, 1)

    def test_forward_all_forwards_every_device(self):
        devices = kismet_bridge.build_test_fixture()
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            posted = kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=True, seen_macs={}, repost_interval_s=60.0)
        self.assertEqual(posted, 3)
        self.assertEqual(post.call_count, 3)

    def test_repost_throttle_suppresses_immediate_repeat(self):
        devices = kismet_bridge.build_test_fixture()
        seen: dict = {}
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            kismet_bridge.poll_once("http://console", {}, "e@x.com", "pw",
                                    devices, forward_all=True, seen_macs=seen,
                                    repost_interval_s=60.0)
            second_posted = kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=True, seen_macs=seen, repost_interval_s=60.0)
        self.assertEqual(second_posted, 0)
        self.assertEqual(post.call_count, 3)  # only from the first poll


class DbRawV2SignatureTests(unittest.TestCase):
    """Task #105: DroneBridge WifiBroadcast raw-v2 protocol signature match,
    per kismet_bridge.detect_db_raw_v2_signature()'s module-docstring-cited
    field layout (db_protocol.h / db_raw_send_receive.c, Apache-2.0,
    constants only -- see module docstring)."""

    def _build_frame(self, radiotap_len=13, fcf=kismet_bridge.DB_FCF_DURATION_DATA,
                     direction=kismet_bridge.DB_DIREC_DRONE, comm_id=0xC8, port=0x03,
                     payload_length=100, seq_num=42, trailing=b"\x00" * 20):
        radiotap = b"\x00" * radiotap_len
        header = (fcf + bytes([direction, comm_id, port]) +
                 payload_length.to_bytes(2, "little") + bytes([seq_num]))
        return radiotap + header + trailing

    def test_matches_genuine_data_frame_header(self):
        frame = self._build_frame()
        match = kismet_bridge.detect_db_raw_v2_signature(frame, 13)
        self.assertIsNotNone(match)
        self.assertEqual(match["frame_kind"], "data")
        self.assertEqual(match["direction"], "drone")
        self.assertEqual(match["port"], 0x03)
        self.assertEqual(match["payload_length"], 100)
        self.assertEqual(match["seq_num"], 42)

    def test_matches_genuine_rts_frame_header(self):
        frame = self._build_frame(fcf=kismet_bridge.DB_FCF_DURATION_RTS,
                                  direction=kismet_bridge.DB_DIREC_GROUND)
        match = kismet_bridge.detect_db_raw_v2_signature(frame, 13)
        self.assertIsNotNone(match)
        self.assertEqual(match["frame_kind"], "rts")
        self.assertEqual(match["direction"], "ground")

    def test_rejects_non_matching_fcf(self):
        frame = self._build_frame(fcf=bytes([0x80, 0x00, 0x00, 0x00]))  # beacon FCF, not data/RTS
        self.assertIsNone(kismet_bridge.detect_db_raw_v2_signature(frame, 13))

    def test_rejects_invalid_direction(self):
        frame = self._build_frame(direction=0x02)  # not DRONE(1) or GROUND(3)
        self.assertIsNone(kismet_bridge.detect_db_raw_v2_signature(frame, 13))

    def test_rejects_out_of_range_port(self):
        frame = self._build_frame(port=0x09)  # DB_PORT_MAX is 0x07
        self.assertIsNone(kismet_bridge.detect_db_raw_v2_signature(frame, 13))

    def test_rejects_truncated_frame(self):
        frame = self._build_frame()[:15]  # cut off mid-header
        self.assertIsNone(kismet_bridge.detect_db_raw_v2_signature(frame, 13))

    def test_variable_radiotap_length_honored(self):
        frame = self._build_frame(radiotap_len=24)
        self.assertIsNone(kismet_bridge.detect_db_raw_v2_signature(frame, 13))  # wrong offset
        match = kismet_bridge.detect_db_raw_v2_signature(frame, 24)
        self.assertIsNotNone(match)


class BuildWifibroadcastDetectionTests(unittest.TestCase):
    def test_associationless_true_is_heuristic_binary(self):
        match = {"frame_kind": "data", "direction": "drone", "comm_id": 0xC8,
                "port": 3, "payload_length": 100, "seq_num": 1, "associationless": True}
        d = kismet_bridge.build_wifibroadcast_detection(match)
        self.assertEqual(d["confidence_type"], "heuristic_binary")
        self.assertEqual(d["threat_level"], "MEDIUM")
        self.assertIn("WifiBroadcast", d["model"])

    def test_associationless_unknown_is_still_heuristic_binary(self):
        match = {"frame_kind": "data", "direction": "drone", "comm_id": 0xC8,
                "port": 3, "payload_length": 100, "seq_num": 1, "associationless": None}
        d = kismet_bridge.build_wifibroadcast_detection(match)
        self.assertEqual(d["confidence_type"], "heuristic_binary")

    def test_associationless_false_downgrades_to_advisory_only(self):
        match = {"frame_kind": "data", "direction": "drone", "comm_id": 0xC8,
                "port": 3, "payload_length": 100, "seq_num": 1, "associationless": False}
        d = kismet_bridge.build_wifibroadcast_detection(match)
        self.assertEqual(d["confidence_type"], "advisory_only")

    def test_callsign_uses_mac_when_given(self):
        match = {"frame_kind": "data", "direction": "drone", "comm_id": 0xC8,
                "port": 3, "payload_length": 100, "seq_num": 1, "associationless": True}
        d = kismet_bridge.build_wifibroadcast_detection(match, mac="AA:BB:CC:DD:EE:FF")
        self.assertIn("AA:BB:CC:DD:EE:FF", d["callsign"])


class KismetRestClientTests(unittest.TestCase):
    """Verifies the Kismet REST client hits the real, documented endpoint
    paths (devicetracker.cc:335/:378) and passes the apikey the way
    Kismet's own AUTH_COOKIE scheme expects, without needing a live server."""

    def test_all_devices_endpoint_used_when_no_since_time(self):
        fake = mock.Mock()
        fake.json.return_value = []
        fake.raise_for_status.return_value = None
        with mock.patch.object(kismet_bridge.requests, "get",
                              return_value=fake) as get:
            kismet_bridge.fetch_kismet_devices("http://kismet:2501", "key123", None)
        args, kwargs = get.call_args
        self.assertEqual(args[0], "http://kismet:2501/devices/all_devices.json")
        self.assertEqual(kwargs["params"], {"KISMET": "key123"})

    def test_last_time_endpoint_used_when_since_time_given(self):
        fake = mock.Mock()
        fake.json.return_value = []
        fake.raise_for_status.return_value = None
        with mock.patch.object(kismet_bridge.requests, "get",
                              return_value=fake) as get:
            kismet_bridge.fetch_kismet_devices("http://kismet:2501", "key123", 12345)
        args, _ = get.call_args
        self.assertEqual(args[0], "http://kismet:2501/devices/last-time/12345/devices.json")

    def test_no_apikey_sends_no_kismet_param(self):
        fake = mock.Mock()
        fake.json.return_value = []
        fake.raise_for_status.return_value = None
        with mock.patch.object(kismet_bridge.requests, "get",
                              return_value=fake) as get:
            kismet_bridge.fetch_kismet_devices("http://kismet:2501", None, None)
        _, kwargs = get.call_args
        self.assertEqual(kwargs["params"], {})


if __name__ == "__main__":
    unittest.main()
