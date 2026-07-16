#!/usr/bin/env python3
"""Baseline vs. active detection validation. RECEIVE ONLY — no transmission.

Step 1: capture a quiet baseline (drone OFF) per band.
Step 2: capture again with the drone ON and nearby.
Step 3: report delta so detection is validated against real evidence,
        not a guessed fixed noise floor.

This does not touch the console/API — it's a standalone validation tool to
run before wiring hackrf_rx.py's threshold to real numbers from your site.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import List, Tuple

BANDS_MHZ: List[Tuple[str, int, int]] = [
    ("SiK-915", 902, 928),
    ("DJI-2G4", 2400, 2483),
    ("DJI-5G8", 5725, 5850),
]


def sweep_band(low_mhz: int, high_mhz: int, bin_width_khz: int = 1000) -> List[float]:
    cmd = ["hackrf_sweep", "-f", f"{low_mhz}:{high_mhz}", "-w", str(bin_width_khz * 1000), "-1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
    except FileNotFoundError:
        print("ERROR: hackrf_sweep not found.", file=sys.stderr)
        sys.exit(1)
    powers: List[float] = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) > 6:
            powers.extend(float(p) for p in parts[6:] if p.strip())
    return powers


def summarize(label: str) -> dict:
    result = {}
    for name, low, high in BANDS_MHZ:
        powers = sweep_band(low, high)
        if not powers:
            print(f"  [{name}] no data — check HackRF connection/permissions", file=sys.stderr)
            result[name] = None
            continue
        peak = max(powers)
        mean = sum(powers) / len(powers)
        result[name] = {"peak": peak, "mean": mean}
        print(f"  [{name}] {label}: peak={peak:.1f}dBm mean={mean:.1f}dBm (n={len(powers)} bins)")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--passes", type=int, default=3, help="sweeps to average per stage")
    ap.add_argument("--countdown-s", type=int, default=10,
                     help="seconds to wait before each stage so you can (de)power the drone")
    ap.add_argument("--no-prompt", action="store_true",
                     help="skip interactive input(); use fixed countdowns only (for SSH/non-tty runs)")
    args = ap.parse_args()

    if args.no_prompt:
        print(f"Step 1/2: Ensure the DJI Mini / MAVLink craft is OFF and out of range. "
              f"Capturing baseline in {args.countdown_s}s...")
        time.sleep(args.countdown_s)
    else:
        input("Step 1/2: Ensure the DJI Mini / MAVLink craft is OFF and out of range. Press Enter to capture baseline...")
    print("\n== BASELINE (drone OFF) ==")
    baseline_runs = [summarize("baseline") for _ in range(args.passes)]

    if args.no_prompt:
        print(f"\nStep 2/2: Power ON the drone NOW and place it ~1-3m from the HackRF antenna. "
              f"Capturing active reading in {args.countdown_s}s...")
        time.sleep(args.countdown_s)
    else:
        input("\nStep 2/2: Power ON the drone and place it ~1-3m from the HackRF antenna. Press Enter to capture active reading...")
    print("\n== ACTIVE (drone ON) ==")
    active_runs = [summarize("active") for _ in range(args.passes)]

    print("\n== DELTA (active peak - baseline peak) ==")
    for name, _, _ in BANDS_MHZ:
        b_peaks = [r[name]["peak"] for r in baseline_runs if r.get(name)]
        a_peaks = [r[name]["peak"] for r in active_runs if r.get(name)]
        if not b_peaks or not a_peaks:
            print(f"  [{name}] insufficient data")
            continue
        b_avg = sum(b_peaks) / len(b_peaks)
        a_avg = sum(a_peaks) / len(a_peaks)
        delta = a_avg - b_avg
        verdict = "DETECTABLE" if delta > 6 else "NOT CLEARLY DETECTABLE at this distance/orientation"
        print(f"  [{name}] baseline={b_avg:.1f}dBm active={a_avg:.1f}dBm delta={delta:+.1f}dB -> {verdict}")

    print("\nUse these deltas to set a realistic per-band threshold in hackrf_rx.py "
          "(currently a fixed -85dBm floor + 12dB threshold, which may not match your actual RF environment).")


if __name__ == "__main__":
    main()
