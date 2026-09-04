"""Unit tests for field-bridge/operator_jam_wrapper.py — the governed wrapper
around the operator's OWN jammer.

Covers the two safety overrides and the fail-closed behavior WITHOUT any
GNU Radio / gr-osmosdr / real HackRF:

  1. DEVICE PIN — the osmosdr sink is always built for the pinned TX serial,
     never index-based "hackrf=0" (which could key the RX detection radio).
  2. BOUNDED / ABORTABLE — the flowgraph runs for at most MAX_DURATION_S and
     stops immediately on abort / tx_halt (verified with a fake flowgraph and
     a fake clock, so no real time passes and no radio is touched).
  3. GNU-Radio-missing — every entry point fails cleanly with a clear
     "Operator mode unavailable: ..." rather than crashing or falling through
     to an ungoverned transmit.

Run: pytest field-bridge/test_operator_jam_wrapper.py -v
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import operator_jam_wrapper as w

TX_SERIAL = "0000000000000000930c"  # the pinned TX unit serial (…930c)


# --------------------------------------------------------------------------
# Safety override #1: device pin
# --------------------------------------------------------------------------
def test_pin_device_string_replaces_index_selector():
    assert w._pin_device_string("hackrf=0", TX_SERIAL) == f"hackrf={TX_SERIAL}"


def test_pin_device_string_replaces_any_index():
    assert w._pin_device_string("hackrf=1", TX_SERIAL) == f"hackrf={TX_SERIAL}"


def test_pin_device_string_adds_token_when_absent():
    # A bare/other args string still gets the pinned hackrf serial forced in.
    out = w._pin_device_string("", TX_SERIAL)
    assert out == f"hackrf={TX_SERIAL}"


def test_pin_device_string_preserves_other_tokens():
    out = w._pin_device_string("hackrf=0,buffers=32", TX_SERIAL)
    tokens = out.split(",")
    assert f"hackrf={TX_SERIAL}" in tokens
    assert "buffers=32" in tokens
    # The index selector must be gone as a STANDALONE token (substring check is
    # unsafe here — a serial can itself begin with "0", e.g. …930c).
    assert "hackrf=0" not in tokens


def test_pin_device_string_fails_closed_without_serial():
    # No TX serial -> refuse (never fall back to hackrf=0 / RX radio).
    with pytest.raises(w.OperatorJamUnavailable):
        w._pin_device_string("hackrf=0", "")


def test_make_pinned_sink_forces_serial_positional():
    captured = {}

    def fake_orig(dev):
        captured["dev"] = dev
        return "SINK"

    pinned = w._make_pinned_sink(fake_orig, TX_SERIAL)
    result = pinned("hackrf=0")  # exactly what their unmodified code passes
    assert result == "SINK"
    assert captured["dev"] == f"hackrf={TX_SERIAL}"
    assert captured["dev"] != "hackrf=0"
    assert "0" != captured["dev"].split("hackrf=", 1)[1]


def test_make_pinned_sink_forces_serial_keyword():
    captured = {}

    def fake_orig(dev):
        captured["dev"] = dev

    pinned = w._make_pinned_sink(fake_orig, TX_SERIAL)
    pinned(args="hackrf=0")  # keyword form
    assert captured["dev"] == f"hackrf={TX_SERIAL}"


def test_make_pinned_sink_forces_serial_even_with_no_args():
    captured = {}

    def fake_orig(dev):
        captured["dev"] = dev

    pinned = w._make_pinned_sink(fake_orig, TX_SERIAL)
    pinned()  # defaults to the known-hardcoded hackrf=0, still forced to serial
    assert captured["dev"] == f"hackrf={TX_SERIAL}"


# --------------------------------------------------------------------------
# Safety override #1 (self-enforcing pin): the pin must be PROVEN applied, or
# the wrapper FAILS CLOSED — regardless of how the operator's code imported the
# osmosdr `sink` name. These exercise the construction window without any real
# GNU Radio, using a fake osmosdr module + a fake CEMA_Jammer.
# --------------------------------------------------------------------------
class FakeSink:
    """Stand-in for a gr-osmosdr sink; remembers the device string it was built
    with. Exposes NO serial-introspection accessor, so read-back is ambiguous
    and correctly SKIPPED (the sentinel remains the guarantee)."""
    def __init__(self, dev):
        self.dev = dev


def _fake_osmosdr():
    """A fake `osmosdr` module whose module-attribute `sink` is what the pinned
    wrapper monkeypatches — exactly like the real gr-osmosdr module."""
    mod = types.SimpleNamespace()
    mod.sink = lambda dev="hackrf=0": FakeSink(dev)
    return mod


def test_pin_import_form_bypass_is_caught_fail_closed():
    # Simulate `from osmosdr import sink`: the operator's __init__ binds a
    # reference to the ORIGINAL osmosdr.sink at import time — BEFORE the wrapper
    # patches the module attribute — and calls THAT captured reference with the
    # raw index selector. The monkeypatch on osmosdr.sink is therefore never hit,
    # so the sentinel stays at zero invocations and the wrapper must refuse.
    osmo = _fake_osmosdr()
    captured_before_patch = osmo.sink  # <- the `from osmosdr import sink` binding

    class FakeJammerImportForm:
        def __init__(self, freq, rate):
            # Bypasses the patched module attribute entirely.
            self.sink = captured_before_patch("hackrf=0")

    with pytest.raises(w.OperatorJamUnavailable) as ei:
        w._construct_with_device_pin(osmo, FakeJammerImportForm,
                                     915e6, 20e6, TX_SERIAL)
    assert "refusing to transmit" in str(ei.value)
    # And the original factory was restored (no lingering monkeypatch).
    assert osmo.sink("hackrf=0").dev == "hackrf=0"


def test_pin_happy_path_module_attribute_form_records_pinned_serial():
    # Their REAL form: `self.sink = osmosdr.sink("hackrf=0")` — the module
    # attribute is read at CALL time, so the wrapper's patch intercepts it. The
    # sink must come back forced to the pinned serial, the block constructs, and
    # (implicitly) the sentinel confirmed the pin so no exception is raised.
    osmo = _fake_osmosdr()

    class FakeJammerModuleForm:
        def __init__(self, freq, rate):
            self.sink = osmo.sink("hackrf=0")  # module-attribute form (real)

    tb = w._construct_with_device_pin(osmo, FakeJammerModuleForm,
                                      915e6, 20e6, TX_SERIAL)
    assert isinstance(tb, FakeJammerModuleForm)
    # The pin actually forced the serial (no false positive on the happy path).
    assert tb.sink.dev == f"hackrf={TX_SERIAL}"
    assert tb.sink.dev != "hackrf=0"
    # Factory restored after the construction window.
    assert osmo.sink("hackrf=0").dev == "hackrf=0"


def test_pin_readback_definite_mismatch_fails_closed():
    # Belt-and-suspenders: if a sink DOES expose its bound serial via
    # introspection and it definitely disagrees with the pin, refuse — even
    # though the patched sink was invoked. (Simulates a sink that ignored the
    # forced args and bound the wrong radio.)
    osmo = types.SimpleNamespace()

    class LyingSink:
        def __init__(self, dev):
            self.dev = dev
        def get_device_serial(self):
            return "0000000000000000a063"  # the RX radio serial, NOT the TX pin

    osmo.sink = lambda dev="hackrf=0": LyingSink(dev)

    class FakeJammer:
        def __init__(self, freq, rate):
            self.sink = osmo.sink("hackrf=0")

    with pytest.raises(w.OperatorJamUnavailable) as ei:
        w._construct_with_device_pin(osmo, FakeJammer, 915e6, 20e6, TX_SERIAL)
    assert "read-back mismatch" in str(ei.value)


def test_pin_readback_ambiguous_is_skipped_not_failed():
    # If introspection is unavailable/ambiguous (no serial accessor), read-back
    # must be SKIPPED silently — the sentinel is the real guarantee. FakeSink has
    # no serial accessor, so a correctly-pinned build must still succeed.
    osmo = _fake_osmosdr()

    class FakeJammer:
        def __init__(self, freq, rate):
            self.sink = osmo.sink("hackrf=0")

    tb = w._construct_with_device_pin(osmo, FakeJammer, 915e6, 20e6, TX_SERIAL)
    assert tb.sink.dev == f"hackrf={TX_SERIAL}"


def test_enforce_device_pin_exact_token_not_incidental_substring():
    # The forced-device check must be an EXACT `hackrf=<serial>` token match, not
    # a bare substring: a serial appearing incidentally inside an unrelated arg
    # (and NO real `hackrf=<serial>` selector) must NOT satisfy the pin.
    sentinel = w._PinSentinel()
    # Sink was "invoked" but the serial only appears inside an unrelated token —
    # there is no standalone `hackrf=<serial>` selector.
    sentinel.record(f"hackrf=0,label={TX_SERIAL}", object())
    with pytest.raises(w.OperatorJamUnavailable) as ei:
        w._enforce_device_pin(sentinel, TX_SERIAL)
    assert "does not carry" in str(ei.value)


def test_enforce_device_pin_accepts_token_with_extra_args():
    # Robust to extra tokens after a comma: as long as the exact
    # `hackrf=<serial>` selector is present as a standalone token, the pin holds.
    sentinel = w._PinSentinel()
    sentinel.record(f"hackrf={TX_SERIAL},buffers=32", object())
    # Must NOT raise (no serial-introspection accessor on a plain object -> skip).
    w._enforce_device_pin(sentinel, TX_SERIAL)


# --------------------------------------------------------------------------
# Safety override #2: bounded, abortable run (fake flowgraph + fake clock)
# --------------------------------------------------------------------------
class FakeTopBlock:
    def __init__(self):
        self.events = []

    def start(self):
        self.events.append("start")

    def stop(self):
        self.events.append("stop")

    def wait(self):
        self.events.append("wait")


class FakeClock:
    """Deterministic monotonic clock: sleep() advances it, so bounded loops
    complete instantly with no wall-clock time and no real radio."""
    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += max(0.0, s)


def _install_fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(w.time, "sleep", clock.sleep)
    return clock


def test_run_operator_jam_caps_duration(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    tb = FakeTopBlock()
    start = clock.now
    result = w.run_operator_jam(
        "915", TX_SERIAL, duration_s=100.0,  # asks for 100s
        flowgraph_factory=lambda freq, serial: tb,
    )
    elapsed = clock.now - start
    assert result == {"ok": True, "stopped_early": False, "error": None}
    # HARD cap: never ran longer than MAX_DURATION_S regardless of the request.
    assert elapsed <= w.MAX_DURATION_S + 0.5
    assert elapsed >= w.MAX_DURATION_S - 0.5
    assert tb.events[0] == "start"
    assert "stop" in tb.events and "wait" in tb.events


def test_run_operator_jam_uses_correct_band_freq(monkeypatch):
    _install_fake_clock(monkeypatch)
    seen = {}

    def factory(freq_mhz, serial):
        seen["freq"] = freq_mhz
        seen["serial"] = serial
        return FakeTopBlock()

    w.run_operator_jam("5g8", TX_SERIAL, 1.0, flowgraph_factory=factory)
    assert seen["freq"] == w.OPERATOR_BANDS["5g8"] == 5800.0
    assert seen["serial"] == TX_SERIAL


def test_run_operator_jam_aborts_immediately_on_event(monkeypatch):
    _install_fake_clock(monkeypatch)
    tb = FakeTopBlock()
    ev = threading.Event()
    ev.set()  # abort already asserted
    result = w.run_operator_jam(
        "915", TX_SERIAL, 10.0, abort_event=ev,
        flowgraph_factory=lambda f, s: tb,
    )
    assert result["ok"] is True
    assert result["stopped_early"] is True
    assert "stop" in tb.events  # in-progress burst was actually torn down


def test_run_operator_jam_stops_on_tx_halt(monkeypatch):
    _install_fake_clock(monkeypatch)
    tb = FakeTopBlock()
    result = w.run_operator_jam(
        "915", TX_SERIAL, 10.0, tx_halt_check=lambda: True,
        flowgraph_factory=lambda f, s: tb,
    )
    assert result["stopped_early"] is True
    assert "stop" in tb.events


def test_run_operator_jam_calls_on_started(monkeypatch):
    _install_fake_clock(monkeypatch)
    tb = FakeTopBlock()
    started = {}
    w.run_operator_jam(
        "915", TX_SERIAL, 1.0,
        on_started=lambda block: started.setdefault("tb", block),
        flowgraph_factory=lambda f, s: tb,
    )
    assert started.get("tb") is tb


def test_run_operator_jam_rejects_unknown_band(monkeypatch):
    _install_fake_clock(monkeypatch)
    result = w.run_operator_jam(
        "gps_l1", TX_SERIAL, 1.0,  # GNSS band NOT supported by the operator jammer
        flowgraph_factory=lambda f, s: FakeTopBlock(),
    )
    assert result["ok"] is False
    assert "unsupported band" in result["error"]


# --------------------------------------------------------------------------
# Safety override #3: GNU-Radio-missing clean fail (real absence on this host)
# --------------------------------------------------------------------------
def test_ensure_available_raises_when_gnuradio_missing():
    # GNU Radio is not installed in the Mac sandbox — the check must fail
    # cleanly with a clear operator-facing message, never a raw ImportError.
    with pytest.raises(w.OperatorJamUnavailable) as ei:
        w.ensure_operator_jam_available()
    assert "not installed" in str(ei.value).lower() or "not importable" in str(ei.value).lower()


def test_run_operator_jam_default_factory_fails_cleanly_without_gnuradio():
    # With NO injected factory, run_operator_jam falls back to the real
    # flowgraph builder, which needs GNU Radio. It must come back as a clean
    # ok=False "Operator mode unavailable: ..." — NOT crash, NOT transmit.
    result = w.run_operator_jam("915", TX_SERIAL, 1.0)
    assert result["ok"] is False
    assert result["stopped_early"] is False
    assert result["error"].startswith("Operator mode unavailable:")
