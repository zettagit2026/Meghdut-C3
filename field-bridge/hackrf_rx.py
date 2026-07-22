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
import collections
import os
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

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

# --- RSSI -> distance ESTIMATE (log-distance path-loss model) ---------------
# distance_m = 10 ** ((RSSI_ref_1m - rssi_dbm) / (10 * path_loss_exponent))
#
# This is a coarse, single-antenna RSSI heuristic — NOT a real range
# measurement (no radar, no TDOA/multilateration, no calibration data at this
# site). It is provided so the dashboard shows a plausible order-of-magnitude
# figure instead of a misleading hardcoded 0, and every value derived from it
# is flagged downstream as "distance_estimated": true so operators/evaluators
# don't mistake it for a precise range.
#
# RSSI_REF_1M_DBM: expected received power at 1 metre from the emitter, per band.
# There is no site calibration for this, so these are documented assumptions,
# not measurements:
#   - DJI-2G4 / DJI-5G8: -30 dBm at 1m is a commonly cited ballpark for
#     typical small-UAS OcuSync/Wi-Fi-class video/control transmitters
#     (order of tens of mW EIRP) at 2.4/5.8GHz.
#   - SiK-915: SiK radios are usually lower TX power than video links, but
#     915MHz suffers less path loss than 2.4/5.8GHz for the same distance, so
#     a similar reference figure with a slightly lower path-loss exponent
#     (see below) is used as a starting approximation.
RSSI_REF_1M_DBM = {
    "SiK-915": -32.0,
    "DJI-2G4": -30.0,
    "DJI-5G8": -30.0,
}

# PATH_LOSS_EXPONENT: environmental attenuation factor. 2.0 = free space,
# 2.5 = light clutter/semi-open outdoor (assumed default for this site absent
# survey/calibration data), 3-4 = urban/indoor/heavy clutter. 915MHz
# penetrates/diffracts around obstacles somewhat better than 2.4/5.8GHz, so it
# gets a slightly lower exponent.
PATH_LOSS_EXPONENT = {
    "SiK-915": 2.3,
    "DJI-2G4": 2.5,
    "DJI-5G8": 2.5,
}

DISTANCE_MIN_M = 1.0      # clamp floor — RSSI-based estimates are meaningless
DISTANCE_MAX_M = 5000.0   # clamp ceiling — well beyond this, noise-floor RSSI
                          # differences are dominated by measurement error, not range


def estimate_distance_m(band_name: str, rssi_dbm: float) -> float:
    """Rough RSSI-based distance ESTIMATE via the log-distance path-loss model.

    This is an approximation for situational awareness only — it has no
    site calibration behind it and should never be treated as a precise
    range measurement. Callers must mark results with distance_estimated=True.
    """
    ref = RSSI_REF_1M_DBM.get(band_name, -30.0)
    exponent = PATH_LOSS_EXPONENT.get(band_name, 2.5)
    distance = 10 ** ((ref - rssi_dbm) / (10 * exponent))
    return max(DISTANCE_MIN_M, min(DISTANCE_MAX_M, distance))

SWEEPS_PER_CYCLE = 2  # DJI OcuSync is frequency-hopping/bursty; one-shot sweeps miss it often.
                       # Kept low (not 4) so a full band cycle stays well under the console's
                       # HackRF freshness window in continuous/live mode.
CONFIRM_CYCLES = 2  # require this many consecutive detecting cycles before reporting a contact,
                    # to reject one-off ISM-band noise spikes (Wi-Fi/Bluetooth bursts)

# --- 2.4GHz Wi-Fi-AP exclusion heuristic --------------------------------------
# The 2.4GHz ISM band is shared by DJI OcuSync/video links AND ordinary Wi-Fi
# access points. Energy-detection alone (no demodulation/IQ analysis -- that is
# future SEI/modulation-classification work, out of scope here) cannot tell
# the two apart from a single sweep. The coarse heuristic used below: a real
# Wi-Fi AP beacons continuously on ONE fixed standard channel for a long time,
# while a drone control/video link is frequency-hopping or bursty and is much
# less likely to sit perfectly still on a legacy Wi-Fi channel center for many
# consecutive cycles. So: if a detected peak persists at/near a standard
# Wi-Fi channel center for WIFI_PERSIST_CYCLES consecutive cycles, treat it as
# a likely Wi-Fi AP and suppress the drone-detection ingest for that cycle.
#
# KNOWN LIMITATION (explicitly not hidden): this is a coarse persistence-based
# heuristic, NOT real protocol classification. A drone hovering in place and
# transmitting continuously on a frequency that happens to overlap a standard
# Wi-Fi channel center could theoretically be filtered out incorrectly (a
# false negative). Real classification requires demodulation/SEI work that
# does not exist yet in this pipeline. This tradeoff is accepted for now to
# avoid the much more visible failure mode of a stationary office Wi-Fi AP
# being reported as a "DJI Mini (candidate)" drone in front of an evaluator.
#
# FIELD FIX 2026-07-22 (part 2): this logic now applies to BOTH DJI-2G4 AND
# DJI-5G8. The DJI-5G8 band (5725-5850MHz) directly overlaps standard 5GHz
# Wi-Fi UNII-3 channels 149/153/157/161/165 (5745-5825MHz) -- exactly the same
# "ordinary Wi-Fi AP could be mistaken for a drone" problem that motivated the
# 2.4GHz heuristic above, just in a different band. Each band gets its own
# independent persistence-tracking state (wifi_persist / wifi_persist_5g8 in
# main()) since DJI-2G4 and DJI-5G8 produce independent peaks every cycle.
# SiK-915 is still untouched -- Wi-Fi does not operate at 915MHz.
WIFI_CHANNEL_CENTERS_MHZ = {  # standard 2.4GHz Wi-Fi channels 1-11 (1/6/11 most common)
    1: 2412, 2: 2417, 3: 2422, 4: 2427, 5: 2432, 6: 2437, 7: 2442,
    8: 2447, 9: 2452, 10: 2457, 11: 2462,
}
WIFI_5G_CHANNEL_CENTERS_MHZ = {  # standard 5GHz Wi-Fi UNII-3 channels 149-165
    149: 5745, 153: 5765, 157: 5785, 161: 5805, 165: 5825,
}
WIFI_PERSIST_CYCLES = 5  # consecutive cycles a peak must sit within tolerance of a
                          # Wi-Fi channel center before being excluded as "likely AP".
                          # Deliberately > CONFIRM_CYCLES (2): CONFIRM_CYCLES only
                          # needs to reject one-off noise spikes, this needs to reject
                          # something that has been sitting still far longer than a
                          # hopping/bursty drone link would.
#
# FIELD FIX 2026-07-22: the live deployment host has a real, unresolved
# hardware/USB issue causing hackrf_sweep to "wedge" (device busy/USB hang) on
# ~35-60% of passes (see sweep_band()/_one_sweep()); when that happens,
# sweep_band() falls back to synthetic noise-floor filler data so the rest of
# the pipeline still has something to process. Field testing found 0 WiFi-AP
# exclusions logged over multiple 90s windows despite confirmed real WiFi APs
# in range. Root cause: the persistence-reset branch below used to treat a
# wedged/filler cycle identically to a genuine "no signal this cycle" reading
# and reset wifi_persist["cycles"] to 0 either way. With a 35-60% wedge rate,
# accumulating WIFI_PERSIST_CYCLES=5 truly-consecutive real-data hits at the
# same channel is very unlikely, so the counter kept getting reset before it
# could ever reach 5 and the filter effectively never fired. Fix: sweep_band()
# now also returns is_real_data, and the persistence-reset logic below only
# resets on a real-data cycle that is genuinely off-channel/below threshold —
# a wedge/fallback cycle leaves wifi_persist untouched (as if that cycle
# didn't happen for persistence-tracking purposes), so persistence can still
# accumulate across intervening wedged cycles.


def _nearest_wifi_channel(freq_mhz: float, tol_mhz: float,
                           channel_centers: Dict[int, int] = WIFI_CHANNEL_CENTERS_MHZ
                           ) -> Optional[Tuple[int, float]]:
    """Return (channel, center_mhz) if freq_mhz is within tol_mhz of a standard
    Wi-Fi channel center from the given channel_centers table, else None.

    channel_centers defaults to the 2.4GHz table (WIFI_CHANNEL_CENTERS_MHZ);
    pass WIFI_5G_CHANNEL_CENTERS_MHZ to check against the 5GHz UNII-3 table
    instead -- one function, parameterized by band, rather than duplicating
    the lookup logic per band."""
    best = None
    for ch, center in channel_centers.items():
        if abs(freq_mhz - center) <= tol_mhz:
            if best is None or abs(freq_mhz - center) < abs(freq_mhz - best[1]):
                best = (ch, float(center))
    return best


def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


SWEEP_TIMEOUT_S = 8.0  # a healthy hackrf_sweep -1 pass completes in well under 1s;
                        # this generous ceiling exists purely to detect a wedged/hung
                        # device, not because sweeps are expected to take this long.
SETTLE_S = 0.4  # let the HackRF's USB stack settle between opens to avoid rapid
                # open/close churn contributing to device-busy/USB-hang failures.

# --- REVERTED 2026-07-22: continuous-sweep rewrite rolled back -----------------
# A ContinuousSweeper (one long-lived `hackrf_sweep` process, no `-1`, all bands
# via repeated `-f` args, background reader thread) was tried here to reduce the
# per-pass USB open/close churn that was causing ~35-50% "wedged" failures. Live
# testing on the deployment host found the multi-`-f` + continuous-stream
# assumption itself was correct (verified against the real binary), BUT the
# reader loop had a real bug: hackrf_sweep reports each band as multiple narrow
# CSV chunk-rows (e.g. ~5MHz-wide slices), and the reader buffered every
# individual chunk-row as if it were one complete full-band sweep, instead of
# concatenating the chunk-rows that together make up one real sweep of the
# configured band. This silently starved the detection logic (near-total loss
# of real output in live foreground testing) -- worse than the prior wedging
# issue, not better. Reverted to the simpler, previously-verified one-shot
# per-pass approach below rather than ship a regression. The USB-churn/wedge
# problem itself is a known, separate, lower-priority follow-up (see project
# task backlog) -- fix the chunk-concatenation bug properly before retrying
# the continuous-sweep approach.


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
        if len(parts) <= 6:
            continue
        try:
            vals = [float(p) for p in parts[6:] if p.strip()]
        except ValueError:
            continue
        powers.extend(vals)
    return powers


def sweep_band(name: str, low_mhz: int, high_mhz: int, bin_width_khz: int = 1000,
               sweeps: int = SWEEPS_PER_CYCLE) -> Tuple[List[float], float, bool]:
    """Run several hackrf_sweep passes over [low, high] MHz and take the per-bin max,
    since frequency-hopping links (e.g. DJI OcuSync) are only in-band intermittently.
    Returns (peak-held power_dbm_bins, center_freq_mhz, is_real_data).

    is_real_data is False when every attempted pass in this cycle failed (device
    wedged/not connected/permissions issue) and the returned bins are synthetic
    noise-floor filler rather than an actual sweep. Callers that track persistence
    across cycles (e.g. the Wi-Fi-AP exclusion heuristic in main()) need this
    distinction: a filler cycle is uninformative and must not be treated the same
    as a genuine "no signal this cycle" reading."""
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
    is_real_data = bool(held)
    if not held:
        # hackrf not connected / permissions issue — fall back to noise-floor filler
        # so the rest of the pipeline (console UI) still has something to show.
        held = list(np.random.normal(-65.0, 2.0, size=64))
    center = (low_mhz + high_mhz) / 2.0
    return held, center, is_real_data


def main() -> None:
    # Credentials/console URL can come from CLI args (as before) or from environment
    # variables (CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD) — the same names used by
    # rf-bridge/env.example — so a systemd unit can supply them via EnvironmentFile=
    # instead of hardcoding secrets on the ExecStart command line. CLI args, if given,
    # take precedence over the environment.
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--interval-s", type=float, default=3.0)
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"Logged in. Sweeping {len(BANDS_MHZ)} bands every {args.interval_s}s. RX ONLY — no transmission.")

    consecutive_hits = {name: 0 for name, *_ in BANDS_MHZ}
    # Tracks, per-band, how many consecutive cycles that band's peak has sat
    # within tolerance of a standard Wi-Fi channel center -- see the
    # WIFI_PERSIST_CYCLES / _nearest_wifi_channel heuristic and its documented
    # limitations above. DJI-2G4 and DJI-5G8 each get their OWN independent
    # tracker since they produce independent peaks every cycle.
    wifi_persist = {"freq_mhz": None, "cycles": 0}  # DJI-2G4 (2.4GHz table)
    wifi_persist_5g8 = {"freq_mhz": None, "cycles": 0}  # DJI-5G8 (5GHz UNII-3 table)
    i = 0
    while args.iterations == 0 or i < args.iterations:
        rows = []
        for name, low, high, label in BANDS_MHZ:
            powers, center_mhz, is_real_data = sweep_band(name, low, high)
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

            # --- Wi-Fi-AP exclusion (see WIFI_PERSIST_CYCLES comment above for the
            # full rationale and documented limitations). Applies to DJI-2G4
            # (2.4GHz table) and DJI-5G8 (5GHz UNII-3 table), each with its own
            # independent persistence tracker. SiK-915 is intentionally left
            # untouched -- Wi-Fi does not operate at 915MHz.
            likely_wifi_ap = False
            if name in ("DJI-2G4", "DJI-5G8"):
                if name == "DJI-2G4":
                    persist_state = wifi_persist
                    channel_centers = WIFI_CHANNEL_CENTERS_MHZ
                else:
                    persist_state = wifi_persist_5g8
                    channel_centers = WIFI_5G_CHANNEL_CENTERS_MHZ

                if peak > floor + DETECT_THRESHOLD_DB and powers:
                    bin_width_mhz = 1.0  # matches sweep_band's default bin width (1000 kHz)
                    peak_idx = int(np.argmax(powers))
                    peak_freq_mhz = low + (peak_idx + 0.5) * bin_width_mhz
                    nearest = _nearest_wifi_channel(peak_freq_mhz, tol_mhz=bin_width_mhz,
                                                     channel_centers=channel_centers)
                    prev_freq = persist_state["freq_mhz"]
                    if nearest and prev_freq is not None and abs(peak_freq_mhz - prev_freq) <= bin_width_mhz:
                        persist_state["cycles"] += 1
                    elif nearest:
                        persist_state["cycles"] = 1
                    else:
                        persist_state["cycles"] = 0
                    persist_state["freq_mhz"] = peak_freq_mhz if nearest else None
                    if nearest and persist_state["cycles"] >= WIFI_PERSIST_CYCLES:
                        likely_wifi_ap = True
                        ch, _center = nearest
                        print(f"[{label}] excluded likely-WiFi-AP at {peak_freq_mhz:.1f}MHz "
                              f"(channel {ch}, persisted {persist_state['cycles']} cycles)")
                elif is_real_data:
                    # No hit this cycle in this band, AND this was a real sweep
                    # (not wedge/fallback filler) — genuinely below threshold or
                    # the signal moved off-channel, so reset persistence tracking.
                    # A later hit at the same channel has to re-earn WIFI_PERSIST_CYCLES.
                    persist_state["cycles"] = 0
                    persist_state["freq_mhz"] = None
                # else: this cycle used wedge/fallback filler data (is_real_data is
                # False) — see WIFI_PERSIST_CYCLES comment block above for why we
                # deliberately leave persist_state untouched here instead of resetting.

            if consecutive_hits[name] >= CONFIRM_CYCLES and not likely_wifi_ap:
                # Coarse RSSI-based distance ESTIMATE (log-distance path-loss model,
                # no site calibration) — see estimate_distance_m() above. Flagged
                # via distance_estimated so the console/operators know this is not
                # a precise range measurement.
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
                    "distance_estimated": True,  # RSSI path-loss model, not a real range measurement
                    "source": "SIK_RADIO" if name == "SiK-915" else "HACKRF",
                }
                try:
                    requests.post(f"{args.console_url}/api/detections/ingest", json=det, headers=headers, timeout=5)
                    print(f"[{label}] CONFIRMED contact: peak {peak:.1f} dBm ({peak - floor:.1f} dB above floor, "
                          f"{consecutive_hits[name]} consecutive cycles)")
                except requests.RequestException as e:
                    print(f"ingest failed: {e}", file=sys.stderr)
            elif consecutive_hits[name] > 0 and not likely_wifi_ap:
                print(f"[{label}] possible contact: peak {peak:.1f} dBm — awaiting confirmation "
                      f"({consecutive_hits[name]}/{CONFIRM_CYCLES} cycles)")

        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
