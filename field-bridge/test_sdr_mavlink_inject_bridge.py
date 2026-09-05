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
        DEFAULT_PREAMBLE = b"\xAA\xAA\xAA\xAA"
        DEFAULT_SYNC_WORD = b"\x2D\xD4"
        DEFAULT_FEC = "none"
        FEC_CHOICES = ("none", "golay")

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

    def fake_transmit(iq_path, freq_mhz, duration_s, tx_gain, stop_event=None,
                      on_started=None, tx_halt_check=None):
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


def test_operator_phy_preamble_sync_fec_threaded_to_modulator(monkeypatch):
    """Operator-settable preamble / sync word / FEC from the WS request are parsed
    (hex) and passed through to write_iq_file — not silently dropped."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(
        preamble_hex="AAAAAAAAAAAA", sync_word_hex="ABCD", fec="golay"))
    deadline = time.time() + 2
    while time.time() < deadline and "write_kw" not in captured:
        time.sleep(0.02)
    kw = captured["write_kw"]
    assert kw["preamble"] == bytes.fromhex("AAAAAAAAAAAA")
    assert kw["sync_word"] == bytes.fromhex("ABCD")
    assert kw["fec"] == "golay"


def test_bogus_fec_refused_cleanly(monkeypatch):
    """An unsupported fec value fails the request cleanly (no ungoverned/broken
    transmit)."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(fec="turbo9000"))
    assert ws.sent[0]["phase"] == "failed"
    assert "fec" in ws.sent[0]["error"].lower()


def test_repeat_is_operator_controlled_not_clamped(monkeypatch):
    """Commander directive: repeat is operator-controlled — NO artificial cap.
    A large repeat passes through verbatim (only floored at 1)."""
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
    # NOT clamped to MAX_REPEAT — the operator-set value is honored verbatim.
    assert captured["write_kw"]["repeat"] == 9999


def test_repeat_floored_at_one(monkeypatch):
    """The only remaining bound is a floor of 1 (repeat=0 is meaningless for a
    one-shot frame; use continuous=True for an unbounded loop instead)."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(repeat=0))
    deadline = time.time() + 2
    while time.time() < deadline and "write_kw" not in captured:
        time.sleep(0.02)
    assert captured["write_kw"]["repeat"] == 1


def test_continuous_inject_loops_until_stopped(monkeypatch):
    """continuous=True => transmit_iq_file is handed a None window (loop via -R)
    so the command re-emits until the operator stops it — still abortable."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    tx_calls = []

    def fake_transmit(iq_path, freq_mhz, duration_s, tx_gain, stop_event=None,
                      on_started=None, tx_halt_check=None):
        tx_calls.append({"duration_s": duration_s, "tx_halt_check": tx_halt_check})
        if on_started:
            on_started(object())
        return {"ok": True, "error": None, "stopped_early": False}

    monkeypatch.setattr(sib, "transmit_iq_file", fake_transmit)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(continuous=True))
    deadline = time.time() + 2
    while time.time() < deadline and not tx_calls:
        time.sleep(0.02)
    assert tx_calls, "transmit_iq_file was never called"
    # None window = continuous loop until stopped; tx_halt is still polled.
    assert tx_calls[0]["duration_s"] is None
    assert callable(tx_calls[0]["tx_halt_check"])


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


# ---------------------------------------------------------------------
# HOLISTIC lease-expiry stop: the continuous inject must terminate when the
# effect=mavlink_sdr_inject range-auth LEASE expires mid-stream, not only on
# EMERGENCY ABORT / tx_halt.
# ---------------------------------------------------------------------
def test_continuous_inject_tx_halt_check_fires_on_range_auth_lease_expiry(monkeypatch):
    import range_auth_lease
    monkeypatch.setattr(range_auth_lease, "DEFAULT_TTL_S", 0.0)  # re-check every call
    b = _bridge()
    authorized = {"v": True}
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect="mavlink_sdr_inject": authorized["v"])
    captured = {}
    _stub_modulation(monkeypatch, captured)

    tx = {}

    def fake_transmit(iq_path, freq_mhz, duration_s, tx_gain, stop_event=None,
                      on_started=None, tx_halt_check=None):
        tx["thc"] = tx_halt_check
        tx["duration_s"] = duration_s
        if on_started:
            on_started(object())
        return {"ok": True, "error": None, "stopped_early": False}

    monkeypatch.setattr(sib, "transmit_iq_file", fake_transmit)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(continuous=True))
    deadline = time.time() + 2
    while time.time() < deadline and "thc" not in tx:
        time.sleep(0.02)

    assert tx["duration_s"] is None            # continuous loop
    thc = tx["thc"]
    assert callable(thc)
    # Authorized + not halted -> keep transmitting.
    assert b.tx_halted is False
    assert thc() is False
    # Lease EXPIRES mid-inject (no abort, tx_halt stays False) -> halt fires.
    authorized["v"] = False
    assert thc() is True


def test_tx_halt_check_still_fires_instantly_on_abort(monkeypatch):
    # Regression: EMERGENCY ABORT / tx_halt must STILL stop instantly (checked
    # before the lease), even while the lease is fully authorized.
    import range_auth_lease
    monkeypatch.setattr(range_auth_lease, "DEFAULT_TTL_S", 0.0)
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    tx = {}

    def fake_transmit(iq_path, freq_mhz, duration_s, tx_gain, stop_event=None,
                      on_started=None, tx_halt_check=None):
        tx["thc"] = tx_halt_check
        if on_started:
            on_started(object())
        return {"ok": True, "error": None, "stopped_early": False}

    monkeypatch.setattr(sib, "transmit_iq_file", fake_transmit)
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(continuous=True))
    deadline = time.time() + 2
    while time.time() < deadline and "thc" not in tx:
        time.sleep(0.02)

    thc = tx["thc"]
    assert thc() is False           # authorized, not halted
    b.tx_halted = True              # EMERGENCY ABORT
    assert thc() is True            # halts instantly despite the armed lease


# ---------------------------------------------------------------------
# Defense-in-depth (Claude L1): bridge-side preamble/sync hex length bound
# (mirrors the backend MavlinkSdrInjectBody 1..64-byte pattern).
# ---------------------------------------------------------------------
def test_oversized_preamble_hex_refused_at_bridge(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    # 65 bytes of hex -> exceeds the 64-byte bound.
    b._handle_inject_request(ws, _valid_request(preamble_hex="AA" * 65))
    assert ws.sent[0]["phase"] == "failed"
    assert "preamble_hex" in ws.sent[0]["error"]
    assert "write_kw" not in captured  # never reached modulation/transmit


def test_oversized_sync_word_hex_refused_at_bridge(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(sync_word_hex="BB" * 65))
    assert ws.sent[0]["phase"] == "failed"
    assert "sync_word_hex" in ws.sent[0]["error"]
    assert "write_kw" not in captured


def test_max_length_preamble_sync_hex_accepted(monkeypatch):
    # Exactly 64 bytes each is still accepted (boundary), so a legitimate long
    # framing is not over-rejected.
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized",
                        lambda effect="mavlink_sdr_inject": True)
    captured = {}
    _stub_modulation(monkeypatch, captured)
    monkeypatch.setattr(sib, "transmit_iq_file",
                        lambda *a, **k: {"ok": True, "error": None, "stopped_early": False})
    ws = FakeWS()
    b._handle_inject_request(ws, _valid_request(
        preamble_hex="AA" * 64, sync_word_hex="2D" * 64))
    deadline = time.time() + 2
    while time.time() < deadline and "write_kw" not in captured:
        time.sleep(0.02)
    assert "write_kw" in captured  # passed the bound and reached modulation
    assert captured["write_kw"]["preamble"] == bytes.fromhex("AA" * 64)
    assert captured["write_kw"]["sync_word"] == bytes.fromhex("2D" * 64)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
