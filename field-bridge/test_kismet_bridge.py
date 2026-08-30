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

    def test_wifi_reference_off_by_default(self):
        # forward_wifi_reference defaults False, so no reference posts happen and
        # the detection-forwarding behavior is unchanged (existing tests above).
        devices = kismet_bridge.build_test_fixture()
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=False, seen_macs={}, repost_interval_s=60.0)
        paths = [c.args[1] for c in post.call_args_list]
        self.assertNotIn("/api/detections/wifi-reference", paths)

    def test_wifi_reference_forwarded_for_ieee80211_only(self):
        # With forwarding on, BOTH IEEE802.11 fixture devices (Samsung client +
        # DJI AP) are forwarded to the reference store; the Bluetooth device is
        # not. The drone-OUI IEEE802.11 device ALSO still posts as a detection.
        devices = kismet_bridge.build_test_fixture()
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=False, seen_macs={}, repost_interval_s=60.0,
                forward_wifi_reference=True)
        paths = [c.args[1] for c in post.call_args_list]
        ref_paths = [p for p in paths if p == "/api/detections/wifi-reference"]
        self.assertEqual(len(ref_paths), 2)
        self.assertIn("/api/detections/ingest", paths)

    def test_wifi_reference_repost_throttled(self):
        # A second immediate poll re-posts nothing to the reference store (per-
        # MAC throttle), same latch pattern as the detection repost throttle.
        devices = kismet_bridge.build_test_fixture()
        seen: dict = {}
        with mock.patch.object(kismet_bridge, "_post_with_reauth",
                              return_value=self._fake_response()) as post:
            kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=False, seen_macs=seen, repost_interval_s=60.0,
                forward_wifi_reference=True, wifi_ref_repost_s=60.0)
            first_refs = sum(1 for c in post.call_args_list
                             if c.args[1] == "/api/detections/wifi-reference")
            kismet_bridge.poll_once(
                "http://console", {}, "e@x.com", "pw", devices,
                forward_all=False, seen_macs=seen, repost_interval_s=60.0,
                forward_wifi_reference=True, wifi_ref_repost_s=60.0)
        total_refs = sum(1 for c in post.call_args_list
                         if c.args[1] == "/api/detections/wifi-reference")
        self.assertEqual(first_refs, 2)
        self.assertEqual(total_refs, 2)  # second poll added no reference posts


class ToWifiReferenceTests(unittest.TestCase):
    """to_wifi_reference(): pure Kismet-device -> reference-body translation."""

    def test_maps_non_drone_wifi_fields(self):
        dev = kismet_bridge.build_test_fixture()[0]  # Samsung client @ 2437 MHz
        ref = kismet_bridge.to_wifi_reference(dev)
        self.assertEqual(ref["mac"], "3C:5A:B4:11:22:33")
        self.assertEqual(ref["oui"], "3C:5A:B4")
        self.assertEqual(ref["phyname"], "IEEE802.11")
        self.assertAlmostEqual(ref["center_freq_ghz"], 2.437, places=3)
        self.assertEqual(ref["ssid"], "Galaxy-S23")
        self.assertFalse(ref["is_drone_oui"])

    def test_flags_drone_oui_and_drops_empty_ssid(self):
        dev = kismet_bridge.build_test_fixture()[2]  # DJI AP, name ""
        ref = kismet_bridge.to_wifi_reference(dev)
        self.assertTrue(ref["is_drone_oui"])
        self.assertIsNone(ref["ssid"])


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


# ---------------------------------------------------------------------------
# Task #116: pcapng-stream consumer tests. Builds a REALISTIC mocked pcapng
# byte stream matching the IETF pcapng block format (Section Header Block +
# Interface Description Block + Enhanced Packet Blocks) that Kismet's real
# /phy/phy80211/pcap/by-bssid/:mac/packets.pcapng route emits (per
# phy_80211.cc/pcapng_stream_futurebuf.h, see kismet_bridge.py's module
# docstring) -- not a fabricated ad-hoc byte layout.
# ---------------------------------------------------------------------------
import struct


def _pcapng_block(block_type: int, body: bytes) -> bytes:
    total_len = 12 + len(body)
    return (struct.pack("<II", block_type, total_len) + body +
           struct.pack("<I", total_len))


def _shb() -> bytes:
    # byte-order-magic(4) + major(2) + minor(2) + section_length(8, -1 = unknown)
    body = struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1)
    return _pcapng_block(kismet_bridge.PCAPNG_BLOCK_SHB, body)


def _idb(linktype: int = 127) -> bytes:
    # linktype(2) + reserved(2) + snaplen(4); 127 = DLT_IEEE802_11_RADIO
    body = struct.pack("<HHI", linktype, 0, 262144)
    return _pcapng_block(kismet_bridge.PCAPNG_BLOCK_IDB, body)


def _epb(packet_data: bytes, interface_id: int = 0) -> bytes:
    captured_len = len(packet_data)
    pad = (-captured_len) % 4
    body = (struct.pack("<IIIII", interface_id, 0, 0, captured_len, captured_len) +
           packet_data + b"\x00" * pad)
    return _pcapng_block(kismet_bridge.PCAPNG_BLOCK_EPB, body)


def _wifibroadcast_frame(radiotap_len: int = 13) -> bytes:
    """A raw 802.11 frame (radiotap header + DroneBridge db_raw_v2_header_t)
    matching detect_db_raw_v2_signature()'s real, existing signature check."""
    # radiotap header: version(1) pad(1) length(2, LE) + rest padding
    radiotap = struct.pack("<BBH", 0, 0, radiotap_len) + b"\x00" * (radiotap_len - 4)
    db_header = (kismet_bridge.DB_FCF_DURATION_DATA +
                bytes([kismet_bridge.DB_DIREC_DRONE, 0xC8, 0x03]) +
                (100).to_bytes(2, "little") + bytes([7]))
    return radiotap + db_header + b"\x00" * 10  # trailing payload bytes


class PcapngStreamParserTests(unittest.TestCase):
    def test_extracts_frames_skipping_shb_and_idb(self):
        frame1 = b"\xAA" * 30
        frame2 = b"\xBB" * 40
        stream = _shb() + _idb() + _epb(frame1) + _epb(frame2)
        frames = list(kismet_bridge.iter_pcapng_frames([stream]))
        self.assertEqual(frames, [frame1, frame2])

    def test_handles_chunked_delivery_mid_block(self):
        frame = b"\xCC" * 50
        stream = _shb() + _idb() + _epb(frame)
        # split into small chunks, including mid-block splits
        chunks = [stream[i:i + 7] for i in range(0, len(stream), 7)]
        frames = list(kismet_bridge.iter_pcapng_frames(chunks))
        self.assertEqual(frames, [frame])

    def test_ignores_incomplete_trailing_block(self):
        frame = b"\xDD" * 20
        stream = _shb() + _idb() + _epb(frame)
        truncated = stream[:-5]  # cut off mid-final-block
        frames = list(kismet_bridge.iter_pcapng_frames([truncated]))
        self.assertEqual(frames, [])  # incomplete EPB never yielded

    def test_real_wifibroadcast_frame_round_trips_through_pcapng(self):
        frame = _wifibroadcast_frame()
        stream = _shb() + _idb() + _epb(frame)
        frames = list(kismet_bridge.iter_pcapng_frames([stream]))
        self.assertEqual(len(frames), 1)
        rt_len = kismet_bridge.radiotap_header_length(frames[0])
        self.assertEqual(rt_len, 13)
        match = kismet_bridge.detect_db_raw_v2_signature(frames[0], rt_len)
        self.assertIsNotNone(match)
        self.assertEqual(match["direction"], "drone")


class RadiotapHeaderLengthTests(unittest.TestCase):
    def test_reads_length_field(self):
        frame = struct.pack("<BBH", 0, 0, 18) + b"\x00" * 20
        self.assertEqual(kismet_bridge.radiotap_header_length(frame), 18)

    def test_none_when_too_short(self):
        self.assertIsNone(kismet_bridge.radiotap_header_length(b"\x00\x00"))


class FetchPcapngFramesTests(unittest.TestCase):
    def test_hits_real_by_bssid_route_with_apikey_param(self):
        frame = _wifibroadcast_frame()
        stream = _shb() + _idb() + _epb(frame)
        fake = mock.Mock()
        fake.raise_for_status.return_value = None
        fake.iter_content.return_value = [stream]
        fake.close.return_value = None
        with mock.patch.object(kismet_bridge.requests, "get", return_value=fake) as get:
            frames = kismet_bridge.fetch_pcapng_frames(
                "http://kismet:2501", "AA:BB:CC:DD:EE:FF", "key123")
        args, kwargs = get.call_args
        self.assertEqual(args[0],
                        "http://kismet:2501/phy/phy80211/pcap/by-bssid/"
                        "AA:BB:CC:DD:EE:FF/packets.pcapng")
        self.assertEqual(kwargs["params"], {"KISMET": "key123"})
        self.assertTrue(kwargs["stream"])
        self.assertEqual(len(frames), 1)
        fake.close.assert_called_once()  # bounded read must explicitly close the live stream

    def test_stops_at_max_frames(self):
        frame = _wifibroadcast_frame()
        stream = _shb() + _idb() + _epb(frame) + _epb(frame) + _epb(frame)
        fake = mock.Mock()
        fake.raise_for_status.return_value = None
        fake.iter_content.return_value = [stream]
        fake.close.return_value = None
        with mock.patch.object(kismet_bridge.requests, "get", return_value=fake):
            frames = kismet_bridge.fetch_pcapng_frames(
                "http://kismet:2501", "AA:BB:CC:DD:EE:FF", None, max_frames=2)
        self.assertLessEqual(len(frames), 2)


class CheckWifibroadcastSignatureTests(unittest.TestCase):
    def test_returns_match_when_signature_present(self):
        frame = _wifibroadcast_frame()
        stream = _shb() + _idb() + _epb(frame)
        with mock.patch.object(kismet_bridge, "fetch_pcapng_frames",
                               return_value=[frame]):
            match = kismet_bridge.check_wifibroadcast_signature(
                "http://kismet:2501", "AA:BB:CC:DD:EE:FF", None)
        self.assertIsNotNone(match)
        self.assertEqual(match["direction"], "drone")
        self.assertIsNone(match["associationless"])

    def test_returns_none_when_no_signature_present(self):
        non_matching = b"\x00" * 4 + b"\x00" * 40  # no valid radiotap/db header pattern
        with mock.patch.object(kismet_bridge, "fetch_pcapng_frames",
                               return_value=[non_matching]):
            match = kismet_bridge.check_wifibroadcast_signature(
                "http://kismet:2501", "AA:BB:CC:DD:EE:FF", None)
        self.assertIsNone(match)

    def test_swallows_request_exception_and_returns_none(self):
        with mock.patch.object(kismet_bridge, "fetch_pcapng_frames",
                               side_effect=kismet_bridge.requests.RequestException("boom")):
            match = kismet_bridge.check_wifibroadcast_signature(
                "http://kismet:2501", "AA:BB:CC:DD:EE:FF", None)
        self.assertIsNone(match)


class PollOnceWifibroadcastWiringTests(unittest.TestCase):
    """Confirms poll_once() genuinely reaches /api/detections/ingest with a
    WifiBroadcast-signature detection when --check-wifibroadcast-signature
    is enabled and a match is found for a drone-OUI-matched IEEE802.11
    device -- the actual task #116 wiring, not just the pieces in isolation."""

    def _drone_device(self):
        return {
            "kismet.device.base.macaddr": "60:60:1F:44:55:66",
            "kismet.device.base.phyname": "IEEE802.11",
            "kismet.device.base.name": "",
            "kismet.device.base.type": "Wi-Fi AP",
            "kismet.device.base.manuf": "Dji Innovations",
            "kismet.device.base.first_time": 1000,
            "kismet.device.base.last_time": 1001,
            "kismet.device.base.channel": "149",
            "kismet.device.base.frequency": 5745000,
            "kismet.device.base.signal": {"kismet.common.signal.last_signal": -55},
        }

    def test_posts_wifibroadcast_detection_when_match_found(self):
        match = {"frame_kind": "data", "direction": "drone", "comm_id": 0xC8,
                "port": 3, "payload_length": 100, "seq_num": 7, "associationless": None}
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {"callsign": "WIFIBROADCAST-60:60:1F:44:55:66"}
        fake_resp.status_code = 200
        with mock.patch.object(kismet_bridge, "check_wifibroadcast_signature",
                              return_value=match) as check_fn, \
             mock.patch.object(kismet_bridge.requests, "post",
                              return_value=fake_resp) as post:
            posted = kismet_bridge.poll_once(
                "http://console", {}, "a@b.com", "pw",
                [self._drone_device()], forward_all=False, seen_macs={},
                repost_interval_s=60.0, check_wifibroadcast=True,
                kismet_url="http://kismet:2501", kismet_apikey="key123")
        check_fn.assert_called_once()
        self.assertEqual(posted, 2)  # the OUI-match detection AND the wifibroadcast one
        posted_bodies = [c.kwargs["json"] for c in post.call_args_list]
        confidence_types = [b["confidence_type"] for b in posted_bodies]
        self.assertIn("heuristic_binary", confidence_types)
        wb_bodies = [b for b in posted_bodies if "WIFIBROADCAST" in b.get("callsign", "")]
        self.assertEqual(len(wb_bodies), 1)

    def test_no_extra_post_when_check_disabled(self):
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {"callsign": "KISMET-60:60:1F:44:55:66"}
        fake_resp.status_code = 200
        with mock.patch.object(kismet_bridge, "check_wifibroadcast_signature") as check_fn, \
             mock.patch.object(kismet_bridge.requests, "post", return_value=fake_resp):
            posted = kismet_bridge.poll_once(
                "http://console", {}, "a@b.com", "pw",
                [self._drone_device()], forward_all=False, seen_macs={},
                repost_interval_s=60.0, check_wifibroadcast=False)
        check_fn.assert_not_called()
        self.assertEqual(posted, 1)


if __name__ == "__main__":
    unittest.main()
