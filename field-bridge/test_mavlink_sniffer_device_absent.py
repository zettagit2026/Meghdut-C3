#!/usr/bin/env python3
"""Regression tests for mavlink_sniffer.py graceful missing-device handling.

A real deploy caught this: the SiK/RFD900 serial radio was unplugged, and the
sniffer opened its serial port ONCE, unguarded, at startup -- so
serial.SerialException/FileNotFoundError propagated out of main(), the process
exited non-zero, and systemd's Restart=always turned it into a crash-loop. The
previous, resilient behaviour was to stay alive (sensor idle) and keep retrying
until the radio appears, then sniff.

These tests prove, WITHOUT any real hardware, that:
  * an absent device makes _open_serial_with_retry() retry + log a clear idle
    heartbeat rather than raise/exit, and it proceeds once the device appears;
  * a device that disappears mid-run makes _sniff_loop() fall back to the
    wait-for-device path (returns False) instead of crashing -- so a
    hot-unplug/replug is survivable.

METHOD: we mock mavutil.mavlink_connection (the exact call the sniffer uses to
open the serial link) to raise the real absence errors, and mock time.sleep so
the bounded backoff doesn't slow the test. Nothing here touches a real serial
port. Run: pytest field-bridge/test_mavlink_sniffer_device_absent.py -v
"""
import pytest

pytest.importorskip("pymavlink")
try:
    import serial  # noqa: F401
    _SERIAL_EXC = serial.SerialException
except ImportError:  # pragma: no cover
    _SERIAL_EXC = OSError

import mavlink_sniffer  # noqa: E402


class _FakeMav:
    """Minimal stand-in for a pymavlink connection object."""
    def __init__(self, script):
        # script: list of ("return", value) or ("raise", exc) actions for
        # successive recv_match() calls.
        self._script = list(script)
        self.closed = False

    def recv_match(self, *args, **kwargs):
        action, payload = self._script.pop(0)
        if action == "raise":
            raise payload
        return payload

    def close(self):
        self.closed = True


def test_absent_device_retries_then_opens(monkeypatch, capsys):
    """A missing device must NOT raise/exit -- _open_serial_with_retry logs the
    idle heartbeat, sleeps the backoff, retries, and returns the connection
    once the mock device 'appears'."""
    fake = _FakeMav([])
    attempts = {"n": 0}

    def flaky_connection(path, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 3:
            # First two attempts: device absent (unplugged / udev path missing).
            raise FileNotFoundError(f"could not open port {path}")
        return fake

    sleeps = []
    monkeypatch.setattr(mavlink_sniffer.mavutil, "mavlink_connection", flaky_connection)
    monkeypatch.setattr(mavlink_sniffer.time, "sleep", lambda s: sleeps.append(s))

    result = mavlink_sniffer._open_serial_with_retry(
        "/dev/cema-sik-adapter", 57600, idle_heartbeat_interval_s=60.0)

    assert result is fake, "should return the connection once the device appears"
    assert attempts["n"] == 3, "should have retried past the two absent attempts"
    assert sleeps == [mavlink_sniffer.DEVICE_RETRY_BACKOFF_S] * 2, "bounded backoff per retry"

    err = capsys.readouterr().err
    assert "waiting for MAVLink serial device /dev/cema-sik-adapter" in err
    assert "sensor idle" in err


def test_serial_exception_also_tolerated(monkeypatch):
    """A pyserial SerialException (port exists but cannot be opened) is treated
    as an absent device too -- retried, not fatal."""
    fake = _FakeMav([])
    attempts = {"n": 0}

    def flaky_connection(path, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _SERIAL_EXC(f"could not open port {path}")
        return fake

    monkeypatch.setattr(mavlink_sniffer.mavutil, "mavlink_connection", flaky_connection)
    monkeypatch.setattr(mavlink_sniffer.time, "sleep", lambda s: None)

    result = mavlink_sniffer._open_serial_with_retry("/dev/ttyUSB0", 57600, 60.0)
    assert result is fake
    assert attempts["n"] == 2


def test_midrun_device_disappearance_falls_back(monkeypatch, capsys):
    """If the device is unplugged mid-run, the read raises SerialException;
    _sniff_loop must catch it, log, and return False (fall back to wait) rather
    than crashing -- so the outer loop can wait for a replug."""
    # First recv returns None (idle, nothing on link), second raises as if the
    # radio was yanked out.
    mav = _FakeMav([
        ("return", None),
        ("raise", _SERIAL_EXC("device reports readiness to read but returned no data "
                              "(device disconnected or multiple access on port?)")),
    ])

    class _Args:
        serial = "/dev/cema-sik-adapter"
        console_url = "http://localhost"
        email = "x@example.com"
        password = "pw"

    monkeypatch.setattr(mavlink_sniffer.time, "sleep", lambda s: None)

    # Must return False (fall back), not raise.
    result = mavlink_sniffer._sniff_loop(
        mav, _Args(), headers={"Authorization": "Bearer t"},
        last_posted={}, REPOST_INTERVAL_S=10.0,
        IDLE_HEARTBEAT_INTERVAL_S=0.0, last_idle_heartbeat=0.0)

    assert result is False, "mid-run disappearance should fall back to wait-for-device"
    err = capsys.readouterr().err
    assert "device may have been unplugged" in err


def test_transient_recv_error_does_not_fall_back(monkeypatch):
    """A generic (non-serial) transient recv error must NOT be mistaken for a
    device disappearance -- it sleeps briefly and keeps sniffing (unregressed
    present-device behaviour)."""
    mav = _FakeMav([
        ("raise", ValueError("transient decode hiccup")),
        ("return", None),          # loop continues after the transient error
        ("raise", _SERIAL_EXC("device disconnected")),  # then a real unplug ends it
    ])

    class _Args:
        serial = "/dev/ttyUSB0"
        console_url = "http://localhost"
        email = "x@example.com"
        password = "pw"

    monkeypatch.setattr(mavlink_sniffer.time, "sleep", lambda s: None)

    result = mavlink_sniffer._sniff_loop(
        mav, _Args(), headers={}, last_posted={}, REPOST_INTERVAL_S=10.0,
        IDLE_HEARTBEAT_INTERVAL_S=0.0, last_idle_heartbeat=0.0)

    assert result is False
    # All three scripted actions consumed -> the transient error did NOT abort
    # the loop; only the serial disconnect did.
    assert mav._script == []
