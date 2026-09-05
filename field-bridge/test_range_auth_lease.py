#!/usr/bin/env python3
"""Tests for the holistic range-auth LEASE-EXPIRY stop applied to every
continuous-TX field bridge (jam, SDR-MAVLink inject) — closing the asymmetry
where a continuous effect only re-polled tx_halt mid-stream, so a bare
range-authorization lease expiry (WITHOUT an operator abort) could let it keep
transmitting.

Covers:
  * range_auth_lease.RangeAuthLease — short-TTL, fail-closed live re-check.
  * range_auth_lease.make_tx_halt_check — "tx_halt OR range-auth lost", with
    tx_halt checked first (so EMERGENCY ABORT is never delayed by the lease).
  * INTEGRATION: a continuous JAM (hackrf_jam.transmit_burst), a swept JAM
    (transmit_sweep) and a continuous SDR-INJECT (transmit_iq_file) all TERMINATE
    their live hackrf_transfer when the lease deauthorizes mid-stream — driven by
    the SAME make_tx_halt_check the bridges build in production.

No real HackRF: subprocess.Popen, the device lock and build_noise_iq are stubbed;
the sweep uses an injected dwell_runner. A fake monotonic clock makes the lease's
TTL re-check deterministic.

Run: pytest field-bridge/test_range_auth_lease.py -v
"""
from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hackrf_jam as hj
from range_auth_lease import RangeAuthLease, make_tx_halt_check


# ======================================================================
# RangeAuthLease — TTL caching + fail-closed
# ======================================================================
def test_lease_caches_within_ttl_and_rechecks_after():
    clock = {"t": 0.0}
    calls = {"n": 0}
    state = {"v": True}

    def check():
        calls["n"] += 1
        return state["v"]

    lease = RangeAuthLease(check, ttl_s=0.5, now=lambda: clock["t"])
    assert lease.authorized() is True   # first call -> live check
    assert calls["n"] == 1
    # Within the TTL: cached, no new live check even if the source flipped.
    state["v"] = False
    clock["t"] = 0.4
    assert lease.authorized() is True
    assert calls["n"] == 1
    # Past the TTL: re-checks and sees the new (deauthorized) value.
    clock["t"] = 0.6
    assert lease.authorized() is False
    assert calls["n"] == 2


def test_lease_first_call_always_live_even_at_time_zero():
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return True

    lease = RangeAuthLease(check, ttl_s=10.0, now=lambda: 0.0)
    assert lease.authorized() is True
    assert calls["n"] == 1  # not served from an uninitialized cache


def test_lease_fails_closed_on_check_exception():
    def boom():
        raise ConnectionError("backend unreachable")

    lease = RangeAuthLease(boom, ttl_s=0.0)
    assert lease.authorized() is False  # error => NOT authorized


def test_lease_default_ttl_is_module_constant(monkeypatch):
    import range_auth_lease
    monkeypatch.setattr(range_auth_lease, "DEFAULT_TTL_S", 0.0)
    # ttl_s=None -> uses the (patched) module default, so every call re-checks.
    state = {"v": True}
    lease = RangeAuthLease(lambda: state["v"])  # no ttl_s
    assert lease.authorized() is True
    state["v"] = False
    assert lease.authorized() is False  # ttl 0 -> immediate re-check


# ======================================================================
# make_tx_halt_check — tx_halt OR range-auth lost, tx_halt checked first
# ======================================================================
def test_halt_check_false_when_authorized_and_not_halted():
    lease = RangeAuthLease(lambda: True, ttl_s=0.0)
    thc = make_tx_halt_check(lambda: False, lease)
    assert thc() is False


def test_halt_check_true_on_tx_halt_even_when_authorized():
    lease = RangeAuthLease(lambda: True, ttl_s=0.0)
    thc = make_tx_halt_check(lambda: True, lease)
    assert thc() is True


def test_halt_check_true_on_lease_expiry_without_abort():
    # The core asymmetry fix: tx_halt STAYS False, but the lease goes
    # unauthorized -> the halt check fires anyway.
    lease = RangeAuthLease(lambda: False, ttl_s=0.0)
    thc = make_tx_halt_check(lambda: False, lease)
    assert thc() is True


def test_halt_check_tx_halt_checked_before_lease():
    # tx_halt is a local instant boolean; the lease must NOT be consulted when
    # tx_halt already fired (so an abort is never delayed by a live poll).
    consulted = {"n": 0}

    def check():
        consulted["n"] += 1
        return True

    lease = RangeAuthLease(check, ttl_s=0.0)
    thc = make_tx_halt_check(lambda: True, lease)
    assert thc() is True
    assert consulted["n"] == 0  # short-circuited before touching the lease


def test_halt_check_fails_safe_on_raising_tx_halted():
    lease = RangeAuthLease(lambda: True, ttl_s=0.0)

    def boom():
        raise RuntimeError("broken predicate")

    thc = make_tx_halt_check(boom, lease)
    assert thc() is True  # a broken tx_halt predicate must fail SAFE (halt)


# ======================================================================
# INTEGRATION: continuous / swept jam + continuous inject terminate on a
# bare lease expiry (no abort), via the real make_tx_halt_check.
# ======================================================================
@pytest.fixture(autouse=True)
def _no_real_radio(monkeypatch):
    monkeypatch.setattr(hj, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(hj, "build_noise_iq", lambda *a, **k: b"\x00\x00")


class FakeProc:
    """A looped hackrf_transfer that never exits on its own — only our
    terminate/kill ends it."""
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None
        self.stderr = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def test_continuous_jam_burst_terminates_on_lease_expiry(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(hj.subprocess, "Popen", lambda cmd, **k: proc)

    clock = {"t": 0.0}
    polls = {"n": 0}
    authorized = {"v": True}  # tx_halt STAYS False the whole time

    # transmit_burst's supervise loop calls time.sleep(poll_s) each poll; advance
    # the lease clock past its TTL there and expire the lease after a few polls.
    def fake_sleep(_s):
        polls["n"] += 1
        clock["t"] += 0.6  # > lease TTL (0.5), so the next authorized() re-checks
        if polls["n"] >= 3:
            authorized["v"] = False  # LEASE EXPIRES mid-stream — no abort

    monkeypatch.setattr(hj.time, "sleep", fake_sleep)

    lease = RangeAuthLease(lambda: authorized["v"], ttl_s=0.5, now=lambda: clock["t"])
    tx_halt_check = make_tx_halt_check(lambda: False, lease)  # tx_halt NEVER set

    result = hj.transmit_burst(2450.0, 500.0, 0, 20,  # 0 => continuous
                               tx_halt_check=tx_halt_check)
    assert result["ok"] is True
    assert result["stopped_early"] is True          # loop exited on lease expiry
    assert proc.terminated is True                  # live process actually killed
    assert authorized["v"] is False


def test_swept_jam_terminates_on_lease_expiry(monkeypatch):
    clock = {"t": 0.0}
    visited = []
    authorized = {"v": True}

    def runner(iq_path, center_mhz):
        visited.append(center_mhz)
        clock["t"] += 0.6  # advance past the lease TTL between dwells
        if len(visited) >= 3:
            authorized["v"] = False  # LEASE EXPIRES mid-sweep — no abort
        return "exited"

    lease = RangeAuthLease(lambda: authorized["v"], ttl_s=0.5, now=lambda: clock["t"])
    tx_halt_check = make_tx_halt_check(lambda: False, lease)

    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20, step_mhz=20.0, dwell_ms=1.0,
        duration_s=None, tx_halt_check=tx_halt_check, dwell_runner=runner)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    # Stopped promptly after the lease expired — did not keep sweeping forever.
    assert len(visited) <= 6


def test_continuous_inject_iq_file_terminates_on_lease_expiry(monkeypatch, tmp_path):
    proc = FakeProc()
    monkeypatch.setattr(hj.subprocess, "Popen", lambda cmd, **k: proc)

    clock = {"t": 0.0}
    polls = {"n": 0}
    authorized = {"v": True}

    def fake_sleep(_s):
        polls["n"] += 1
        clock["t"] += 0.6
        if polls["n"] >= 3:
            authorized["v"] = False  # LEASE EXPIRES mid-inject — no abort

    monkeypatch.setattr(hj.time, "sleep", fake_sleep)

    lease = RangeAuthLease(lambda: authorized["v"], ttl_s=0.5, now=lambda: clock["t"])
    tx_halt_check = make_tx_halt_check(lambda: False, lease)

    iq = tmp_path / "inject.iq"
    iq.write_bytes(b"\x00\x00")
    result = hj.transmit_iq_file(str(iq), 915.0, None, 20,  # None => continuous
                                 tx_halt_check=tx_halt_check)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert proc.terminated is True


def test_continuous_jam_keeps_running_while_lease_stays_authorized(monkeypatch):
    # Control: while the lease stays armed and tx_halt is False, the continuous
    # burst keeps transmitting (only an explicit stop ends it) — the fix must NOT
    # stop a still-authorized effect.
    proc = FakeProc()
    monkeypatch.setattr(hj.subprocess, "Popen", lambda cmd, **k: proc)

    clock = {"t": 0.0}
    stop = threading.Event()
    polls = {"n": 0}

    def fake_sleep(_s):
        polls["n"] += 1
        clock["t"] += 0.6
        if polls["n"] >= 5:
            stop.set()  # operator finally stops it — NOT the lease

    monkeypatch.setattr(hj.time, "sleep", fake_sleep)

    lease = RangeAuthLease(lambda: True, ttl_s=0.5, now=lambda: clock["t"])
    tx_halt_check = make_tx_halt_check(lambda: False, lease)

    result = hj.transmit_burst(2450.0, 500.0, 0, 20,
                               stop_event=stop, tx_halt_check=tx_halt_check)
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert polls["n"] >= 5  # kept transmitting across many polls while authorized
    assert proc.terminated is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
