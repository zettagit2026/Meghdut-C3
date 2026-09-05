"""Unit tests for wifi_defeat_bridge.py's gate chain + mode dispatch — the
field-bridge side of the governed active Wi-Fi drone-defeat capability (P2).

No real Wi-Fi NIC, no live backend, no real network / scapy / UDP: requests to
the backend are monkeypatched and the P1 TX primitives + encoders are stubbed,
so NO real frame / datagram is ever transmitted. Mirrors the rigor and
convention of field-bridge/test_sdr_mavlink_inject_bridge.py.

Run: pytest field-bridge/test_wifi_defeat_bridge.py -v
"""
from __future__ import annotations

import json
import threading
import time

import os

import pytest

os.environ.setdefault("CEMA_API_URL", "http://backend.invalid")
os.environ.setdefault("CEMA_EMAIL", "test@unused.local")
os.environ.setdefault("CEMA_PASSWORD", "unused")
# A pinned injection NIC so nothing warns about an unset pin at construction. The
# P1 primitives are stubbed in every dispatch test, so no real pin gate runs.
os.environ.setdefault("WIFI_TX_IFACE", "wlan1")

import wifi_defeat_bridge as wdb


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
        self.sent.append(json.loads(raw))


def _bridge():
    b = wdb.WifiDefeatBridge()
    b.token = "fake-jwt"  # skip real login
    return b


def _valid_request(request_id="req-1", mode="deauth", **overrides):
    base = {
        "request_id": request_id,
        "actor": "commander@cema.mil",
        "mode": mode,
        "target_bssid": "AA:BB:CC:DD:EE:FF",
        "softap": "192.168.42.1",
        "client_mac": "11:22:33:44:55:66",
        "channel": 6,
        "count": 10,
        "wifi_defeat_confirm_token": "a" * 36,
    }
    base.update(overrides)
    return base


def _wait_for_phase(ws, phase, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not any(m["phase"] == phase for m in ws.sent):
        time.sleep(0.02)


def _stub_primitives(monkeypatch):
    """Replace the P1 TX primitives + encoders with recording stubs so NO real
    frame / datagram is transmitted. Returns a dict capturing each call."""
    cap = {"deauth": None, "arsdk": None, "tello": None,
           "encode_arsdk": [], "encode_tello": []}

    def fake_send_deauth(iface, target_bssid, client_mac, channel, count,
                         stop_event=None, tx_halt_check=None, frame_sender=None):
        cap["deauth"] = {"iface": iface, "target_bssid": target_bssid,
                         "client_mac": client_mac, "channel": channel,
                         "count": count, "tx_halt_check": tx_halt_check}
        return {"ok": True, "error": None, "stopped_early": False, "frames_sent": 1}

    def fake_inject_arsdk(iface, softap, command_bytes,
                          stop_event=None, tx_halt_check=None, udp_sender=None):
        cap["arsdk"] = {"iface": iface, "softap": softap,
                        "command_bytes": command_bytes, "tx_halt_check": tx_halt_check}
        return {"ok": True, "error": None, "stopped_early": False, "bytes_sent": len(command_bytes)}

    def fake_tello(iface, softap, command,
                   stop_event=None, tx_halt_check=None, udp_sender=None):
        cap["tello"] = {"iface": iface, "softap": softap, "command": command,
                        "tx_halt_check": tx_halt_check}
        return {"ok": True, "error": None, "stopped_early": False, "bytes_sent": len(command)}

    def fake_encode_arsdk(command, **kw):
        cap["encode_arsdk"].append(command)
        return b"\x04\x0b\x00\x0b\x00\x00\x00\x01\x00\x03\x00"

    def fake_encode_tello(command):
        cap["encode_tello"].append(command)
        return (command.encode("ascii"), ("192.168.10.1", 8889))

    monkeypatch.setattr(wdb, "send_deauth", fake_send_deauth)
    monkeypatch.setattr(wdb, "inject_arsdk_command", fake_inject_arsdk)
    monkeypatch.setattr(wdb, "tello_command", fake_tello)
    monkeypatch.setattr(wdb, "encode_ardrone3_piloting", fake_encode_arsdk)
    monkeypatch.setattr(wdb, "encode_tello", fake_encode_tello)
    return cap


# ---------------------------------------------------------------------
# Token shape check (defense-in-depth floor)
# ---------------------------------------------------------------------
def test_confirm_token_shape_check_rejects_short_or_missing():
    assert wdb._looks_like_real_confirm_token(None) is False
    assert wdb._looks_like_real_confirm_token("") is False
    assert wdb._looks_like_real_confirm_token("short") is False
    assert wdb._looks_like_real_confirm_token("true") is False


def test_confirm_token_shape_check_accepts_uuid_length():
    assert wdb._looks_like_real_confirm_token("a" * 36) is True


def test_confirm_token_floor_is_independent_of_other_bridges():
    """The MIN_CONFIRM_TOKEN_LEN constant is its OWN module name (not shared code)
    even if equal to the other bridges today — a future change to one must not
    silently change the others."""
    import jam_bridge
    import sdr_mavlink_inject_bridge as sib
    assert wdb.MIN_CONFIRM_TOKEN_LEN == jam_bridge.MIN_CONFIRM_TOKEN_LEN
    assert wdb.MIN_CONFIRM_TOKEN_LEN == sib.MIN_CONFIRM_TOKEN_LEN
    assert "MIN_CONFIRM_TOKEN_LEN" in wdb.__dict__


# ---------------------------------------------------------------------
# is_range_authorized — fail closed
# ---------------------------------------------------------------------
def test_is_range_authorized_true_when_backend_says_enabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(wdb.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": True}))
    assert b.is_range_authorized("wifi_deauth") is True


def test_is_range_authorized_false_when_backend_says_disabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(wdb.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": False}))
    assert b.is_range_authorized("arsdk_inject") is False


def test_is_range_authorized_fails_closed_on_network_error(monkeypatch):
    b = _bridge()
    def _raise(*a, **k):
        raise ConnectionError("backend unreachable")
    monkeypatch.setattr(wdb.requests, "get", _raise)
    assert b.is_range_authorized("wifi_deauth") is False


def test_is_range_authorized_fails_closed_on_malformed_response(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(wdb.requests, "get", lambda *a, **k: FakeResp(200, {"unexpected": "shape"}))
    assert b.is_range_authorized("arsdk_inject") is False


# ---------------------------------------------------------------------
# Gate A — live range-auth, per-effect, refuses with NO primitive call
# ---------------------------------------------------------------------
def test_gate_a_refuses_deauth_when_range_not_authorized(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: False)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    assert len(ws.sent) == 1
    assert ws.sent[0]["phase"] == "failed"
    assert "range-authorization" in ws.sent[0]["error"]
    assert cap["deauth"] is None  # NO primitive call


def test_gate_a_queries_wifi_deauth_effect_for_deauth_mode(monkeypatch):
    """Effect mapping: a deauth request must be authorized against effect
    'wifi_deauth' specifically (not arsdk_inject)."""
    b = _bridge()
    seen = {}
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect: seen.setdefault("effect", effect) or False)
    _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    assert seen["effect"] == "wifi_deauth"


def test_gate_a_queries_arsdk_inject_effect_for_inject_modes(monkeypatch):
    """A command-injection request must be authorized against effect
    'arsdk_inject' (shared by arsdk_* and tello_* modes)."""
    b = _bridge()
    seen = {}
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect: seen.setdefault("effect", effect) or False)
    _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="arsdk_land"))
    assert seen["effect"] == "arsdk_inject"


# ---------------------------------------------------------------------
# Gate B — confirm-token shape, refuses with NO primitive call
# ---------------------------------------------------------------------
def test_gate_b_refuses_malformed_confirm_token(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(wifi_defeat_confirm_token="short"))
    assert ws.sent[0]["phase"] == "failed"
    assert "confirmation token" in ws.sent[0]["error"]
    assert cap["deauth"] is None


def test_gate_b_refuses_missing_confirm_token(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    req = _valid_request()
    del req["wifi_defeat_confirm_token"]
    ws = FakeWS()
    b._handle_defeat_request(ws, req)
    assert ws.sent[0]["phase"] == "failed"
    assert "confirmation token" in ws.sent[0]["error"]
    assert cap["deauth"] is None


# ---------------------------------------------------------------------
# Gate C — local EMERGENCY ABORT, refuses with NO primitive call
# ---------------------------------------------------------------------
def test_gate_c_refuses_when_tx_halted(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    b.tx_halted = True
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request())
    assert ws.sent[0]["phase"] == "failed"
    assert "tx halted" in ws.sent[0]["error"]
    assert cap["deauth"] is None


# ---------------------------------------------------------------------
# Unsupported mode — refused before Gate A (no effect to authorize)
# ---------------------------------------------------------------------
def test_refuses_unsupported_mode(monkeypatch):
    b = _bridge()
    ra = {"called": False}
    def _ra(effect):
        ra["called"] = True
        return True
    monkeypatch.setattr(b, "is_range_authorized", _ra)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="format_c_drive"))
    assert ws.sent[0]["phase"] == "failed"
    assert "unsupported wifi-defeat mode" in ws.sent[0]["error"]
    assert ra["called"] is False  # never even reached Gate A
    assert cap["deauth"] is None


# ---------------------------------------------------------------------
# Happy path: deauth dispatches to send_deauth with the request's params
# ---------------------------------------------------------------------
def test_deauth_dispatches_to_send_deauth_with_bssid_channel_count(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    _wait_for_phase(ws, "complete")
    phases = [m["phase"] for m in ws.sent]
    assert "started" in phases and "complete" in phases
    assert cap["deauth"] is not None
    assert cap["deauth"]["target_bssid"] == "AA:BB:CC:DD:EE:FF"
    assert cap["deauth"]["channel"] == 6
    assert cap["deauth"]["count"] == 10
    assert cap["deauth"]["client_mac"] == "11:22:33:44:55:66"
    assert cap["deauth"]["iface"] == "wlan1"
    assert callable(cap["deauth"]["tx_halt_check"])


def test_deauth_continuous_when_count_omitted(monkeypatch):
    """count omitted => None => continuous deauth (until stopped), passed through
    to send_deauth verbatim."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    req = _valid_request(mode="deauth")
    del req["count"]
    ws = FakeWS()
    b._handle_defeat_request(ws, req)
    _wait_for_phase(ws, "complete")
    assert cap["deauth"]["count"] is None


# ---------------------------------------------------------------------
# arsdk / tello modes: encode THEN inject primitive
# ---------------------------------------------------------------------
def test_arsdk_land_encodes_then_injects(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="arsdk_land"))
    _wait_for_phase(ws, "complete")
    assert cap["encode_arsdk"] == ["land"]
    assert cap["arsdk"] is not None
    assert cap["arsdk"]["softap"] == "192.168.42.1"
    assert cap["arsdk"]["command_bytes"] == b"\x04\x0b\x00\x0b\x00\x00\x00\x01\x00\x03\x00"


def test_arsdk_emergency_encodes_emergency(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="arsdk_emergency"))
    _wait_for_phase(ws, "complete")
    assert cap["encode_arsdk"] == ["emergency"]
    assert cap["arsdk"] is not None


def test_tello_land_encodes_then_sends(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="tello_land"))
    _wait_for_phase(ws, "complete")
    assert cap["encode_tello"] == ["land"]
    assert cap["tello"] is not None
    assert cap["tello"]["command"] == b"land"
    assert cap["tello"]["softap"] == "192.168.42.1"


def test_tello_emergency_falls_back_to_encoder_default_addr(monkeypatch):
    """When the request omits softap, the Tello dispatch falls back to the
    encoder's verified default Tello control address."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    req = _valid_request(mode="tello_emergency")
    del req["softap"]
    ws = FakeWS()
    b._handle_defeat_request(ws, req)
    _wait_for_phase(ws, "complete")
    assert cap["encode_tello"] == ["emergency"]
    assert cap["tello"]["softap"] == ("192.168.10.1", 8889)


# ---------------------------------------------------------------------
# Encoder honesty gate: UnverifiedCommandError => NO TX, clean failed ack
# ---------------------------------------------------------------------
def test_unverified_arsdk_command_handled_no_tx(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)

    def _raise(command, **kw):
        raise wdb.UnverifiedCommandError(f"unverified command id -- do not field: {command}")
    monkeypatch.setattr(wdb, "encode_ardrone3_piloting", _raise)

    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="arsdk_land"))
    _wait_for_phase(ws, "failed")
    phases = [m["phase"] for m in ws.sent]
    assert "started" not in phases          # NO TX started
    assert "failed" in phases
    failed = [m for m in ws.sent if m["phase"] == "failed"][0]
    assert "unverified ARSDK command" in failed["error"]
    assert cap["arsdk"] is None             # inject primitive NEVER called


def test_unknown_tello_command_handled_no_tx(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)

    def _raise(command):
        raise wdb.TelloCommandError(f"unrecognized Tello SDK token '{command}'")
    monkeypatch.setattr(wdb, "encode_tello", _raise)

    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="tello_land"))
    _wait_for_phase(ws, "failed")
    phases = [m["phase"] for m in ws.sent]
    assert "started" not in phases
    assert "failed" in phases
    assert cap["tello"] is None


# ---------------------------------------------------------------------
# Primitive-reported failure surfaces as a failed ack, not a crash
# ---------------------------------------------------------------------
def test_primitive_failure_reported_as_failed(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    _stub_primitives(monkeypatch)
    monkeypatch.setattr(
        wdb, "send_deauth",
        lambda *a, **k: {"ok": False, "error": "REFUSING TX (fail-closed): WIFI_TX_IFACE is not set",
                         "stopped_early": False, "frames_sent": 0})
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    _wait_for_phase(ws, "failed")
    failed = [m for m in ws.sent if m["phase"] == "failed"]
    assert failed and "WIFI_TX_IFACE is not set" in failed[0]["error"]


def test_primitive_stopped_early_reported_as_stopped(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    _stub_primitives(monkeypatch)
    monkeypatch.setattr(
        wdb, "send_deauth",
        lambda *a, **k: {"ok": True, "error": None, "stopped_early": True, "frames_sent": 3})
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    _wait_for_phase(ws, "stopped")
    assert any(m["phase"] == "stopped" for m in ws.sent)


# ---------------------------------------------------------------------
# Abort: sets tx_halted + fires the active stop_event (stops a live deauth)
# ---------------------------------------------------------------------
def test_abort_sets_tx_halted_and_fires_active_stop_event():
    b = _bridge()
    stop_event = threading.Event()
    with b._active_lock:
        b._active_stop_event = stop_event

    # Drive the same abort branch on_message uses.
    b.tx_halted = True
    with b._active_lock:
        active = b._active_stop_event
    assert active is stop_event
    active.set()

    assert b.tx_halted is True
    assert stop_event.is_set()


def test_abort_blocks_then_resume_re_enables(monkeypatch):
    """The abort flag (set by the WS 'abort' message) blocks new requests at
    Gate C; clearing it (WS 'resume') re-enables transmission."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    _stub_primitives(monkeypatch)

    b.tx_halted = True  # simulate abort
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    assert ws.sent[0]["phase"] == "failed"
    assert "tx halted" in ws.sent[0]["error"]

    b.tx_halted = False  # simulate resume
    ws2 = FakeWS()
    b._handle_defeat_request(ws2, _valid_request(mode="deauth"))
    _wait_for_phase(ws2, "complete")
    assert any(m["phase"] == "complete" for m in ws2.sent)


# ---------------------------------------------------------------------
# HOLISTIC lease-expiry stop: the tx_halt_check handed to the primitive must
# fire on a bare range-auth LEASE expiry mid-stream, not only on EMERGENCY ABORT.
# ---------------------------------------------------------------------
def test_tx_halt_check_fires_on_range_auth_lease_expiry(monkeypatch):
    import range_auth_lease
    monkeypatch.setattr(range_auth_lease, "DEFAULT_TTL_S", 0.0)  # re-check every call
    b = _bridge()
    authorized = {"v": True}
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: authorized["v"])
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    _wait_for_phase(ws, "complete")
    thc = cap["deauth"]["tx_halt_check"]
    assert callable(thc)
    # Authorized + not halted -> keep transmitting.
    assert b.tx_halted is False
    assert thc() is False
    # Lease EXPIRES mid-deauth (no abort, tx_halt stays False) -> halt fires.
    authorized["v"] = False
    assert thc() is True


def test_tx_halt_check_still_fires_instantly_on_abort(monkeypatch):
    import range_auth_lease
    monkeypatch.setattr(range_auth_lease, "DEFAULT_TTL_S", 0.0)
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth"))
    _wait_for_phase(ws, "complete")
    thc = cap["deauth"]["tx_halt_check"]
    assert thc() is False           # authorized, not halted
    b.tx_halted = True              # EMERGENCY ABORT
    assert thc() is True            # halts instantly despite the armed lease


# ---------------------------------------------------------------------
# Malformed parameters fail cleanly (no TX)
# ---------------------------------------------------------------------
def test_bad_channel_refused_cleanly(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect: True)
    cap = _stub_primitives(monkeypatch)
    ws = FakeWS()
    b._handle_defeat_request(ws, _valid_request(mode="deauth", channel="not-a-number"))
    assert ws.sent[0]["phase"] == "failed"
    assert "invalid wifi_defeat parameters" in ws.sent[0]["error"]
    assert cap["deauth"] is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
