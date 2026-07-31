#!/usr/bin/env python3
"""Unit test for dronecan_parser.py's run_forever() idle-loop liveness
heartbeat (task #151).

dronecan_parser.py had no prior companion test file (its self_test() covers
the DSDL decode logic via a virtual-CAN round trip and is run standalone via
`python3 dronecan_parser.py --self-test`). This file adds just the
regression coverage for the new idle-heartbeat behaviour, following the same
pattern used for crsf_parser.py/graupner_hott_parser.py/ltm_parser.py (task
#151): DroneCANBridge.run_forever()'s `spin(1.0)` loop gives no
per-iteration signal of whether anything genuine arrived, so a "still
listening" line is now printed on a fixed cadence regardless.

Run: pytest field-bridge/test_dronecan_heartbeat.py -v
"""
import pytest

import dronecan_parser
from dronecan_parser import DroneCANBridge


class _StopLoop(BaseException):
    """Sentinel used to break run_forever()'s `while True` after a fixed
    number of iterations, so the test doesn't hang. Deliberately derives
    from BaseException, NOT Exception, so it can't be swallowed by any
    `except Exception` handler in the loop under test."""


class _FakeIdleNode:
    """Fake dronecan node whose .spin() is a no-op (genuinely idle bus,
    same as a real spin() call that received nothing this cycle), and whose
    .close() is a no-op. Raises _StopLoop once the caller has exhausted the
    number of spin() calls the test wants to observe."""

    def __init__(self, max_spins: int):
        self.max_spins = max_spins
        self.n = 0
        self.closed = False

    def add_handler(self, *args, **kwargs):
        pass

    def spin(self, timeout):
        self.n += 1
        if self.n > self.max_spins:
            raise _StopLoop()

    def close(self):
        self.closed = True


def test_run_forever_prints_idle_heartbeat_on_cadence(monkeypatch, capsys):
    bridge = DroneCANBridge("http://console.example", {}, "socketcan", "can0",
                             1000000, listen_node_id=127,
                             email="e@example.com", password="pw")
    fake_node = _FakeIdleNode(max_spins=3)
    monkeypatch.setattr(dronecan_parser.dronecan, "make_node",
                         lambda *a, **k: fake_node)

    # Fake clock: three idle spins at t=1000, t=1061, t=1062 (61s then 1s
    # apart) so exactly two of the three should cross the 60s cadence.
    fake_times = iter([1000.0, 1061.0, 1062.0])
    monkeypatch.setattr(dronecan_parser.time, "time", lambda: next(fake_times))

    with pytest.raises(_StopLoop):
        bridge.run_forever()

    out = capsys.readouterr().out
    heartbeat_lines = [line for line in out.splitlines() if "[heartbeat]" in line]
    assert len(heartbeat_lines) == 2
    assert "still listening" in heartbeat_lines[0]
    assert "can0" in heartbeat_lines[0]
    # run_forever()'s `finally: self.node.close()` must still run even
    # though the loop was broken by an exception, not KeyboardInterrupt.
    assert fake_node.closed
