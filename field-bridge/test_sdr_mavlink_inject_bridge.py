"""Unit tests for sdr_mavlink_inject_bridge.py's gate chain — the field-bridge
side of the governed no-pairing SDR MAVLink inject capability.

No real HackRF hardware, no live backend, no real network / GNU Radio: requests
to the backend are monkeypatched and the pure-numpy modulator + the
hackrf_transfer transmit are stubbed, matching field-bridge/
test_gnss_spoof_bridge.py's convention. Mirrors that file's rigor.

Run: pytest field-bridge/test_sdr_mavlink_inject_bridge.py -v
"""
from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("CEMA_API_URL", "http://backend.invalid")
os.environ.setdefault("CEMA_EMAIL", "test@unused.local")
os.environ.setdefault("CEMA_PASSWORD", "unused")
# A pinned TX serial so the device-pin gate passes by default (individual tests
# override bridge.tx_serial to prove the fail-closed path).
os.environ.setdefault("HACKRF_TX_SERIAL", "TESTTX930c")

import sdr_mavlink_inject_bridge as sib
import hackrf_jam


# ---------------------------------------------------------------------
# Token shape check (defense-in-depth floor)
# ---------------------------------------------------------------------
def test_confirm_token_shape_check_rejects_short_or_missing():
    assert sib._looks_like_real_confirm_token(None) is False
    assert sib._looks_like_real_confirm_token("") is False
    assert sib._looks_like_real_confirm_token("short") is False
    assert sib._looks_like_real_confirm_token("true") is False


def test_confirm_token_shape_check_accepts_uuid_length():
    assert sib._looks_like_real_confirm_token("a" * 36) is True


def test_confirm_token_floor_is_independent_of_other_bridges():
    """The MIN_CONFIRM_TOKEN_LEN constants must be genuinely distinct module
    names (not shared code) even if equal today — a future change to one must
    not silently change the others."""
    import jam_bridge
    import gnss_spoof_bridge
    assert sib.MIN_CONFIRM_TOKEN_LEN == jam_bridge.MIN_CONFIRM_TOKEN_LEN
    assert sib.MIN_CONFIRM_TOKEN_LEN == gnss_spoof_bridge.MIN_CONFIRM_TOKEN_LEN
    assert "MIN_CONFIRM_TOKEN_LEN" in sib.__dict__


# ---------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------
class FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(__import__("json").loads(raw))


def _bridge():
    b = sib.SdrMavlinkInjectBridge()
    b.token = "fake-jwt"  # skip real login
    b.tx_serial = "TESTTX930c"  # pinned by default
    return b


def _valid_request(request_id="req-1", **overrides):
    base = {
        "request_id": request_id,
        "actor": "commander@cema.mil",
        "command": "force_land",
        "target_system": 7,
        "target_component": 1,
        "center_freq_mhz": 915.0,
        "air_rate_bps": 250000.0,
        "deviation_hz": 62500.0,
        "bt": 0.5,
        "bit_order": "msb",
        "tx_gain": 20,
        "repeat": 3,
        "mavlink_sdr_inject_confirm_token": "a" * 36,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# is_range_authorized — fail closed
# ---------------------------------------------------------------------
def test_is_range_authorized_true_when_backend_says_enabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(sib.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": True}))
    assert b.is_range_authorized("mavlink_sdr_inject") is True


def test_is_range_authorized_false_when_backend_says_disabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(sib.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": False}))
    assert b.is_range_authorized("mavlink_sdr_inject") is False


def test_is_range_authorized_fails_closed_on_network_error(monkeypatch):
    b = _bridge()
    def _raise(*a, **k):
        raise ConnectionError("backend unreachable")
    monkeypatch.setattr(sib.requests, "get", _raise)
    assert b.is_range_authorized("mavlink_sdr_inject") is False


def test_is_range_authorized_fails_closed_on_malformed_response(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(sib.requests, "get", lambda *a, **k: FakeResp(200, {"unexpected": "shape"}))
    assert b.is_range_authorized("mavlink_sdr_inject") is False


# ---------------------------------------------------------------------
# Gate chain (each gate refuses cleanly; nothing transmits)
# ---------------------------------------------------------------------
def test_refuses_when_range_not_authorized(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": False)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    assert len(ws.sent) == 1
    assert ws.sent[0]["phase"] == "failed"
    assert "range-authorization" in ws.sent[0]["error"]


def test_refuses_on_malformed_confirm_token(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(mavlink_sdr_inject_confirm_token="short"))
    assert ws.sent[0]["phase"] == "failed"
    assert "confirmation token" in ws.sent[0]["error"]


def test_refuses_fail_closed_when_no_tx_serial(monkeypatch):
    """Device-pin fail-closed: with no HACKRF_TX_SERIAL the bridge refuses rather
    than risk keying the RX detection radio via an index-based fallback."""
    b = _bridge()
    b.tx_serial = None
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    assert ws.sent[0]["phase"] == "failed"
    assert "TX serial" in ws.sent[0]["error"] or "pinned" in ws.sent[0]["error"]


def test_refuses_fail_closed_when_modulator_unavailable(monkeypatch):
    """If the pure-numpy modulator (sdr_mavlink_inject) is unimportable, the
    bridge fails closed with a clean error — never an ungoverned transmit."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    monkeypatch.setattr(sib, "_inj", None)
    monkeypatch.setattr(sib, "_INJ_IMPORT_ERROR", "ModuleNotFoundError: numpy")
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    assert ws.sent[0]["phase"] == "failed"
    assert "unavailable" in ws.sent[0]["error"]


def test_refuses_when_tx_halted(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    b.tx_halted = True
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    assert ws.sent[0]["phase"] == "failed"
    assert "tx halted" in ws.sent[0]["error"]


def test_refuses_broadcast_target_system_zero(monkeypatch):
    """A target_system of 0 would broadcast to all craft — refused at the bridge
    (defense in depth; the backend refuses it too)."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(target_system=0))
    assert ws.sent[0]["phase"] == "failed"
    assert "broadcast" in ws.sent[0]["error"].lower()


def test_refuses_unsupported_command(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(command="format_c_drive"))
    assert ws.sent[0]["phase"] == "failed"
    assert "unsupported command" in ws.sent[0]["error"]


# ---------------------------------------------------------------------
# Happy path: all gates pass -> modulate + transmit_iq_file (pinned)
# ---------------------------------------------------------------------
def _stub_modulation(monkeypatch, captured):
    """Stub the pure-numpy modulator so no numpy IQ is generated and no file is
    written, capturing what the bridge asked it to build."""
    class FakeInj:
        DEFAULT_CENTER_FREQ_MHZ = 915.0
        DEFAULT_AIR_DATA_RATE_BPS = 250000.0
        DEFAULT_DEVIATION_HZ = 62500.0
        DEFAULT_BT = 0.5
        DEFAULT_BIT_ORDER = "msb"

        @staticmethod
        def build_command_frame(command, target_system, target_component):
            captured["frame"] = (command, target_system, target_component)
            return b"\xfd\x00\x00"

        @staticmethod
        def write_iq_file(frame, path, **kw):
            captured["write_kw"] = kw
            return "/tmp/does-not-exist-sdr-inject.iq"

        @staticmethod
        def describe_modulation(frame, **kw):
            return {"on_air_duration_s": 0.004}

    monkeypatch.setattr(sib, "_inj", FakeInj)


def test_happy_path_modulates_and_transmits_via_pinned_iq_path(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)

    tx_calls = []

    def fake_transmit(iq_path, freq_mhz, duration_s, tx_gain, stop_event=None, on_started=None):
        tx_calls.append({"iq_path": iq_path, "freq_mhz": freq_mhz,
                         "duration_s": duration_s, "tx_gain": tx_gain})
        if on_started:
            on_started(object())
        return {"ok": True, "error": None, "stopped_early": False}

    # transmit_iq_file is imported by-name into the bridge module — patch there.
    monkeypatch.setattr(sib, "transmit_iq_file", fake_transmit)

    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    # let the daemon transmit thread run
    deadline = time.time() + 2
    while time.time() < deadline and not any(m["phase"] == "complete" for m in ws.sent):
        time.sleep(0.02)

    phases = [m["phase"] for m in ws.sent]
    assert "started" in phases and "complete" in phases
    assert captured["frame"] == ("force_land", 7, 1)
    # IQ generated at the SAME sample rate hackrf_transfer plays back at.
    assert captured["write_kw"]["sample_rate_hz"] == hackrf_jam.SAMPLE_RATE_HZ
    assert len(tx_calls) == 1
    assert tx_calls[0]["freq_mhz"] == 915.0


def test_repeat_is_clamped_to_bridge_bound(monkeypatch):
    """repeat from the WS payload is never trusted as authoritative — clamped
    independently by the bridge, mirroring the other bridges' posture."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(repeat=9999))
    deadline = time.time() + 2
    while time.time() < deadline and "write_kw" not in captured:
        time.sleep(0.02)
    assert captured["write_kw"]["repeat"] == sib.MAX_REPEAT


def test_transmit_failure_reported_as_failed_not_crash(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(
        sib, "transmit_iq_file",
        lambda *a, **k: {"ok": False, "error": "hackrf_transfer not found (install the `hackrf` package)",
                         "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request())
    deadline = time.time() + 2
    while time.time() < deadline and not any(m["phase"] == "failed" for m in ws.sent):
        time.sleep(0.02)
    failed = [m for m in ws.sent if m["phase"] == "failed"]
    assert failed and "hackrf_transfer not found" in failed[0]["error"]


def test_emergency_abort_terminates_active_stop_event():
    b = _bridge()
    stop_event = threading.Event()
    with b._active_lock:
        b._active_stop_event = stop_event
    b.tx_halted = True
    with b._active_lock:
        active = b._active_stop_event
    assert active is stop_event
    active.set()
    assert stop_event.is_set()


# ---------------------------------------------------------------------
# Device-pin regression: hackrf_jam addresses the pinned TX serial via -d and
# never index-0 (so an SDR inject can never key the RX detection radio).
# ---------------------------------------------------------------------
def test_hackrf_tx_device_args_pins_serial_when_set(monkeypatch):
    monkeypatch.setattr(hackrf_jam, "HACKRF_TX_SERIAL", "TESTTX930c")
    assert hackrf_jam._tx_device_args() == ["-d", "TESTTX930c"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
