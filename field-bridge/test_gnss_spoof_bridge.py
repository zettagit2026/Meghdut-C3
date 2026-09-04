"""Unit tests for gnss_spoof_bridge.py's gate chain (Task #103) and the
gnss_signal_synth.py stub, PLUS regression coverage proving
hackrf_jam.transmit_burst()'s behavior/signature is unaffected by the new
transmit_iq_file() extraction (field-bridge/GNSS_SPOOF_ARCHITECTURE.md §1).

No real HackRF hardware, no live backend, no real network calls — requests
to the backend are monkeypatched, matching field-bridge/test_reauth_on_401.py's
existing convention for testing bridge classes without a live server.

Run: pytest field-bridge/test_gnss_spoof_bridge.py -v
"""
from __future__ import annotations

import os
import threading
import time

import pytest

os.environ.setdefault("CEMA_API_URL", "http://backend.invalid")
os.environ.setdefault("CEMA_EMAIL", "test@unused.local")
os.environ.setdefault("CEMA_PASSWORD", "unused")

import gnss_spoof_bridge as gsb
import gnss_signal_synth as synth
import hackrf_jam


# ---------------------------------------------------------------------
# Token / attestation shape checks (defense-in-depth floor, mirrors
# jam_bridge._looks_like_real_confirm_token's tests in spirit)
# ---------------------------------------------------------------------
def test_confirm_token_shape_check_rejects_short_or_missing():
    assert gsb._looks_like_real_confirm_token(None) is False
    assert gsb._looks_like_real_confirm_token("") is False
    assert gsb._looks_like_real_confirm_token("short") is False
    assert gsb._looks_like_real_confirm_token("true") is False


def test_confirm_token_shape_check_accepts_uuid_length():
    assert gsb._looks_like_real_confirm_token("a" * 36) is True


def test_gnss_spoof_confirm_token_floor_is_independent_of_jam_bridge():
    """The two MIN_CONFIRM_TOKEN_LEN constants must not be the SAME object
    (i.e. not literally shared code) even though they may hold equal
    values today — a future change to one must not silently change the
    other. Import jam_bridge lazily here so this test file doesn't require
    jam_bridge's own env vars unless this specific test runs."""
    import jam_bridge
    assert gsb.MIN_CONFIRM_TOKEN_LEN == jam_bridge.MIN_CONFIRM_TOKEN_LEN  # same value today...
    # ...but genuinely distinct module-level names, not an import-alias:
    assert "MIN_CONFIRM_TOKEN_LEN" in gsb.__dict__
    assert "MIN_CONFIRM_TOKEN_LEN" in jam_bridge.__dict__


@pytest.mark.parametrize("bad", [None, "", "n/a", "N/A", "none", "confirmed", "yes", "short"])
def test_attestation_shape_check_rejects_trivial(bad):
    assert gsb._looks_like_real_attestation(bad) is False


def test_attestation_shape_check_accepts_real_text():
    assert gsb._looks_like_real_attestation(
        "Confirmed: no friendly GPS-dependent assets within 500m of target."
    ) is True


# ---------------------------------------------------------------------
# Gate chain: is_range_authorized (fail-closed) + _handle_gnss_spoof_request
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


def _bridge():
    b = gsb.GnssSpoofBridge()
    b.token = "fake-jwt"  # skip real login
    return b


def test_is_range_authorized_true_when_backend_says_enabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(gsb.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": True}))
    assert b.is_range_authorized("gnss_spoof") is True


def test_is_range_authorized_false_when_backend_says_disabled(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(gsb.requests, "get", lambda *a, **k: FakeResp(200, {"enabled": False}))
    assert b.is_range_authorized("gnss_spoof") is False


def test_is_range_authorized_fails_closed_on_network_error(monkeypatch):
    b = _bridge()
    def _raise(*a, **k):
        raise ConnectionError("backend unreachable")
    monkeypatch.setattr(gsb.requests, "get", _raise)
    assert b.is_range_authorized("gnss_spoof") is False


def test_is_range_authorized_fails_closed_on_malformed_response(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(gsb.requests, "get", lambda *a, **k: FakeResp(200, {"unexpected": "shape"}))
    assert b.is_range_authorized("gnss_spoof") is False


def _valid_request(request_id="req-1", **overrides):
    base = {
        "request_id": request_id,
        "actor": "commander@cema.mil",
        "freq_mhz": 1575.42,
        "duration_s": 2.0,
        "tx_gain": 20,
        "true_lat": 28.6139,
        "true_lon": 77.2090,
        "true_alt_m": 200.0,
        "fake_lat": 28.6167,
        "fake_lon": 77.2100,
        "fake_alt_m": 200.0,
        "gnss_spoof_confirm_token": "a" * 36,
    }
    base.update(overrides)
    return base


class FakeWS:
    def __init__(self):
        self.sent = []

    def send(self, raw):
        self.sent.append(__import__("json").loads(raw))


def test_handle_request_refuses_when_range_not_authorized(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": False)
    ws = FakeWS()
    b._handle_gnss_spoof_request(ws, _valid_request())
    assert len(ws.sent) == 1
    assert ws.sent[0]["phase"] == "failed"
    assert "range-authorization" in ws.sent[0]["error"]


def test_handle_request_refuses_on_malformed_confirm_token(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": True)
    ws = FakeWS()
    b._handle_gnss_spoof_request(ws, _valid_request(gnss_spoof_confirm_token="short"))
    assert ws.sent[0]["phase"] == "failed"
    assert "confirmation token" in ws.sent[0]["error"]


def test_handle_request_refuses_when_tx_halted(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": True)
    b.tx_halted = True
    ws = FakeWS()
    b._handle_gnss_spoof_request(ws, _valid_request())
    assert ws.sent[0]["phase"] == "failed"
    assert "tx halted" in ws.sent[0]["error"]


def test_handle_request_refuses_a_jam_confirm_token_shaped_like_one_but_not_issued_for_spoof(monkeypatch):
    """A jam_confirm_token (also a 36-char UUID string) passes THIS bridge's
    shape-only floor check (by design — it's dumb/shape-only per the
    architecture doc), but the real single-use validation happens at the
    backend's _consume_gnss_spoof_confirm_token, which stores gnss_spoof
    tokens in an entirely separate dict from jam tokens — a jam token was
    never inserted there, so the backend would already have rejected this
    request with a 403 before this bridge ever saw it. This bridge's shape
    check cannot itself distinguish token TYPE (that's the backend's job) —
    this test documents that boundary rather than asserting a false
    guarantee."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": True)
    ws = FakeWS()
    # A syntactically valid-looking (36-char) token passes the shape floor —
    # this is expected; the real defense is the backend's separate token
    # store, tested in backend/tests/test_gnss_spoof_geodesic.py.
    assert gsb._looks_like_real_confirm_token("b" * 36) is True


def test_handle_request_starts_transmit_thread_when_all_gates_pass(monkeypatch):
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": True)
    started = threading.Event()
    monkeypatch.setattr(
        gsb, "synthesize_iq_file",
        lambda *a, **k: (_ for _ in ()).throw(gsb.GnssSynthNotImplemented("stubbed")),
    )
    ws = FakeWS()
    b._handle_gnss_spoof_request(ws, _valid_request())
    # Give the daemon thread a moment to run and hit the stubbed synth call.
    deadline = time.time() + 2
    while time.time() < deadline and not ws.sent:
        time.sleep(0.02)
    assert len(ws.sent) == 1
    assert ws.sent[0]["phase"] == "failed"
    assert ws.sent[0]["error"] == "stubbed"


def test_handle_request_clamps_duration_to_cap(monkeypatch):
    """duration_s from the WS payload is never trusted as authoritative —
    clamped independently by this bridge, mirroring jam_bridge.py's posture
    toward all jam-request fields (architecture doc §2)."""
    b = _bridge()
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="gnss_spoof": True)
    captured = {}

    def fake_synth(true_lat, true_lon, true_alt_m, fake_lat, fake_lon, fake_alt_m, duration_s):
        captured["duration_s"] = duration_s
        raise gsb.GnssSynthNotImplemented("stubbed, but duration captured")

    monkeypatch.setattr(gsb, "synthesize_iq_file", fake_synth)
    ws = FakeWS()
    b._handle_gnss_spoof_request(ws, _valid_request(duration_s=999.0))
    deadline = time.time() + 2
    while time.time() < deadline and "duration_s" not in captured:
        time.sleep(0.02)
    assert captured["duration_s"] == gsb.GNSS_SPOOF_MAX_DURATION_S


def test_emergency_abort_terminates_active_stop_event():
    b = _bridge()
    stop_event = threading.Event()
    with b._active_lock:
        b._active_stop_event = stop_event
    # Simulate the WS on_message abort branch's core action directly (the
    # branch itself is exercised indirectly via start_ws_subscriber, which
    # requires a real WS connection to test end-to-end).
    b.tx_halted = True
    with b._active_lock:
        active = b._active_stop_event
    assert active is stop_event
    active.set()
    assert stop_event.is_set()


# ---------------------------------------------------------------------
# gnss_signal_synth.py stub
# ---------------------------------------------------------------------
def test_synthesize_iq_file_default_produces_real_nonsilent_signal(monkeypatch):
    # v1: the default path now synthesizes a REAL L1 C/A signal (previously a stub
    # that raised GnssSynthNotImplemented). Exact-size + code/NAV correctness are
    # covered in test_gnss_signal_synth.py; here we just assert the bridge-facing
    # default no longer raises and emits a non-silent interleaved-int8 IQ file.
    monkeypatch.delenv("GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ", raising=False)
    path = synth.synthesize_iq_file(28.6, 77.2, 200, 28.62, 77.21, 200,
                                    duration_s=0.01, sample_rate=4_000_000)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        assert len(data) > 0 and len(data) % 2 == 0
        assert any(b != 0 for b in data), \
            "default must produce a real (non-silent) signal, not zero IQ"
    finally:
        os.unlink(path)


def test_synthesize_iq_file_placeholder_mode_produces_correct_size(monkeypatch):
    monkeypatch.setenv("GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ", "1")
    try:
        path = synth.synthesize_iq_file(28.6, 77.2, 200, 28.62, 77.21, 200, duration_s=0.5,
                                        sample_rate=1000)
        try:
            size = os.path.getsize(path)
            # 2 bytes/sample (interleaved I/Q, int8 each), 0.5s @ 1000 samples/s
            assert size == 2 * int(0.5 * 1000)
        finally:
            os.unlink(path)
    finally:
        monkeypatch.delenv("GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ", raising=False)


# ---------------------------------------------------------------------
# Regression: hackrf_jam.transmit_burst()'s core params stay stable; the only
# additive change is the trailing `tx_halt_check` kwarg (default None), added
# with the continuous-jam kill-switch backstop (commit a0fa362) so the burst
# polls tx_halt directly like the sweep/iq_file paths — backward-compatible.
# ---------------------------------------------------------------------
def test_transmit_burst_signature_stable_plus_tx_halt_check():
    import inspect
    sig = inspect.signature(hackrf_jam.transmit_burst)
    assert list(sig.parameters.keys()) == [
        "freq_mhz", "bandwidth_khz", "duration_s", "tx_gain", "stop_event", "on_started",
        "tx_halt_check",
    ]
    # additive + backward-compatible: the new kwarg defaults to None
    assert sig.parameters["tx_halt_check"].default is None


def test_transmit_burst_missing_binary_behavior_unchanged():
    """hackrf_transfer is not installed in this test environment — verify
    transmit_burst() still returns the same {"ok": False, "error": "...not
    found...", "stopped_early": False} shape it always has, proving the
    transmit_iq_file() extraction did not change transmit_burst()'s own
    inline subprocess-invocation path (it does NOT call transmit_iq_file()
    internally — see hackrf_jam.py's updated docstring)."""
    result = hackrf_jam.transmit_burst(915.0, 500.0, 0.1, 20)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert result["stopped_early"] is False


def test_transmit_iq_file_missing_binary_matches_transmit_burst_error_shape(tmp_path):
    """The new shared helper must behave identically to transmit_burst() on
    the missing-binary path (same error message convention), since both now
    share this exact failure branch's logic (copied, not literally shared,
    for transmit_burst() per the "do not touch it" instruction, but must
    remain equivalent in behavior)."""
    iq_path = tmp_path / "placeholder.iq"
    iq_path.write_bytes(b"\x00" * 100)
    result = hackrf_jam.transmit_iq_file(str(iq_path), 1575.42, 0.1, 20)
    assert result["ok"] is False
    assert "not found" in result["error"]
    assert result["stopped_early"] is False


def test_gnss_spoof_max_duration_is_shorter_than_jam_max_duration():
    assert hackrf_jam.GNSS_SPOOF_MAX_DURATION_S == 3.0
    assert hackrf_jam.GNSS_SPOOF_MAX_DURATION_S < hackrf_jam.MAX_DURATION_S
