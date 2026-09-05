#!/usr/bin/env python3
"""SAFETY-CRITICAL regression tests: hackrf_jam.py must FAIL CLOSED when the TX
serial is unpinned.

On the production box there are TWO HackRFs (…930c PA/antenna = TX, …a063 =
RX/detection). With HACKRF_TX_SERIAL unset, `hackrf_transfer` runs with no `-d`
and grabs index-0 / whichever unit answers first — which can be the RX radio,
keying the wrong antenna (fratricide / wrong radiator). Every TRANSMIT entry
point in hackrf_jam.py (transmit_iq_file, transmit_burst incl. the continuous
-R path, transmit_sweep, and the interactive main() CLI) must therefore REFUSE
to transmit — spawning NO hackrf_transfer — unless either:
  * HACKRF_TX_SERIAL is pinned (the governed/production path, unchanged), or
  * the explicit single-HackRF dev opt-out HACKRF_ALLOW_UNPINNED_TX=1 is set.

No real radio: subprocess.Popen / subprocess.run and the device lock are mocked
or asserted-never-called; the fresh-import helper mirrors
test_hackrf_jam_cli_pinning.py so HACKRF_TX_SERIAL (read once at import) can be
set/unset per test.

NOTE: field-bridge/conftest.py sets HACKRF_ALLOW_UNPINNED_TX=1 for the suite
(models a single-HackRF dev box). The REFUSE tests below explicitly clear it
with monkeypatch.delenv, which works because the opt-out is read LIVE from the
environment at each transmit.

Run: pytest field-bridge/test_hackrf_jam_device_pin.py -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TX_SERIAL = "930cCAFEBABE"
ALLOW_ENV = "HACKRF_ALLOW_UNPINNED_TX"


def _load_module(monkeypatch, serial):
    """Import a FRESH copy of hackrf_jam.py with HACKRF_TX_SERIAL set to
    `serial` (or unset), since the module reads it once at import time."""
    if serial is None:
        monkeypatch.delenv("HACKRF_TX_SERIAL", raising=False)
    else:
        monkeypatch.setenv("HACKRF_TX_SERIAL", serial)
    spec = importlib.util.spec_from_file_location(
        f"hackrf_jam_devpin_{serial}", str(HERE / "hackrf_jam.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeProc:
    """A hackrf_transfer that our terminate() ends — models a live -R loop."""
    def __init__(self):
        self.terminated = False
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
        self.returncode = -9


def _install_spy_popen(monkeypatch, mod):
    """Replace subprocess.Popen with a spy that records every argv it is asked
    to spawn (so a refuse path can be proven to spawn NOTHING) and otherwise
    returns a controllable fake process. Also stub the device lock and IQ
    builder so a permitted path never touches a real radio."""
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(mod, "build_noise_iq", lambda *a, **k: b"\x00\x00")
    return calls


# ---------------------------------------------------------------------------
# 1) serial UNSET + opt-out UNSET  ->  REFUSE, spawn NOTHING
# ---------------------------------------------------------------------------
def test_transmit_iq_file_refuses_when_unpinned(monkeypatch, tmp_path):
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)

    iq = tmp_path / "x.iq"
    iq.write_bytes(b"\x00\x00")
    result = mod.transmit_iq_file(str(iq), 915.0, None, 20)

    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert "HACKRF_TX_SERIAL" in result["error"]
    assert calls == [], "REFUSE path must spawn no hackrf_transfer"


def test_transmit_burst_refuses_when_unpinned(monkeypatch):
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)

    result = mod.transmit_burst(2450.0, 500.0, None, 20)  # continuous
    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert "HACKRF_TX_SERIAL" in result["error"]
    assert calls == [], "REFUSE path must spawn no hackrf_transfer"


def test_transmit_burst_bounded_also_refuses_when_unpinned(monkeypatch):
    # The bounded (short-burst) path must fail closed too, not just continuous.
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)

    result = mod.transmit_burst(915.0, 500.0, 0.1, 20)
    assert result["ok"] is False
    assert calls == []


def test_transmit_sweep_refuses_when_unpinned(monkeypatch):
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    _install_spy_popen(monkeypatch, mod)

    runner_calls = {"n": 0}

    def runner(iq_path, center_mhz):
        runner_calls["n"] += 1
        return "exited"

    result = mod.transmit_sweep(2400.0, 2483.5, 500.0, 20, dwell_ms=1.0,
                                duration_s=None, dwell_runner=runner)
    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert "HACKRF_TX_SERIAL" in result["error"]
    # It refused before ever running a single dwell.
    assert runner_calls["n"] == 0


def test_cli_main_refuses_and_exits_nonzero_when_unpinned(monkeypatch):
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)
    # The refuse must land BEFORE the terminal 'TRANSMIT' prompt / any spawn.
    monkeypatch.setattr("builtins.input",
                        lambda *a, **k: pytest.fail("prompted before refusing"))
    monkeypatch.setenv("CEMA_AUTHORIZED_RANGE", "1")
    monkeypatch.setattr(sys, "argv",
                        ["hackrf_jam.py", "--band", "2g4", "--duration-s", "1",
                         "--i-confirm-authorized-range"])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code != 0
    assert calls == [], "CLI REFUSE path must spawn no hackrf_transfer"


def test_refuse_path_emits_no_device_flag_with_empty_serial(monkeypatch):
    # Belt-and-braces: with the serial unset the refuse path must never produce
    # a `-d ''`/`-d None` — because it produces no command at all.
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)
    mod.transmit_burst(915.0, 500.0, None, 20)
    assert calls == []
    # And _tx_device_args() (the pinning helper) yields nothing when unset.
    assert mod._tx_device_args() == []


# ---------------------------------------------------------------------------
# 2) serial UNSET + HACKRF_ALLOW_UNPINNED_TX=1  ->  dev opt-out proceeds + WARN
# ---------------------------------------------------------------------------
def test_optout_permits_unpinned_transmit_with_warning(monkeypatch, tmp_path, capsys):
    mod = _load_module(monkeypatch, None)
    monkeypatch.setenv(ALLOW_ENV, "1")
    calls = _install_spy_popen(monkeypatch, mod)

    iq = tmp_path / "x.iq"
    iq.write_bytes(b"\x00\x00")
    stop = mod.threading.Event()
    stop.set()  # terminate on the first supervise poll -> returns promptly
    result = mod.transmit_iq_file(str(iq), 915.0, None, 20, stop_event=stop)

    assert result["ok"] is True
    assert len(calls) == 1, "opt-out must actually transmit (one spawn)"
    # Unpinned: no device flag in the argv.
    assert "-d" not in calls[0], calls[0]
    # The warning is surfaced (dev-only unpinned TX).
    err = capsys.readouterr().err
    assert "UNPINNED" in err
    assert ALLOW_ENV in err


def test_optout_burst_proceeds_unpinned(monkeypatch, capsys):
    mod = _load_module(monkeypatch, None)
    monkeypatch.setenv(ALLOW_ENV, "1")
    calls = _install_spy_popen(monkeypatch, mod)

    stop = mod.threading.Event()
    stop.set()
    result = mod.transmit_burst(2450.0, 500.0, None, 20, stop_event=stop)
    assert result["ok"] is True
    assert len(calls) == 1
    assert "-d" not in calls[0]
    assert "UNPINNED" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 3) serial SET  ->  pinned `-d <serial>` exactly as before (path unchanged)
# ---------------------------------------------------------------------------
def test_pinned_transmit_iq_file_carries_device_flag(monkeypatch, tmp_path, capsys):
    mod = _load_module(monkeypatch, TX_SERIAL)
    # Opt-out must be irrelevant on the pinned path: clear it and still transmit.
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)

    iq = tmp_path / "x.iq"
    iq.write_bytes(b"\x00\x00")
    stop = mod.threading.Event()
    stop.set()
    result = mod.transmit_iq_file(str(iq), 915.0, None, 20, stop_event=stop)

    assert result["ok"] is True
    assert len(calls) == 1
    cmd = calls[0]
    assert "-d" in cmd
    assert cmd[cmd.index("-d") + 1] == TX_SERIAL
    # No unpinned warning on the governed/pinned path.
    assert "UNPINNED" not in capsys.readouterr().err


def test_pinned_burst_carries_device_flag(monkeypatch):
    mod = _load_module(monkeypatch, TX_SERIAL)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _install_spy_popen(monkeypatch, mod)

    stop = mod.threading.Event()
    stop.set()
    result = mod.transmit_burst(2450.0, 500.0, None, 20, stop_event=stop)
    assert result["ok"] is True
    assert len(calls) == 1
    cmd = calls[0]
    assert "-d" in cmd and cmd[cmd.index("-d") + 1] == TX_SERIAL


def test_pinned_cli_burst_carries_device_flag_and_transmits(monkeypatch):
    # The interactive CLI, when pinned, still reaches the transmit and emits -d.
    mod = _load_module(monkeypatch, TX_SERIAL)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    captured = {"cmd": None}

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _Done()

    monkeypatch.setattr(mod, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.setattr(mod, "build_noise_iq", lambda *a, **k: b"\x00\x00")
    monkeypatch.setattr("builtins.input", lambda *a, **k: "TRANSMIT")
    monkeypatch.setenv("CEMA_AUTHORIZED_RANGE", "1")
    monkeypatch.setattr(sys, "argv",
                        ["hackrf_jam.py", "--band", "2g4", "--duration-s", "1",
                         "--i-confirm-authorized-range"])
    mod.main()
    assert captured["cmd"] is not None, "pinned CLI never transmitted"
    assert "-d" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("-d") + 1] == TX_SERIAL


# ---------------------------------------------------------------------------
# 4) the gate helper itself
# ---------------------------------------------------------------------------
def test_gate_helper_returns_error_only_when_unpinned_and_no_optout(monkeypatch):
    unset = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert unset._tx_pinning_error() is not None

    monkeypatch.setenv(ALLOW_ENV, "1")
    assert unset._tx_pinning_error() is None  # dev opt-out

    pinned = _load_module(monkeypatch, TX_SERIAL)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    assert pinned._tx_pinning_error() is None  # pinned ignores the opt-out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
