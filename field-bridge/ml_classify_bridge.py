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
    _post_with_reauth,  # shared 401-retry-once helper -- see hackrf_rx.py for rationale
)
from gamutrf_infer import GamutRFClassifier, load_sigmf

DEFAULT_CHECKPOINT = "/tmp/resnet18_leesburg_split_0.02_1_current.pt"

# UNCLASSIFIED-SIGNAL THRESHOLD (2026-07-23): the deployed checkpoint is a
# closed-world 3-class model {drone, wifi_2_4, wifi_5} with no reject/
# "none of the above" class -- see module docstring above for the
# reproduced 99%-confident-on-noise finding. This is the cheap, zero-new-
# dependency mitigation for that gap: the model's OWN softmax output
# already tells us when it isn't sure. If the winning class's probability
# is below this threshold, the energy gate above genuinely found real RF
# energy (that part is real -- hackrf_sweep peak > noise floor + margin)
# but the classifier cannot confidently place it in any of its 3 known
# classes. We report that honestly as "unclassified_signal" instead of
# forcing the top (weak) softmax guess into ml_label/model, so the
# operator sees "real energy, unknown type" rather than a fabricated
# confident-looking label. See CEMA_ML_UNCLASSIFIED_MAX_CONFIDENCE to tune.
#
# THRESHOLD COHERENCE WITH backend/server.py's ML_RECLASSIFY_MIN_CONFIDENCE
# (2026-07-23 fix): this default is intentionally set EQUAL to that
# threshold (0.60), not an independently-chosen lower value (previously
# 0.5). With two different cutoffs, any wifi_2_4/wifi_5 top-class read in
# the gap between them (e.g. 55%) would be too weak to trigger the
# backend's wifi-reclassification display fix (needs >=0.60) but ALSO too
# high to be reported honestly as unclassified_signal (needed <0.50) --
# leaving it silently displayed as the stale drone-candidate heuristic
# guess with no informational badge at all, the exact "confident-looking
# but meaningless label" failure mode this feature exists to prevent.
# Equalizing the thresholds removes that unhandled middle ground: anything
# below the ML_RECLASSIFY_MIN_CONFIDENCE bar is now honestly reported as
# unclassified rather than silently falling through as an unqualified
# heuristic guess. If these two constants are ever tuned independently in
# the future, re-derive UNCLASSIFIED_MAX_CONFIDENCE from
# ML_RECLASSIFY_MIN_CONFIDENCE (or vice versa) rather than letting them
# drift apart again.
UNCLASSIFIED_MAX_CONFIDENCE = float(
    os.environ.get("CEMA_ML_UNCLASSIFIED_MAX_CONFIDENCE", "0.6")
)

# OUT-OF-DOMAIN BAND EXCLUSION (2026-07-23): the deployed checkpoint
# (resnet18_leesburg_split_0.02_1_current.pt) is a CLOSED-WORLD 3-class
# model trained ONLY on {drone, wifi_2_4, wifi_5} -- i.e. DJI-style
# OcuSync/Wi-Fi-band drone video-link energy. It has never seen 915MHz
# ISM / SiK-radio / MAVLink telemetry energy and has no "none of the
# above" class, so feeding it "SiK-915" IQ produces a confident-looking
# but MEANINGLESS softmax score (observed: 98.37% "drone" on real
# 915MHz energy that was actually SiK/MAVLink telemetry, not a drone
# video downlink). That is out-of-domain input, not a real
# classification, and must never reach this ML pass. hackrf_rx.py's
# existing RSSI/persistence heuristic already covers SiK-915 detection
# and is untouched by this exclusion -- only this classifier's own
# target list is narrowed.
ML_EXCLUDED_BANDS = {"SiK-915"}
ML_BANDS_MHZ = [b for b in BANDS_MHZ if b[0] not in ML_EXCLUDED_BANDS]

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
    print(f"[ml_classify_bridge] logged in. Gate-checking {len(ML_BANDS_MHZ)} bands every "
          f"{args.interval_s}s (excluded from ML pass, out-of-domain: "
          f"{sorted(ML_EXCLUDED_BANDS)}). RX ONLY -- no transmission.")

    i = 0
    with tempfile.TemporaryDirectory(prefix="cema_ml_bridge_") as tmp_dir:
        while args.iterations == 0 or i < args.iterations:
            for name, low, high, label in ML_BANDS_MHZ:
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
                # UNCLASSIFIED-SIGNAL CHECK: this is real, energy-gated RF
                # (peak already confirmed > noise floor + margin above), but
                # if the classifier's own winning-class softmax probability
                # is below UNCLASSIFIED_MAX_CONFIDENCE, none of the 3 known
                # classes fit confidently. Report that honestly instead of
                # forcing the band-name heuristic guess ("DJI Mini
                # (candidate)"/"MAVLink craft (candidate)") -- this is the
                # one case where we say "energy present, type unknown"
                # rather than picking a label nobody is confident in.
                is_unclassified = ml_confidence < UNCLASSIFIED_MAX_CONFIDENCE
                if is_unclassified:
                    print(f"[{label}] ML result INCONCLUSIVE (top class "
                          f"{ml_label} @ {ml_confidence:.4f} < "
                          f"{UNCLASSIFIED_MAX_CONFIDENCE} threshold) -- "
                          f"reporting as unclassified_signal, not forcing a label")
                # MERGE-MATCH INTEGRITY (2026-07-23 fix): backend/server.py's
                # detection_ingest merges an ingest into an existing ACTIVE
                # record by an EXACT match on (source, model, protocol) -- see
                # detectionConfidence.js's isUnconfirmedDetection comment,
                # which documents this exact same invariant from the frontend
                # side. hackrf_rx.py always posts model="DJI Mini (candidate)"/
                # protocol="OcuSync/Wi-Fi" (or "MAVLink craft (candidate)"/
                # "SiK/MAVLink") for these bands. This bridge's non-
                # unclassified branch matches those strings byte-for-byte on
                # purpose so its ML read merges into hackrf_rx.py's record
                # instead of spawning a competing one (see module docstring).
                # The unclassified_signal branch MUST follow the same rule:
                # sending "Unclassified emitter (candidate)"/"Unknown" here
                # would never match the existing "DJI Mini (candidate)"/
                # "OcuSync/Wi-Fi" record, silently defeating the merge and
                # creating a SECOND, duplicate ACTIVE detection for the same
                # physical contact -- one heuristic-labeled, one
                # "unclassified", both live at once. So model/protocol/
                # threat_level sent over the wire always stay the plain
                # heuristic-consistent guess; the honest "unclassified"
                # DISPLAY override (what the operator actually sees) is
                # applied server-side in backend/server.py's detection_ingest,
                # keyed off confidence_type=="unclassified_signal" -- the same
                # pattern already used for ML_WIFI_RECLASSIFY_DISPLAY. Only
                # confidence_type/ml_label/ml_confidence below carry the
                # "I don't know" signal on the wire.
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
                    # real softmax prob from a known-flawed closed-world model,
                    # unless it was too weak to trust any class at all
                    "confidence_type": "unclassified_signal" if is_unclassified else "ml_probability",
                }
                try:
                    _post_with_reauth(args.console_url, "/api/detections/ingest", det,
                                       headers, args.email, args.password, timeout=10)
                except requests.RequestException as e:
                    print(f"[{label}] ingest failed: {e}", file=sys.stderr)

            i += 1
            time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
