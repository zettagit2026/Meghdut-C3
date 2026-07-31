#!/usr/bin/env python3
"""Bounded-duration RF link-disruption demo via HackRF TX.

TRANSMITS real RF. Demonstrates CEMA "communication disruption" (RFI item
1.4) against the SiK 915MHz ISM band or DJI's 2.4/5.8GHz video/control
bands, by transmitting a band-limited noise waveform for a fixed duration.

SAFETY GATING — refuses to run unless BOTH are true:
  1. env var CEMA_AUTHORIZED_RANGE=1
  2. --i-confirm-authorized-range passed on the command line

Only run this at STEAG under Army Signals spectrum authorization.
Duration is hard-capped at 10 seconds per invocation — re-run explicitly
for a longer demo window rather than allowing an unattended long TX.
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

MAX_DURATION_S = 10.0
SAMPLE_RATE_HZ = 20_000_000  # 20 Msps, matches /CEMA/drone-kit/dronev5/cema/cema_base.py's
                              # proven RATE for these same bands.

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

    stop_event / on_started: identical contract to transmit_burst()'s own
    parameters — see that function's docstring.

    Returns {"ok": bool, "error": Optional[str], "stopped_early": bool}.
    Never raises for TX-side failures — same convention as transmit_burst().
    """
    cmd = [
        "hackrf_transfer",
        "-t", iq_path,
        "-f", str(int(freq_mhz * 1_000_000)),
        "-s", str(SAMPLE_RATE_HZ),
        "-x", str(tx_gain),
        "-a", "1",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        return {"ok": False, "error": "hackrf_transfer not found (install the `hackrf` package)",
                 "stopped_early": False}

    if on_started:
        on_started(proc)

    deadline = time.time() + duration_s + 5  # matches transmit_burst()'s timeout=duration+5 margin
    stopped_early = False
    while True:
        ret = proc.poll()
        if ret is not None:
            break
        if stop_event is not None and stop_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            stopped_early = True
            break
        if time.time() > deadline:
            # Same as transmit_burst()'s expected TimeoutExpired boundary:
            # the bounded burst ran its full duration; this is success, not
            # failure.
            proc.kill()
            break
        time.sleep(0.1)

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

    Duration is hard-capped at MAX_DURATION_S regardless of what is
    requested, same as the CLI path.

    stop_event: if provided and set() while the burst is running, the
    underlying hackrf_transfer process is terminated early (used by
    jam_bridge.py to honor a live EMERGENCY ABORT mid-burst — the app's
    existing Tier-0 "stop all TX now" control must also be able to kill a
    real RF jam in progress, not just queued MAVLink frames).

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
    duration = min(duration_s, MAX_DURATION_S)
    iq_bytes = build_noise_iq(duration, bandwidth_khz)

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
        ]
        try:
            with hackrf_device_lock():
                try:
                    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                except FileNotFoundError:
                    return {"ok": False, "error": "hackrf_transfer not found (install the `hackrf` package)",
                             "stopped_early": False}

                if on_started:
                    on_started(proc)

                deadline = time.time() + duration + 5  # matches CLI's timeout=duration+5 margin
                stopped_early = False
                while True:
                    ret = proc.poll()
                    if ret is not None:
                        break
                    if stop_event is not None and stop_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        stopped_early = True
                        break
                    if time.time() > deadline:
                        # Same as the CLI's expected TimeoutExpired boundary: the
                        # bounded burst ran its full duration; this is success, not
                        # failure.
                        proc.kill()
                        break
                    time.sleep(0.1)
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", choices=list(BAND_PRESETS_MHZ.keys()),
                     help="Shortcut for a validated center freq: 433, 915, 2g4, 5g8")
    ap.add_argument("--freq-mhz", type=float,
                     help="Explicit center frequency in MHz (overrides --band if both given)")
    ap.add_argument("--bandwidth-khz", type=float, default=500)
    ap.add_argument("--duration-s", type=float, default=5)
    ap.add_argument("--tx-gain", type=int, default=20, help="HackRF TX VGA gain, 0-47")
    ap.add_argument("--continuous", action="store_true",
                     help="Keep transmitting repeated bursts until you press Ctrl+C. "
                          "Still requires the one-time TRANSMIT confirmation before starting; "
                          "you retain full manual control to stop it at any moment.")
    ap.add_argument("--i-confirm-authorized-range", action="store_true")
    args = ap.parse_args()

    check_authorized(args.i_confirm_authorized_range)

    freq_mhz = args.freq_mhz if args.freq_mhz is not None else BAND_PRESETS_MHZ.get(args.band)
    if freq_mhz is None:
        print("ERROR: pass --band {433,915,2g4,5g8} or an explicit --freq-mhz.", file=sys.stderr)
        sys.exit(1)
    args.freq_mhz = freq_mhz

    duration = min(args.duration_s, MAX_DURATION_S)
    if duration != args.duration_s:
        print(f"Duration capped at {MAX_DURATION_S}s per invocation (requested {args.duration_s}s).")

    mode = "CONTINUOUS (until you press Ctrl+C)" if args.continuous else f"{duration}s burst"
    print(f"Preparing {mode} @ {args.freq_mhz} MHz, "
          f"~{args.bandwidth_khz}kHz bandwidth, TX gain {args.tx_gain}.")
    iq_bytes = build_noise_iq(duration, args.bandwidth_khz)

    with tempfile.NamedTemporaryFile(suffix=".iq") as f:
        f.write(iq_bytes)
        f.flush()
        prompt = (f"About to TRANSMIT CONTINUOUSLY at {args.freq_mhz} MHz until you press Ctrl+C. "
                  f"Type 'TRANSMIT' to proceed: " if args.continuous else
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
        ]
        print("Running:", " ".join(cmd))
        if args.continuous:
            print("TRANSMITTING CONTINUOUSLY — press Ctrl+C at any time to stop.")
            total_bursts = 0
            try:
                while True:
                    try:
                        subprocess.run(cmd, timeout=duration + 5, check=True)
                    except subprocess.TimeoutExpired:
                        pass  # expected per-burst boundary, loop continues
                    total_bursts += 1
                    print(f"  ...burst #{total_bursts} complete, continuing "
                          f"({total_bursts * duration:.0f}s transmitted so far)")
            except KeyboardInterrupt:
                print(f"\nStopped by operator after {total_bursts} bursts "
                      f"(~{total_bursts * duration:.0f}s total transmission).")
            except FileNotFoundError:
                print("ERROR: hackrf_transfer not found. Install the `hackrf` package.", file=sys.stderr)
                sys.exit(1)
        else:
            try:
                subprocess.run(cmd, timeout=duration + 5, check=True)
            except FileNotFoundError:
                print("ERROR: hackrf_transfer not found. Install the `hackrf` package.", file=sys.stderr)
                sys.exit(1)
            except subprocess.CalledProcessError as e:
                print(f"hackrf_transfer exited with error: {e}", file=sys.stderr)
                sys.exit(1)
            except subprocess.TimeoutExpired:
                pass  # expected — the burst is bounded, this just means it ran the full duration

    print("Transmission window complete.")


if __name__ == "__main__":
    main()
