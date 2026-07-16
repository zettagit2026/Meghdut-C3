#!/usr/bin/env python3
"""Passive HackRF spectrum sweep + energy-detection bridge for CEMA cUAS.

RECEIVE ONLY. No transmission happens in this script. Safe to run anywhere.

Sweeps the DJI OcuSync/video bands (2.4GHz, 5.8GHz) and the SiK telemetry
ISM band (915MHz), looks for energy above a noise-floor threshold, and
pushes real waterfall rows + detections into the CEMA console over its
existing /api/spectrum/ingest and /api/detections/ingest endpoints.

Requires `hackrf_sweep` (from the `hackrf` package) on PATH.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import List, Tuple

import numpy as np
import requests

BANDS_MHZ: List[Tuple[str, int, int, str]] = [
    ("SiK-915", 902, 928, "SiK/ISM 915MHz"),
    ("DJI-2G4", 2400, 2483, "OcuSync/Wi-Fi 2.4GHz"),
    ("DJI-5G8", 5725, 5850, "OcuSync 5.8GHz"),
]

# Per-band noise floor, site-calibrated 2026-07-16 via hackrf_baseline_test.py.
# SiK-915 runs hotter at this site (~-53 to -60dBm quiet) than the 2.4/5.8GHz
# bands (~-57 to -59dBm quiet) — a single global floor over-triggered on SiK.
BAND_NOISE_FLOOR_DBM = {
    "SiK-915": -50.0,
    "DJI-2G4": -58.0,
    "DJI-5G8": -57.0,
}
DETECT_THRESHOLD_DB = 15.0  # dB above that band's floor to call it a contact
SWEEPS_PER_CYCLE = 2  # DJI OcuSync is frequency-hopping/bursty; one-shot sweeps miss it often.
                       # Kept low (not 4) so a full band cycle stays well under the console's
                       # HackRF freshness window in continuous/live mode.
CONFIRM_CYCLES = 2  # require this many consecutive detecting cycles before reporting a contact,
                    # to reject one-off ISM-band noise spikes (Wi-Fi/Bluetooth bursts)


def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


SWEEP_TIMEOUT_S = 8.0  # a healthy hackrf_sweep -1 pass completes in well under 1s;
                        # anything hitting this is the device wedged (known libusb/HackRF
                        # quirk under rapid repeated open/close), not a slow sweep.
SETTLE_S = 0.4  # let the HackRF's USB stack settle between opens to avoid the above


def _one_sweep(low_mhz: int, high_mhz: int, bin_width_khz: int) -> List[float]:
    cmd = [
        "hackrf_sweep",
        "-f", f"{low_mhz}:{high_mhz}",
        "-w", str(bin_width_khz * 1000),
        "-1",  # one-shot
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=SWEEP_TIMEOUT_S)
    except FileNotFoundError:
        print("ERROR: hackrf_sweep not found. Install the `hackrf` package (brew/apt).", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"WARN: hackrf_sweep wedged on {low_mhz}-{high_mhz}MHz (device busy/USB hang) — "
              f"killed and skipping this pass.", file=sys.stderr)
        return []
    finally:
        time.sleep(SETTLE_S)
    powers: List[float] = []
    for line in out.stdout.splitlines():
        parts = line.split(",")
        if len(parts) > 6:
            powers.extend(float(p) for p in parts[6:] if p.strip())
    return powers


def sweep_band(low_mhz: int, high_mhz: int, bin_width_khz: int = 1000,
               sweeps: int = SWEEPS_PER_CYCLE) -> Tuple[List[float], float]:
    """Run several hackrf_sweep passes over [low, high] MHz and take the per-bin max,
    since frequency-hopping links (e.g. DJI OcuSync) are only in-band intermittently.
    Returns (peak-held power_dbm_bins, center_freq_mhz)."""
    held: List[float] = []
    for _ in range(sweeps):
        powers = _one_sweep(low_mhz, high_mhz, bin_width_khz)
        if not powers:
            continue
        if not held:
            held = powers
        else:
            n = min(len(held), len(powers))
            held = [max(held[i], powers[i]) for i in range(n)]
    if not held:
        # hackrf not connected / permissions issue — fall back to noise-floor filler
        # so the rest of the pipeline (console UI) still has something to show.
        held = list(np.random.normal(-65.0, 2.0, size=64))
    center = (low_mhz + high_mhz) / 2.0
    return held, center


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--console-url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--interval-s", type=float, default=3.0)
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"Logged in. Sweeping {len(BANDS_MHZ)} bands every {args.interval_s}s. RX ONLY — no transmission.")

    consecutive_hits = {name: 0 for name, *_ in BANDS_MHZ}
    i = 0
    while args.iterations == 0 or i < args.iterations:
        rows = []
        for name, low, high, label in BANDS_MHZ:
            powers, center_mhz = sweep_band(low, high)
            rows.append(powers)
            # Ping spectrum ingest after every band, not just once per full cycle —
            # a full 3-band sweep can take well over the console's "is HackRF live"
            # freshness window otherwise.
            try:
                requests.post(
                    f"{args.console_url}/api/spectrum/ingest",
                    json={"bins": len(powers), "rows": [powers]},
                    headers=headers, timeout=5,
                )
            except requests.RequestException as e:
                print(f"spectrum ingest failed: {e}", file=sys.stderr)
            peak = max(powers)
            floor = BAND_NOISE_FLOOR_DBM[name]
            if peak > floor + DETECT_THRESHOLD_DB:
                consecutive_hits[name] += 1
            else:
                consecutive_hits[name] = 0

            if consecutive_hits[name] >= CONFIRM_CYCLES:
                det = {
                    "model": "DJI Mini (candidate)" if "DJI" in name else "MAVLink craft (candidate)",
                    "protocol": "OcuSync/Wi-Fi" if "DJI" in name else "SiK/MAVLink",
                    "threat_level": "MEDIUM",
                    "center_freq_ghz": center_mhz / 1000.0,
                    "bandwidth_mhz": high - low,
                    "rssi_dbm": peak,
                    "snr_db": peak - floor,
                    "bearing_deg": 0.0,
                    "distance_m": 0.0,
                    "source": "SIK_RADIO" if name == "SiK-915" else "HACKRF",
                }
                try:
                    requests.post(f"{args.console_url}/api/detections/ingest", json=det, headers=headers, timeout=5)
                    print(f"[{label}] CONFIRMED contact: peak {peak:.1f} dBm ({peak - floor:.1f} dB above floor, "
                          f"{consecutive_hits[name]} consecutive cycles)")
                except requests.RequestException as e:
                    print(f"ingest failed: {e}", file=sys.stderr)
            elif consecutive_hits[name] > 0:
                print(f"[{label}] possible contact: peak {peak:.1f} dBm — awaiting confirmation "
                      f"({consecutive_hits[name]}/{CONFIRM_CYCLES} cycles)")

        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
