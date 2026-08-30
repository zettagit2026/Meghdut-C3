"""Unit tests for the WiFi identification-confidence fusion pure helpers.

Context: the 2.4GHz band is shared by DJI OcuSync/video control links and
ordinary WiFi, so the RSSI heuristic (hackrf_rx.py) and the closed-world ML
classifier (ml_classify_bridge.py) both flag ambient WiFi APs as "DJI Mini
(candidate)". server.py's fusion cross-references a real Kismet 802.11 monitor
as ground truth to re-attribute those to WiFi (or corroborate a real drone).

These are true unit tests (no live server/Mongo, same style as
tests/test_audit_anchor.py): they exercise the REAL pure helpers
_is_24ghz_drone_candidate() and _wifi_attribution_override() from server.py.
The Mongo-backed _wifi_fusion_lookup() is exercised live on the appliance, not
here.

Run: pytest backend/tests/test_wifi_fusion.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server as srv  # noqa: E402


# --- _is_24ghz_drone_candidate ------------------------------------------------

def test_heuristic_dji_candidate_in_24ghz_is_candidate():
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.45, None, "heuristic_binary", False) is True


def test_ml_drone_in_24ghz_is_candidate():
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.437, "drone", "ml_probability", False) is True


def test_unclassified_candidate_in_24ghz_is_candidate():
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.42, None, "unclassified_signal", False) is True


def test_protocol_confirmed_is_never_a_candidate():
    # A genuinely decoded drone must NEVER be suppressed by a co-channel WiFi AP.
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.45, "drone", "protocol_verified", True) is False
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.45, None, "protocol_verified", False) is False


def test_out_of_band_58ghz_is_not_a_candidate():
    # 5.8GHz has no 2.4GHz WiFi ground truth to cross-check -> stays a candidate.
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 5.8, "drone", "ml_probability", False) is False


def test_non_candidate_model_in_band_is_not_a_candidate():
    assert srv._is_24ghz_drone_candidate(
        "Samsung Wi-Fi Client", 2.437, None, "advisory_only", False) is False


def test_feature_flag_disables_candidate_detection(monkeypatch):
    monkeypatch.setattr(srv, "DETECTION_WIFI_FUSION_ENABLED", False)
    assert srv._is_24ghz_drone_candidate(
        "DJI Mini (candidate)", 2.45, "drone", "heuristic_binary", False) is False


# --- _wifi_attribution_override -----------------------------------------------

def test_non_drone_device_reattributes_to_wifi():
    override = srv._wifi_attribution_override(
        None, {"manuf": "Cisco Systems", "ssid": "guest-wifi",
               "oui": "00:11:22", "rssi_dbm": -60})
    assert override["confidence_type"] == "wifi_attributed"
    assert override["threat_level"] == "LOW"
    assert override["model"].startswith("Wi-Fi")
    assert "Cisco Systems" in override["model"]
    assert "guest-wifi" in override["model"]
    assert override["protocol"] == "Wi-Fi 802.11"
    assert override["wifi_fusion"]["verdict"] == "attributed_wifi"


def test_drone_oui_device_corroborates():
    override = srv._wifi_attribution_override(
        {"manuf": "Dji Innovations", "ssid": None, "oui": "60:60:1F",
         "rssi_dbm": -55}, None)
    assert override["confidence_type"] == "multidomain_fused"
    assert override["threat_level"] == "HIGH"
    # Corroboration keeps the existing display model (no "model" override key).
    assert "model" not in override
    assert override["wifi_fusion"]["verdict"] == "corroborated_drone"
    assert override["wifi_fusion"]["matched_manuf"] == "Dji Innovations"


def test_drone_oui_takes_precedence_over_non_drone():
    override = srv._wifi_attribution_override(
        {"manuf": "Dji Innovations", "oui": "60:60:1F", "rssi_dbm": -70},
        {"manuf": "Cisco Systems", "oui": "00:11:22", "rssi_dbm": -50})
    assert override["confidence_type"] == "multidomain_fused"


def test_no_ground_truth_returns_none():
    assert srv._wifi_attribution_override(None, None) is None
