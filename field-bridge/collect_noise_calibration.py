#!/usr/bin/env python3
"""Collects REAL noise-floor softmax calibration statistics for
ml_classify_bridge.py's OOD-rejection layer (ml_calibration.py).

RECEIVE ONLY. No transmission happens anywhere in this script. Must be run
on real HackRF hardware in the field -- it cannot produce anything
meaningful in a sandboxed dev environment with no RF front end attached.

=============================================================================
WHAT THIS DOES AND WHY IT IS NOT FABRICATED DATA
=============================================================================
ml_classify_bridge.py already computes, every gate-check cycle, whether a
band's RSSI peak clears BAND_NOISE_FLOOR_DBM[band] + DETECT_THRESHOLD_DB.
When it does NOT clear that bar, the system's OWN existing, already-trusted
logic has just labeled that moment "no real signal here" -- that is the
exact same gate hackrf_rx.py's live heuristic detector relies on. This
script does nothing new: it reuses that identical gate math (imported from
hackrf_rx.py, not reimplemented) and, ONLY on cycles where the gate says
"noise floor, not a contact", captures a short REAL IQ window (same
iq_capture.capture_iq real hackrf_transfer path ml_classify_bridge.py uses)
and runs it through the SAME classifier checkpoint to see how it scores
genuine ambient RF at this specific site.

This is real, honestly-labeled data collection from production hardware,
not synthetic noise and not a fabricated substitute for NoisyDroneRFv2 (see
ml_calibration.py's module docstring for why that dataset could not be
pulled into this environment). It only ever runs during moments the system
has already, independently decided are noise, using the exact same
threshold the live RSSI-heuristic detector trusts for that same decision.

Usage (on deployed hardware only):
    CEMA_ML_CHECKPOINT=/tmp/resnet18_leesburg_split_0.02_1_current.pt \\
    python3 collect_noise_calibration.py --duration-s 600

Writes/updates the calibration file (default
/var/lib/cema/ml_noise_calibration.json, override via
CEMA_ML_CALIBRATION_FILE) that ml_classify_bridge.py reads at startup.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from iq_capture import capture_iq
from hackrf_rx import BANDS_MHZ, BAND_NOISE_FLOOR_DBM, DETECT_THRESHOLD_DB, sweep_band
from gamutrf_infer import GamutRFClassifier, load_sigmf
from ml_calibration import NoiseCalibrationStats, load_calibration, save_calibration

HACKRF_SERIAL = os.environ.get("HACKRF_SERIAL") or None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", default=os.environ.get(
        "CEMA_ML_CHECKPOINT", "/tmp/resnet18_leesburg_split_0.02_1_current.pt"))
    ap.add_argument("--duration-s", type=float, default=600.0,
                     help="how long to collect for before exiting (0 = forever)")
    ap.add_argument("--interval-s", type=float, default=10.0)
    ap.add_argument("--sample-rate-hz", type=float, default=10e6)
    ap.add_argument("--capture-s", type=float, default=0.3)
    args = ap.parse_args()

    if not os.path.exists(args.checkpoint):
        ap.error(f"checkpoint not found: {args.checkpoint}")

    print(f"[collect_noise_calibration] loading checkpoint {args.checkpoint} ...")
    classifier = GamutRFClassifier(args.checkpoint)

    stats = load_calibration()
    print(f"[collect_noise_calibration] existing calibration: n={stats.n} "
          f"(source={stats.source}) -- new samples will be merged in")

    start = time.time()
    collected_this_run = 0
    with tempfile.TemporaryDirectory(prefix="cema_calib_") as tmp_dir:
        while args.duration_s == 0 or (time.time() - start) < args.duration_s:
            for name, low, high, label in BANDS_MHZ:
                powers, center_mhz, is_real_data = sweep_band(name, low, high, serial=HACKRF_SERIAL)
                if not is_real_data:
                    continue
                peak = max(powers)
                floor = BAND_NOISE_FLOOR_DBM[name]
                gated_in = peak > floor + DETECT_THRESHOLD_DB
                if gated_in:
                    # Real signal energy present -- NOT a noise-floor moment,
                    # skip. We only calibrate on genuine below-gate cycles.
                    continue

                try:
                    center_hz = center_mhz * 1e6
                    out_path = os.path.join(tmp_dir, f"calib_{name}.sigmf-data")
                    meta_path = capture_iq(
                        center_freq_hz=center_hz,
                        sample_rate_hz=args.sample_rate_hz,
                        duration_s=args.capture_s,
                        out_path=out_path,
                        serial=HACKRF_SERIAL,
                        description=f"noise-floor calibration capture, band={name}",
                    )
                    samples, sr, freq, dtype = load_sigmf(meta_path, out_path)
                    window_n = classifier.min_window_samples(sr)
                    if len(samples) < window_n:
                        continue
                    _, confidence, all_probs = classifier.classify_window(samples, sr)
                    stats.update(confidence, all_probs)
                    collected_this_run += 1
                    print(f"[{label}] noise-floor sample #{stats.n}: "
                          f"top_conf={confidence:.4f} probs={all_probs}")
                    try:
                        os.remove(out_path)
                        os.remove(meta_path)
                    except OSError:
                        pass
                except Exception as e:
                    print(f"[{label}] calibration capture failed (non-fatal): {e}", file=sys.stderr)
                    continue

                save_calibration(stats)

            time.sleep(args.interval_s)

    print(f"[collect_noise_calibration] done. collected {collected_this_run} new samples "
          f"this run, total n={stats.n}, mean_confidence={stats.mean_confidence:.4f}, "
          f"std_confidence={stats.std_confidence:.4f}, mean_entropy={stats.mean_entropy:.4f}")


if __name__ == "__main__":
    main()
