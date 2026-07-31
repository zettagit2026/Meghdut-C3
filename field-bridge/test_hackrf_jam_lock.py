#!/usr/bin/env python3
"""Unit tests for task #152: hackrf_jam.py's transmit_burst() (the real TX
invocation point behind jam_bridge.py) must participate in the same
hackrf_device_lock.py cross-process mutex hackrf_rx.py/iq_capture.py already
use, so a live jam burst can never collide with hackrf_rx.py's sweep loop or
ml_classify_bridge.py's gate-check sweep/IQ capture against the same
physical HackRF.

Covers:
  - transmit_burst() refuses cleanly (ok=False, does not raise/crash) when
    the device lock is already held by someone else, exactly matching the
    HackrfDeviceBusy handling convention hackrf_rx.py's _one_sweep() uses.
  - transmit_burst() actually acquires+releases the SAME lockfile
    hackrf_device_lock() uses (verified by holding the lock ourselves and
    confirming transmit_burst() is blocked out, then confirming the lock is
    free again immediately after transmit_burst() returns).
  - fpv_video_bridge.py's only HackRF invocation point is iq_capture.py's
    capture_iq(), which was already lock-guarded before this task -- no
    fpv_video_bridge.py code change was needed/made; this is asserted here
    so a future refactor that adds a *new* direct hackrf_sweep/hackrf_transfer
    call to fpv_video_bridge.py doesn't silently reintroduce the coordination
    gap this task closed for the jam path.

Run: pytest field-bridge/test_hackrf_jam_lock.py -v
"""
import fcntl
import functools
import inspect
import os

import pytest

import hackrf_device_lock as lockmod
import hackrf_jam


@pytest.fixture
def isolated_lock_path(tmp_path, monkeypatch):
    """Point hackrf_device_lock at a throwaway lockfile for this test only,
    so these tests can't collide with a real HackRF lock (or each other) on
    a shared machine."""
    path = str(tmp_path / "test_hackrf_device.lock")
    monkeypatch.setattr(lockmod, "HACKRF_DEVICE_LOCK_PATH", path)
    return path


def test_transmit_burst_returns_busy_error_without_crashing_when_locked(isolated_lock_path, monkeypatch):
    """If another process/thread is already holding the device lock,
    transmit_burst() must return {"ok": False, ...} promptly instead of
    blocking forever, crashing, or silently proceeding to open the device
    anyway. Uses a short timeout_s (via a patched hackrf_device_lock) purely
    to keep this test fast -- production behavior (LOCK_ACQUIRE_TIMEOUT_S =
    5.0s) is unaffected, only this test's wait is shortened."""
    monkeypatch.setattr(
        hackrf_jam, "hackrf_device_lock",
        functools.partial(lockmod.hackrf_device_lock, timeout_s=0.2),
    )
    fd = os.open(isolated_lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = hackrf_jam.transmit_burst(915.0, 500.0, 0.1, 20)
        assert result["ok"] is False
        assert result["stopped_early"] is False
        assert "busy" in result["error"].lower()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_transmit_burst_releases_lock_after_missing_binary_failure(isolated_lock_path):
    """hackrf_transfer isn't installed in this test environment, so
    transmit_burst() takes its FileNotFoundError branch -- but that branch
    now lives INSIDE the lock's `with` block (task #152: the lock wraps the
    whole subprocess lifecycle, not just a successful Popen). Confirm the
    lock is still correctly released afterward (no leaked/held lock on this
    failure path) by acquiring it ourselves immediately after."""
    result = hackrf_jam.transmit_burst(915.0, 500.0, 0.1, 20)
    assert result["ok"] is False
    assert "not found" in result["error"]

    fd = os.open(isolated_lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        # Must succeed immediately -- if transmit_burst() had leaked the
        # lock, this would raise BlockingIOError.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def test_transmit_burst_signature_unchanged():
    """Task #152 must not change transmit_burst()'s public signature --
    only wire in the shared lock around its existing subprocess logic."""
    sig = inspect.signature(hackrf_jam.transmit_burst)
    assert list(sig.parameters.keys()) == [
        "freq_mhz", "bandwidth_khz", "duration_s", "tx_gain", "stop_event", "on_started",
    ]


def test_transmit_burst_imports_shared_device_lock():
    """transmit_burst() must actually use the shared hackrf_device_lock.py
    mutex (same module hackrf_rx.py/iq_capture.py use), not a bespoke
    lock -- guards against a future edit accidentally reimplementing
    locking instead of reusing the shared primitive."""
    assert hackrf_jam.hackrf_device_lock is lockmod.hackrf_device_lock
    assert hackrf_jam.HackrfDeviceBusy is lockmod.HackrfDeviceBusy


def test_fpv_video_bridge_has_no_direct_hackrf_subprocess_call():
    """fpv_video_bridge.py's only HackRF access is via iq_capture.capture_iq(),
    which already wraps hackrf_transfer in hackrf_device_lock() (predates
    task #152). This asserts that invariant holds -- fpv_video_bridge.py
    itself must never gain a direct subprocess call to hackrf_sweep/
    hackrf_transfer, or it would bypass the shared device lock entirely."""
    import fpv_video_bridge

    src = inspect.getsource(fpv_video_bridge)
    assert "subprocess" not in src
    assert "capture_iq" in src
