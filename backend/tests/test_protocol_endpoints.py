"""Live-backend tests for the Protocol-Library over-the-air ingest + status
endpoints (make-protocol-library-live task).

Same live-preview-backend convention as test_new_endpoints.py: resolves the
backend URL + admin credentials from the environment / frontend .env, logs in,
and exercises the real endpoints end-to-end. Run against a booted backend
(these are skipped/uncollectable in the pure sandbox, exactly like the other
backend/tests/*).
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest
import requests

if "REACT_APP_BACKEND_URL" in os.environ:
    BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
else:
    env_txt = Path("/app/frontend/.env").read_text()
    BASE_URL = None
    for line in env_txt.splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL not resolvable"

API = f"{BASE_URL}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "operator@meghaduta.mil")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(16)


@pytest.fixture(scope="module")
def auth_headers() -> dict:
    r = requests.post(f"{API}/auth/login",
                      json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _status(auth_headers):
    r = requests.get(f"{API}/protocols/status", headers=auth_headers, timeout=10)
    assert r.status_code == 200, r.text
    return r.json()


def _op(board, pid):
    return next(o for o in board["operational"] if o["id"] == pid)


class TestProtocolStatusBoard:
    def test_board_shape(self, auth_headers):
        board = _status(auth_headers)
        assert {o["id"] for o in board["operational"]} == \
            {"remoteid", "droneid", "control_link", "fpv_osd", "adsb", "parrot",
             "wifi_drone", "fpv_analog_5g8", "gnss_l1_jammer", "lora_subghz"}
        assert len(board["forensic"]) == 12
        assert all(f["status"] == "FORENSIC" for f in board["forensic"])
        # Wire decoders must be forensic, never operational.
        forensic_ids = {f["id"] for f in board["forensic"]}
        assert {"crsf", "msp", "canopen", "dronecan"}.issubset(forensic_ids)

    def test_heartbeat_takes_protocol_ready(self, auth_headers):
        r = requests.post(f"{API}/protocols/heartbeat",
                          headers=auth_headers, json={"protocol": "control_link"}, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "control_link")["status"] in ("READY", "LIVE")

    def test_heartbeat_accepts_adsb_and_parrot(self, auth_headers):
        for pid in ("adsb", "parrot"):
            r = requests.post(f"{API}/protocols/heartbeat",
                              headers=auth_headers, json={"protocol": pid}, timeout=10)
            assert r.status_code == 200, r.text
            board = _status(auth_headers)
            assert _op(board, pid)["status"] in ("READY", "LIVE")

    def test_unknown_protocol_heartbeat_rejected(self, auth_headers):
        r = requests.post(f"{API}/protocols/heartbeat",
                          headers=auth_headers, json={"protocol": "not_a_real_protocol"}, timeout=10)
        assert r.status_code == 400


class TestRemoteIdIngest:
    def test_decode_takes_remoteid_live(self, auth_headers):
        body = {
            "uas_id": f"TESTUAS-{secrets.token_hex(4)}",
            "id_type": "SERIAL_NUMBER",
            "latitude_deg": 28.6139, "longitude_deg": 77.2090,
            "operator_id": "OP-123", "transport": "wifi",
            "source_mac": "60:60:1F:00:11:22",
        }
        r = requests.post(f"{API}/remoteid/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "remoteid")["status"] == "LIVE"
        assert _op(board, "remoteid")["decode_count"] >= 1

        latest = requests.get(f"{API}/remoteid/latest", headers=auth_headers, timeout=10).json()
        assert latest["available"] is True
        assert latest["uas_id"] == body["uas_id"]


class TestFpvOsdIngest:
    def test_telemetry_takes_fpv_osd_live(self, auth_headers):
        body = {
            "source": "FPV_OSD_OCR", "method": "max7456_glyph_template_match",
            "telemetry": {"craft_name": "HAWK01", "battery_voltage": 16.2, "altitude": 150.0},
            "mean_confidence": 0.9, "video_standard": "NTSC",
        }
        r = requests.post(f"{API}/fpv/osd/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["telemetry_present"] is True
        board = _status(auth_headers)
        assert _op(board, "fpv_osd")["status"] == "LIVE"

    def test_empty_telemetry_is_heartbeat_only(self, auth_headers):
        # A frame with no legible OSD must NOT be reported as a decode.
        body = {"source": "FPV_OSD_OCR",
                "telemetry": {"craft_name": None, "battery_voltage": None}}
        r = requests.post(f"{API}/fpv/osd/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["telemetry_present"] is False


class TestControlLinkIngest:
    def test_classification_takes_control_link_live(self, auth_headers):
        body = {
            "detection_id": "det-test", "center_freq_ghz": 2.44,
            "link_type": "2.4 GHz hobby-RC LRS-class (ELRS 2.4 / DSMX / FrSky / Flysky)",
            "link_family": "hobby_rc_2g4", "confidence_type": "advisory_only",
            "rationale": "narrowband 2.4 GHz", "evidence": {"bandwidth_mhz": 1.5},
        }
        r = requests.post(f"{API}/control-link/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "control_link")["status"] == "LIVE"

    def test_unknown_classification_is_heartbeat_only(self, auth_headers):
        body = {"link_type": "unknown", "confidence_type": "advisory_only"}
        r = requests.post(f"{API}/control-link/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text


class TestAdsbIngest:
    def test_decode_takes_adsb_live_and_stores_latest(self, auth_headers):
        icao = secrets.token_hex(3).upper()
        body = {
            "icao24": icao, "callsign": "TEST123",
            "latitude_deg": 28.6139, "longitude_deg": 77.2090,
            "altitude_ft": 35000.0, "ground_speed_kt": 450.0, "squawk": "7000",
        }
        r = requests.post(f"{API}/adsb/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "adsb")["status"] == "LIVE"
        assert _op(board, "adsb")["decode_count"] >= 1

        latest = requests.get(f"{API}/adsb/latest", headers=auth_headers, timeout=10).json()
        assert latest["available"] is True
        assert latest["icao24"] == icao

    def test_adsb_stores_latest_only(self, auth_headers):
        first = secrets.token_hex(3).upper()
        second = secrets.token_hex(3).upper()
        for icao in (first, second):
            r = requests.post(f"{API}/adsb/ingest", headers=auth_headers,
                              json={"icao24": icao}, timeout=10)
            assert r.status_code == 200, r.text
        latest = requests.get(f"{API}/adsb/latest", headers=auth_headers, timeout=10).json()
        assert latest["icao24"] == second  # latest-only: last write wins


class TestParrotIngest:
    def test_observation_takes_parrot_live_and_stores_latest(self, auth_headers):
        ssid = f"ANAFI-{secrets.token_hex(3)}"
        body = {
            "project": "ardrone3", "drone_class": "Piloting", "command": "PCMD",
            "source_mac": "90:03:B7:00:11:22", "ssid": ssid, "rssi_dbm": -52.0,
        }
        r = requests.post(f"{API}/parrot/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "parrot")["status"] == "LIVE"
        assert _op(board, "parrot")["decode_count"] >= 1

        latest = requests.get(f"{API}/parrot/latest", headers=auth_headers, timeout=10).json()
        assert latest["available"] is True
        assert latest["ssid"] == ssid

    def test_parrot_stores_latest_only(self, auth_headers):
        first = f"ANAFI-{secrets.token_hex(3)}"
        second = f"ANAFI-{secrets.token_hex(3)}"
        for ssid in (first, second):
            r = requests.post(f"{API}/parrot/ingest", headers=auth_headers,
                              json={"ssid": ssid, "command": "PCMD"}, timeout=10)
            assert r.status_code == 200, r.text
        latest = requests.get(f"{API}/parrot/latest", headers=auth_headers, timeout=10).json()
        assert latest["ssid"] == second  # latest-only: last write wins


class TestWifiDroneIngest:
    def test_match_takes_wifi_drone_live_and_stores_latest(self, auth_headers):
        mac = f"60:60:1F:{secrets.token_hex(1).upper()}:11:22"
        body = {
            "ssid": f"TELLO-{secrets.token_hex(3)}", "oui": "60:60:1F", "manuf": "SZ DJI",
            "make_candidate": "DJI/Ryze Tello", "match_basis": "ssid+oui",
            "channel": 6, "signal_dbm": -48.0, "source_mac": mac,
        }
        r = requests.post(f"{API}/wifi-drone/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "wifi_drone")["status"] == "LIVE"
        assert _op(board, "wifi_drone")["decode_count"] >= 1
        # HONEST: the board copy must state the candidate/spoofable caveat, never a serial.
        assert "candidate" in _op(board, "wifi_drone")["identifies"].lower()
        latest = requests.get(f"{API}/wifi-drone/latest", headers=auth_headers, timeout=10).json()
        assert latest["available"] is True
        assert latest["make_candidate"] == "DJI/Ryze Tello"


class TestFpvAnalogIngest:
    def test_channel_id_takes_fpv_analog_live_and_stores_latest(self, auth_headers):
        body = {
            "band": "Raceband", "channel": "R4", "carrier_mhz": 5769.0,
            "center_freq_mhz": 5769.5, "offset_mhz": 0.5, "rssi_dbm": -40.0,
        }
        r = requests.post(f"{API}/fpv-analog/ingest", headers=auth_headers, json=body, timeout=10)
        assert r.status_code == 200, r.text
        board = _status(auth_headers)
        assert _op(board, "fpv_analog_5g8")["status"] == "LIVE"
        latest = requests.get(f"{API}/fpv-analog/latest", headers=auth_headers, timeout=10).json()
        assert latest["available"] is True
        assert latest["channel"] == "R4"


class TestGnssL1JammerIngest:
    def test_jamming_true_takes_live_clean_is_ready(self, auth_headers):
        # jamming=True -> LIVE (a real jammer assessment).
        jam = {"jamming": True, "center_freq_mhz": 1575.42, "peak_dbm": -20.0,
               "median_dbm": -35.0, "elevation_db": 23.0, "occupied_frac": 0.8}
        r = requests.post(f"{API}/gnss-l1-jammer/ingest", headers=auth_headers, json=jam, timeout=10)
        assert r.status_code == 200, r.text
        assert _op(_status(auth_headers), "gnss_l1_jammer")["status"] == "LIVE"
        # A clean (jamming=False) assessment is a heartbeat only -> stays READY/LIVE,
        # never OFFLINE, and never fabricates a jamming decode.
        clean = {"jamming": False, "center_freq_mhz": 1575.42, "peak_dbm": -55.0,
                 "median_dbm": -58.0, "elevation_db": 0.0, "occupied_frac": 0.0}
        r = requests.post(f"{API}/gnss-l1-jammer/ingest", headers=auth_headers, json=clean, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["jamming"] is False
        # HONEST: the board must say jamming, NOT spoofing.
        ident = _op(_status(auth_headers), "gnss_l1_jammer")["identifies"].lower()
        assert "jamming" in ident and "not" in ident and "spoof" in ident


class TestLoRaSubghzIngest:
    def test_present_takes_lora_live_absent_is_heartbeat(self, auth_headers):
        present = {"present": True, "center_freq_mhz": 915.0, "peak_dbm": -40.0,
                   "hit_ratio": 0.25, "window_cycles": 8}
        r = requests.post(f"{API}/lora-subghz/ingest", headers=auth_headers, json=present, timeout=10)
        assert r.status_code == 200, r.text
        assert _op(_status(auth_headers), "lora_subghz")["status"] == "LIVE"
        absent = {"present": False, "center_freq_mhz": 915.0, "peak_dbm": -70.0,
                  "hit_ratio": 0.9, "window_cycles": 8}
        r = requests.post(f"{API}/lora-subghz/ingest", headers=auth_headers, json=absent, timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["present"] is False
        # HONEST: advisory/presence only, explicitly no decode.
        assert "advisory" in _op(_status(auth_headers), "lora_subghz")["identifies"].lower()
