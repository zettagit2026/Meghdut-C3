#!/usr/bin/env python3
"""Continuous full-band sweep monitor. RECEIVE ONLY.

Unlike monitor_freq.py (single fixed frequency), this sweeps a whole band
each cycle and reports the peak + which sub-frequency it occurred at, so a
frequency-hopping target (or a jam burst that lands off-center) is still
caught.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def sweep(low_mhz: int, high_mhz: int, bin_width_khz: int = 1000):
    cmd = ["hackrf_sweep", "-f", f"{low_mhz}:{high_mhz}", "-w", str(bin_width_khz * 1000), "-1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return None, None
    best_power = float("-inf")
    best_freq = None
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) > 6:
            row_low = float(parts[2])
            step = float(parts[4])
            powers = [float(p) for p in parts[6:] if p.strip()]
            for i, p in enumerate(powers):
                if p > best_power:
                    best_power = p
                    best_freq = (row_low + i * step) / 1e6
    return best_power, best_freq


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--low-mhz", type=int, required=True)
    ap.add_argument("--high-mhz", type=int, required=True)
    ap.add_argument("--interval-s", type=float, default=0.5)
    ap.add_argument("--duration-s", type=float, default=30)
    args = ap.parse_args()

    print(f"Sweeping {args.low_mhz}-{args.high_mhz} MHz every ~{args.interval_s}s "
          f"for {args.duration_s}s. RX ONLY.")
    start = time.time()
    baseline = None
    while time.time() - start < args.duration_s:
        power, freq = sweep(args.low_mhz, args.high_mhz)
        if power is None:
            print(f"[+{time.time()-start:5.1f}s] sweep timed out/empty")
        elif baseline is None:
            baseline = power
            print(f"[baseline] peak {power:.1f} dBm @ {freq:.1f} MHz")
        else:
            delta = power - baseline
            flag = " <<<< ELEVATED" if delta > 10 else ""
            print(f"[+{time.time()-start:5.1f}s] peak {power:.1f} dBm @ {freq:.1f} MHz  (delta {delta:+.1f} dB){flag}")
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
