#!/usr/bin/env python3
"""Unit tests for the commander-directed effectiveness changes to
field-bridge/hackrf_jam.py:

  * NO artificial auto-stop cap — a continuous run transmits until the operator
    stops it (stop_event / tx_halt), NOT a fixed timer.
  * SWEPT-BARRAGE (transmit_sweep) steps the TX center across a band so a FHSS
    control link is hit on every hop over the sweep's revisit interval.
  * The one invariant that never changes: the effect is ALWAYS instantly
    stoppable — every transmit loop terminates the live hackrf_transfer the
    moment stop_event OR tx_halt fires.

No real HackRF: subprocess.Popen, the device lock, and build_noise_iq are all
stubbed; the sweep uses an injected dwell_runner so no subprocess is spawned.

Run: pytest field-bridge/test_hackrf_jam_continuous_sweep.py -v
"""
from __future__ import annotations

import contextlib
import threading

import pytest

import hackrf_jam as hj


# --------------------------------------------------------------------------
# _is_continuous — the continuous sentinel
# --------------------------------------------------------------------------
@pytest.mark.parametrize("val", [None, 0, 0.0, -1, "continuous", "CONT", ""])
def test_is_continuous_true(val):
    assert hj._is_continuous(val) is True


@pytest.mark.parametrize("val", [1, 5.0, 100.0, "5"])
def test_is_continuous_false(val):
    assert hj._is_continuous(val) is False


# --------------------------------------------------------------------------
# sweep_centers_mhz — pure coverage of the band
# --------------------------------------------------------------------------
def test_sweep_centers_cover_full_band_2g4():
    centers = hj.sweep_centers_mhz(2400.0, 2483.5, 20.0)
    # Bottom and top of the band are both illuminated.
    assert centers[0] == 2400.0
    assert centers[-1] == 2483.5
    # Consecutive centers are no more than one step apart, so a ~20MHz-wide
    # HackRF window tiles the whole ~83.5MHz band with no gap.
    for a, b in zip(centers, centers[1:]):
        assert (b - a) <= 20.0 + 1e-6


def test_sweep_centers_stop_pinned_even_on_uneven_span():
    centers = hj.sweep_centers_mhz(5725.0, 5875.0, 40.0)
    assert centers[0] == 5725.0
    assert centers[-1] == 5875.0  # top always covered even if span % step != 0


# --------------------------------------------------------------------------
# transmit_sweep — steps across the band, continuous until stopped
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_radio(monkeypatch):
    """Never touch a real device lock or build a real IQ buffer."""
    monkeypatch.setattr(hj, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(hj, "build_noise_iq", lambda *a, **k: b"\x00\x00")


def test_transmit_sweep_steps_across_band_then_stops_on_event(monkeypatch):
    visited = []
    stop = threading.Event()

    def runner(iq_path, center_mhz):
        visited.append(center_mhz)
        # Let it complete a bit more than one full pass, then the operator stops.
        if len(visited) >= 12:
            stop.set()
        return "exited"

    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20,
        step_mhz=20.0, dwell_ms=1.0, duration_s=None,  # continuous
        stop_event=stop, dwell_runner=runner,
    )
    assert result["ok"] is True
    assert result["stopped_early"] is True
    # It really stepped ACROSS the band (every center of a pass was illuminated),
    # not parked on one frequency.
    expected = set(hj.sweep_centers_mhz(2400.0, 2483.5, 20.0))
    assert expected.issubset(set(visited))


def test_transmit_sweep_stops_immediately_on_tx_halt(monkeypatch):
    visited = []
    halted = {"v": False}

    def runner(iq_path, center_mhz):
        visited.append(center_mhz)
        halted["v"] = True  # tx_halt asserted after the first dwell
        return "exited"

    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20,
        step_mhz=20.0, dwell_ms=1.0, duration_s=None,
        tx_halt_check=lambda: halted["v"], dwell_runner=runner,
    )
    assert result["stopped_early"] is True
    # It stopped promptly — did not keep sweeping the whole band after tx_halt.
    assert len(visited) <= 2


def test_transmit_sweep_dwell_outcome_stopped_ends_sweep():
    # If a dwell itself is aborted mid-transmission (runner returns "stopped"),
    # the whole sweep ends stopped_early.
    def runner(iq_path, center_mhz):
        return "stopped"

    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20, duration_s=None, dwell_runner=runner)
    assert result["ok"] is True
    assert result["stopped_early"] is True


def test_transmit_sweep_bounded_duration_completes():
    # A positive duration_s runs a bounded sweep window and completes normally.
    calls = {"n": 0}

    def runner(iq_path, center_mhz):
        calls["n"] += 1
        return "exited"

    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20, dwell_ms=1.0, duration_s=0.05,
        dwell_runner=runner)
    assert result["ok"] is True
    assert result["stopped_early"] is False
    assert calls["n"] >= 1


# --------------------------------------------------------------------------
# transmit_burst — continuous loops with -R and no deadline, stops on abort
# --------------------------------------------------------------------------
class FakeProc:
    """A hackrf_transfer that never exits on its own — only our terminate/kill
    ends it (models a real -R looped transmit)."""
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


def test_transmit_burst_continuous_uses_repeat_flag_and_stops_on_event(monkeypatch):
    captured = {}
    proc = FakeProc()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return proc

    monkeypatch.setattr(hj.subprocess, "Popen", fake_popen)

    stop = threading.Event()
    sleeps = {"n": 0}

    def fake_sleep(s):
        # Prove the loop keeps polling (i.e. keeps transmitting) across several
        # iterations, THEN the operator stops it — not a fixed auto-timer.
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            stop.set()

    monkeypatch.setattr(hj.time, "sleep", fake_sleep)

    result = hj.transmit_burst(2450.0, 500.0, 0, 20, stop_event=stop)  # 0 => continuous
    assert result["ok"] is True
    assert result["stopped_early"] is True
    # Looped the chunk on the radio (hackrf_transfer -R).
    assert "-R" in captured["cmd"]
    # Kept transmitting for multiple poll cycles before the stop, then the live
    # process was actually terminated.
    assert sleeps["n"] >= 3
    assert proc.terminated is True


def test_transmit_burst_bounded_short_burst_has_no_repeat_flag(monkeypatch):
    captured = {}

    class DoneProc(FakeProc):
        def poll(self):
            return 0  # exits immediately (bounded short burst)

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return DoneProc()

    monkeypatch.setattr(hj.subprocess, "Popen", fake_popen)
    result = hj.transmit_burst(2450.0, 500.0, 0.1, 20)
    assert result["ok"] is True
    assert "-R" not in captured["cmd"]  # a short bounded burst plays once


def test_transmit_iq_file_continuous_loops_with_repeat_flag(monkeypatch, tmp_path):
    captured = {}
    proc = FakeProc()

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return proc

    monkeypatch.setattr(hj.subprocess, "Popen", fake_popen)
    stop = threading.Event()
    stop.set()  # stop already asserted -> terminates on the first poll

    iq = tmp_path / "x.iq"
    iq.write_bytes(b"\x00\x00")
    result = hj.transmit_iq_file(str(iq), 915.0, None, 20, stop_event=stop)  # None => continuous
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert "-R" in captured["cmd"]
    assert proc.terminated is True


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
