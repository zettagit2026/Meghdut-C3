#!/usr/bin/env python3
"""Continuous single-frequency power monitor. RECEIVE ONLY.

Independent verification tool: point this at a frequency on ANOTHER
HackRF (not the one transmitting) while a jam/inject test runs elsewhere,
to confirm real radiated power without trusting the TX tool's own readout.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time


def read_peak(center_mhz: float, span_khz: float) -> float:
    # hackrf_sweep requires integer MHz boundaries; round outward so the
    # requested span is always fully covered.
    import math
    low = int(math.floor(center_mhz - span_khz / 2000.0))
    high = int(math.ceil(center_mhz + span_khz / 2000.0))
    if high <= low:
        high = low + 1
    cmd = ["hackrf_sweep", "-f", f"{low}:{high}", "-w", "1000000", "-1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return float("nan")
    powers = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) > 6:
            powers.extend(float(p) for p in parts[6:] if p.strip())
    return max(powers) if powers else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--freq-mhz", type=float, required=True)
    ap.add_argument("--span-khz", type=float, default=2000)
    ap.add_argument("--interval-s", type=float, default=0.5)
    ap.add_argument("--duration-s", type=float, default=30)
    args = ap.parse_args()

    print(f"Monitoring {args.freq_mhz} MHz (+/-{args.span_khz/2:.0f}kHz) every {args.interval_s}s "
          f"for {args.duration_s}s. RX ONLY.")
    start = time.time()
    baseline = None
    while time.time() - start < args.duration_s:
        peak = read_peak(args.freq_mhz, args.span_khz)
        if baseline is None:
            baseline = peak
            print(f"[baseline] {peak:.1f} dBm")
        else:
            delta = peak - baseline
            flag = " <<<< ELEVATED" if delta > 10 else ""
            print(f"[+{time.time()-start:5.1f}s] {peak:.1f} dBm  (delta {delta:+.1f} dB){flag}")
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
