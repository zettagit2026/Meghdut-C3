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
            {"remoteid", "droneid", "control_link", "fpv_osd", "adsb", "parrot"}
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
