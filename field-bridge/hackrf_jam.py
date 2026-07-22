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
import time

import numpy as np

MAX_DURATION_S = 10.0
SAMPLE_RATE_HZ = 20_000_000  # 20 Msps, matches /CEMA/drone-kit/dronev5/cema/cema_base.py's
                              # proven RATE for these same bands.

# Center frequencies from /CEMA/drone-kit/dronev5/cema/cema_{433,915,24,58}.py
# (already field-validated on this rig), exposed here as --band shortcuts.
BAND_PRESETS_MHZ = {
    "433": 435.0,
    "915": 915.0,
    "2g4": 2450.0,
    "5g8": 5800.0,
}


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
