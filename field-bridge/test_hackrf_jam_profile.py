#!/usr/bin/env python3
"""Unit tests for the JAM POWER PROFILE (anti-fade Phase 1) added to
field-bridge/hackrf_jam.py.

A profile changes ONLY the HackRF amp-enable (`-a`) and IF VGA (`-x`) in every
hackrf_transfer command — never the center freq, sweep span/dwell, sample rate,
`-R`, device-pin (`-d`), or any safety gate. The three profiles:

  * MAX (default / None):  `-a 1 -x <tx_gain>`  — EXACTLY today's behavior.
  * FLAT:                  `-a 0 -x FLAT_TX_VGA` — amp OFF + reduced fixed VGA
                           (lower peak, zero thermal fade; bare-radio, no PA).
  * EXTERNAL_PA:           `-a 0 -x EXTERNAL_PA_DRIVE_VGA` — amp OFF + a modest
                           fixed exciter-drive VGA for an external duty-rated PA.

These tests assert, across transmit_burst / transmit_iq_file / transmit_sweep /
the interactive CLI:
  - FLAT builds `-a 0` + the reduced VGA; EXTERNAL_PA builds `-a 0` + exciter VGA.
  - MAX is `-a 1 -x <tx_gain>` and is the default (None => MAX, byte-identical).
  - every profile's VGA is <= MAX_TX_VGA_GAIN (47).
  - a bad/unknown profile falls back SAFE to MAX (documented safe default).
  - the device-pin (`-d`), fail-closed pinning, and stop-poll behavior are
    UNCHANGED across all profiles (the profile never bypasses a gate).

No real HackRF: subprocess.Popen/run, the device lock and build_noise_iq are all
stubbed.

Run: pytest field-bridge/test_hackrf_jam_profile.py -v
"""
from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path

import pytest

import hackrf_jam as hj

HERE = Path(__file__).resolve().parent
TX_SERIAL = "930cCAFEBABE"
ALLOW_ENV = "HACKRF_ALLOW_UNPINNED_TX"


# --------------------------------------------------------------------------
# small helpers to read the -a / -x values out of a captured argv
# --------------------------------------------------------------------------
def _amp_of(cmd) -> str:
    return cmd[cmd.index("-a") + 1]


def _vga_of(cmd) -> str:
    return cmd[cmd.index("-x") + 1]


@pytest.fixture(autouse=True)
def _no_real_radio(monkeypatch):
    """Never touch a real device lock or build a real IQ buffer."""
    monkeypatch.setattr(hj, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(hj, "build_noise_iq", lambda *a, **k: b"\x00\x00")


class FakeProc:
    """A hackrf_transfer that never exits on its own — only terminate()/kill()
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


def _spy_popen(monkeypatch):
    """Replace subprocess.Popen with a spy recording every argv, returning a
    FakeProc that only our terminate ends."""
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(hj.subprocess, "Popen", fake_popen)
    return calls


# ==========================================================================
# 1) _tx_amp_vga_args — the single command-construction helper
# ==========================================================================
def test_amp_vga_max_is_byte_identical_default():
    # None (unspecified) and "max" both => today's exact `-x <tx_gain> -a 1`.
    assert hj._tx_amp_vga_args(None, 20) == ["-x", "20", "-a", "1"]
    assert hj._tx_amp_vga_args(hj.JAM_PROFILE_MAX, 20) == ["-x", "20", "-a", "1"]
    # Max-power case (the CLI --max-gain / operator-set 47).
    assert hj._tx_amp_vga_args(None, 47) == ["-x", "47", "-a", "1"]


def test_amp_vga_flat_amp_off_reduced_vga():
    frag = hj._tx_amp_vga_args(hj.JAM_PROFILE_FLAT, 47)
    assert _amp_of(frag) == "0"                        # on-board amp OFF
    assert _vga_of(frag) == str(hj.FLAT_TX_VGA)        # fixed reduced VGA
    assert hj.FLAT_TX_VGA < hj.MAX_TX_VGA_GAIN         # genuinely lower peak
    # tx_gain is IGNORED for FLAT (fixed VGA), regardless of what is passed.
    assert hj._tx_amp_vga_args(hj.JAM_PROFILE_FLAT, 10) == frag


def test_amp_vga_external_pa_amp_off_exciter_vga():
    frag = hj._tx_amp_vga_args(hj.JAM_PROFILE_EXTERNAL_PA, 47)
    assert _amp_of(frag) == "0"                             # amp OFF
    assert _vga_of(frag) == str(hj.EXTERNAL_PA_DRIVE_VGA)   # fixed exciter drive
    assert hj.EXTERNAL_PA_DRIVE_VGA < hj.MAX_TX_VGA_GAIN


def test_amp_vga_every_profile_clamped_to_ceiling():
    # No profile may exceed MAX_TX_VGA_GAIN=47 on the -x value.
    for prof in (None, "max", "flat", "external_pa", "bogus"):
        for gain in (0, 20, 47, 48, 99, 1000):
            frag = hj._tx_amp_vga_args(prof, gain)
            assert int(_vga_of(frag)) <= hj.MAX_TX_VGA_GAIN, (prof, gain, frag)


def test_amp_vga_max_clamps_over_ceiling_gain():
    # Defence-in-depth: a MAX gain ABOVE the ceiling is clamped to 47 (the radio
    # caps there anyway); in-range gains are passed through verbatim.
    assert hj._tx_amp_vga_args("max", 99) == ["-x", "47", "-a", "1"]


@pytest.mark.parametrize("bad", ["bogus", "", "  ", "MAXX", None, 123, "flatt"])
def test_unknown_profile_falls_back_to_max(bad):
    # Safe documented default: unknown/None/blank/non-string => MAX (today's
    # fully-gated behavior), never a silent lower-power mode.
    assert hj._normalize_jam_profile(bad) == hj.JAM_PROFILE_MAX
    # ...and the built fragment is the MAX fragment (amp ON).
    assert _amp_of(hj._tx_amp_vga_args(bad, 20)) == "1"


@pytest.mark.parametrize("prof,norm", [
    ("max", "max"), ("MAX", "max"), ("  flat ", "flat"),
    ("Flat", "flat"), ("EXTERNAL_PA", "external_pa"),
])
def test_normalize_accepts_known_case_insensitive(prof, norm):
    assert hj._normalize_jam_profile(prof) == norm


def test_default_profile_constant_is_max():
    assert hj.DEFAULT_JAM_PROFILE == hj.JAM_PROFILE_MAX


# ==========================================================================
# 2) transmit_burst — profile threads into the built command
# ==========================================================================
def _run_continuous_burst(monkeypatch, profile):
    calls = _spy_popen(monkeypatch)
    stop = hj.threading.Event()
    stop.set()  # terminate on the first supervise poll -> returns promptly
    result = hj.transmit_burst(2450.0, 500.0, None, 20, stop_event=stop,
                               profile=profile)
    return result, calls


def test_burst_max_default_amp_on(monkeypatch):
    result, calls = _run_continuous_burst(monkeypatch, None)  # default => MAX
    assert result["ok"] is True and result["stopped_early"] is True
    cmd = calls[0]
    assert _amp_of(cmd) == "1"
    assert _vga_of(cmd) == "20"
    assert "-R" in cmd  # continuous loop flag unchanged


def test_burst_flat_amp_off_reduced_vga(monkeypatch):
    result, calls = _run_continuous_burst(monkeypatch, "flat")
    assert result["ok"] is True
    cmd = calls[0]
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.FLAT_TX_VGA)
    assert "-R" in cmd


def test_burst_external_pa_amp_off_exciter_vga(monkeypatch):
    result, calls = _run_continuous_burst(monkeypatch, "external_pa")
    assert result["ok"] is True
    cmd = calls[0]
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.EXTERNAL_PA_DRIVE_VGA)


def test_burst_stop_poll_unchanged_across_profiles(monkeypatch):
    # The kill-switch (stop_event) still terminates the live process for EVERY
    # profile — the profile never touches the stop path.
    for prof in (None, "max", "flat", "external_pa"):
        calls = _spy_popen(monkeypatch)
        stop = hj.threading.Event()
        stop.set()
        result = hj.transmit_burst(2450.0, 500.0, None, 20, stop_event=stop,
                                   profile=prof)
        assert result["stopped_early"] is True, prof


# ==========================================================================
# 3) transmit_iq_file — profile threads into the built command
# ==========================================================================
def test_iq_file_flat_and_max(monkeypatch, tmp_path):
    iq = tmp_path / "x.iq"
    iq.write_bytes(b"\x00\x00")

    for prof, exp_amp, exp_vga in [
        (None, "1", "20"),
        ("flat", "0", str(hj.FLAT_TX_VGA)),
        ("external_pa", "0", str(hj.EXTERNAL_PA_DRIVE_VGA)),
    ]:
        calls = _spy_popen(monkeypatch)
        stop = hj.threading.Event()
        stop.set()
        result = hj.transmit_iq_file(str(iq), 915.0, None, 20, stop_event=stop,
                                     profile=prof)
        assert result["ok"] is True, prof
        cmd = calls[0]
        assert _amp_of(cmd) == exp_amp, (prof, cmd)
        assert _vga_of(cmd) == exp_vga, (prof, cmd)
        assert "-R" in cmd  # continuous flag preserved


# ==========================================================================
# 4) transmit_sweep — profile threads into every dwell's command
# ==========================================================================
def _run_one_dwell_sweep(monkeypatch, profile):
    """Let exactly one real dwell run (capturing its argv) then stop."""
    captured = []
    n = {"c": 0}

    class Done(FakeProc):
        def poll(self):
            return 0  # dwell exits immediately

    def fake_popen(cmd, **kwargs):
        captured.append(list(cmd))
        n["c"] += 1
        return Done()

    monkeypatch.setattr(hj.subprocess, "Popen", fake_popen)
    result = hj.transmit_sweep(
        2400.0, 2483.5, 500.0, 20, step_mhz=20.0, dwell_ms=1.0,
        duration_s=None, tx_halt_check=lambda: n["c"] >= 1, profile=profile)
    return result, captured


def test_sweep_max_default_amp_on(monkeypatch):
    result, captured = _run_one_dwell_sweep(monkeypatch, None)
    assert result["ok"] is True
    assert captured, "expected one dwell to have run"
    cmd = captured[0]
    assert _amp_of(cmd) == "1"
    assert _vga_of(cmd) == "20"
    assert cmd.count("-f") == 1  # a sweep dwell still targets one center


def test_sweep_flat_amp_off_reduced_vga(monkeypatch):
    result, captured = _run_one_dwell_sweep(monkeypatch, "flat")
    assert result["ok"] is True
    cmd = captured[0]
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.FLAT_TX_VGA)


def test_sweep_external_pa_amp_off_exciter_vga(monkeypatch):
    result, captured = _run_one_dwell_sweep(monkeypatch, "external_pa")
    assert result["ok"] is True
    cmd = captured[0]
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.EXTERNAL_PA_DRIVE_VGA)


# ==========================================================================
# 5) fresh-import: device-pin (-d) and fail-closed pinning UNCHANGED by profile
# ==========================================================================
def _load_module(monkeypatch, serial):
    """Import a FRESH copy of hackrf_jam.py with HACKRF_TX_SERIAL set/unset
    (read once at import), mirroring test_hackrf_jam_device_pin.py."""
    if serial is None:
        monkeypatch.delenv("HACKRF_TX_SERIAL", raising=False)
    else:
        monkeypatch.setenv("HACKRF_TX_SERIAL", serial)
    spec = importlib.util.spec_from_file_location(
        f"hackrf_jam_profile_{serial}", str(HERE / "hackrf_jam.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _spy_popen_mod(monkeypatch, mod):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(mod, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(mod, "build_noise_iq", lambda *a, **k: b"\x00\x00")
    return calls


def test_pinned_flat_carries_device_flag_and_amp_off(monkeypatch):
    # Pinning (`-d <serial>`) is independent of the profile: a FLAT transmit on a
    # pinned box still carries -d AND uses amp-off/reduced-VGA.
    mod = _load_module(monkeypatch, TX_SERIAL)
    monkeypatch.delenv(ALLOW_ENV, raising=False)  # pinned path ignores the opt-out
    calls = _spy_popen_mod(monkeypatch, mod)

    stop = mod.threading.Event()
    stop.set()
    result = mod.transmit_burst(2450.0, 500.0, None, 20, stop_event=stop,
                                profile="flat")
    assert result["ok"] is True
    cmd = calls[0]
    assert "-d" in cmd and cmd[cmd.index("-d") + 1] == TX_SERIAL
    assert cmd[cmd.index("-a") + 1] == "0"
    assert cmd[cmd.index("-x") + 1] == str(mod.FLAT_TX_VGA)


@pytest.mark.parametrize("prof", ["max", "flat", "external_pa"])
def test_unpinned_refuses_for_every_profile(monkeypatch, prof):
    # The fail-closed pinning gate is BEFORE command construction: an unpinned
    # transmit (no serial, no opt-out) refuses and spawns NOTHING for EVERY
    # profile — the profile can never bypass this safety gate.
    mod = _load_module(monkeypatch, None)
    monkeypatch.delenv(ALLOW_ENV, raising=False)
    calls = _spy_popen_mod(monkeypatch, mod)

    result = mod.transmit_burst(2450.0, 500.0, None, 20, profile=prof)
    assert result["ok"] is False
    assert "HACKRF_TX_SERIAL" in result["error"]
    assert calls == [], "REFUSE path must spawn no hackrf_transfer for any profile"


# ==========================================================================
# 6) interactive CLI --profile
# ==========================================================================
def _run_cli(monkeypatch, argv_extra):
    """Drive main() through the bounded (subprocess.run) path, capturing argv."""
    captured = {"cmd": None}

    class _Done:
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        return _Done()

    monkeypatch.setattr(hj, "hackrf_device_lock",
                        lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(hj.subprocess, "run", fake_run)
    monkeypatch.setattr("builtins.input", lambda *a, **k: "TRANSMIT")
    monkeypatch.setenv("CEMA_AUTHORIZED_RANGE", "1")
    monkeypatch.setattr(sys, "argv",
                        ["hackrf_jam.py", "--band", "2g4", "--duration-s", "1",
                         "--i-confirm-authorized-range", *argv_extra])
    hj.main()
    return captured["cmd"]


def test_cli_default_profile_is_max(monkeypatch):
    cmd = _run_cli(monkeypatch, [])  # no --profile => MAX
    assert cmd is not None
    assert _amp_of(cmd) == "1"
    assert _vga_of(cmd) == "20"  # default --tx-gain


def test_cli_flat_profile_amp_off_reduced_vga(monkeypatch):
    cmd = _run_cli(monkeypatch, ["--profile", "flat"])
    assert cmd is not None
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.FLAT_TX_VGA)


def test_cli_external_pa_profile(monkeypatch):
    cmd = _run_cli(monkeypatch, ["--profile", "external_pa"])
    assert cmd is not None
    assert _amp_of(cmd) == "0"
    assert _vga_of(cmd) == str(hj.EXTERNAL_PA_DRIVE_VGA)


def test_cli_max_gain_still_max_profile(monkeypatch):
    # --max-gain sets tx_gain 47; default profile MAX => `-a 1 -x 47`.
    cmd = _run_cli(monkeypatch, ["--max-gain"])
    assert cmd is not None
    assert _amp_of(cmd) == "1"
    assert _vga_of(cmd) == "47"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
