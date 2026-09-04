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

  2. NON-INTERACTIVE, ALWAYS-STOPPABLE RUN.
     Their main() ends on a blocking input("Press Enter...") — which cannot
     work over a WS-driven bridge with no terminal. This wrapper NEVER calls
     their main(); it start()s their top_block and runs it CONTINUOUSLY (the
     commander directive: no artificial auto-stop timer — their own main() was
     also continuous-until-Enter, and this restores that spirit), OR for a
     bounded duration when one is explicitly given. Throughout, it polls an
     abort / tx_halt signal on every iteration and stop()s + wait()s the
     flowgraph the instant either fires. An EMERGENCY ABORT / Stand Down
     terminates the transmission immediately. This is the one invariant that is
     NOT relaxed: the operator can always stop it instantly — "no timing limit"
     means "runs until the operator stops it", never "cannot be switched off".

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

# MAX_DURATION_S is retained ONLY as a non-binding default import (no longer a
# hard cap — per the commander directive there is no artificial auto-stop
# timer). _is_continuous decides whether a request is continuous (operator-
# stopped) or a bounded window. Both come from hackrf_jam so the two jam modes
# share one definition of "continuous".
try:
    from hackrf_jam import MAX_DURATION_S, MAX_TX_VGA_GAIN, _is_continuous
except Exception:  # pragma: no cover - hackrf_jam always importable on the bridge host
    MAX_DURATION_S = 10.0
    # HackRF TX VGA hardware ceiling (dB). NOT an artificial cap — it is the
    # device's own maximum; the operator gain is clamped only to [0, this].
    MAX_TX_VGA_GAIN = 47

    def _is_continuous(duration_s) -> bool:
        if duration_s is None:
            return True
        try:
            return float(duration_s) <= 0.0
        except (TypeError, ValueError):
            return False

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


class _PinSentinel:
    """Records whether the wrapper's OWN device-pinned sink was ACTUALLY invoked
    during the operator flowgraph construction, and the exact forced device
    string(s) it was called with.

    This is the import-form-independent proof that safety override #1 (the
    device pin) took effect. The monkeypatch on ``osmosdr.sink`` only fires for
    the module-attribute call form ``osmosdr.sink(...)``. If the operator's file
    were ever edited to ``from osmosdr import sink`` (binding the name at import,
    BEFORE this wrapper patches the module attribute), that call would bypass the
    patch silently — an unmodified ``hackrf=0`` could then reach a dual-radio
    host and key the RX detection radio. Only the pinned sink writes here, so a
    bypass leaves ``invocations == 0`` and the wrapper FAILS CLOSED regardless of
    how their code imported ``sink``. Guarded by a lock — cheap and correct for
    the single-construction window (constructions never overlap in practice)."""

    __slots__ = ("_lock", "invocations", "forced_device", "sinks")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.invocations = 0
        self.forced_device: Optional[str] = None
        self.sinks: list = []

    def record(self, forced: str, sink: Any) -> None:
        with self._lock:
            self.invocations += 1
            self.forced_device = forced
            self.sinks.append(sink)


def _make_pinned_sink(orig_sink: Callable[..., Any], serial: str,
                      sentinel: Optional["_PinSentinel"] = None) -> Callable[..., Any]:
    """Wrap osmosdr.sink so EVERY constructed sink is forced onto the pinned TX
    serial. This is the single interception point for safety override #1: their
    unmodified code calls osmosdr.sink("hackrf=0"); we transparently rebuild it
    as osmosdr.sink("hackrf=<serial>"). Never lets an index-based selector
    through.

    When a ``sentinel`` is supplied, every invocation is recorded (count + the
    exact forced device string + the constructed sink object) so the caller can
    PROVE the pin actually took — see _enforce_device_pin. The sentinel is
    optional so existing unit tests that call this factory directly are
    unaffected."""
    def pinned(*args, **kwargs):
        requested = _extract_device_arg(args, kwargs)
        forced = _pin_device_string(requested, serial)
        if requested != forced:
            log.warning(
                "Operator Jam device pin: rewriting osmosdr sink device %r -> %r "
                "(forcing the pinned TX unit, never index-0/RX radio).",
                requested, forced)
        sink = orig_sink(forced)
        if sentinel is not None:
            sentinel.record(forced, sink)
        return sink
    return pinned


def _readback_serial(sink: Any) -> Optional[str]:
    """Best-effort: recover the SERIAL the gr-osmosdr sink actually bound to, via
    serial-specific introspection accessors ONLY. Returns a non-empty string when
    a serial accessor yields one, else None (introspection unavailable — the
    sentinel remains the real guarantee, so a None here is NOT a failure). Fully
    guarded: no accessor error can escape.

    Deliberately restricted to accessors whose value is genuinely a SERIAL
    (``get_device_serial`` / ``get_serial``). Ambiguous non-serial identifiers
    (device name / device args / index selector) are NOT consulted, so a value
    returned here truly is a serial and the caller's 'definite mismatch -> fail
    closed' is literally true (a human-readable device NAME can never be
    mislabelled as a serial mismatch)."""
    for name in ("get_device_serial", "get_serial"):
        try:
            attr = getattr(sink, name, None)
            if attr is None:
                continue
            val = attr() if callable(attr) else attr
            if isinstance(val, bytes):
                val = val.decode(errors="replace")
            if isinstance(val, str) and val.strip():
                return val
        except Exception:
            continue
    return None


def _enforce_device_pin(sentinel: "_PinSentinel", serial: str) -> None:
    """FAIL CLOSED unless the device pin is PROVEN applied. Import-form-independent:
    it relies on the sentinel recorded by the wrapper's own pinned sink, not on
    how the operator's code imported ``sink``.

      * patched sink invoked ZERO times -> the pin did not apply (import-form
        bypass, or their code built the sink some other way) -> refuse.
      * invoked, but the forced string does NOT carry the pinned TX serial ->
        refuse.
      * belt-and-suspenders read-back: if a sink exposes its bound device/serial
        via introspection AND it definitely mismatches the pinned serial ->
        refuse. If introspection is unavailable/ambiguous, SKIP silently.

    Never proceeds to transmit on a pin that cannot be proven."""
    if sentinel.invocations == 0:
        raise OperatorJamUnavailable(
            "device-pin not applied — refusing to transmit "
            "(the pinned osmosdr sink was never invoked; the operator's code may "
            "have bound `sink` before the patch via `from osmosdr import sink`, "
            "bypassing the device pin — an unpinned build could key the RX radio)")
    # Exact-token match, mirroring how _pin_device_string emits the selector
    # ("hackrf=<serial>", comma-joined with any other tokens). Checking the exact
    # token as a standalone device selector — not a bare substring — means a
    # serial appearing incidentally inside some unrelated arg can never satisfy
    # the pin, while still being robust to extra tokens after a comma.
    forced_tokens = [t.strip() for t in (sentinel.forced_device or "").split(",")]
    if f"hackrf={serial}" not in forced_tokens:
        raise OperatorJamUnavailable(
            "device-pin not applied — refusing to transmit "
            f"(pinned sink was invoked but its forced device "
            f"{sentinel.forced_device!r} does not carry the pinned hackrf=<serial> "
            f"selector)")
    for sink in sentinel.sinks:
        rb = _readback_serial(sink)
        if rb is not None and serial not in rb:
            raise OperatorJamUnavailable(
                "device-pin read-back mismatch — refusing to transmit "
                f"(sink reports bound device {rb!r}, not the pinned TX serial)")


def _construct_with_device_pin(osmosdr_module: Any, block_cls: Any,
                               freq_hz: float, rate_hz: float, serial: str):
    """The device-pin construction window (safety override #1), factored out so
    it is unit-testable without real GNU Radio. Monkeypatches
    ``osmosdr_module.sink`` to the pinned+recording sink for the duration of the
    block construction, ENFORCES that the pin was actually applied (fail-closed,
    import-form-independent), then restores the original sink factory in finally
    exactly as before — the smallest possible window, the ONLY change imposed on
    their flowgraph."""
    orig_sink = osmosdr_module.sink
    sentinel = _PinSentinel()
    osmosdr_module.sink = _make_pinned_sink(orig_sink, serial, sentinel)
    try:
        tb = _construct_operator_block(block_cls, freq_hz, rate_hz)
        _enforce_device_pin(sentinel, serial)
    finally:
        osmosdr_module.sink = orig_sink
    # Expose the exact osmosdr sink object(s) the pinned construction captured so
    # the operator-adjustable TX gain (Directive #1) can be driven onto the REAL
    # sink — their CEMA_Jammer sets set_gain/set_if_gain/set_bb_gain on the sink
    # inside __init__, so the gain accessors live on the sink, not necessarily on
    # the top_block. Best-effort attribute; never fatal (a gr.top_block accepts
    # arbitrary attributes; guarded regardless).
    try:
        tb._cema_tx_sinks = list(sentinel.sinks)
    except Exception:  # pragma: no cover - top_block should always accept an attr
        pass
    return tb


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
    # Force the pinned TX serial for the single sink their __init__ builds, prove
    # the pin actually took (fail-closed if it did not), then restore the
    # original factory immediately — the smallest possible window, and the ONLY
    # change we impose on their flowgraph.
    return _construct_with_device_pin(osmosdr, cema_base.CEMA_Jammer, freq_hz,
                                      float(OPERATOR_SAMPLE_RATE_HZ), serial)


# ---------------------------------------------------------------------------
# Safety override #2: bounded, abortable run (unit-testable via a fake factory)
# ---------------------------------------------------------------------------
def _apply_operator_tx_gain(tb: Any, tx_gain: Optional[int]) -> Optional[int]:
    """Directive #1 (operator-adjustable TX gain, NO artificial cap): drive the
    operator-requested TX gain onto the flowgraph's osmosdr sink.

    The operator's CEMA_Jammer hardcodes its gains (set_gain(47)/set_if_gain(47)/
    set_bb_gain(20)); this lets the operator raise/lower the TX gain from the app
    instead of being stuck at the flowgraph's baked-in value. The value is
    clamped ONLY to the HackRF TX VGA hardware ceiling [0, MAX_TX_VGA_GAIN=47] —
    that is the device's own maximum, NOT an artificial software cap.

    The osmosdr sink exposes the gain accessors (set_gain = RF/TX gain,
    set_if_gain = TXVGA/IF gain); both are set to the operator value so the
    hardware actually responds up to the ceiling. The waveform (noise source ×12,
    sample rate, band center) is left byte-for-byte untouched — only the gain
    knob moves. Targets the real sink object(s) captured during the device-pin
    construction (tb._cema_tx_sinks), falling back to the top_block itself (GRC
    flowgraphs expose set_gain proxies).

    Returns the clamped value actually applied, or None when no gain was
    requested (tx_gain is None) or no setter could be found. NEVER raises — a
    missing/failing setter must not crash a TX (the operator's own baked-in gain
    then stands), same fail-open-on-nicety / fail-closed-on-safety discipline as
    the rest of this wrapper (gain is not a safety gate; the device pin and the
    abort/tx_halt stop are)."""
    if tx_gain is None:
        return None
    try:
        g = max(0, min(int(tx_gain), MAX_TX_VGA_GAIN))
    except (TypeError, ValueError):
        log.warning("operator jam: non-numeric tx_gain %r ignored — the operator "
                    "flowgraph's own baked-in gain stands.", tx_gain)
        return None
    # Prefer the real osmosdr sink(s) captured by the device-pin construction;
    # fall back to the top_block (a GRC flowgraph proxies set_gain to its sink).
    targets = list(getattr(tb, "_cema_tx_sinks", None) or [])
    targets.append(tb)
    for tgt in targets:
        hit = False
        for setter in ("set_gain", "set_if_gain"):
            fn = getattr(tgt, setter, None)
            if callable(fn):
                try:
                    fn(g)
                    hit = True
                except Exception as e:
                    log.warning("operator jam: gain setter %s(%d) failed: %s", setter, g, e)
        if hit:
            log.warning("Operator Jam TX gain set to %d dB (operator-adjustable, "
                        "clamped to the HackRF TX VGA ceiling %d dB — no artificial cap).",
                        g, MAX_TX_VGA_GAIN)
            return g
    log.warning("operator jam: no set_gain/set_if_gain accessor found on the "
                "flowgraph or its sink — requested TX gain %d dB NOT applied; the "
                "operator waveform's own baked-in gain stands.", g)
    return None


def run_operator_jam(
    band: str,
    serial: str,
    duration_s: float,
    *,
    tx_gain: Optional[int] = None,
    abort_event: Optional[threading.Event] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    on_started: Optional[Callable[[Any], None]] = None,
    flowgraph_factory: Optional[Callable[[float, str], Any]] = None,
    poll_interval_s: float = 0.1,
) -> Dict[str, Any]:
    """Run the operator's jammer pinned to the TX serial, abortable at any
    moment. CONTINUOUS by default (duration_s a continuous sentinel: None /
    <=0) — the flowgraph runs until the operator stops it (abort_event /
    tx_halt_check), exactly as their own main() blocked until Enter. A positive
    duration_s runs a bounded window instead. There is NO artificial cap on the
    duration (commander directive).

    SAFETY INVARIANT (never relaxed): abort_event AND tx_halt_check are polled
    on EVERY loop iteration, so EMERGENCY ABORT / Stand Down / tx_halt stop an
    in-progress operator jam immediately — a jammer that cannot be switched off
    must never be built.

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
    continuous = _is_continuous(duration_s)
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

    # Directive #1: drive the operator-adjustable TX gain onto the sink BEFORE
    # starting the flowgraph. Clamped only to the HackRF TX VGA hardware ceiling
    # (0-47 dB), never an artificial cap; leaves the operator's waveform
    # otherwise untouched. No-op (baked-in gain stands) when tx_gain is None.
    _apply_operator_tx_gain(tb, tx_gain)

    def _stop_now() -> bool:
        return (abort_event is not None and abort_event.is_set()) or \
               (tx_halt_check is not None and tx_halt_check())

    stopped_early = False
    try:
        tb.start()
        if on_started is not None:
            try:
                on_started(tb)
            except Exception as e:
                log.warning("operator jam on_started callback error: %s", e)
        # continuous -> no deadline (only an abort/tx_halt ends it); bounded ->
        # exactly the requested window (uncapped). Either way, poll the stop
        # signals every poll_interval_s so a stop is honored promptly.
        deadline = None if continuous else time.monotonic() + float(duration_s)
        while True:
            if _stop_now():
                stopped_early = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if deadline is None:
                time.sleep(poll_interval_s)
            else:
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
