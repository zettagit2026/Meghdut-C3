"""Unit tests for field-bridge/operator_jam_bridge.py — the OperatorJamBridge
subclass that runs the operator's jammer through JamBridge's governed spine.

Verifies:
  * mode routing — the meghdut and operator bridges each act ONLY on their own
    jam_mode, so they never double-fire on the shared WS channel;
  * _do_transmit routing — OperatorJamBridge routes to the operator wrapper
    (run_operator_jam) with the pinned serial + abort wiring, while the base
    JamBridge still routes to hackrf_jam.transmit_burst;
  * GNU-Radio-missing — an operator transmit fails cleanly ("Operator mode
    unavailable: ...") instead of crashing or falling through to a transmit.

No sockets/HTTP: only __init__ config + the pure routing methods are exercised.
Run: pytest field-bridge/test_operator_jam_bridge.py -v
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

TX_SERIAL = "0000000000000000930c"


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch):
    # JamBridge.__init__ requires these (via _cfg); no network is touched.
    monkeypatch.setenv("CEMA_API_URL", "http://localhost:8001")
    monkeypatch.setenv("CEMA_EMAIL", "test@unused.local")
    monkeypatch.setenv("CEMA_PASSWORD", "unused")
    monkeypatch.setenv("HACKRF_TX_SERIAL", TX_SERIAL)


def _make_bridges():
    import jam_bridge
    import operator_jam_bridge
    return jam_bridge.JamBridge(), operator_jam_bridge.OperatorJamBridge()


def test_mode_routing_no_double_fire():
    meghdut, operator = _make_bridges()
    # A request with no jam_mode is legacy "meghdut".
    assert meghdut._handles_mode({}) is True
    assert operator._handles_mode({}) is False
    # Explicit meghdut.
    assert meghdut._handles_mode({"jam_mode": "meghdut"}) is True
    assert operator._handles_mode({"jam_mode": "meghdut"}) is False
    # Explicit operator.
    assert meghdut._handles_mode({"jam_mode": "operator"}) is False
    assert operator._handles_mode({"jam_mode": "operator"}) is True


def test_class_jam_mode_labels():
    meghdut, operator = _make_bridges()
    assert meghdut.JAM_MODE == "meghdut"
    assert operator.JAM_MODE == "operator"


def test_operator_do_transmit_routes_to_wrapper(monkeypatch):
    import operator_jam_bridge
    _, operator = _make_bridges()

    captured = {}

    def fake_run(band, serial, duration_s, **kwargs):
        captured["band"] = band
        captured["serial"] = serial
        captured["duration_s"] = duration_s
        captured["abort_event"] = kwargs.get("abort_event")
        captured["tx_halt_check"] = kwargs.get("tx_halt_check")
        captured["on_started"] = kwargs.get("on_started")
        return {"ok": True, "stopped_early": False, "error": None}

    monkeypatch.setattr(operator_jam_bridge, "run_operator_jam", fake_run)

    stop_event = threading.Event()
    sentinel_started = object()
    params = {"band": "2g4", "freq_mhz": 2450.0, "bandwidth_khz": 500.0,
              "duration_s": 5.0, "tx_gain": 20, "request_id": "r1", "actor": "a"}
    result = operator._do_transmit(params, stop_event, sentinel_started)

    assert result == {"ok": True, "stopped_early": False, "error": None}
    assert captured["band"] == "2g4"
    assert captured["serial"] == TX_SERIAL  # the pinned TX serial, not index-0
    assert captured["duration_s"] == 5.0
    assert captured["abort_event"] is stop_event  # abort wiring preserved
    assert captured["on_started"] is sentinel_started
    # tx_halt_check reads the live bridge flag so a mid-burst abort stops TX.
    operator.tx_halted = True
    assert captured["tx_halt_check"]() is True


def test_operator_do_transmit_rejects_unsupported_band():
    _, operator = _make_bridges()
    params = {"band": "gps_l1", "freq_mhz": 1575.42, "bandwidth_khz": 500.0,
              "duration_s": 2.0, "tx_gain": 20, "request_id": "r2", "actor": "a"}
    result = operator._do_transmit(params, threading.Event(), lambda _p: None)
    assert result["ok"] is False
    assert "unsupported band" in result["error"]


def test_operator_do_transmit_fails_cleanly_without_gnuradio():
    # Real run_operator_jam + real (missing) GNU Radio: must be a clean failure,
    # never a crash, never a transmit.
    _, operator = _make_bridges()
    params = {"band": "915", "freq_mhz": 915.0, "bandwidth_khz": 500.0,
              "duration_s": 2.0, "tx_gain": 20, "request_id": "r3", "actor": "a"}
    result = operator._do_transmit(params, threading.Event(), lambda _p: None)
    assert result["ok"] is False
    assert result["error"].startswith("Operator mode unavailable:")


def test_base_bridge_do_transmit_uses_transmit_burst(monkeypatch):
    import jam_bridge
    meghdut, _ = _make_bridges()

    called = {}

    def fake_burst(freq, bw, dur, gain, **kwargs):
        called["args"] = (freq, bw, dur, gain)
        called["stop_event"] = kwargs.get("stop_event")
        return {"ok": True, "stopped_early": False, "error": None}

    monkeypatch.setattr(jam_bridge, "transmit_burst", fake_burst)
    stop_event = threading.Event()
    params = {"band": "915", "freq_mhz": 915.0, "bandwidth_khz": 500.0,
              "duration_s": 5.0, "tx_gain": 20, "sweep": False, "request_id": "r4", "actor": "a"}
    result = meghdut._do_transmit(params, stop_event, lambda _p: None)
    assert result["ok"] is True
    assert called["args"] == (915.0, 500.0, 5.0, 20)
    assert called["stop_event"] is stop_event


def test_base_bridge_do_transmit_continuous_passes_none_duration(monkeypatch):
    # Commander directive: a continuous jam is routed to transmit_burst with
    # duration_s=None (run until stopped), NOT a capped value.
    import jam_bridge
    meghdut, _ = _make_bridges()
    called = {}

    def fake_burst(freq, bw, dur, gain, **kwargs):
        called["dur"] = dur
        return {"ok": True, "stopped_early": False, "error": None}

    monkeypatch.setattr(jam_bridge, "transmit_burst", fake_burst)
    params = {"band": "915", "freq_mhz": 915.0, "bandwidth_khz": 500.0,
              "duration_s": None, "tx_gain": 20, "sweep": False, "request_id": "r5", "actor": "a"}
    meghdut._do_transmit(params, threading.Event(), lambda _p: None)
    assert called["dur"] is None


def test_base_bridge_do_transmit_routes_sweep_to_transmit_sweep(monkeypatch):
    # sweep=True routes to transmit_sweep with the band edges + tx_halt wiring,
    # so a MEGHDUT swept barrage covers the full hop band.
    import jam_bridge
    meghdut, _ = _make_bridges()
    seen = {}

    def fake_sweep(start, stop, bw, gain, **kwargs):
        seen["start"] = start
        seen["stop"] = stop
        seen["duration_s"] = kwargs.get("duration_s")
        seen["tx_halt_check"] = kwargs.get("tx_halt_check")
        seen["stop_event"] = kwargs.get("stop_event")
        return {"ok": True, "stopped_early": False, "error": None}

    monkeypatch.setattr(jam_bridge, "transmit_sweep", fake_sweep)
    stop_event = threading.Event()
    params = {"band": None, "freq_mhz": None, "bandwidth_khz": 500.0,
              "duration_s": None, "tx_gain": 47, "sweep": True,
              "freq_start_mhz": 2400.0, "freq_stop_mhz": 2483.5,
              "step_mhz": 20.0, "dwell_ms": 5.0, "request_id": "r6", "actor": "a"}
    result = meghdut._do_transmit(params, stop_event, lambda _p: None)
    assert result["ok"] is True
    assert seen["start"] == 2400.0 and seen["stop"] == 2483.5
    assert seen["duration_s"] is None  # continuous sweep until stopped
    assert seen["stop_event"] is stop_event
    # tx_halt is polled so EMERGENCY ABORT stops an in-progress sweep.
    meghdut.tx_halted = True
    assert seen["tx_halt_check"]() is True
