"""Tests for the READ-ONLY Wi-Fi Environment (RF situational-awareness) survey.

Covers:
  * kismet_survey.parse_kismet_device -- produces the survey shape from a
    mocked Kismet device (the SAME field schema Kismet's REST API returns,
    verified against ../kismet source in kismet_survey.py's docstring).
  * non-actionable "possible UAS" tagging (drone OUI + drone SSID pattern).
  * build_survey -- sorts by RSSI (strongest first) and caps to top_n.
  * GET /api/wifi-environment endpoint:
      - is auth-gated (get_current_user dependency present),
      - returns the survey shape from a mocked Kismet response,
      - is READ-ONLY (no db writes, no detection creation),
      - degrades gracefully on a Kismet failure (empty list + honest status,
        not a crash) and when Kismet is not configured.

Run: pytest backend/tests/test_wifi_environment.py -v
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kismet_survey  # noqa: E402


# --- Fixtures: real Kismet device-JSON shapes --------------------------------

def _wpa2_ap_device():
    """A normal WPA2 AP with PMF supported and 3 associated clients."""
    return {
        "kismet.device.base.macaddr": "AC:DE:48:00:11:22",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.name": "OfficeNet",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.manuf": "Cisco Systems, Inc",
        "kismet.device.base.channel": "36",
        "kismet.device.base.frequency": 5180000,
        "kismet.device.base.first_time": 1_700_000_000,
        "kismet.device.base.last_time": 1_700_000_600,
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": -47},
        "dot11.device": {
            "dot11.device.num_associated_clients": 3,
            "dot11.device.last_beaconed_ssid_record": {
                "dot11.advertisedssid.ssid": "OfficeNet",
                "dot11.advertisedssid.crypt_string": "WPA2-PSK-AES",
                "dot11.advertisedssid.wpa_mfp_required": False,
                "dot11.advertisedssid.wpa_mfp_supported": True,
            },
        },
    }


def _open_ap_device():
    return {
        "kismet.device.base.macaddr": "00:11:22:33:44:55",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.name": "GuestWiFi",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.manuf": "Netgear",
        "kismet.device.base.channel": "6",
        "kismet.device.base.frequency": 2437000,
        "kismet.device.base.first_time": 1_700_000_100,
        "kismet.device.base.last_time": 1_700_000_700,
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": -70},
        "dot11.device": {
            "dot11.device.num_associated_clients": 0,
            "dot11.device.last_beaconed_ssid_record": {
                "dot11.advertisedssid.ssid": "GuestWiFi",
                "dot11.advertisedssid.crypt_string": "Open",
            },
        },
    }


def _dji_oui_device():
    """DJI-OUI-prefixed AP -- exercises the non-actionable possible-UAS tag."""
    return {
        "kismet.device.base.macaddr": "60:60:1F:AA:BB:CC",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.name": "",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.manuf": "Dji Innovations",
        "kismet.device.base.channel": "149",
        "kismet.device.base.frequency": 5745000,
        "kismet.device.base.first_time": 1_700_000_200,
        "kismet.device.base.last_time": 1_700_000_800,
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": -55},
    }


def _projected_ap_device():
    """A FIELD-VIEW (projected) response row -- the shape the field-projected
    POST returns: Kismet ships ONLY the requested KISMET_SURVEY_FIELDS and
    flattens the nested signal path to its terminal key. parse_kismet_device
    must read this projected shape as fully as a full device object -- RSSI,
    ssid, bssid, channel, vendor, encryption, PMF and client-count all populate.
    """
    return {
        "kismet.device.base.macaddr": "AC:DE:48:00:33:44",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.name": "ProjNet",
        "kismet.device.base.type": "Wi-Fi AP",
        "kismet.device.base.manuf": "Ubiquiti Inc",
        "kismet.device.base.channel": "44",
        "kismet.device.base.frequency": 5220000,
        "kismet.device.base.first_time": 1_700_000_000,
        "kismet.device.base.last_time": 1_700_000_600,
        # Field-view flattens the "signal/last_signal" path to its terminal key.
        "kismet.common.signal.last_signal": -52,
        # dot11 children projected under their requested parent object.
        "dot11.device": {
            "dot11.device.num_associated_clients": 5,
            "dot11.device.last_beaconed_ssid_record": {
                "dot11.advertisedssid.ssid": "ProjNet",
                "dot11.advertisedssid.crypt_string": "WPA3-SAE",
                "dot11.advertisedssid.wpa_mfp_required": True,
                "dot11.advertisedssid.wpa_mfp_supported": True,
            },
        },
    }


# --- parse_kismet_device ------------------------------------------------------

def test_parse_produces_survey_shape():
    row = kismet_survey.parse_kismet_device(_wpa2_ap_device())
    assert row["bssid"] == "AC:DE:48:00:11:22"
    assert row["ssid"] == "OfficeNet"
    assert row["channel"] == "36"
    assert row["rssi_dbm"] == -47.0
    assert row["vendor"] == "Cisco Systems, Inc"
    assert row["encryption"] == "WPA2-PSK-AES"
    assert row["pmf_supported"] is True
    assert row["pmf_required"] is False
    assert row["client_count"] == 3
    assert row["first_seen"] == 1_700_000_000
    assert row["last_seen"] == 1_700_000_600
    assert row["frequency_ghz"] == 5.18
    assert row["possible_uas"] is False
    # Every documented survey key is present (stable shape for the frontend).
    for key in ("bssid", "ssid", "channel", "rssi_dbm", "vendor", "encryption",
                "pmf_required", "pmf_supported", "client_count", "first_seen",
                "last_seen", "possible_uas"):
        assert key in row


def test_parse_projected_field_view_shape_populates_all():
    # The field-projected POST returns the compact field-view shape (flattened
    # signal key + projected dot11 children). Every survey field must still
    # populate from it -- this is what the live SRE fix verified end-to-end.
    row = kismet_survey.parse_kismet_device(_projected_ap_device())
    assert row["bssid"] == "AC:DE:48:00:33:44"
    assert row["oui"] == "AC:DE:48"
    assert row["ssid"] == "ProjNet"
    assert row["channel"] == "44"
    assert row["vendor"] == "Ubiquiti Inc"
    assert row["rssi_dbm"] == -52.0            # flattened signal key still read
    assert row["encryption"] == "WPA3-SAE"
    assert row["pmf_required"] is True
    assert row["pmf_supported"] is True
    assert row["client_count"] == 5
    assert row["frequency_ghz"] == 5.22
    assert row["possible_uas"] is False


def test_parse_open_ap_encryption_open():
    row = kismet_survey.parse_kismet_device(_open_ap_device())
    assert row["encryption"] == "Open"
    assert row["client_count"] == 0
    assert row["possible_uas"] is False


def test_parse_degrades_gracefully_on_missing_fields():
    # A bare device (e.g. a Bluetooth device or a stripped field-view) must
    # never raise -- missing fields degrade to None/unknown.
    row = kismet_survey.parse_kismet_device(
        {"kismet.device.base.macaddr": "F4:5C:89:00:00:01",
         "kismet.device.base.phyname": "Bluetooth"})
    assert row["bssid"] == "F4:5C:89:00:00:01"
    assert row["encryption"] is None
    assert row["client_count"] is None
    assert row["rssi_dbm"] is None
    assert row["ssid"] is None
    assert row["possible_uas"] is False


def test_parse_flattened_signal_field():
    # Kismet field-view responses can flatten the "parent/child" path.
    row = kismet_survey.parse_kismet_device({
        "kismet.device.base.macaddr": "AA:BB:CC:DD:EE:FF",
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.common.signal.last_signal": -60,
    })
    assert row["rssi_dbm"] == -60.0


# --- possible-UAS tagging (NON-ACTIONABLE) ------------------------------------

def test_drone_oui_tags_possible_uas():
    row = kismet_survey.parse_kismet_device(_dji_oui_device())
    assert row["possible_uas"] is True
    assert "DJI" in (row["possible_uas_reason"] or "")


def test_drone_ssid_pattern_tags_possible_uas():
    dev = _open_ap_device()
    dev["kismet.device.base.name"] = "Mavic-Air-1234"
    dev["dot11.device"]["dot11.device.last_beaconed_ssid_record"][
        "dot11.advertisedssid.ssid"] = "Mavic-Air-1234"
    row = kismet_survey.parse_kismet_device(dev)
    assert row["possible_uas"] is True


def test_ordinary_ap_not_tagged():
    assert kismet_survey.parse_kismet_device(_wpa2_ap_device())["possible_uas"] is False


# --- build_survey: sort by RSSI, cap to top_n --------------------------------

def test_build_survey_sorts_by_rssi_desc():
    rows = kismet_survey.build_survey(
        [_open_ap_device(), _wpa2_ap_device(), _dji_oui_device()])
    rssis = [r["rssi_dbm"] for r in rows]
    assert rssis == sorted(rssis, reverse=True)
    assert rows[0]["rssi_dbm"] == -47.0  # strongest first


def test_build_survey_caps_to_top_n():
    devices = [_wpa2_ap_device(), _open_ap_device(), _dji_oui_device()]
    rows = kismet_survey.build_survey(devices, top_n=2)
    assert len(rows) == 2


def test_build_survey_none_rssi_sorts_last():
    no_rssi = {"kismet.device.base.macaddr": "00:00:00:00:00:99",
               "kismet.device.base.phyname": "IEEE802.11"}
    rows = kismet_survey.build_survey([no_rssi, _wpa2_ap_device()])
    assert rows[-1]["bssid"] == "00:00:00:00:00:99"


# --- fetch: read-only field-projected POST -----------------------------------

def test_fetch_uses_post_field_projection(monkeypatch):
    """The Kismet client must issue a field-projected POST to the device-list
    route: a recent negative window, the apikey as the KISMET query param, and
    a `json` POST var carrying {"fields": KISMET_SURVEY_FIELDS}. It must NEVER
    issue a bare GET of full device objects (which times out against a busy
    Kismet), nor any write verb (PUT/DELETE/PATCH)."""
    import json as _json
    calls = {"post": 0}
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return [_wpa2_ap_device()]

    def fake_post(url, params=None, data=None, timeout=None):
        calls["post"] += 1
        seen["url"] = url
        seen["params"] = params or {}
        seen["data"] = data or {}
        seen["timeout"] = timeout
        assert "/devices/" in url  # a Kismet device-list route
        return _Resp()

    monkeypatch.setattr(kismet_survey.requests, "post", fake_post)
    # No GET-of-full-objects and no write verbs may be issued.
    for verb in ("get", "put", "delete", "patch"):
        monkeypatch.setattr(kismet_survey.requests, verb,
                            MagicMock(side_effect=AssertionError(f"{verb} not allowed")))

    out = kismet_survey.fetch_kismet_devices("http://kismet:2501", "apikey")

    assert calls["post"] == 1
    # apikey travels as the KISMET query param (server-side), not in the body.
    assert seen["params"].get("KISMET") == "apikey"
    # Recent negative window in the route (not a 24h/-86400 window).
    assert f"/devices/last-time/-{kismet_survey.KISMET_SURVEY_WINDOW_S}/devices.json" in seen["url"]
    # Field-projection: a `json` POST var carrying the survey fields list.
    body = _json.loads(seen["data"]["json"])
    assert body["fields"] == kismet_survey.KISMET_SURVEY_FIELDS
    # Raised read timeout for headroom against a busy Kismet.
    assert seen["timeout"] >= 12.0
    assert out and out[0]["kismet.device.base.macaddr"] == "AC:DE:48:00:11:22"


# =====================================================================
# Endpoint: GET /api/wifi-environment (auth-gated, read-only, graceful)
# =====================================================================
# Imported lazily inside the tests so the pure-helper tests above can run
# even if the full server module (Mongo client construction) is unavailable.

def _import_server():
    import server as srv  # noqa: E402
    return srv


def test_endpoint_is_auth_gated():
    srv = _import_server()
    # The route must depend on get_current_user (same gate as other reads).
    route = next(r for r in srv.app.routes
                 if getattr(r, "path", None) == "/api/wifi-environment")
    dep_calls = [d.call for d in route.dependant.dependencies]
    assert srv.get_current_user in dep_calls


def test_endpoint_returns_survey_and_is_read_only(monkeypatch):
    srv = _import_server()
    monkeypatch.setattr(srv, "KISMET_URL", "http://kismet:2501")
    monkeypatch.setattr(srv, "KISMET_APIKEY", "apikey")
    srv._wifi_env_cache["payload"] = None  # bypass any warm cache
    srv._wifi_env_cache["at"] = 0.0

    monkeypatch.setattr(kismet_survey, "fetch_kismet_devices",
                        lambda url, key: [_wpa2_ap_device(), _dji_oui_device()])

    # READ-ONLY guard: replace db with a mock and assert NOTHING is called on it.
    mock_db = MagicMock()
    monkeypatch.setattr(srv, "db", mock_db)

    out = asyncio.run(srv.get_wifi_environment(user={"role": "operator"}))

    assert out["configured"] is True
    assert out["available"] is True
    assert out["source"] == "KISMET"
    assert out["total_seen"] == 2
    assert out["ap_count"] == 2
    assert out["aps"][0]["rssi_dbm"] == -47.0  # sorted strongest-first
    # No database interaction whatsoever -> no detection created, no write.
    assert mock_db.mock_calls == []


def test_endpoint_degrades_gracefully_on_kismet_failure(monkeypatch):
    srv = _import_server()
    monkeypatch.setattr(srv, "KISMET_URL", "http://kismet:2501")
    monkeypatch.setattr(srv, "KISMET_APIKEY", "apikey")
    srv._wifi_env_cache["payload"] = None
    srv._wifi_env_cache["at"] = 0.0

    def _boom(url, key):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(kismet_survey, "fetch_kismet_devices", _boom)
    mock_db = MagicMock()
    monkeypatch.setattr(srv, "db", mock_db)

    out = asyncio.run(srv.get_wifi_environment(user={"role": "operator"}))
    # Honest empty result, NOT a crash/500.
    assert out["configured"] is True
    assert out["available"] is False
    assert out["aps"] == []
    assert "unreachable" in out["status"].lower() or "error" in out["status"].lower()
    assert mock_db.mock_calls == []


def test_endpoint_error_status_never_leaks_apikey(monkeypatch):
    """The graceful-degrade error status must NEVER echo the Kismet apikey or
    any query string carrying it -- requests embeds the full ?KISMET=<key> URL
    in its exception text, and that must be redacted before it reaches the UI."""
    srv = _import_server()
    monkeypatch.setattr(srv, "KISMET_URL", "http://kismet:2501")
    monkeypatch.setattr(srv, "KISMET_APIKEY", "SUPERSECRETKEY123")
    srv._wifi_env_cache["payload"] = None
    srv._wifi_env_cache["at"] = 0.0

    def _boom(url, key):
        # Exactly the shape requests raises: the full URL WITH the apikey query.
        raise requests.ConnectionError(
            "HTTPConnectionPool(host='kismet', port=2501): Max retries exceeded "
            "with url: /devices/last-time/-600/devices.json?KISMET=SUPERSECRETKEY123 "
            "(Caused by NewConnectionError('Connection refused'))")

    monkeypatch.setattr(kismet_survey, "fetch_kismet_devices", _boom)
    mock_db = MagicMock()
    monkeypatch.setattr(srv, "db", mock_db)

    out = asyncio.run(srv.get_wifi_environment(user={"role": "operator"}))
    status = out["status"]
    assert out["available"] is False
    # No apikey, no query string leaking the apikey.
    assert "SUPERSECRETKEY123" not in status
    assert "KISMET=" not in status
    assert "?" not in status
    # Still honest + human-readable, and keeps a clean host:port.
    assert "unreachable" in status.lower() or "error" in status.lower()
    assert "kismet:2501" in status
    assert mock_db.mock_calls == []


def test_endpoint_reports_not_configured_when_kismet_url_blank(monkeypatch):
    srv = _import_server()
    monkeypatch.setattr(srv, "KISMET_URL", "")
    srv._wifi_env_cache["payload"] = None
    srv._wifi_env_cache["at"] = 0.0
    out = asyncio.run(srv.get_wifi_environment(user={"role": "operator"}))
    assert out["configured"] is False
    assert out["available"] is False
    assert out["aps"] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
