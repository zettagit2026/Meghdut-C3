#!/usr/bin/env python3
"""Governed wrapper around the OPERATOR'S OWN jammer (the "Operator Jam" mode).

=============================================================================
WHAT THIS IS
=============================================================================
The operator supplied their own GNU Radio barrage jammer, which lives OUTSIDE
this repo/deploy tree on the transmit host (default /CEMA/operator-jam/,
overridable via OPERATOR_JAM_DIR):

  cema_base.py : class CEMA_Jammer(gr.top_block) — a GNU Radio flowgraph:
                 analog.noise_source_c(GR_GAUSSIAN) -> blocks.multiply_const_cc(12.0)
                 -> osmosdr.sink("hackrf=0"), with set_sample_rate / set_center_freq /
                 set_gain(47) / set_if_gain(47) / set_bb_gain(20) and a 20 Msps rate.
                 main(freq, rate) starts it then blocks on input("Press Enter...").
  cema_433.py / cema_915.py / cema_24.py / cema_58.py :
                 thin per-band callers -> main(FREQ, 20e6) for
                 435 MHz / 915 MHz / 2.45 GHz / 5.8 GHz.

Those files are used AS-IS and are NEVER modified or copied into this repo.
This wrapper *imports* their CEMA_Jammer and runs their exact flowgraph so the
operator can A/B their own waveform against MEGHDUT's built-in barrage jam
(field-bridge/hackrf_jam.py) — same governed spine for both.

=============================================================================
TWO — AND ONLY TWO — SAFETY OVERRIDES THIS WRAPPER IMPOSES
=============================================================================
Their files are run unmodified; the wrapper changes nothing about their
waveform, gains, or band centers. It imposes exactly two governed overrides,
BOTH here in the wrapper (never by editing their code):

  1. DEVICE PIN (the one safety override on the flowgraph itself).
     Their osmosdr sink is created as "hackrf=0" — an INDEX-based selector
     that, on this dual-radio host, could grab the RX *detection* radio
     instead of the TX antenna. This wrapper forces the sink onto the pinned
     TX unit by SERIAL (HACKRF_TX_SERIAL, e.g. the ...930c TX unit) by
     intercepting the single osmosdr.sink(...) construction and rewriting ONLY
     its device string to "hackrf=<serial>". Everything else about their
     flowgraph is left byte-for-byte identical. If no TX serial is configured,
     this wrapper FAILS CLOSED — it refuses to build the flowgraph rather than
     fall back to "hackrf=0" (which could key the RX radio).

  2. BOUNDED, NON-INTERACTIVE RUN.
     Their main() ends on a blocking input("Press Enter...") — an unattended,
     WS-triggered transmit could otherwise run forever with no terminal to
     press Enter at. This wrapper NEVER calls their main(); it start()s their
     top_block, runs it for a HARD-CAPPED bounded duration (the SAME
     MAX_DURATION_S cap the governed MEGHDUT jam uses), polling an abort /
     tx_halt signal the whole time, then stop()s and wait()s it. An
     EMERGENCY ABORT mid-burst terminates the transmission immediately.

If GNU Radio / gr-osmosdr (or their module) is not importable, every entry
point here fails cleanly with a clear "Operator mode unavailable: ..."
(OperatorJamUnavailable) — it NEVER crashes the host process and NEVER falls
through to any ungoverned transmit path.

This wrapper does NOT relax any of the governed jam gates — the arm token,
jam-confirm token, live range-authorization lease, commander role and
tx_halt checks are all still enforced by field-bridge/operator_jam_bridge.py
(which reuses field-bridge/jam_bridge.py's spine unchanged).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("operator-jam-wrapper")

# Reuse the EXACT same hard duration cap the governed MEGHDUT jam enforces, so
# "Operator Jam" can never transmit for longer than the built-in barrage jam.
try:
    from hackrf_jam import MAX_DURATION_S  # same 10s cap as the governed jam
except Exception:  # pragma: no cover - hackrf_jam always importable on the bridge host
    MAX_DURATION_S = 10.0

# Their proven flowgraph parameters (from /CEMA/operator-jam/cema_base.py),
# reproduced here ONLY as documentation / for the informational log line — the
# actual values come from running THEIR unmodified CEMA_Jammer. The wrapper
# does not re-implement the flowgraph from these constants.
OPERATOR_SAMPLE_RATE_HZ = 20_000_000  # 20 Msps
OPERATOR_MULTIPLY_CONST = 12.0
OPERATOR_RF_GAIN = 47
OPERATOR_IF_GAIN = 47
OPERATOR_BB_GAIN = 20

# The four bands the operator's per-band callers cover (cema_433/915/24/58.py).
# Values match field-bridge/hackrf_jam.py's BAND_PRESETS_MHZ for these keys so
# the two jam modes target identical center frequencies.
OPERATOR_BANDS: Dict[str, float] = {
    "433": 435.0,
    "915": 915.0,
    "2g4": 2450.0,
    "5g8": 5800.0,
}

# Where the operator's unmodified files live on the TX host. Overridable via
# the systemd EnvironmentFile (OPERATOR_JAM_DIR); NEVER copied into this repo.
DEFAULT_OPERATOR_JAM_DIR = "/CEMA/operator-jam"


class OperatorJamUnavailable(RuntimeError):
    """Raised when the operator jammer cannot be run in this environment —
    GNU Radio / gr-osmosdr / the operator module missing, or no TX serial
    pinned. Callers translate this into a clean 'failed' ack; it must NEVER be
    swallowed into an ungoverned transmit."""


# ---------------------------------------------------------------------------
# Safety override #1: device pin (pure, unit-testable, no GNU Radio needed)
# ---------------------------------------------------------------------------
def _pin_device_string(original: Optional[str], serial: str) -> str:
    """Rewrite an osmosdr device-args string so the hackrf device is selected
    by the pinned TX SERIAL rather than by index ("hackrf=0").

    Any existing ``hackrf=...`` token is replaced with ``hackrf=<serial>``;
    any other tokens (rare) are preserved. If no hackrf token is present, one
    is added. The result NEVER contains an index-based ``hackrf=0`` selector.
    """
    if not serial:
        raise OperatorJamUnavailable(
            "no TX serial pinned (HACKRF_TX_SERIAL unset) — refusing to build the "
            "operator flowgraph rather than fall back to index-based 'hackrf=0', "
            "which could key the RX detection radio")
    forced = f"hackrf={serial}"
    tokens = [t.strip() for t in (original or "").split(",") if t.strip()]
    replaced = False
    out = []
    for t in tokens:
        if t.lower().startswith("hackrf="):
            out.append(forced)
            replaced = True
        else:
            out.append(t)
    if not replaced:
        out.insert(0, forced)
    return ",".join(out)


def _extract_device_arg(args: tuple, kwargs: dict) -> str:
    """Best-effort recovery of the device string their code passed to
    osmosdr.sink(...), whether positional ("hackrf=0") or keyword
    (args="hackrf=0" / device="hackrf=0"). Defaults to the known hardcoded
    "hackrf=0" if it cannot be found, so the pin still forces the serial."""
    for a in args:
        if isinstance(a, str):
            return a
    for key in ("args", "device", "dev"):
        v = kwargs.get(key)
        if isinstance(v, str):
            return v
    return "hackrf=0"


def _make_pinned_sink(orig_sink: Callable[..., Any], serial: str) -> Callable[..., Any]:
    """Wrap osmosdr.sink so EVERY constructed sink is forced onto the pinned TX
    serial. This is the single interception point for safety override #1: their
    unmodified code calls osmosdr.sink("hackrf=0"); we transparently rebuild it
    as osmosdr.sink("hackrf=<serial>"). Never lets an index-based selector
    through."""
    def pinned(*args, **kwargs):
        requested = _extract_device_arg(args, kwargs)
        forced = _pin_device_string(requested, serial)
        if requested != forced:
            log.warning(
                "Operator Jam device pin: rewriting osmosdr sink device %r -> %r "
                "(forcing the pinned TX unit, never index-0/RX radio).",
                requested, forced)
        return orig_sink(forced)
    return pinned


# ---------------------------------------------------------------------------
# Availability + flowgraph construction (needs GNU Radio + the operator module)
# ---------------------------------------------------------------------------
def operator_jam_dir() -> str:
    return os.environ.get("OPERATOR_JAM_DIR") or DEFAULT_OPERATOR_JAM_DIR


def _ensure_on_path() -> None:
    d = operator_jam_dir()
    if d not in sys.path:
        sys.path.insert(0, d)


def ensure_operator_jam_available() -> None:
    """Import GNU Radio, gr-osmosdr and the operator's cema_base module, or
    raise OperatorJamUnavailable with a clear, operator-facing message. Does
    NOT construct or start anything — pure importability check."""
    _ensure_on_path()
    try:
        import gnuradio  # noqa: F401
        from gnuradio import gr  # noqa: F401
    except Exception as e:
        raise OperatorJamUnavailable("GNU Radio not installed") from e
    try:
        import osmosdr  # noqa: F401
    except Exception as e:
        raise OperatorJamUnavailable("gr-osmosdr not installed") from e
    try:
        import cema_base  # noqa: F401  (the operator's UNMODIFIED file)
    except Exception as e:
        raise OperatorJamUnavailable(
            f"operator module cema_base not importable from {operator_jam_dir()!r} "
            f"(set OPERATOR_JAM_DIR to the directory holding the operator's files)"
        ) from e


def _construct_operator_block(cls, freq_hz: float, rate_hz: float):
    """Construct the operator's CEMA_Jammer defensively across the likely
    constructor shapes (their per-band callers invoke main(freq, rate), so the
    block most plausibly takes freq+rate). Tries positional, then common
    keyword names, then a no-arg construct + setters. Raises
    OperatorJamUnavailable if none work — never returns a half-built block."""
    attempts = (
        lambda: cls(freq_hz, rate_hz),
        lambda: cls(freq_hz),
        lambda: cls(center_freq=freq_hz, sample_rate=rate_hz),
        lambda: cls(freq=freq_hz, rate=rate_hz),
        lambda: cls(),
    )
    last_err: Optional[Exception] = None
    for make in attempts:
        try:
            tb = make()
        except Exception as e:
            last_err = e
            continue
        # Best-effort: (re)assert the target center frequency / rate if the
        # block exposes the operator's own setters, so a no-arg construct still
        # lands on the requested band. Never fatal if a setter is absent.
        for setter, val in (("set_center_freq", freq_hz), ("set_sample_rate", rate_hz)):
            fn = getattr(tb, setter, None)
            if callable(fn):
                try:
                    fn(val)
                except Exception:
                    pass
        return tb
    raise OperatorJamUnavailable(
        f"could not construct operator CEMA_Jammer (last error: {last_err})")


def _build_operator_flowgraph(freq_mhz: float, serial: str):
    """Import the operator's UNMODIFIED CEMA_Jammer and construct it with the
    device pin (safety override #1) applied. GNU Radio must be present."""
    ensure_operator_jam_available()
    _ensure_on_path()
    import osmosdr
    import cema_base

    freq_hz = float(freq_mhz) * 1e6
    orig_sink = osmosdr.sink
    # Force the pinned TX serial for the single sink their __init__ builds, then
    # restore the original factory immediately — the smallest possible window,
    # and the ONLY change we impose on their flowgraph.
    osmosdr.sink = _make_pinned_sink(orig_sink, serial)
    try:
        tb = _construct_operator_block(cema_base.CEMA_Jammer, freq_hz,
                                       float(OPERATOR_SAMPLE_RATE_HZ))
    finally:
        osmosdr.sink = orig_sink
    return tb


# ---------------------------------------------------------------------------
# Safety override #2: bounded, abortable run (unit-testable via a fake factory)
# ---------------------------------------------------------------------------
def run_operator_jam(
    band: str,
    serial: str,
    duration_s: float,
    *,
    abort_event: Optional[threading.Event] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    on_started: Optional[Callable[[Any], None]] = None,
    flowgraph_factory: Optional[Callable[[float, str], Any]] = None,
    poll_interval_s: float = 0.1,
) -> Dict[str, Any]:
    """Run the operator's jammer for a HARD-CAPPED bounded duration, pinned to
    the TX serial, abortable at any moment.

    Returns a result dict matching field-bridge/hackrf_jam.transmit_burst()'s
    shape so the bridge can ack it identically:
        {"ok": bool, "stopped_early": bool, "error": Optional[str]}

    Never raises for an expected condition (GNU Radio missing, unknown band,
    no TX serial) — those come back as ok=False with a clear error. Any real
    GNU Radio runtime error is likewise captured into the result.
    """
    if band not in OPERATOR_BANDS:
        return {"ok": False, "stopped_early": False,
                "error": f"operator jam: unsupported band {band!r} "
                         f"(supports {sorted(OPERATOR_BANDS)})"}
    duration = min(float(duration_s), MAX_DURATION_S)
    freq_mhz = OPERATOR_BANDS[band]
    factory = flowgraph_factory or _build_operator_flowgraph

    try:
        tb = factory(freq_mhz, serial)
    except OperatorJamUnavailable as e:
        return {"ok": False, "stopped_early": False,
                "error": f"Operator mode unavailable: {e}"}
    except Exception as e:  # defensive: never let construction crash the bridge
        return {"ok": False, "stopped_early": False,
                "error": f"operator flowgraph construction failed: {e}"}

    stopped_early = False
    try:
        tb.start()
        if on_started is not None:
            try:
                on_started(tb)
            except Exception as e:
                log.warning("operator jam on_started callback error: %s", e)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if (abort_event is not None and abort_event.is_set()) or \
               (tx_halt_check is not None and tx_halt_check()):
                stopped_early = True
                break
            remaining = deadline - time.monotonic()
            time.sleep(max(0.0, min(poll_interval_s, remaining)))
    except Exception as e:
        # Best-effort teardown, then report the failure.
        _safe_stop(tb)
        return {"ok": False, "stopped_early": stopped_early,
                "error": f"operator jam runtime error: {e}"}

    _safe_stop(tb)
    return {"ok": True, "stopped_early": stopped_early, "error": None}


def _safe_stop(tb) -> None:
    for meth in ("stop", "wait"):
        fn = getattr(tb, meth, None)
        if callable(fn):
            try:
                fn()
            except Exception as e:
                log.warning("operator jam flowgraph %s() error: %s", meth, e)
