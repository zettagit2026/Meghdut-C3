#!/usr/bin/env python3
"""Operator-controlled RF link-disruption via HackRF TX.

TRANSMITS real RF. Implements CEMA "communication disruption" (RFI item
1.4) against the SiK 915MHz ISM band or DJI's 2.4/5.8GHz video/control
bands, by transmitting a band-limited noise waveform.

EFFECTIVENESS (commander directive, post spectrum-analyser field test):
There is NO artificial auto-stop duration/repeat cap here. A capped ~5s
burst lets a drone's FHSS+FEC control link re-sync and recover, and a
single fixed-center ~20MHz barrage only covers a fraction of an ~80MHz
hopping band. This module therefore supports:
  * CONTINUOUS transmission — runs until the OPERATOR stops it (never an
    unattended auto-stop timer). See transmit_burst()/transmit_iq_file()
    (loop-and-retransmit via hackrf_transfer -R) and the CLI --continuous.
  * SWEPT-BARRAGE (transmit_sweep) — steps the TX center frequency across a
    configurable band so a frequency-hopping control link is hit on every
    hop over the sweep's revisit interval (HackRF's instantaneous bandwidth
    is only ~20MHz; the sweep is what covers the full ~80MHz hop band).

SAFETY — the effect ALWAYS remains instantly stoppable by the operator.
Every transmit loop below polls BOTH a stop_event (set by EMERGENCY ABORT /
Stand Down via the governed bridges) AND an optional tx_halt_check on every
iteration, and terminates the live hackrf_transfer process promptly when
either fires. "No timing limit" means "runs until the operator stops it",
NOT "cannot be switched off".

SAFETY GATING (CLI) — refuses to run unless BOTH are true:
  1. env var CEMA_AUTHORIZED_RANGE=1
  2. --i-confirm-authorized-range passed on the command line
Only run this at STEAG under Army Signals spectrum authorization. The
governed bridges (jam_bridge.py / operator_jam_bridge.py) additionally
enforce the arm token, jam-confirm token, live range-authorization lease,
commander role, tx_halt, and the …930c TX device-pin (fail-closed).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hackrf_device_lock import HackrfDeviceBusy, hackrf_device_lock  # shared device mutex, see that module's docstring

# TX DEVICE PINNING (task #TX-pin): reserve the PA/antenna HackRF for TX only.
# When HACKRF_TX_SERIAL is set (jam_bridge.py's systemd EnvironmentFile sets it
# to the TX unit's serial), EVERY hackrf_transfer invocation below is (a)
# addressed to that specific unit via `-d <serial>` so a burst leaves the TX
# antenna and never index-0/whichever-responds-first, and (b) serialized behind
# that unit's OWN per-serial device lock (hackrf_device_lock(serial=...)), NOT
# the shared/default lock the RX consumers (hackrf_rx.py sweep, ml_classify_
# bridge.py gate-sweep/IQ capture on the SEPARATE RX unit) use. With RX pinned
# to the RX serial and TX pinned here to the TX serial, the two physical units
# never contend at all. Unset/empty (default) preserves the original
# "whichever HackRF responds first" behavior + shared default lock, so existing
# single-device deployments and the test suite (which never sets this) are
# byte-for-byte unaffected.
HACKRF_TX_SERIAL = os.environ.get("HACKRF_TX_SERIAL") or None


def _tx_device_args() -> list:
    """`-d <serial>` for hackrf_transfer when a TX unit is pinned, else []."""
    return ["-d", HACKRF_TX_SERIAL] if HACKRF_TX_SERIAL else []


# FAIL-CLOSED TX PINNING AT THE SOURCE (safety-critical). On the production box
# there are TWO HackRFs — …930c (PA + antenna) is the TX radiator, …a063 is the
# RX/detection unit. If HACKRF_TX_SERIAL is UNSET, hackrf_transfer runs with NO
# `-d` and grabs index-0 / whichever unit answers libusb first — which can be
# the RX radio, keying the wrong antenna (fratricide / wrong radiator). The
# governed bridges (jam_bridge.py / operator_jam_bridge.py) pin HACKRF_TX_SERIAL
# via their systemd EnvironmentFile, so production is pinned and this guard is a
# no-op there. But the shared primitive itself must REFUSE an unpinned transmit
# rather than silently fall back to index-0: no `hackrf_transfer` process is
# ever spawned on the refuse path.
#
# DEV OPT-OUT — HACKRF_ALLOW_UNPINNED_TX=1: legitimate single-HackRF development
# (and this repo's unit tests, which never own a real radio) explicitly permits
# the old unpinned behavior ONLY by setting this flag. Default (flag absent) =
# fail-closed. It is read LIVE from the environment at each transmit (NOT cached
# at import) so a dev/test can toggle it per-call, and — crucially — it is
# consulted ONLY when HACKRF_TX_SERIAL is unset: the SET-serial (governed /
# production) path never looks at it and is byte-for-byte unchanged.
HACKRF_ALLOW_UNPINNED_TX_ENV = "HACKRF_ALLOW_UNPINNED_TX"


def _tx_pinning_error() -> Optional[str]:
    """Fail-closed TX pinning gate, shared by every transmit entry point below.

    Returns None when the transmit is permitted (either HACKRF_TX_SERIAL is
    pinned — the governed/production path, unchanged — or the explicit
    HACKRF_ALLOW_UNPINNED_TX=1 dev opt-out is set), and a human-readable error
    string when the transmit must be REFUSED (no serial pinned, no opt-out).
    Never spawns a subprocess. When the dev opt-out IS in effect, emits a single
    one-line WARNING to stderr so an unpinned transmit can never be mistaken for
    a governed run."""
    if HACKRF_TX_SERIAL:
        return None  # pinned -> governed/production path, byte-for-byte unchanged
    if os.environ.get(HACKRF_ALLOW_UNPINNED_TX_ENV) == "1":
        print("WARNING: HACKRF_TX_SERIAL is unset — transmitting UNPINNED "
              "(HACKRF_ALLOW_UNPINNED_TX=1). Single-HackRF DEV ONLY; on a "
              "dual-radio box an unpinned transmit can key the RX antenna.",
              file=sys.stderr)
        return None
    return ("REFUSING TX (fail-closed): HACKRF_TX_SERIAL is not set. An unpinned "
            "hackrf_transfer would grab index-0 / whichever HackRF responds first "
            "and could key the RX radio. Pin the TX unit's serial via "
            "HACKRF_TX_SERIAL (the governed bridges do this through systemd), or "
            "set HACKRF_ALLOW_UNPINNED_TX=1 for explicit single-HackRF dev use.")


# RETAINED as a NON-binding default only (e.g. the CLI --duration-s default and
# backward-compatible imports in operator_jam_wrapper.py / jam_bridge.py). It is
# NO LONGER a hard auto-stop cap — per the commander directive there is no
# artificial timing limit; a continuous effect runs until the operator stops it
# (stop_event / tx_halt / Stand Down). Left defined so existing `from hackrf_jam
# import MAX_DURATION_S` callers keep working.
MAX_DURATION_S = 10.0
SAMPLE_RATE_HZ = 20_000_000  # 20 Msps, matches /CEMA/drone-kit/dronev5/cema/cema_base.py's
                              # proven RATE for these same bands.

# Length of ONE noise-IQ chunk built for a continuous / long transmission. The
# chunk is transmitted on a loop (hackrf_transfer -R) so we never materialize a
# multi-minute IQ buffer in memory; the loop is what makes the effect run
# indefinitely, and stop_event/tx_halt terminate it between/within chunks.
CONTINUOUS_CHUNK_S = 0.5

# Swept-barrage defaults. HackRF's instantaneous TX bandwidth is ~20MHz, so a
# single center only covers a slice of a wide hop band; the sweep retunes across
# the band every SWEEP_DEFAULT_STEP_MHZ with SWEEP_DEFAULT_DWELL_MS dwell so a
# hopping control link is hit on every hop over the sweep's revisit interval.
# HONEST NOTE: revisit_interval ≈ n_steps * dwell — a fast hopper is only denied
# on the fraction of hops that land in the currently-illuminated ~20MHz window
# during each dwell; a shorter dwell / narrower band improves revisit at the
# cost of per-step energy. These are tuning knobs, not guarantees.
SWEEP_DEFAULT_STEP_MHZ = 20.0
SWEEP_DEFAULT_DWELL_MS = 5.0
# FREQUENCY-SCOPE safety bounds — a SECOND, bridge-side layer mirroring the
# backend's deploy_jam sweep validation (backend/server.py: HACKRF_MIN/MAX_FREQ_MHZ,
# MAX_SWEEP_SPAN_MHZ). These are NOT timing/effectiveness caps (the commander
# removed all duration caps): they only bound WHERE the sweep may radiate so a
# malformed/hostile request can't fan the TX across the whole 1-6000MHz span and
# blanket aviation/GNSS/cellular. The span cap is GENEROUS (covers any single
# drone band); real drone-band jamming is unaffected. Defence-in-depth: the
# backend gate is primary; this is the belt-and-braces backstop if the bridge is
# ever driven directly.
# TODO(range-auth): the right long-term fix is a FREQUENCY-SCOPED range-auth
# lease the sweep must be a subset of; until then this static bound closes the
# blast-radius hole.
HACKRF_MIN_FREQ_MHZ = 1.0
HACKRF_MAX_FREQ_MHZ = 6000.0
MAX_SWEEP_SPAN_MHZ = 500.0
# The two common hop bands, as (start_mhz, stop_mhz) — 2.4GHz ISM (DJI/Wi-Fi/BT)
# and the 5.8GHz video band. Exposed for the CLI/bridge; any explicit range is
# accepted.
SWEEP_BAND_2G4 = (2400.0, 2483.5)
SWEEP_BAND_5G8 = (5725.0, 5875.0)

# Maximum HackRF TX gains for maximum radiated power (within the device's own
# limits): TX VGA (IF) gain caps at 47 dB. build the command with `-x 47` when
# the operator asks for max power. NOT auto-applied — the operator still sets
# tx_gain; this is the ceiling the UI/CLI expose.
MAX_TX_VGA_GAIN = 47


def _is_continuous(duration_s) -> bool:
    """True when the caller asked for a continuous (operator-stopped) run rather
    than a fixed-duration burst. Accepts None, <=0, or the string "continuous"
    as the continuous sentinel; any positive number is a bounded duration."""
    if duration_s is None:
        return True
    if isinstance(duration_s, str):
        return duration_s.strip().lower() in ("continuous", "cont", "", "0")
    try:
        return float(duration_s) <= 0.0
    except (TypeError, ValueError):
        return False


def _stop_requested(stop_event, tx_halt_check) -> bool:
    """Single place both continuous/sweep supervision loops poll to decide
    whether the operator has demanded a stop. Checks the EMERGENCY-ABORT
    stop_event AND an optional tx_halt_check callback — either one ends TX."""
    if stop_event is not None and stop_event.is_set():
        return True
    if tx_halt_check is not None:
        try:
            if tx_halt_check():
                return True
        except Exception:
            # A failing tx_halt probe must fail SAFE (treat as "stop"): never
            # let a broken predicate keep a jammer transmitting.
            return True
    return False


def _supervise_transfer(proc, deadline, stop_event, tx_halt_check, poll_s: float = 0.1) -> str:
    """Poll a live hackrf_transfer subprocess until it exits, its bounded
    deadline passes, or the operator demands a stop (stop_event / tx_halt).
    Returns one of: "exited" (process ended on its own), "deadline" (bounded
    window elapsed — normal completion), or "stopped" (operator abort/tx_halt —
    process was terminated). `deadline` is an absolute time.time() value, or
    None for a continuous run with no time bound (only a stop ends it)."""
    while True:
        if proc.poll() is not None:
            return "exited"
        if _stop_requested(stop_event, tx_halt_check):
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            return "stopped"
        if deadline is not None and time.time() > deadline:
            proc.kill()
            return "deadline"
        time.sleep(poll_s)

# GNSS-spoof ("soft-kill", Task #103) duration cap — deliberately much
# shorter than jamming's MAX_DURATION_S. See
# field-bridge/GNSS_SPOOF_ARCHITECTURE.md §2 for the full justification: a
# deception effect's failsafe trigger fires off a single bad position
# report, not sustained exposure, so there is no "more seconds = more
# effect" scaling once a fake fix is accepted — a short, hard cap is part of
# the safety design, not just a tuning knob. Lives here (not only in
# backend/server.py / gnss_spoof_bridge.py) so any direct caller of
# hackrf_jam.py has the same authoritative constant available.
GNSS_SPOOF_MAX_DURATION_S = 3.0

# Center frequencies from /CEMA/drone-kit/dronev5/cema/cema_{433,915,24,58}.py
# (already field-validated on this rig), exposed here as --band shortcuts.
#
# GNSS L1-band presets (RFI item, OPERATIONAL REQUIREMENTS.md): these target
# satellite navigation reception rather than the drone's comms/video link.
# All four are civil L1 signals clustered ~1561-1602 MHz:
#   - gps_l1:      GPS L1 C/A,      1575.42 MHz
#   - galileo_e1:  Galileo E1,      1575.42 MHz (co-located with GPS L1 —
#                  same center frequency, different modulation/PRN codes;
#                  a barrage-noise burst at this freq denies both at once,
#                  which is why they share one number here)
#   - beidou_b1:   BeiDou B1I,      1561.098 MHz
#   - glonass_l1:  GLONASS L1OF,    1602.0 MHz — this is the BASE frequency
#                  only. GLONASS (unlike GPS/Galileo/BeiDou) is FDMA: each
#                  satellite transmits on its own channel k in roughly
#                  [-7, +6], spaced 0.5625 MHz apart from this base
#                  (f_k = 1602.0 + k * 0.5625 MHz), so real GLONASS energy
#                  spans ~1598-1606 MHz, not a single line. A barrage burst
#                  centered here with bandwidth_khz widened accordingly
#                  covers the channel spread; this preset intentionally does
#                  not attempt per-satellite channel targeting.
#   - bt_2g4: Bluetooth Classic/BLE, 2442.0 MHz — center of the shared
#             2.4-2.4835 GHz ISM band. Bluetooth Classic frequency-hops
#             across 79x 1MHz channels and BLE across 40x 2MHz channels
#             WITHIN that same range that "2g4" (DJI video/control) already
#             targets — it is NOT a separate slice of spectrum. A barrage-
#             noise burst here is the same broad-band-noise-vs-hopper
#             approach already used for "2g4"/DJI (DJI OcuSync/Wi-Fi also
#             hops/spreads within 2.4-2.4835GHz); it denies Bluetooth by
#             raising the noise floor across the band it hops within, NOT by
#             tracking/following each hop in real time. This preset exists
#             for OPERATOR CLARITY (an explicit, correctly-labeled Bluetooth
#             target in the UI) — it is not new signal-generation logic, and
#             widening `bandwidth_khz` (already an operator-controlled
#             parameter) toward the ~83.5MHz full ISM-band width is what
#             actually improves odds of hitting a hopping Bluetooth link, not
#             this preset's center frequency alone. See jam_bridge.py /
#             frontend/src/pages/Jamming.jsx for how this label is surfaced.
BAND_PRESETS_MHZ = {
    "433": 435.0,
    "915": 915.0,
    "2g4": 2450.0,
    "bt_2g4": 2442.0,
    "5g8": 5800.0,
    "gps_l1": 1575.42,
    "galileo_e1": 1575.42,
    "beidou_b1": 1561.098,
    "glonass_l1": 1602.0,
}

# Full width of the shared 2.4GHz ISM band Bluetooth Classic/BLE hops within
# (2400.0-2483.5 MHz). Exposed as a named constant purely so callers/UI copy
# can recommend a bandwidth_khz value that plausibly covers the whole hop
# range, rather than the default 500kHz (which covers neither Bluetooth's
# hop spread nor DJI's) — NOT an enforced minimum; bandwidth_khz remains a
# free operator parameter, same as every other band.
BLUETOOTH_ISM_FULL_WIDTH_KHZ = 83_500.0

# Bands that deny satellite navigation reception rather than a comms/video
# link. Used by jam_bridge.py/backend/server.py purely for logging/labeling
# clarity — carries NO safety-gate weight of its own; the extra GNSS warning
# text lives in the frontend (frontend/src/pages/Jamming.jsx) as additional
# copy inside the SAME SafetyGate confirm flow, not a new gate.
GNSS_BANDS = frozenset({"gps_l1", "galileo_e1", "beidou_b1", "glonass_l1"})


def check_authorized(confirmed_flag: bool) -> None:
    if os.environ.get("CEMA_AUTHORIZED_RANGE") != "1" or not confirmed_flag:
        print("REFUSING: this transmits real RF. Requires env CEMA_AUTHORIZED_RANGE=1 "
              "AND --i-confirm-authorized-range. Only run at STEAG under Army Signals "
              "spectrum authorization.", file=sys.stderr)
        sys.exit(1)


def build_noise_iq(duration_s: float, bandwidth_khz: float, sample_rate: int = SAMPLE_RATE_HZ) -> bytes:
    """Band-limited complex noise, HackRF's native interleaved int8 IQ format."""
    n = int(duration_s * sample_rate)
    noise = (np.random.randn(n) + 1j * np.random.randn(n))
    # crude band-limiting: scale down and let the HackRF's baseband filter do the rest;
    # a real deployment would shape this with an actual FIR filter sized to bandwidth_khz.
    noise = noise / np.max(np.abs(noise)) * 100
    iq = np.empty(2 * n, dtype=np.int8)
    iq[0::2] = np.clip(noise.real, -127, 127).astype(np.int8)
    iq[1::2] = np.clip(noise.imag, -127, 127).astype(np.int8)
    return iq.tobytes()


def transmit_iq_file(
    iq_path: str,
    freq_mhz: float,
    duration_s: float,
    tx_gain: int,
    stop_event: Optional["threading.Event"] = None,
    on_started: Optional[Callable[["subprocess.Popen"], None]] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Shared subprocess-management / abort-mid-transmission mechanics,
    factored out of transmit_burst() (Task #103, see
    field-bridge/GNSS_SPOOF_ARCHITECTURE.md §1) so both the noise-burst path
    (transmit_burst(), which builds its own IQ bytes in-process) and the new
    GNSS-spoof path (field-bridge/gnss_spoof_bridge.py, which gets a
    pre-built IQ file from gnss_signal_synth.py) can share the SAME
    already-audited hackrf_transfer invocation, process-lifecycle, and
    EMERGENCY-ABORT-kill logic, instead of the spoof path growing a second,
    independently-reviewed bespoke TX primitive.

    Takes a PATH to an already-written IQ file (rather than raw bytes),
    since GNSS spoof IQ generation is comparatively expensive and the
    caller may want to generate it once and reuse/inspect the file. Runs
    `hackrf_transfer -t <iq_path> ...` — otherwise byte-for-byte the same
    command construction transmit_burst() has always used.

    stop_event / on_started / tx_halt_check: identical contract to
    transmit_burst()'s own parameters — see that function's docstring.

    CONTINUOUS: when duration_s is a continuous sentinel (None / <=0 /
    "continuous", per _is_continuous), the IQ file is transmitted ON A LOOP
    (hackrf_transfer -R) with NO time deadline — it runs until stop_event or
    tx_halt_check fires (the operator stopping it), then the live process is
    terminated. This lets a takeover command be re-emitted continuously until
    the operator stops it, exactly as the commander directed, while staying
    instantly abortable.

    Returns {"ok": bool, "error": Optional[str], "stopped_early": bool}.
    Never raises for TX-side failures — same convention as transmit_burst().
    """
    # Fail-closed TX pinning: refuse before spawning anything if no TX serial is
    # pinned and the dev opt-out is not set (see _tx_pinning_error).
    pin_err = _tx_pinning_error()
    if pin_err:
        return {"ok": False, "error": pin_err, "stopped_early": False}
    continuous = _is_continuous(duration_s)
    cmd = [
        "hackrf_transfer",
        "-t", iq_path,
        "-f", str(int(freq_mhz * 1_000_000)),
        "-s", str(SAMPLE_RATE_HZ),
        "-x", str(tx_gain),
        "-a", "1",
        *(["-R"] if continuous else []),  # loop the file until the operator stops it
        *_tx_device_args(),  # `-d <TX serial>` when a TX unit is pinned (see HACKRF_TX_SERIAL)
    ]
    # TX device pinning (task #TX-pin): serialize this TX burst behind the TX
    # unit's OWN per-serial lock so it can never collide with the RX consumers
    # on the SEPARATE RX unit, nor with a concurrent jam burst on this same TX
    # unit. Mirrors transmit_burst()'s lock discipline; previously this shared
    # helper took NO device lock at all — a latent collision gap for the
    # gnss_spoof path, which also drives the (now TX-pinned) HackRF.
    try:
        with hackrf_device_lock(serial=HACKRF_TX_SERIAL):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except FileNotFoundError:
                return {"ok": False, "error": "hackrf_transfer not found (install the `hackrf` package)",
                         "stopped_early": False}

            if on_started:
                on_started(proc)

            # continuous -> no deadline (only a stop ends it); bounded -> the
            # requested window + a 5s margin, matching the legacy behavior.
            deadline = None if continuous else time.time() + float(duration_s) + 5
            outcome = _supervise_transfer(proc, deadline, stop_event, tx_halt_check)
            stopped_early = (outcome == "stopped")
    except HackrfDeviceBusy as e:
        # Another TX/RX consumer holds this device — treat like any other
        # TX-side failure (never crash the caller), same convention as
        # transmit_burst().
        return {"ok": False, "error": f"HackRF device busy: {e}", "stopped_early": False}

    if stopped_early:
        return {"ok": True, "error": None, "stopped_early": True}

    rc = proc.returncode
    if rc == 0 or rc is None or rc < 0:
        # rc < 0 / None: killed by our own deadline above — expected
        # bounded-burst completion, not a failure (mirrors transmit_burst()'s
        # "pass # expected" handling of subprocess.TimeoutExpired).
        return {"ok": True, "error": None, "stopped_early": False}

    stderr = ""
    if proc.stderr:
        try:
            stderr = proc.stderr.read().decode(errors="replace")
        except Exception:
            pass
    return {"ok": False, "error": f"hackrf_transfer exited {rc}: {stderr[:300]}",
             "stopped_early": False}


def transmit_burst(
    freq_mhz: float,
    bandwidth_khz: float,
    duration_s: float,
    tx_gain: int,
    stop_event: Optional["threading.Event"] = None,
    on_started: Optional[Callable[["subprocess.Popen"], None]] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """Non-interactive, mechanical HackRF TX primitive — the exact same
    hackrf_transfer invocation as main()'s interactive CLI path below, minus
    the terminal input() prompt (which cannot work over a WS-driven bridge —
    there is no attached terminal to type into).

    CALLER RESPONSIBILITY, NOT THIS FUNCTION'S: authorization gating. This
    function does NOT check CEMA_AUTHORIZED_RANGE and does NOT ask for any
    confirmation — it assumes the caller has already independently verified
    both are satisfied. The two real callers are:
      * main() below (interactive CLI): checks env var + --i-confirm flag at
        argparse time, then still makes the operator type 'TRANSMIT' at the
        terminal before ever reaching a transmit call.
      * field-bridge/jam_bridge.py (WS-driven bridge, no terminal available):
        makes its own LIVE GET /api/range-authorization/status?effect=jam
        call to the backend at the moment of transmission (independently of
        any app-side check already performed; see
        backend/RANGE_AUTHORIZATION_REDESIGN.md — this replaced an older
        static CEMA_AUTHORIZED_RANGE bridge-host env var) AND requires the
        incoming WS request to carry a jam_confirm_token that the backend
        only mints at the exact moment an operator completes the app UI's
        two-step SafetyGate-style confirm (checklist + ARM & FIRE -> CONFIRM
        FIRE). That token is the digital equivalent of physically typing
        'TRANSMIT' — it cannot exist unless a human deliberately went through
        the real confirmation flow.

    Duration is NOT capped (commander directive: no artificial auto-stop
    timer). A positive duration_s is a bounded burst; a continuous sentinel
    (None / <=0 / "continuous", per _is_continuous) transmits a looped noise
    chunk (hackrf_transfer -R) with NO deadline — it runs until the operator
    stops it via stop_event / tx_halt_check. Either way the effect is always
    instantly stoppable (that is the one invariant that never changes).

    stop_event: if provided and set() while the burst is running, the
    underlying hackrf_transfer process is terminated early (used by
    jam_bridge.py to honor a live EMERGENCY ABORT mid-burst — the app's
    existing Tier-0 "stop all TX now" control must also be able to kill a
    real RF jam in progress, not just queued MAVLink frames). For a continuous
    jam this is the operator's stop control, polled on every loop iteration.

    tx_halt_check: optional zero-arg callable polled on EVERY supervise poll
    (via _supervise_transfer/_stop_requested); returning True terminates the
    burst immediately, independently of stop_event. This gives the single-center
    continuous path the SAME direct tx_halt backstop that transmit_sweep /
    transmit_iq_file / the operator paths already have (jam_bridge.py passes
    `lambda: self.tx_halted`), so a global EMERGENCY ABORT is honored even if the
    per-request stop_event was never wired. A failing probe fails SAFE (treated
    as "stop"). Omit it (None) to rely on stop_event alone.

    on_started: optional callback invoked with the live subprocess.Popen the
    instant the process is spawned, so the caller can store a handle to it
    (e.g. to terminate it from a different thread on EMERGENCY ABORT).

    Returns {"ok": bool, "error": Optional[str], "stopped_early": bool}.
    Never raises for TX-side failures (bad hackrf_transfer exit, missing
    binary) — those come back as ok=False with a reason so the caller can
    send an honest ack rather than crash the bridge process.

    NOTE: this function's own behavior/signature is UNCHANGED by the
    transmit_iq_file() extraction above (Task #103) — it still builds its
    own noise IQ bytes and writes them to its own temp file inline, exactly
    as before. It does NOT call transmit_iq_file() internally, specifically
    so the proven, already-audited jam TX path has zero code-path overlap
    with the new spoof path beyond the shared, mechanically-identical
    subprocess logic (kept as a deliberate near-duplicate rather than a
    shared call, per the architecture doc's explicit instruction not to
    change this function at all).

    DEVICE-ACCESS COORDINATION (task #152): jam_bridge.py is a long-lived
    service that can run concurrently with hackrf_rx.py's sweep loop and
    ml_classify_bridge.py's gate-check sweep/IQ-capture cycle against the
    SAME physical HackRF (only one open libusb handle supported at a time).
    Those two already coordinate via hackrf_device_lock() (see
    hackrf_device_lock.py, hackrf_rx.py's _one_sweep(), iq_capture.py's
    capture_iq()) — this jam TX path previously did not participate in that
    same mutex at all, a real latent collision risk with any of them.  The
    lock is acquired here around the ENTIRE hackrf_transfer subprocess
    lifecycle (spawn through poll/terminate/kill), not just the initial
    Popen call, because for a TX burst the "critical section" IS the whole
    live transmission, not a single quick blocking call like a one-shot
    sweep — the device must stay exclusively held for as long as
    hackrf_transfer actually has it open. If the lock cannot be acquired
    within LOCK_ACQUIRE_TIMEOUT_S (device busy with another sweep/capture),
    this returns ok=False with a clear error — same "log clearly, don't
    crash the caller" convention hackrf_rx.py already uses for
    HackrfDeviceBusy, so jam_bridge.py sends a normal "failed" jam_ack for
    this request rather than the whole service dying.
    """
    # Fail-closed TX pinning: refuse before building IQ or spawning anything if
    # no TX serial is pinned and the dev opt-out is not set (see
    # _tx_pinning_error). The SET-serial governed path is unaffected.
    pin_err = _tx_pinning_error()
    if pin_err:
        return {"ok": False, "error": pin_err, "stopped_early": False}
    continuous = _is_continuous(duration_s)
    # For a continuous or long run, build ONE short chunk and loop it on the
    # radio (-R) rather than materializing a giant IQ buffer. For a short
    # bounded burst, build exactly that many seconds and play it once (legacy
    # behavior — keeps small-burst callers byte-identical).
    if continuous or float(duration_s) > CONTINUOUS_CHUNK_S:
        chunk_s = CONTINUOUS_CHUNK_S
        loop = True
    else:
        chunk_s = float(duration_s)
        loop = False
    iq_bytes = build_noise_iq(chunk_s, bandwidth_khz)

    with tempfile.NamedTemporaryFile(suffix=".iq") as f:
        f.write(iq_bytes)
        f.flush()
        cmd = [
            "hackrf_transfer",
            "-t", f.name,
            "-f", str(int(freq_mhz * 1_000_000)),
            "-s", str(SAMPLE_RATE_HZ),
            "-x", str(tx_gain),
            "-a", "1",
            *(["-R"] if loop else []),  # loop the chunk (continuous / long burst)
            *_tx_device_args(),  # `-d <TX serial>` when a TX unit is pinned (see HACKRF_TX_SERIAL)
        ]
        try:
            # TX device pinning (task #TX-pin): hold the TX unit's OWN per-serial
            # lock (not the shared/RX default) so a jam burst on the TX HackRF
            # can never collide with the RX-sweep / ML-classify consumers pinned
            # to the separate RX HackRF. serial=None (unset) keeps the original
            # shared-default-lock behavior the test suite relies on.
            with hackrf_device_lock(serial=HACKRF_TX_SERIAL):
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except FileNotFoundError:
                    return {"ok": False, "error": "hackrf_transfer not found (install the `hackrf` package)",
                             "stopped_early": False}

                if on_started:
                    on_started(proc)

                # continuous -> no deadline (only stop_event/tx_halt ends it);
                # bounded -> the requested window + 5s margin, as before.
                deadline = None if continuous else time.time() + float(duration_s) + 5
                # Forward tx_halt_check so this path polls tx_halt DIRECTLY as an
                # independent stop trigger (matching transmit_sweep /
                # transmit_iq_file / the operator paths) — not solely via the
                # caller's stop_event. Both are honored on every poll.
                outcome = _supervise_transfer(proc, deadline, stop_event, tx_halt_check)
                stopped_early = (outcome == "stopped")
        except HackrfDeviceBusy as e:
            # Another process (hackrf_rx.py's sweep loop or
            # ml_classify_bridge.py's gate-check sweep/IQ capture) is
            # currently holding the device. Treat this exactly like any
            # other TX-side failure -- log-worthy, but never crash the
            # bridge process; jam_bridge.py's caller sends a normal
            # "failed" jam_ack for this request and the service keeps
            # running for the next one.
            return {"ok": False, "error": f"HackRF device busy: {e}", "stopped_early": False}

        if stopped_early:
            return {"ok": True, "error": None, "stopped_early": True}

        rc = proc.returncode
        if rc == 0 or rc is None or rc < 0:
            # rc < 0 / None: killed by our own deadline above — expected
            # bounded-burst completion, not a failure (mirrors the CLI's
            # "pass # expected" handling of subprocess.TimeoutExpired).
            return {"ok": True, "error": None, "stopped_early": False}

        stderr = ""
        if proc.stderr:
            try:
                stderr = proc.stderr.read().decode(errors="replace")
            except Exception:
                pass
        return {"ok": False, "error": f"hackrf_transfer exited {rc}: {stderr[:300]}",
                 "stopped_early": False}


def sweep_centers_mhz(start_mhz: float, stop_mhz: float, step_mhz: float) -> list:
    """The ordered list of TX center frequencies (MHz) for ONE pass across
    [start_mhz, stop_mhz], spaced step_mhz apart, with the final center pinned
    to stop_mhz so the top of the band is always illuminated even when the span
    is not an exact multiple of the step. Pure/deterministic — unit-testable
    without any radio. A HackRF at each center covers roughly ±(step/2) of
    instantaneous bandwidth, so consecutive centers tile the band."""
    lo, hi = float(min(start_mhz, stop_mhz)), float(max(start_mhz, stop_mhz))
    # Frequency-scope clamp (defence-in-depth, mirrors the backend bound): never
    # emit a center outside the HackRF tunable range even if called directly with
    # an out-of-range band. Not a timing/effectiveness limit.
    lo = max(HACKRF_MIN_FREQ_MHZ, min(lo, HACKRF_MAX_FREQ_MHZ))
    hi = max(HACKRF_MIN_FREQ_MHZ, min(hi, HACKRF_MAX_FREQ_MHZ))
    step = abs(float(step_mhz)) or SWEEP_DEFAULT_STEP_MHZ
    centers: list = []
    f = lo
    while f < hi:
        centers.append(round(f, 6))
        f += step
    if not centers or centers[-1] < hi:
        centers.append(round(hi, 6))
    return centers


def transmit_sweep(
    freq_start_mhz: float,
    freq_stop_mhz: float,
    bandwidth_khz: float,
    tx_gain: int,
    step_mhz: float = SWEEP_DEFAULT_STEP_MHZ,
    dwell_ms: float = SWEEP_DEFAULT_DWELL_MS,
    duration_s=None,
    stop_event: Optional["threading.Event"] = None,
    on_started: Optional[Callable[["subprocess.Popen"], None]] = None,
    tx_halt_check: Optional[Callable[[], bool]] = None,
    dwell_runner: Optional[Callable[..., str]] = None,
) -> Dict[str, Any]:
    """MEGHDUT swept-barrage jam: step the TX center frequency across
    [freq_start_mhz, freq_stop_mhz] (see sweep_centers_mhz), dwelling dwell_ms
    at each center, wrapping around and repeating. This is how a ~20MHz-
    instantaneous HackRF denies a wide (~80MHz) frequency-hopping control link:
    every hop is hit within one sweep revisit interval (≈ n_centers * dwell).

    CONTINUOUS by default (duration_s continuous sentinel) — sweeps until the
    operator stops it (stop_event / tx_halt_check). A positive duration_s runs
    the sweep for a bounded window instead. STOP IS ALWAYS HONORED: the loop
    polls stop_event AND tx_halt_check before every dwell and terminates the
    live hackrf_transfer immediately (this is the non-negotiable safety
    invariant — a swept jammer that cannot be switched off must never be built).

    HONEST EFFECTIVENESS: the sweep denies a fast hopper only on the fraction of
    hops that land in the currently-illuminated window during each dwell;
    revisit interval, per-step energy, PA power, antenna and proximity all bound
    real-world effect. See module-level SWEEP_* notes.

    dwell_runner: injectable "run one dwell at center f, return an outcome
    string" hook for unit tests; default = a real hackrf_transfer subprocess.
    Returns {"ok", "error", "stopped_early"} like transmit_burst.
    """
    # Fail-closed TX pinning: refuse before building IQ or entering the sweep
    # loop if no TX serial is pinned and the dev opt-out is not set (see
    # _tx_pinning_error). The SET-serial governed path is unaffected.
    pin_err = _tx_pinning_error()
    if pin_err:
        return {"ok": False, "error": pin_err, "stopped_early": False}
    continuous = _is_continuous(duration_s)
    dwell_s = max(0.001, float(dwell_ms) / 1000.0)
    # Frequency-scope safety bound (SECOND layer; backend deploy_jam is primary).
    # Refuse an out-of-range or over-wide span outright rather than radiating it.
    # NOT a timing/effectiveness cap — see MAX_SWEEP_SPAN_MHZ note above.
    lo_req, hi_req = float(min(freq_start_mhz, freq_stop_mhz)), float(max(freq_start_mhz, freq_stop_mhz))
    if lo_req < HACKRF_MIN_FREQ_MHZ or hi_req > HACKRF_MAX_FREQ_MHZ:
        return {"ok": False,
                "error": f"sweep band outside HackRF range "
                         f"[{HACKRF_MIN_FREQ_MHZ:g},{HACKRF_MAX_FREQ_MHZ:g}] MHz",
                "stopped_early": False}
    if (hi_req - lo_req) > MAX_SWEEP_SPAN_MHZ:
        return {"ok": False,
                "error": f"sweep span {hi_req - lo_req:g} MHz exceeds frequency-scope safety "
                         f"bound {MAX_SWEEP_SPAN_MHZ:g} MHz",
                "stopped_early": False}
    centers = sweep_centers_mhz(freq_start_mhz, freq_stop_mhz, step_mhz)
    if not centers:
        return {"ok": False, "error": "empty sweep band", "stopped_early": False}

    # Build ONE dwell-length noise chunk, reused (retuned) at every center.
    iq_bytes = build_noise_iq(dwell_s, bandwidth_khz)
    deadline = None if continuous else time.time() + float(duration_s)
    started_fired = {"done": False}

    def _default_runner(iq_path: str, center_mhz: float) -> str:
        cmd = [
            "hackrf_transfer",
            "-t", iq_path,
            "-f", str(int(center_mhz * 1_000_000)),
            "-s", str(SAMPLE_RATE_HZ),
            "-x", str(tx_gain),
            "-a", "1",
            *_tx_device_args(),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if on_started and not started_fired["done"]:
            started_fired["done"] = True
            on_started(proc)
        # Each dwell is a bounded mini-burst; the sweep's own stop check happens
        # between dwells and within _supervise_transfer during the dwell.
        return _supervise_transfer(proc, time.time() + dwell_s, stop_event, tx_halt_check)

    runner = dwell_runner or _default_runner

    stopped_early = False
    try:
        with tempfile.NamedTemporaryFile(suffix=".iq") as f:
            f.write(iq_bytes)
            f.flush()
            # Hold the TX unit's own per-serial lock for the WHOLE sweep session
            # (mirrors the continuous-CLI branch) so the swept barrage can never
            # collide with the RX consumers on the separate RX unit.
            with hackrf_device_lock(serial=HACKRF_TX_SERIAL):
                idx = 0
                while True:
                    if _stop_requested(stop_event, tx_halt_check):
                        stopped_early = True
                        break
                    if deadline is not None and time.time() > deadline:
                        break
                    center = centers[idx % len(centers)]
                    idx += 1
                    try:
                        outcome = runner(f.name, center)
                    except FileNotFoundError:
                        return {"ok": False,
                                "error": "hackrf_transfer not found (install the `hackrf` package)",
                                "stopped_early": False}
                    if outcome == "stopped":
                        stopped_early = True
                        break
    except HackrfDeviceBusy as e:
        return {"ok": False, "error": f"HackRF device busy: {e}", "stopped_early": False}

    return {"ok": True, "error": None, "stopped_early": stopped_early}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", choices=list(BAND_PRESETS_MHZ.keys()),
                     help="Shortcut for a validated center freq: 433, 915, 2g4, 5g8")
    ap.add_argument("--freq-mhz", type=float,
                     help="Explicit center frequency in MHz (overrides --band if both given)")
    ap.add_argument("--bandwidth-khz", type=float, default=500)
    ap.add_argument("--duration-s", type=float, default=5,
                     help="Bounded burst length in seconds. NOT capped — use --continuous "
                          "for an operator-stopped run. 0 / negative also means continuous.")
    ap.add_argument("--tx-gain", type=int, default=20, help="HackRF TX VGA gain, 0-47")
    ap.add_argument("--max-gain", action="store_true",
                     help=f"Shortcut: set TX VGA gain to the HackRF maximum ({MAX_TX_VGA_GAIN}) "
                          f"for maximum radiated power.")
    ap.add_argument("--continuous", action="store_true",
                     help="Transmit continuously until YOU press Ctrl+C (no auto-stop timer). "
                          "Still requires the one-time TRANSMIT confirmation before starting; "
                          "you retain full manual control to stop it at any moment.")
    # Swept-barrage (MEGHDUT effectiveness mode): step the TX center across a
    # band so a frequency-hopping control link is hit on every hop over time.
    ap.add_argument("--sweep", action="store_true",
                     help="Swept-barrage: step the TX center across [--freq-start-mhz, "
                          "--freq-stop-mhz]. Continuous unless --duration-s > 0.")
    ap.add_argument("--freq-start-mhz", type=float, help="Sweep band start (MHz).")
    ap.add_argument("--freq-stop-mhz", type=float, help="Sweep band stop (MHz).")
    ap.add_argument("--step-mhz", type=float, default=SWEEP_DEFAULT_STEP_MHZ,
                     help=f"Sweep step (MHz), ~HackRF instantaneous BW. Default {SWEEP_DEFAULT_STEP_MHZ}.")
    ap.add_argument("--dwell-ms", type=float, default=SWEEP_DEFAULT_DWELL_MS,
                     help=f"Dwell at each center (ms). Default {SWEEP_DEFAULT_DWELL_MS}.")
    ap.add_argument("--i-confirm-authorized-range", action="store_true")
    args = ap.parse_args()

    check_authorized(args.i_confirm_authorized_range)

    # Fail-closed TX pinning (interactive CLI): refuse — with a clear message
    # and a non-zero exit — before building any IQ or spawning hackrf_transfer
    # if no TX serial is pinned and the dev opt-out is not set. Covers BOTH the
    # swept-barrage branch and the single-center continuous/bounded branch below
    # in one place, so no CLI transmit path can fall back to index-0. The
    # SET-serial governed path is unaffected.
    pin_err = _tx_pinning_error()
    if pin_err:
        print(f"ERROR: {pin_err}", file=sys.stderr)
        sys.exit(1)

    if args.max_gain:
        args.tx_gain = MAX_TX_VGA_GAIN

    # continuous when explicitly asked OR duration_s is a continuous sentinel.
    continuous = args.continuous or _is_continuous(args.duration_s)

    # ---- Swept-barrage branch (MEGHDUT full-band coverage) --------------------
    if args.sweep:
        start = args.freq_start_mhz
        stop = args.freq_stop_mhz
        if start is None or stop is None:
            print("ERROR: --sweep needs --freq-start-mhz and --freq-stop-mhz "
                  "(e.g. 2400 2483.5 for the 2.4GHz ISM band).", file=sys.stderr)
            sys.exit(1)
        span_desc = (f"SWEEP {start}->{stop} MHz, step {args.step_mhz}MHz, dwell {args.dwell_ms}ms, "
                     f"~{args.bandwidth_khz}kHz BW, gain {args.tx_gain}")
        mode_desc = "CONTINUOUS (until Ctrl+C)" if continuous else f"{args.duration_s}s"
        print(f"Preparing swept barrage [{mode_desc}]: {span_desc}.")
        confirm = input(f"About to TRANSMIT a swept barrage across {start}-{stop} MHz "
                        f"({'until you press Ctrl+C' if continuous else str(args.duration_s) + 's'}). "
                        f"Type 'TRANSMIT' to proceed: ")
        if confirm.strip() != "TRANSMIT":
            print("Aborted — no transmission sent.")
            return
        print("TRANSMITTING SWEPT BARRAGE — press Ctrl+C at any time to stop.")
        stop_event = threading.Event()
        try:
            result = transmit_sweep(
                start, stop, args.bandwidth_khz, args.tx_gain,
                step_mhz=args.step_mhz, dwell_ms=args.dwell_ms,
                duration_s=(None if continuous else args.duration_s),
                stop_event=stop_event,
            )
        except KeyboardInterrupt:
            stop_event.set()
            print("\nStopped by operator (Ctrl+C).")
            return
        if not result["ok"]:
            print(f"ERROR: swept barrage failed: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print("Swept barrage complete." if not result["stopped_early"]
              else "Swept barrage stopped by operator.")
        return

    freq_mhz = args.freq_mhz if args.freq_mhz is not None else BAND_PRESETS_MHZ.get(args.band)
    if freq_mhz is None:
        print("ERROR: pass --band {433,915,2g4,5g8} or an explicit --freq-mhz.", file=sys.stderr)
        sys.exit(1)
    args.freq_mhz = freq_mhz

    # NO cap — the operator-set duration is honored verbatim (commander directive).
    duration = args.duration_s

    # For a continuous run, build a short chunk and loop it per burst; for a
    # bounded run, build the requested duration and play it once.
    chunk = CONTINUOUS_CHUNK_S if continuous else duration
    mode = "CONTINUOUS (until you press Ctrl+C)" if continuous else f"{duration}s burst"
    print(f"Preparing {mode} @ {args.freq_mhz} MHz, "
          f"~{args.bandwidth_khz}kHz bandwidth, TX gain {args.tx_gain}.")
    iq_bytes = build_noise_iq(chunk, args.bandwidth_khz)

    with tempfile.NamedTemporaryFile(suffix=".iq") as f:
        f.write(iq_bytes)
        f.flush()
        prompt = (f"About to TRANSMIT CONTINUOUSLY at {args.freq_mhz} MHz until you press Ctrl+C. "
                  f"Type 'TRANSMIT' to proceed: " if continuous else
                  f"About to TRANSMIT at {args.freq_mhz} MHz for {duration}s. Type 'TRANSMIT' to proceed: ")
        confirm = input(prompt)
        if confirm.strip() != "TRANSMIT":
            print("Aborted — no transmission sent.")
            return
        cmd = [
            "hackrf_transfer",
            "-t", f.name,
            "-f", str(int(args.freq_mhz * 1_000_000)),
            "-s", str(SAMPLE_RATE_HZ),
            "-x", str(args.tx_gain),
            "-a", "1",
            # CONTINUOUS = GAPLESS: loop the noise chunk seamlessly on the radio
            # itself (hackrf_transfer -R) as ONE long-lived process. The previous
            # loop-and-relaunch (a fresh subprocess.run per chunk, no -R) left an
            # inter-launch USB re-init gap between chunks that showed up on a
            # spectrum analyser as PULSING/HOPPING — a battlefield jammer set to
            # continuous must emit unbroken noise at the single center, not pulse.
            # This mirrors transmit_burst()/transmit_iq_file(), which have always
            # used -R for exactly this reason. There is NO center hop here (single
            # -f); center-stepping happens ONLY on the explicit --sweep branch.
            *(["-R"] if continuous else []),  # loop the chunk gaplessly (continuous)
            *_tx_device_args(),  # `-d <TX serial>` when a TX unit is pinned (see HACKRF_TX_SERIAL)
        ]
        print("Running:", " ".join(cmd))
        if continuous:
            print("TRANSMITTING CONTINUOUSLY (gapless -R, single center) — "
                  "press Ctrl+C at any time to stop.")
            try:
                # TX device pinning (task #TX-pin): hold the TX unit's OWN
                # per-serial lock for the whole continuous session so this
                # ungoverned-CLI transmission addresses the pinned TX unit (`-d`
                # above) and can never key the RX antenna or collide with the RX
                # consumers (hackrf_rx.py sweep / ml_classify_bridge.py) on the
                # separate RX unit. serial=None (HACKRF_TX_SERIAL unset) keeps
                # the original shared-default behavior single-radio hosts rely on.
                with hackrf_device_lock(serial=HACKRF_TX_SERIAL):
                    try:
                        # ONE process, running -R — NOT a relaunch loop. The radio
                        # repeats the chunk with no restart gap; the operator's
                        # Ctrl+C terminates the single live process below.
                        proc = subprocess.Popen(cmd)
                    except FileNotFoundError:
                        print("ERROR: hackrf_transfer not found. Install the `hackrf` package.",
                              file=sys.stderr)
                        sys.exit(1)
                    try:
                        proc.wait()  # blocks until the process exits or Ctrl+C
                        print("hackrf_transfer exited on its own — transmission ended.")
                    except KeyboardInterrupt:
                        print("\nStopping — terminating the live hackrf_transfer.")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        print("Stopped by operator (Ctrl+C).")
            except HackrfDeviceBusy as e:
                print(f"ERROR: HackRF TX device busy (another TX/RX consumer holds it): {e}",
                      file=sys.stderr)
                sys.exit(1)
        else:
            try:
                # Same TX device pinning as the continuous branch: address the
                # pinned TX unit and serialize behind its own per-serial lock so
                # this ungoverned single burst cannot key the RX antenna.
                with hackrf_device_lock(serial=HACKRF_TX_SERIAL):
                    subprocess.run(cmd, timeout=duration + 5, check=True)
            except FileNotFoundError:
                print("ERROR: hackrf_transfer not found. Install the `hackrf` package.", file=sys.stderr)
                sys.exit(1)
            except subprocess.CalledProcessError as e:
                print(f"hackrf_transfer exited with error: {e}", file=sys.stderr)
                sys.exit(1)
            except subprocess.TimeoutExpired:
                pass  # expected — the burst is bounded, this just means it ran the full duration
            except HackrfDeviceBusy as e:
                print(f"ERROR: HackRF TX device busy (another TX/RX consumer holds it): {e}",
                      file=sys.stderr)
                sys.exit(1)

    print("Transmission window complete.")


if __name__ == "__main__":
    main()
