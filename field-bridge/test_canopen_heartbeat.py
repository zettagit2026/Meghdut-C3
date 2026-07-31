#!/usr/bin/env python3
"""Unit test for canopen_parser.py's idle-loop liveness heartbeat (task
#151).

canopen_parser.py had no prior companion test file (its parsing logic was
verified interactively against a real virtual-CAN round trip, see module
docstring). This file adds just the regression coverage for the new
idle-heartbeat behaviour: run()'s main loop was a bare `while True:
time.sleep(1)` with zero periodic liveness signal after startup (all real
work happens asynchronously in canopen's own listener thread via
on_error_control()) -- a "still listening" line is now printed on a fixed
cadence regardless of whether any node's heartbeat was ever decoded.

Run: pytest field-bridge/test_canopen_heartbeat.py -v
"""
import pytest

import canopen_parser


class _StopLoop(BaseException):
    """Sentinel used to break run()'s `while True` after a fixed number of
    iterations, so the test doesn't hang. Deliberately derives from
    BaseException, NOT Exception, so it can't be swallowed by any
    `except Exception` handler in the loop under test (there isn't one here,
    but this keeps the sentinel consistent with the other run_forever()
    heartbeat tests in this suite)."""


class _FakeScanner:
    pass


class _FakeNetwork:
    """Fake canopen.Network whose .connect()/.subscribe()/.disconnect() are
    no-ops, so run()'s bus-idle main loop can be exercised without a real
    (or virtual) CAN interface."""

    def __init__(self):
        self.scanner = _FakeScanner()
        self.disconnected = False

    def connect(self, **kwargs):
        pass

    def subscribe(self, can_id, callback):
        pass

    def disconnect(self):
        self.disconnected = True


def test_run_prints_idle_heartbeat_on_cadence(monkeypatch, capsys):
    monkeypatch.setattr(canopen_parser, "login", lambda *a, **k: "fake-token")
    fake_network = _FakeNetwork()
    monkeypatch.setattr(canopen_parser.canopen, "Network", lambda: fake_network)

    # Fake clock: run()'s loop calls time.sleep(1) then time.time() once per
    # iteration. Three idle iterations at t=1000, t=1061, t=1062 (61s then
    # 1s apart) so exactly two of the three should cross the 60s cadence.
    fake_times = iter([1000.0, 1061.0, 1062.0])
    monkeypatch.setattr(canopen_parser.time, "time", lambda: next(fake_times))

    call_count = {"n": 0}
    real_sleep = canopen_parser.time.sleep

    def fake_sleep(seconds):
        call_count["n"] += 1
        if call_count["n"] > 3:
            raise _StopLoop()

    monkeypatch.setattr(canopen_parser.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        canopen_parser.run("http://console.example", "e@example.com", "pw",
                           "socketcan", "can0", None)

    out = capsys.readouterr().out
    heartbeat_lines = [line for line in out.splitlines() if "[heartbeat]" in line]
    assert len(heartbeat_lines) == 2
    assert "still listening" in heartbeat_lines[0]
    assert "can0" in heartbeat_lines[0]
    # run()'s `finally: network.disconnect()` must still run even though the
    # loop was broken by an exception, not KeyboardInterrupt.
    assert fake_network.disconnected
