"""Regression test for FIX 2 (TX-review MEDIUM): the ungoverned hackrf_jam.py
CLI (main() / --continuous) must, on a dual-radio host, address the pinned TX
HackRF via `-d <HACKRF_TX_SERIAL>` AND serialize its hackrf_transfer behind
that unit's own per-serial device lock — so it can never key the RX antenna or
collide with the RX consumers. Previously main() built its command with no
`-d` and took no lock at all.

No radio is involved: hackrf_transfer (subprocess.run) and the device lock are
both mocked; we only assert the command shape and the lock's serial argument.
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TX_SERIAL = "DEADBEEF1234"


def _load_module(monkeypatch, serial):
    """Import a FRESH copy of hackrf_jam.py with HACKRF_TX_SERIAL set to
    `serial` (or unset), since the module reads it once at import time."""
    if serial is None:
        monkeypatch.delenv("HACKRF_TX_SERIAL", raising=False)
    else:
        monkeypatch.setenv("HACKRF_TX_SERIAL", serial)
    spec = importlib.util.spec_from_file_location(
        f"hackrf_jam_pin_{serial}", str(HERE / "hackrf_jam.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_single_burst(monkeypatch, mod):
    """Drive main() through ONE bounded burst, capturing the hackrf_transfer
    argv and the serial the device lock was acquired with."""
    captured = {"cmd": None, "lock_serial": "unset"}

    @contextlib.contextmanager
    def fake_lock(*args, **kwargs):
        captured["lock_serial"] = kwargs.get("serial", args[1] if len(args) > 1 else None)
        yield

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(mod, "hackrf_device_lock", fake_lock)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "build_noise_iq", lambda *a, **k: b"\x00\x00")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "TRANSMIT")
    # Satisfy the CLI's OWN independent authorization gate (unchanged by FIX 2)
    # so we reach the transmit path — subprocess.run is mocked, nothing radiates.
    monkeypatch.setenv("CEMA_AUTHORIZED_RANGE", "1")
    monkeypatch.setattr(sys, "argv",
                        ["hackrf_jam.py", "--band", "2g4", "--duration-s", "1",
                         "--i-confirm-authorized-range"])
    mod.main()
    return captured


def test_cli_pins_device_and_locks_when_tx_serial_set(monkeypatch):
    mod = _load_module(monkeypatch, TX_SERIAL)
    cap = _run_single_burst(monkeypatch, mod)
    # (a) the hackrf_transfer command targets the pinned TX unit
    assert cap["cmd"] is not None, "hackrf_transfer was never invoked"
    assert "-d" in cap["cmd"], cap["cmd"]
    assert cap["cmd"][cap["cmd"].index("-d") + 1] == TX_SERIAL, cap["cmd"]
    # (b) the burst was serialized behind THAT unit's own per-serial lock
    assert cap["lock_serial"] == TX_SERIAL


def test_cli_preserves_shared_lock_and_no_pin_when_unset(monkeypatch):
    # Single-radio hosts / the test suite never set HACKRF_TX_SERIAL: behavior
    # must be byte-for-byte as before — no `-d`, shared/default lock (serial=None).
    mod = _load_module(monkeypatch, None)
    cap = _run_single_burst(monkeypatch, mod)
    assert cap["cmd"] is not None
    assert "-d" not in cap["cmd"], cap["cmd"]
    assert cap["lock_serial"] in (None, "unset")  # default shared lock


def test_cli_burst_is_device_locked_at_all(monkeypatch):
    # Guard against a regression to the old "no lock at all" main(): the lock
    # context manager MUST have been entered (serial recorded, not left at the
    # sentinel 'unset').
    mod = _load_module(monkeypatch, TX_SERIAL)
    cap = _run_single_burst(monkeypatch, mod)
    assert cap["lock_serial"] != "unset", "main() ran hackrf_transfer OUTSIDE any device lock"
