#!/usr/bin/env python3
"""Unit tests: JamBridge._do_transmit threads the jam power PROFILE (anti-fade
Phase 1) from the request params into the transmit primitive, without touching
any gate.

The bridge reads an optional `profile` from the jam_request (backend does not
stamp it yet, so absence => None => MAX => today's `-a 1 -x <tx_gain>`), stores
it in params, and forwards it to transmit_burst / transmit_sweep. It changes
ONLY the amp/VGA the primitive builds; every gate in _handle_jam_request stays
exactly as before.

No real radio / no network: the transmit functions are stubbed to capture the
profile kwarg they receive.

Run: pytest field-bridge/test_jam_bridge_profile.py -v
"""
from __future__ import annotations

import sys
import threading

import pytest

import jam_bridge


@pytest.fixture
def bridge(monkeypatch):
    """A JamBridge instance without running __init__'s env-config reads."""
    b = jam_bridge.JamBridge.__new__(jam_bridge.JamBridge)
    b.tx_halted = False
    b._active_stop_event = None
    b._active_lock = threading.Lock()
    # is_range_authorized is only consulted by the lease inside tx_halt_check;
    # stub it so _do_transmit never touches the network.
    monkeypatch.setattr(b, "is_range_authorized", lambda effect="jam": True)
    return b


def _capture_transmit(monkeypatch):
    """Stub transmit_burst/transmit_sweep to record the profile kwarg."""
    seen = {}

    def fake_burst(*a, **k):
        seen["fn"] = "burst"
        seen["profile"] = k.get("profile")
        return {"ok": True, "stopped_early": False, "error": None}

    def fake_sweep(*a, **k):
        seen["fn"] = "sweep"
        seen["profile"] = k.get("profile")
        return {"ok": True, "stopped_early": False, "error": None}

    monkeypatch.setattr(jam_bridge, "transmit_burst", fake_burst)
    monkeypatch.setattr(jam_bridge, "transmit_sweep", fake_sweep)
    return seen


def _base_params(**over):
    p = {
        "band": "2g4",
        "freq_mhz": 2450.0,
        "bandwidth_khz": 500.0,
        "duration_s": None,
        "tx_gain": 20,
        "sweep": False,
        "freq_start_mhz": None,
        "freq_stop_mhz": None,
        "step_mhz": 20.0,
        "dwell_ms": 5.0,
        "profile": None,
        "request_id": "r1",
        "actor": "tester",
    }
    p.update(over)
    return p


def test_do_transmit_burst_defaults_profile_none(bridge, monkeypatch):
    seen = _capture_transmit(monkeypatch)
    bridge._do_transmit(_base_params(), threading.Event(), lambda p: None)
    assert seen["fn"] == "burst"
    assert seen["profile"] is None  # => MAX in the primitive (byte-identical)


def test_do_transmit_burst_forwards_flat(bridge, monkeypatch):
    seen = _capture_transmit(monkeypatch)
    bridge._do_transmit(_base_params(profile="flat"), threading.Event(),
                        lambda p: None)
    assert seen["fn"] == "burst"
    assert seen["profile"] == "flat"


def test_do_transmit_sweep_forwards_external_pa(bridge, monkeypatch):
    seen = _capture_transmit(monkeypatch)
    params = _base_params(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2483.5,
                          profile="external_pa")
    bridge._do_transmit(params, threading.Event(), lambda p: None)
    assert seen["fn"] == "sweep"
    assert seen["profile"] == "external_pa"


def test_handle_jam_request_reads_profile_into_params(bridge, monkeypatch):
    """End-to-end through the gated handler: a jam_request carrying profile=flat
    reaches the transmit primitive as profile=flat. Every gate is satisfied via
    stubs — the point is that profile survives the gauntlet unchanged."""
    seen = _capture_transmit(monkeypatch)
    # Satisfy the independent gates.
    monkeypatch.setattr(bridge, "is_range_authorized", lambda effect="jam": True)

    sent = []

    class FakeWS:
        def send(self, msg):
            sent.append(msg)

    data = {
        "type": "jam_request",
        "request_id": "r2",
        "actor": "tester",
        "jam_mode": "meghdut",
        "jam_confirm_token": "x" * 36,  # passes the shape check
        "band": "2g4",
        "tx_gain": 20,
        "continuous": True,
        "profile": "flat",
    }
    bridge._handle_jam_request(FakeWS(), data)
    # run() spawns a daemon thread; join it so the transmit stub has run.
    for t in threading.enumerate():
        if t.name == "jam-r2":
            t.join(timeout=5)
    assert seen.get("profile") == "flat", seen


def test_handle_jam_request_absent_profile_is_none(bridge, monkeypatch):
    seen = _capture_transmit(monkeypatch)
    monkeypatch.setattr(bridge, "is_range_authorized", lambda effect="jam": True)

    class FakeWS:
        def send(self, msg):
            pass

    data = {
        "type": "jam_request",
        "request_id": "r3",
        "actor": "tester",
        "jam_confirm_token": "x" * 36,
        "band": "2g4",
        "tx_gain": 20,
        "continuous": True,
        # no "profile" key => MAX (byte-identical to today)
    }
    bridge._handle_jam_request(FakeWS(), data)
    for t in threading.enumerate():
        if t.name == "jam-r3":
            t.join(timeout=5)
    assert seen.get("profile") is None, seen


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
