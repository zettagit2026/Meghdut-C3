#!/usr/bin/env python3
"""Gated ML classify-and-ingest bridge for CEMA cUAS -- REAL IQ, REAL model,
energy-GATED so a closed-world classifier is never asked to guess on silence.

RECEIVE ONLY. No transmission happens anywhere in this script.

=============================================================================
WHAT THIS IS / WHY IT EXISTS
=============================================================================
hackrf_rx.py already does passive energy-detection (hackrf_sweep + RSSI
thresholds + persistence heuristics) and posts RSSI-based "candidate"
detections. This script is a SEPARATE, ADDITIONAL signal layered on top of
that -- it does not replace or modify hackrf_rx.py's RSSI-heuristic
classification in any way. It periodically:

  1. Reuses hackrf_rx.py's own `sweep_band()` (imported, not duplicated) to
     get a quick per-band energy read and applies the SAME already-tuned
     gate hackrf_rx.py uses to call something a real contact:
     peak_dBm > BAND_NOISE_FLOOR_DBM[band] + DETECT_THRESHOLD_DB.
  2. ONLY when that gate passes (real signal energy present, not noise
     floor) does it capture a short REAL IQ window via
     iq_capture.py's `capture_iq()` (imported, not duplicated -- no second
     hackrf_transfer wrapper anywhere in this file) and run REAL inference
     through the proven GamutRF-style ResNet18 pipeline
     (gamutrf_infer.GamutRFClassifier, factored out of the already-verified
     /tmp/gamutrf_sigmf_infer.py).
  3. Posts the result to /api/detections/ingest as ADDITIONAL fields
     (ml_label / ml_confidence / ml_gated) on top of the same detection
     record shape hackrf_rx.py already posts (same source/model/protocol
     keys), so the backend's existing DETECTION_MERGE_WINDOW_S logic merges
     this into hackrf_rx.py's own detection record when there is one,
     rather than spawning a duplicate/competing detection.

=============================================================================
WHY THE ENERGY GATE (Phase 1 finding -- this is the concrete fix)
=============================================================================
Empirical test performed on this deployment: a 2s real IQ capture at
3.6 GHz (well outside every CEMA-swept band: 902-928 / 2400-2483 /
5725-5850 MHz -- i.e. genuinely no CEMA-relevant emitter expected) was fed
through the SAME proven inference pipeline used here. Result across FOUR
independent analysis windows (offsets 0.0/0.5/1.0/1.5s): the model
confidently predicted "drone" every single time, with softmax confidence
0.9905 / 0.9947 / 0.9990 / 0.9971. The checkpoint
(resnet18_leesburg_split_0.02_1_current.pt) is a CLOSED-WORLD 3-class model
-- {drone, wifi_2_4, wifi_5} -- with NO idle/noise/background/"none of the
above" class, so it cannot express "I don't know" and will always emit a
confident answer even on pure noise-floor energy. This is a REAL, honest,
reproduced finding, not a hypothetical -- and it is the reason this bridge
NEVER calls the classifier without the energy gate above passing first.

Even gated-in, ml_confidence must still be treated as informational/
supplementary: the model can only choose among 3 known classes, so a
"wifi_2_4"/"wifi_5" prediction on a gated-in signal that is actually some
OTHER kind of emitter will still come back confident and wrong -- there is
no reject class. Weight ml_label/ml_confidence accordingly, especially at
lower confidence values, and never treat it as a substitute for the
RSSI-heuristic detection already produced by hackrf_rx.py.

=============================================================================
WHY POLL-AND-GATE INSTEAD OF HOOKING INTO hackrf_rx.py's IN-PROCESS STATE
=============================================================================
hackrf_rx.py is a separate long-running process; there is no shared-memory
or IPC channel between it and this script, and building one (refactoring
hackrf_rx.py's main loop into a producer that publishes energy readings for
consumers) is a bigger, riskier change to an already-deployed production
bridge than this task calls for. Instead, this script imports and re-runs
hackrf_rx.py's OWN `sweep_band()`/threshold constants directly, so the gate
math is byte-for-byte identical to what hackrf_rx.py itself uses -- it just
runs its own independent (infrequent) sweep to evaluate that gate, rather
than duplicating the threshold LOGIC with a second implementation.

This does mean two processes take turns opening the HackRF. To keep that
contention low: this bridge's default cycle interval (CEMA_ML_INTERVAL_S,
default 12s) is 4x hackrf_rx.py's default 3s sweep interval, and on most
cycles the ONLY device access is one quick gate-check sweep per band
(hackrf_sweep -1, sub-second) -- the more expensive real IQ capture only
happens on the (expected to be rare) cycles where genuine signal energy is
present. This was judged more practical than trying to have hackrf_rx.py
hand off "already-captured energy readings" across a process boundary that
doesn't currently exist.

=============================================================================
CREDENTIALS / CONFIG -- same convention as hackrf_rx.py / mavlink_sniffer.py
=============================================================================
No secrets hardcoded here. All via env vars (systemd EnvironmentFile=):
  CEMA_API_URL           backend base URL (e.g. http://localhost:8001)
  CEMA_EMAIL             operator login email
  CEMA_PASSWORD          operator login password
  CEMA_ML_CHECKPOINT     path to the .pt checkpoint (default: the currently
                         deployed /tmp/resnet18_leesburg_split_0.02_1_current.pt --
                         override once this artifact has a permanent home)
  CEMA_ML_INTERVAL_S     seconds between gate-check cycles (default 12)
  CEMA_ML_SAMPLE_RATE_HZ IQ capture sample rate for gated-in captures (default 10e6)
  CEMA_ML_CAPTURE_S      IQ capture duration in seconds when gated-in (default 0.3)
  CEMA_ML_OFFSET_S       offset into the capture the analysis window is taken
                         from, to skip hackrf_transfer's brief startup
                         transient (default 0.05)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from typing import Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from iq_capture import capture_iq  # real hackrf_transfer capture, RX only -- reused, not duplicated
from hackrf_rx import (  # gate math reused verbatim from the live energy-detection bridge
    BANDS_MHZ,
    BAND_NOISE_FLOOR_DBM,
    DETECT_THRESHOLD_DB,
    estimate_distance_m,
    login,
    sweep_band,
)
from gamutrf_infer import GamutRFClassifier, load_sigmf

DEFAULT_CHECKPOINT = "/tmp/resnet18_leesburg_split_0.02_1_current.pt"

# MULTI-DEVICE NOTE (2026-07): optional pin to one specific physical HackRF
# for both this bridge's gate-check sweeps (sweep_band(), imported above)
# and its gated IQ captures (capture_iq(), below). Unset/empty (default) =
# unchanged "whichever HackRF responds first" behavior. See
# hackrf_config.py for assigning a serial to this role via a named
# PRIMARY/SECONDARY role instead of a raw serial, if preferred.
HACKRF_SERIAL = os.environ.get("HACKRF_SERIAL") or None


def gated_capture_and_classify(
    classifier: GamutRFClassifier,
    name: str,
    low_mhz: int,
    high_mhz: int,
    center_mhz: float,
    sample_rate_hz: float,
    capture_s: float,
    offset_s: float,
    tmp_dir: str,
):
    """Capture a short REAL IQ window at this band's center frequency and run
    REAL inference on it. Only called after the energy gate has passed."""
    center_hz = center_mhz * 1e6
    out_path = os.path.join(tmp_dir, f"ml_gate_{name}.sigmf-data")
    meta_path = capture_iq(
        center_freq_hz=center_hz,
        sample_rate_hz=sample_rate_hz,
        duration_s=capture_s,
        out_path=out_path,
        serial=HACKRF_SERIAL,
        description=f"ml_classify_bridge gated capture, band={name}",
    )
    data_path = out_path
    samples, sr, freq, dtype = load_sigmf(meta_path, data_path)

    offset_n = int(offset_s * sr)
    window_n = classifier.min_window_samples(sr)
    if offset_n + window_n > len(samples):
        offset_n = max(0, len(samples) - window_n)
    label, confidence, all_probs = classifier.classify_window(samples[offset_n:], sr)

    try:
        os.remove(data_path)
        os.remove(meta_path)
    except OSError:
        pass

    return label, confidence, all_probs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--checkpoint", default=os.environ.get("CEMA_ML_CHECKPOINT", DEFAULT_CHECKPOINT))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("CEMA_ML_INTERVAL_S", "12.0")))
    ap.add_argument("--sample-rate-hz", type=float,
                     default=float(os.environ.get("CEMA_ML_SAMPLE_RATE_HZ", "10e6")))
    ap.add_argument("--capture-s", type=float,
                     default=float(os.environ.get("CEMA_ML_CAPTURE_S", "0.3")))
    ap.add_argument("--offset-s", type=float,
                     default=float(os.environ.get("CEMA_ML_OFFSET_S", "0.05")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    if not os.path.exists(args.checkpoint):
        ap.error(f"checkpoint not found: {args.checkpoint} (set CEMA_ML_CHECKPOINT)")

    print(f"[ml_classify_bridge] loading checkpoint {args.checkpoint} ...")
    classifier = GamutRFClassifier(args.checkpoint)
    print(f"[ml_classify_bridge] loaded. classes={classifier.idx_to_class} "
          f"sample_secs={classifier.sample_secs} nfft={classifier.nfft} device={classifier.device}")
    print("[ml_classify_bridge] KNOWN LIMITATION: closed-world 3-class model, no "
          "idle/noise/other class -- energy gate below is the mitigation (see module docstring).")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[ml_classify_bridge] logged in. Gate-checking {len(BANDS_MHZ)} bands every "
          f"{args.interval_s}s. RX ONLY -- no transmission.")

    i = 0
    with tempfile.TemporaryDirectory(prefix="cema_ml_bridge_") as tmp_dir:
        while args.iterations == 0 or i < args.iterations:
            for name, low, high, label in BANDS_MHZ:
                powers, center_mhz, is_real_data = sweep_band(name, low, high, serial=HACKRF_SERIAL)
                if not is_real_data:
                    continue  # wedged/fallback filler cycle -- nothing genuine to gate on
                peak = max(powers)
                floor = BAND_NOISE_FLOOR_DBM[name]
                gated_in = peak > floor + DETECT_THRESHOLD_DB
                if not gated_in:
                    continue  # Phase 1 mitigation: do not classify noise-floor energy

                print(f"[{label}] energy gate PASSED: peak {peak:.1f} dBm "
                      f"({peak - floor:.1f} dB above floor) -- running real ML inference")
                try:
                    ml_label, ml_confidence, all_probs = gated_capture_and_classify(
                        classifier, name, low, high, center_mhz,
                        args.sample_rate_hz, args.capture_s, args.offset_s, tmp_dir,
                    )
                except Exception as e:
                    print(f"[{label}] ML capture/inference failed: {e}", file=sys.stderr)
                    continue

                print(f"[{label}] ML result: {ml_label} (confidence {ml_confidence:.4f}) "
                      f"all_probs={all_probs}")

                est_distance_m = estimate_distance_m(name, peak)
                det = {
                    "model": "DJI Mini (candidate)" if "DJI" in name else "MAVLink craft (candidate)",
                    "protocol": "OcuSync/Wi-Fi" if "DJI" in name else "SiK/MAVLink",
                    "threat_level": "MEDIUM",
                    "center_freq_ghz": center_mhz / 1000.0,
                    "bandwidth_mhz": high - low,
                    "rssi_dbm": peak,
                    "snr_db": peak - floor,
                    "bearing_deg": 0.0,
                    "distance_m": round(est_distance_m, 1),
                    "distance_estimated": True,
                    "source": "SIK_RADIO" if name == "SiK-915" else "HACKRF",
                    "ml_label": ml_label,
                    "ml_confidence": round(ml_confidence, 4),
                    "ml_gated": False,
                }
                try:
                    requests.post(f"{args.console_url}/api/detections/ingest",
                                  json=det, headers=headers, timeout=10)
                except requests.RequestException as e:
                    print(f"[{label}] ingest failed: {e}", file=sys.stderr)

            i += 1
            time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
