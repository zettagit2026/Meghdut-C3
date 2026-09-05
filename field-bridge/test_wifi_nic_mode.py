#!/usr/bin/env python3
"""SAFETY-CRITICAL tests for the Wi-Fi NIC mode-arbiter (wifi_nic_mode.py).

These prove the fail-closed spine WITHOUT a real NIC: the ONLY real-subprocess
seam (`_run`) is injected as a recording fake on every call, so NO real `iw` /
`ip` / `dhclient` is ever executed. The load-bearing safety proofs here are:

  * the arbiter REFUSES any iface that is not the pinned WIFI_TX_IFACE, and
    refuses entirely when WIFI_TX_IFACE is blank/unset — running NO subprocess
    (it can therefore NEVER touch the detection NIC);
  * every mode-switch fails closed on any step failure / exception;
  * argument validation rejects a malformed iface / bssid before any command;
  * the mode-arbiter lock serializes NIC2;
  * restore_safe tears the association down to the safe baseline.

Run: pytest field-bridge/test_wifi_nic_mode.py -q
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import wifi_nic_mode as wnm  # noqa: E402

TX_IFACE = "wlan1"
PIN_ENV = "WIFI_TX_IFACE"
GOOD_BSSID = "AA:BB:CC:11:22:33"
GOOD_SSID = "TELLO-9F1C2A"


class _RecordingRunner:
    """Records every argv LIST it is asked to run; returns a scripted exit code.

    fail_on: if set, the argv whose element `fail_on` appears in returns rc=1.
    raise_on: if set, the argv containing `raise_on` raises (models a missing
    binary / timeout)."""
    def __init__(self, fail_on=None, raise_on=None):
        self.calls = []
        self.fail_on = fail_on
        self.raise_on = raise_on

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        if self.raise_on is not None and self.raise_on in argv:
            raise FileNotFoundError(f"{self.raise_on}: not found")
        if self.fail_on is not None and self.fail_on in argv:
            return 1, f"simulated failure on {self.fail_on}"
        return 0, ""


@pytest.fixture
def pinned(monkeypatch):
    """Pin NIC2 to TX_IFACE — the governed/production posture. The pin gate reads
    env live, so this needs no re-import."""
    monkeypatch.setenv(PIN_ENV, TX_IFACE)


@pytest.fixture
def unpinned(monkeypatch):
    monkeypatch.delenv(PIN_ENV, raising=False)


# ---------------------------------------------------------------------------
# ensure_monitor — happy path + fail-closed
# ---------------------------------------------------------------------------
def test_ensure_monitor_success(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor(TX_IFACE, channel=6, runner=r)
    assert res["ok"] is True and res["error"] is None
    assert res["mode"] == "monitor"
    # Correct standard sequence: down -> set type monitor -> up -> set channel.
    assert r.calls == [
        ["ip", "link", "set", TX_IFACE, "down"],
        ["iw", "dev", TX_IFACE, "set", "type", "monitor"],
        ["ip", "link", "set", TX_IFACE, "up"],
        ["iw", "dev", TX_IFACE, "set", "channel", "6"],
    ]


def test_ensure_monitor_no_channel_skips_channel_set(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor(TX_IFACE, runner=r)
    assert res["ok"] is True
    assert ["iw", "dev", TX_IFACE, "set", "type", "monitor"] in r.calls
    assert not any("channel" in c for c in r.calls)


def test_ensure_monitor_fails_closed_on_step_failure(pinned):
    r = _RecordingRunner(fail_on="monitor")  # `set type monitor` step exits 1
    res = wnm.ensure_monitor(TX_IFACE, runner=r)
    assert res["ok"] is False
    assert "exited 1" in res["error"]
    # Stopped at the failing step: `up` was never run.
    assert ["ip", "link", "set", TX_IFACE, "up"] not in r.calls


def test_ensure_monitor_fails_closed_on_exception(pinned):
    r = _RecordingRunner(raise_on="iw")  # first `iw` invocation raises
    res = wnm.ensure_monitor(TX_IFACE, runner=r)
    assert res["ok"] is False
    assert "failed" in res["error"]


# ---------------------------------------------------------------------------
# ensure_managed_associated — happy path + fail-closed on associate / DHCP
# ---------------------------------------------------------------------------
def test_ensure_managed_associated_success(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated(TX_IFACE, GOOD_SSID, GOOD_BSSID, channel=6, runner=r)
    assert res["ok"] is True and res["error"] is None
    assert res["mode"] == "managed"
    assert ["iw", "dev", TX_IFACE, "set", "type", "managed"] in r.calls
    assert ["iw", "dev", TX_IFACE, "connect", GOOD_SSID, GOOD_BSSID] in r.calls
    assert ["dhclient", TX_IFACE] in r.calls


def test_ensure_managed_associated_fails_closed_on_associate_failure(pinned):
    r = _RecordingRunner(fail_on="connect")
    res = wnm.ensure_managed_associated(TX_IFACE, GOOD_SSID, GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "exited 1" in res["error"]
    # DHCP must NOT run once association failed.
    assert ["dhclient", TX_IFACE] not in r.calls


def test_ensure_managed_associated_fails_closed_on_dhcp_failure(pinned):
    r = _RecordingRunner(fail_on="dhclient")
    res = wnm.ensure_managed_associated(TX_IFACE, GOOD_SSID, GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "dhclient" in res["error"]


# ---------------------------------------------------------------------------
# SAFETY-CRITICAL: refuse a non-pinned iface — NEVER touch the detection NIC
# ---------------------------------------------------------------------------
def test_ensure_monitor_refuses_wrong_iface_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor("wlan0", channel=6, runner=r)  # detection NIC!
    assert res["ok"] is False
    assert "does not match the pinned WIFI_TX_IFACE" in res["error"]
    assert r.calls == []  # NOTHING was run against the detection NIC


def test_ensure_managed_refuses_wrong_iface_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated("wlan0", GOOD_SSID, GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "does not match the pinned WIFI_TX_IFACE" in res["error"]
    assert r.calls == []


def test_restore_safe_refuses_wrong_iface_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.restore_safe("wlan0", runner=r)
    assert res["ok"] is False
    assert r.calls == []


# ---------------------------------------------------------------------------
# SAFETY-CRITICAL: refuse when WIFI_TX_IFACE is blank / unset
# ---------------------------------------------------------------------------
def test_ensure_monitor_refuses_when_pin_unset(unpinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor("wlan1", channel=6, runner=r)
    assert res["ok"] is False
    assert "WIFI_TX_IFACE is not set" in res["error"]
    assert r.calls == []


def test_ensure_managed_refuses_when_pin_unset(unpinned):
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated("wlan1", GOOD_SSID, GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "WIFI_TX_IFACE is not set" in res["error"]
    assert r.calls == []


def test_ensure_monitor_refuses_when_pin_blank(monkeypatch):
    monkeypatch.setenv(PIN_ENV, "   ")  # whitespace-only == unset
    r = _RecordingRunner()
    res = wnm.ensure_monitor("wlan1", runner=r)
    assert res["ok"] is False
    assert "WIFI_TX_IFACE is not set" in res["error"]
    assert r.calls == []


def test_ensure_monitor_refuses_blank_iface_argument(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor("   ", runner=r)
    assert res["ok"] is False
    assert r.calls == []


# ---------------------------------------------------------------------------
# Argument validation — malformed iface / bssid rejected before any command
# ---------------------------------------------------------------------------
def test_malformed_iface_rejected_no_subprocess(monkeypatch):
    # Pin equals the malformed iface so the pin bind passes and we specifically
    # exercise the iface-shape validator.
    monkeypatch.setenv(PIN_ENV, "wlan1; rm -rf /")
    r = _RecordingRunner()
    res = wnm.ensure_monitor("wlan1; rm -rf /", runner=r)
    assert res["ok"] is False
    assert "well-formed network-interface name" in res["error"]
    assert r.calls == []


def test_malformed_bssid_rejected_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated(TX_IFACE, GOOD_SSID, "not-a-mac", runner=r)
    assert res["ok"] is False
    assert "well-formed MAC address" in res["error"]
    assert r.calls == []


def test_malformed_ssid_rejected_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated(TX_IFACE, "", GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "SSID" in res["error"]
    assert r.calls == []


@pytest.mark.parametrize("hostile_ssid", ["-w", "--help", "-", "-x"])
def test_leading_dash_ssid_refused_no_subprocess(pinned, hostile_ssid):
    """A DETECTED softAP SSID beginning with `-` (e.g. `-w`, `--help`) must be
    REFUSED before any command runs — passed unguarded as a positional argv
    token to `iw dev <iface> connect <ssid> <bssid>`, such an SSID could
    otherwise be consumed by iw's getopt as an OPTION rather than the SSID."""
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated(TX_IFACE, hostile_ssid, GOOD_BSSID, runner=r)
    assert res["ok"] is False
    assert "SSID" in res["error"]
    assert r.calls == []


@pytest.mark.parametrize("normal_ssid", ["TELLO-ABC123", "ANAFI-1234", GOOD_SSID])
def test_normal_ssid_still_passes_validation(pinned, normal_ssid):
    """Sanity check the leading-dash rejection doesn't collaterally break real
    drone softAP SSIDs."""
    r = _RecordingRunner()
    res = wnm.ensure_managed_associated(TX_IFACE, normal_ssid, GOOD_BSSID, runner=r)
    assert res["ok"] is True
    assert ["iw", "dev", TX_IFACE, "connect", normal_ssid, GOOD_BSSID] in r.calls


def test_bad_channel_rejected_no_subprocess(pinned):
    r = _RecordingRunner()
    res = wnm.ensure_monitor(TX_IFACE, channel="not-a-number", runner=r)
    assert res["ok"] is False
    assert "channel" in res["error"]
    assert r.calls == []


# ---------------------------------------------------------------------------
# restore_safe tears down association + returns to safe baseline
# ---------------------------------------------------------------------------
def test_restore_safe_tears_down_to_baseline(pinned):
    r = _RecordingRunner()
    res = wnm.restore_safe(TX_IFACE, runner=r)
    assert res["ok"] is True
    # Association torn down, then link cycled into monitor (safe baseline).
    assert ["iw", "dev", TX_IFACE, "disconnect"] in r.calls
    assert ["ip", "link", "set", TX_IFACE, "down"] in r.calls
    assert ["iw", "dev", TX_IFACE, "set", "type", "monitor"] in r.calls


def test_restore_safe_tolerates_disconnect_failure(pinned):
    """A NIC that was never associated -> disconnect exits non-zero, but the safe
    baseline still succeeds, so restore is ok."""
    r = _RecordingRunner(fail_on="disconnect")
    res = wnm.restore_safe(TX_IFACE, runner=r)
    assert res["ok"] is True  # best-effort teardown failure does not fail restore
    assert ["ip", "link", "set", TX_IFACE, "up"] in r.calls


def test_teardown_is_restore_safe():
    assert wnm.teardown is wnm.restore_safe


# ---------------------------------------------------------------------------
# Mode-arbiter lock serializes NIC2
# ---------------------------------------------------------------------------
def test_lock_serializes():
    acquired_order = []
    with wnm.wifi_nic_mode_lock():
        acquired_order.append("outer")
        # A second contender cannot acquire while the outer holder has it.
        with pytest.raises(wnm.WifiNicModeBusy):
            with wnm.wifi_nic_mode_lock(timeout_s=0.2):
                acquired_order.append("inner-should-not-happen")
    # Once released, it can be re-acquired.
    with wnm.wifi_nic_mode_lock(timeout_s=1.0):
        acquired_order.append("after-release")
    assert acquired_order == ["outer", "after-release"]


def test_lock_serializes_across_threads():
    order = []
    held = threading.Event()
    release = threading.Event()

    def holder():
        with wnm.wifi_nic_mode_lock():
            order.append("holder-acquired")
            held.set()
            release.wait(2.0)
        order.append("holder-released")

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(2.0)
    # While the holder thread has it, this thread is refused (Busy).
    with pytest.raises(wnm.WifiNicModeBusy):
        with wnm.wifi_nic_mode_lock(timeout_s=0.2):
            pass
    release.set()
    t.join(2.0)
    # Now free.
    with wnm.wifi_nic_mode_lock(timeout_s=1.0):
        order.append("main-acquired-after")
    assert order == ["holder-acquired", "holder-released", "main-acquired-after"]


# ---------------------------------------------------------------------------
# The default runner is the real subprocess seam (never invoked in these tests)
# ---------------------------------------------------------------------------
def test_default_runner_is_real_subprocess_seam():
    # Identity check only — we never call it, so no real iw/ip/dhclient runs.
    import subprocess
    assert wnm._run.__module__ == "wifi_nic_mode"
    assert callable(wnm._run)
    assert subprocess is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
