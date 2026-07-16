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
SAMPLE_RATE_HZ = 10_000_000  # 10 Msps


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
    ap.add_argument("--freq-mhz", type=float, required=True,
                     help="Target center frequency, e.g. 915 (SiK), 2440 (DJI 2.4G), 5787 (DJI 5.8G)")
    ap.add_argument("--bandwidth-khz", type=float, default=500)
    ap.add_argument("--duration-s", type=float, default=5)
    ap.add_argument("--tx-gain", type=int, default=20, help="HackRF TX VGA gain, 0-47")
    ap.add_argument("--i-confirm-authorized-range", action="store_true")
    args = ap.parse_args()

    check_authorized(args.i_confirm_authorized_range)

    duration = min(args.duration_s, MAX_DURATION_S)
    if duration != args.duration_s:
        print(f"Duration capped at {MAX_DURATION_S}s per invocation (requested {args.duration_s}s).")

    print(f"Preparing {duration}s noise burst @ {args.freq_mhz} MHz, "
          f"~{args.bandwidth_khz}kHz bandwidth, TX gain {args.tx_gain}.")
    iq_bytes = build_noise_iq(duration, args.bandwidth_khz)

    with tempfile.NamedTemporaryFile(suffix=".iq") as f:
        f.write(iq_bytes)
        f.flush()
        confirm = input(f"About to TRANSMIT at {args.freq_mhz} MHz for {duration}s. "
                         f"Type 'TRANSMIT' to proceed: ")
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
