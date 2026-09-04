#!/usr/bin/env python3
"""Unit tests for parrot_arsdk_ingest_bridge.py -- the LIVE Kismet -> ARSDK
decode wiring. No hardware, no drone, no real network.

The ARSDK frames used here are built with parrot_arsdk_decode_bridge's OWN
frame/command builders from the real, cited ARSDK3 wire-format constants
(frame header from libARNetworkAL, ARCommand layout from arsdk-xml), so a
decode success is genuine, not a self-consistent fabrication.
"""
import parrot_arsdk_ingest_bridge as b
import parrot_arsdk_decode_bridge as pa


def _battery_frame_hex(percent: int = 77) -> str:
    """A real common.CommonState.BatteryStateChanged DATA frame (project=0,
    class=5, command=1), assembled from the decoder's verified builders."""
    payload = pa._build_arcommand(
        pa.PROJECT_COMMON, pa.CLASS_COMMON_COMMONSTATE,
        pa.CMD_COMMON_COMMONSTATE_BATTERYSTATECHANGED, bytes([percent]))
    return pa._build_frame(pa.FRAME_TYPE_DATA, 125, 42, payload).hex()


def _parrot_wifi_device(frame_hex):
    return {
        "kismet.device.base.macaddr": "90:03:B7:AA:BB:CC",   # Parrot SA OUI
        "kismet.device.base.phyname": "IEEE802.11",
        "kismet.device.base.manuf": "Parrot SA",
        "kismet.device.base.name": "ANAFI-123456",
        "kismet.device.base.signal": {"kismet.common.signal.last_signal": -47},
        # ARSDK frame bytes surfaced under an ARSDK-hinted key (schema-agnostic
        # extraction, so the exact key path does not matter).
        "dot11.device": {"arsdk.raw": frame_hex},
    }


class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status
        self.text = "ok"

    def json(self):
        return {"ok": True}


def _install_capture(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json))
        return _FakeResp(200)

    monkeypatch.setattr(b.requests, "post", fake_post)
    return calls


# ---------------------------------------------------------------------------
# Decode + identification
# ---------------------------------------------------------------------------
def test_decode_device_arsdk_finds_real_command():
    dev = _parrot_wifi_device(_battery_frame_hex(77))
    obs = b.decode_device_arsdk(dev)
    assert len(obs) == 1
    assert obs[0]["arcommand"]["command"] == "common.CommonState.BatteryStateChanged"
    assert obs[0]["arcommand"]["percent"] == 77


def test_non_parrot_device_decodes_nothing():
    dev = {"kismet.device.base.macaddr": "3C:5A:B4:11:22:33",
           "kismet.device.base.phyname": "IEEE802.11",
           "kismet.device.base.name": "Galaxy-S23",
           "kismet.device.base.manuf": "Samsung"}
    assert b.decode_device_arsdk(dev) == []


def test_arsdk_hinted_but_garbage_bytes_do_not_fabricate():
    # A key that LOOKS like ARSDK but carries non-ARSDK bytes must not produce a
    # fake decode (the decoder validates frame + ARCommand headers and raises).
    dev = {"arsdk_raw": "01"}   # too short for even a frame header
    assert b.decode_device_arsdk(dev) == []


def test_is_parrot_device_by_oui_ssid_manuf():
    assert b.is_parrot_device({"kismet.device.base.macaddr": "90:03:B7:00:00:01"})
    assert b.is_parrot_device({"kismet.device.base.manuf": "Parrot SA"})
    assert b.is_parrot_device({"kismet.device.base.name": "Bebop2-000000"})
    assert not b.is_parrot_device({"kismet.device.base.macaddr": "3C:5A:B4:11:22:33",
                                   "kismet.device.base.manuf": "Samsung",
                                   "kismet.device.base.name": "Galaxy-S23"})


def test_arcommand_to_ingest_body_shape():
    dev = _parrot_wifi_device(_battery_frame_hex(50))
    obs = b.decode_device_arsdk(dev)[0]
    body = b.arcommand_to_ingest_body(obs, source_mac=b._device_mac(dev),
                                      ssid=b._device_ssid(dev),
                                      rssi_dbm=b._device_rssi(dev))
    assert body["project"] == "common"
    assert body["drone_class"] == "CommonState"
    assert body["command"] == "common.CommonState.BatteryStateChanged"
    assert body["source_mac"] == "90:03:B7:AA:BB:CC"
    assert body["ssid"] == "ANAFI-123456"
    assert body["rssi_dbm"] == -47.0
    assert body["source"] == "PARROT_ARSDK_KISMET"
    assert body["caveats"]


# ---------------------------------------------------------------------------
# poll_once: a real decode POSTs an ingest AND a heartbeat
# ---------------------------------------------------------------------------
def test_poll_once_ingests_and_heartbeats(monkeypatch):
    calls = _install_capture(monkeypatch)
    dev = _parrot_wifi_device(_battery_frame_hex(88))
    monkeypatch.setattr(b, "fetch_kismet_devices",
                        lambda *a, **k: [dev])

    n = b.poll_once("http://kismet", None, "http://x",
                    {"Authorization": "Bearer t"}, "e@x", "pw", None)
    assert n == 1

    ingest_calls = [c for c in calls if c[0].endswith("/api/parrot/ingest")]
    heartbeat_calls = [c for c in calls if c[0].endswith("/api/protocols/heartbeat")]
    assert ingest_calls, "a real ARSDK decode must POST /api/parrot/ingest"
    assert heartbeat_calls, "every up-feed cycle must POST a heartbeat"
    assert heartbeat_calls[0][1]["protocol"] == "parrot"
    assert ingest_calls[0][1]["project"] == "common"
    assert ingest_calls[0][1]["command"] == "common.CommonState.BatteryStateChanged"
    assert ingest_calls[0][1]["ssid"] == "ANAFI-123456"


def test_poll_once_quiet_feed_still_heartbeats(monkeypatch):
    # Kismet reachable but no Parrot frames -> 0 ingests, heartbeat still fires.
    calls = _install_capture(monkeypatch)
    plain = {"kismet.device.base.macaddr": "3C:5A:B4:11:22:33",
             "kismet.device.base.name": "Galaxy-S23"}
    monkeypatch.setattr(b, "fetch_kismet_devices", lambda *a, **k: [plain])
    n = b.poll_once("http://kismet", None, "http://x",
                    {"Authorization": "Bearer t"}, "e@x", "pw", None)
    assert n == 0
    assert any(c[0].endswith("/api/protocols/heartbeat") for c in calls)
    assert not any(c[0].endswith("/api/parrot/ingest") for c in calls)


def test_poll_once_offline_does_not_heartbeat(monkeypatch):
    # Kismet UNREACHABLE -> honest OFFLINE: poll_once returns None and NO
    # heartbeat is posted (a heartbeat with no feed behind it would be a lie).
    calls = _install_capture(monkeypatch)
    import requests

    def raise_conn(*a, **k):
        raise requests.ConnectionError("kismet down")

    monkeypatch.setattr(b, "fetch_kismet_devices", raise_conn)
    n = b.poll_once("http://kismet", None, "http://x",
                    {"Authorization": "Bearer t"}, "e@x", "pw", None)
    assert n is None
    assert calls == [], "OFFLINE cycle must post nothing (no fake-READY heartbeat)"
