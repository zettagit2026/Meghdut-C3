#!/usr/bin/env python3
"""Passive HackRF spectrum sweep + energy-detection bridge for CEMA cUAS.

RECEIVE ONLY. No transmission happens in this script. Safe to run anywhere.

Sweeps the DJI OcuSync/video bands (2.4GHz, 5.8GHz) and the SiK telemetry
ISM band (915MHz), looks for energy above a noise-floor threshold, and
pushes real waterfall rows + detections into the CEMA console over its
existing /api/spectrum/ingest and /api/detections/ingest endpoints.

BAND COVERAGE (expanded 2026-07-25, Army directive priority (a) / task #51
(C18)): by default this also cycles through 433MHz (LRS-433), 868MHz
(SRD-868), and 1.2/1.3GHz analog FPV video (FPV-1G3) -- see
EXTRA_BANDS_MHZ/load_bands_config() below for rationale, and set
HACKRF_EXTRA_BANDS=0 to revert to only the original 3 bands. The full band
list can also be overridden via HACKRF_BANDS_JSON. This is still a
prioritized sub-band cycle, not a continuous 400MHz-6GHz sweep -- true
continuous wideband coverage needs multiple synchronized SDRs / dedicated
antennas, which is hardware-procurement scope (task #55), not this script.

Requires `hackrf_sweep` (from the `hackrf` package) on PATH.

MULTI-DEVICE NOTE (2026-07): a second physical HackRF now exists (not yet
passed through to this VM). `_one_sweep()`/`sweep_band()` below can now
pin their `hackrf_sweep` invocation to one specific unit via the
HACKRF_RX_SERIAL env var, which is passed through as `hackrf_sweep -d
<serial>` -- the same `-d <serial>` device-select flag `hackrf_transfer`
already uses (see iq_capture.py), and the standard convention across the
`hackrf` CLI tool family (all built on the same libhackrf device-open code,
which takes a serial for `hackrf_device_open_by_serial()`). This could not
be verified against `hackrf_sweep --help` on this machine (hackrf-tools is
not installed here -- only the VM running the actual devices has it), so
IF a future `hackrf_sweep` build uses a different flag name, update the
`DEVICE_SELECT_FLAG` constant below. When HACKRF_RX_SERIAL is unset
(default), no `-d` flag is added at all -- unchanged "whichever HackRF
responds first" behavior, exactly as before this change.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests

from hackrf_device_lock import HackrfDeviceBusy, hackrf_device_lock
from consumer_iot_signatures import consumer_iot_annotation
from rf_features import compute_bandwidth_mhz

# See MULTI-DEVICE NOTE above. Empty/unset = backward-compatible default
# (no device selector passed to hackrf_sweep, first-responding HackRF wins).
HACKRF_RX_SERIAL = os.environ.get("HACKRF_RX_SERIAL") or None
DEVICE_SELECT_FLAG = "-d"  # matches hackrf_transfer's convention (see iq_capture.py)

# --- Band configuration (expanded 2026-07-25, Army directive priority (a) /
# task #51 (C18): "entire spectrum scan for detection and identification") ---
#
# The HackRF One's real tuning range is ~1MHz-6GHz. A single HackRF cannot
# usefully sweep that ENTIRE range continuously at fine resolution and still
# revisit each sub-band often enough to catch short/bursty transmissions
# (frequency-hopping OcuSync, intermittent SiK telemetry, etc.) --  every
# extra band added to the cycle proportionally lowers the revisit rate for
# every other band, since sweep_band() runs each configured band in series
# once per outer while-loop iteration (see main()). So this is NOT literally
# "scan every MHz from 400M-6G" -- it's a prioritized, evidence-based list of
# real, narrow ISM/LRS/video sub-bands that actual consumer/DIY drone
# hardware transmits on, cycled through the same way the original 3 bands
# were. Anything needing true simultaneous wideband coverage (continuous
# 400MHz-6GHz, multiple synchronized SDRs, dedicated antennas for the very
# low/high ends) is hardware-procurement scope -- that's task #55, explicitly
# NOT attempted here.
#
# DEFAULT_BANDS_MHZ: the two original always-on bands. Contents UNCHANGED
# from before this change -- this is additive, not a rewrite.
DEFAULT_BANDS_MHZ: List[Tuple[str, int, int, str]] = [
    ("SiK-915", 902, 928, "SiK/ISM 915MHz"),
    ("DJI-2G4", 2400, 2483, "OcuSync/Wi-Fi 2.4GHz"),
    ("DJI-5G8", 5725, 5850, "OcuSync 5.8GHz"),
]

# EXTRA_BANDS_MHZ: additional sub-bands added 2026-07-25 as the software-
# achievable part of expanding coverage toward 400MHz-6GHz within the
# existing HackRF One's tuning range (no new hardware). Prioritized by real
# consumer/DIY drone RF usage, not arbitrary spectrum:
#   - LRS-433 (420-450MHz): 433MHz ISM/70cm-ham band used by long-range
#     control links (TBS Crossfire/ExpressLRS-class LRS radios) and generic
#     telemetry modules, common in India/EU where 915MHz SiK is restricted
#     or less common than in the US. Already flagged for the separate
#     consumer-IoT/RF-Protocol-Database signature work (task #72) -- that
#     task is about protocol/device *fingerprints* in this band; this change
#     is only about making the band visible to the energy-detection sweep at
#     all. The two should stay coordinated (same band, different layers of
#     analysis) but are not merged here.
#   - SRD-868 (863-870MHz): EU SRD860 ISM band, the EU-region telemetry/LRS
#     analog of SiK-915 (which is primarily a US ISM allocation) -- covers
#     LRS/telemetry hardware configured for EU-legal frequencies instead of
#     915MHz.
#   - FPV-1G3 (1080-1300MHz): legacy/DIY analog FPV video ("1.2GHz"/"1.3GHz"
#     band, exact legal sub-range varies by country/hobbyist convention).
#     This is a genuinely common analog FPV video band on kit-built/racing
#     drones that predates and coexists with the digital 5.8GHz OcuSync/DJI
#     video link already covered by DJI-5G8 -- a real, previously-uncovered
#     gap, not a duplicate of the 5.8GHz band.
# NOTE: a completely-uncovered major FPV video band, 5.8GHz analog/digital
# (5725-5850MHz), was flagged as a top candidate in the original task
# framing -- but DJI-5G8 in DEFAULT_BANDS_MHZ above already covers exactly
# that range (added in an earlier pass), so no new 5.8GHz band is added here.
EXTRA_BANDS_MHZ: List[Tuple[str, int, int, str]] = [
    ("LRS-433", 420, 450, "433MHz ISM/70cm-ham LRS & telemetry (Crossfire/ExpressLRS-class)"),
    ("SRD-868", 863, 870, "868MHz EU SRD860 ISM LRS/telemetry"),
    ("FPV-1G3", 1080, 1300, "1.2/1.3GHz analog FPV video (legacy/DIY racing drones)"),
]


def load_bands_config(env: Optional[Dict[str, str]] = None) -> List[Tuple[str, int, int, str]]:
    """Build the active band list, configurable without code changes so the
    sweep can be tuned as more bands get added incrementally.

    Priority order:
      1. HACKRF_BANDS_JSON — a full override, JSON array of
         [name, low_mhz, high_mhz, label] 4-tuples/lists, e.g.:
           HACKRF_BANDS_JSON='[["SiK-915",902,928,"SiK"],["LRS-433",420,450,"433"]]'
         Invalid JSON (malformed, wrong shape, empty list) is logged to
         stderr and IGNORED -- falls through to the built-in list below
         rather than crashing the bridge or silently sweeping zero bands.
      2. HACKRF_EXTRA_BANDS — "0"/"false"/"no"/"off" disables EXTRA_BANDS_MHZ,
         leaving only DEFAULT_BANDS_MHZ (the original, pre-2026-07-25
         behavior). Anything else (including unset, the default) enables
         the extra bands -- opt-out, not opt-in, per the Army directive's
         priority on expanding coverage now.

    `env` defaults to os.environ; a caller-supplied dict is accepted purely
    to make this testable without mutating process-global environment state.
    """
    env = os.environ if env is None else env

    override = env.get("HACKRF_BANDS_JSON")
    if override:
        try:
            data = json.loads(override)
            bands = [(str(b[0]), int(b[1]), int(b[2]), str(b[3])) for b in data]
            if not bands:
                raise ValueError("HACKRF_BANDS_JSON parsed to an empty list")
            return bands
        except (ValueError, TypeError, IndexError, KeyError, json.JSONDecodeError) as e:
            print(f"WARN: invalid HACKRF_BANDS_JSON ({e}) -- falling back to the "
                  f"built-in band list instead of crashing or sweeping nothing.",
                  file=sys.stderr)

    bands = list(DEFAULT_BANDS_MHZ)
    extra_flag = env.get("HACKRF_EXTRA_BANDS", "1").strip().lower()
    if extra_flag not in ("0", "false", "no", "off"):
        bands = bands + list(EXTRA_BANDS_MHZ)
    return bands


BANDS_MHZ: List[Tuple[str, int, int, str]] = load_bands_config()

# Per-band noise floor, site-calibrated 2026-07-16 via hackrf_baseline_test.py
# for the original 3 bands. SiK-915 runs hotter at this site (~-53 to -60dBm
# quiet) than the 2.4/5.8GHz bands (~-57 to -59dBm quiet) — a single global
# floor over-triggered on SiK.
#
# The three bands added 2026-07-25 (LRS-433, SRD-868, FPV-1G3) are NOT yet
# site-calibrated -- they reuse the -58dBm DJI-2G4/5G8 ballpark as a
# placeholder floor until hackrf_baseline_test.py is actually run against
# them at the deployment site. Flagged here, not hidden: detections in these
# bands should be treated as less-tuned than the original 3 until that
# calibration pass happens.
BAND_NOISE_FLOOR_DBM = {
    "SiK-915": -50.0,
    "DJI-2G4": -58.0,
    "DJI-5G8": -57.0,
    "LRS-433": -58.0,  # placeholder, not site-calibrated -- see note above
    "SRD-868": -58.0,  # placeholder, not site-calibrated -- see note above
    "FPV-1G3": -58.0,  # placeholder, not site-calibrated -- see note above
}
DEFAULT_NOISE_FLOOR_DBM = -58.0  # fallback for any band name not in the table
                                   # above (e.g. a custom HACKRF_BANDS_JSON
                                   # entry) so a lookup miss degrades to a
                                   # reasonable default instead of a KeyError
                                   # crashing the sweep loop.
DETECT_THRESHOLD_DB = 15.0  # dB above that band's floor to call it a contact

# --- Per-band detection labeling ---------------------------------------------
# Previously this was two inline ternaries in main() keyed off `"DJI" in name`
# / `name == "SiK-915"` -- that worked when there were exactly 3 hardcoded
# bands, but silently mislabeled anything else as "MAVLink craft (candidate)"
# / "SiK/MAVLink" once new bands were added (e.g. an FPV-1G3 hit would have
# been reported as a MAVLink/SiK contact, which is wrong). Generalized to an
# explicit per-band table instead. Values for SiK-915/DJI-2G4/DJI-5G8 below
# are UNCHANGED from the original inline logic -- same strings, same output.
BAND_DETECTION_META = {
    "SiK-915": {"model": "MAVLink craft (candidate)", "protocol": "SiK/MAVLink",
                "source": "SIK_RF_HEURISTIC"},
    "DJI-2G4": {"model": "DJI Mini (candidate)", "protocol": "OcuSync/Wi-Fi",
                "source": "HACKRF"},
    "DJI-5G8": {"model": "DJI Mini (candidate)", "protocol": "OcuSync/Wi-Fi",
                "source": "HACKRF"},
    # Added 2026-07-25 for the new bands -- see EXTRA_BANDS_MHZ comment above
    # for the rationale behind each band's real-world association.
    "LRS-433": {"model": "LRS/telemetry craft (candidate)",
                "protocol": "433MHz ISM LRS/telemetry", "source": "HACKRF"},
    "SRD-868": {"model": "LRS/telemetry craft (candidate)",
                "protocol": "868MHz SRD LRS/telemetry", "source": "HACKRF"},
    "FPV-1G3": {"model": "Analog FPV video craft (candidate)",
                "protocol": "1.2/1.3GHz analog FPV video", "source": "HACKRF"},
}
# Fallback for any band name not in the table above (e.g. a custom band added
# only via HACKRF_BANDS_JSON) -- degrades to a generic, honestly-unclassified
# label instead of a KeyError or a wrong DJI/SiK guess.
DEFAULT_DETECTION_META = {"model": "Unidentified emitter (candidate)",
                           "protocol": "unclassified", "source": "HACKRF"}

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
    # Added 2026-07-25 -- same "no site calibration, documented assumption"
    # caveat as above; -30dBm@1m is a generic low/mid-power-transmitter
    # ballpark reused across bands absent per-band survey data.
    "LRS-433": -30.0,
    "SRD-868": -30.0,
    "FPV-1G3": -30.0,
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
    # Added 2026-07-25 -- 433/868MHz penetrate/diffract similarly to or
    # better than 915MHz (lower frequency), so grouped with SiK-915's
    # exponent; FPV-1G3 (1.2/1.3GHz) sits between SiK-915 and the 2.4/5.8GHz
    # bands, so it gets the same 2.5 default as the other video-class bands.
    "LRS-433": 2.3,
    "SRD-868": 2.3,
    "FPV-1G3": 2.5,
}

# --- ELRS/Crossfire-class FHSS hop-interval-consistency heuristic ----------
# ADDED 2026-07-25 (task #88), per Software Architect design review that
# REJECTED an earlier naive approach (fixed -75dBm global threshold +
# unique_freqs/time_span "hop_rate" formula flagging anything > 0.4). That
# was rejected because it conflates ordinary frequency diversity (e.g. a
# few stray Wi-Fi APs on different channels) with real hopping behavior,
# uses an arbitrary constant instead of real protocol timing, and repeats
# the site-calibration mistake already fixed elsewhere in this file (a
# single global dBm floor over-triggers -- SiK-915 alone runs ~10dB hotter
# here than the other bands; see BAND_NOISE_FLOOR_DBM above).
#
# This heuristic applies ONLY to LRS-433 and SRD-868 (task #51's new
# sub-1GHz LRS/telemetry bands) -- it does not touch DJI-2.4GHz/SiK-915/
# DJI-5.8GHz or FPV-1G3's existing detection behavior in any way.
#
# ELRS_HOP_RATE_RANGE_HZ derivation (verified directly against the real
# ExpressLRS firmware source, not assumed):
#   ~/Desktop/Zettawise/PMO Suraj/tool/ExpressLRS/src/include/common.h
#     line 169: "uint8_t FHSShopInterval; // every X packets we hop to a
#     new frequency."
#   ~/Desktop/Zettawise/PMO Suraj/tool/ExpressLRS/src/src/common.cpp
#     air_rate_config tables (SX127x/LR11XX/SX1280), each row encoding
#     (..., TLM_RATIO, FHSShopInterval, interval_us, PayloadLength, numOfSends).
#     packet_rate_hz = 1e6 / interval_us; hop_rate_hz = packet_rate_hz / FHSShopInterval.
#   Sub-1GHz (RATE_LORA_900_*) rows, the band class LRS-433/SRD-868 are
#   modeled on:
#     900_200HZ:  interval=5000us  (200Hz), FHSShopInterval=4  -> 50.0 Hz
#     900_100HZ:  interval=10000us (100Hz), FHSShopInterval=4  -> 25.0 Hz
#     900_100HZ_8CH: interval=10000us (100Hz), FHSShopInterval=4 -> 25.0 Hz
#     900_50HZ:   interval=20000us (50Hz),  FHSShopInterval=4  -> 12.5 Hz
#     900_25HZ:   interval=40000us (25Hz),  FHSShopInterval=2  -> 12.5 Hz
#   2.4GHz (RATE_LORA_2G4_*, both LR11XX and SX1280 tables) rows, included
#   here because "ELRS/Crossfire-class" hop timing is a property of the
#   protocol's packet-rate/FHSShopInterval design, not the specific ISM
#   band, and the task explicitly asked for ELRS's full supported hop-rate
#   span, not just its sub-1GHz subset:
#     2G4_500HZ:  interval=2000us  (500Hz), FHSShopInterval=4  -> 125.0 Hz
#     2G4_250HZ:  interval=4000us  (250Hz), FHSShopInterval=4  -> 62.5 Hz
#     2G4_200HZ:  interval=5000us  (200Hz), FHSShopInterval=4  -> 50.0 Hz
#     2G4_150HZ:  interval=6666us  (~150Hz), FHSShopInterval=4 -> 37.5 Hz
#     2G4_100HZ:  interval=10000us (100Hz), FHSShopInterval=4  -> 25.0 Hz
#     2G4_50HZ:   interval=20000us (50Hz),  FHSShopInterval=2  -> 25.0 Hz
#   So the real, source-derived hop-rate span across ELRS's air rates is
#   12.5 Hz (900_50HZ/900_25HZ) to 125.0 Hz (2G4_500HZ) -- confirming, not
#   just assuming, the "roughly 12.5-125 hops/sec" figure from today's
#   design review. TBS Crossfire (CRSF) uses a comparable order-of-magnitude
#   hop scheme but its firmware source was not available locally to verify
#   line-by-line, so "ELRS/Crossfire-class" here means "consistent with
#   this family of LRS hop timing," not a Crossfire-specific verified figure.
ELRS_HOP_RATE_RANGE_HZ: Tuple[float, float] = (12.5, 125.0)

# A rate far above ELRS_HOP_RATE_RANGE_HZ's upper bound is the same class of
# signature the existing Bluetooth non-persistence heuristic targets (BLE/
# classic BT hops ~1600 times/sec -- see BT_NONPERSIST_CYCLES block above).
# Anything whose *implied* reappearance rate is at/above this is treated as
# "BT-like" for routing purposes (per design step 4: route it away from an
# ELRS conclusion instead of double-flagging), not as new evidence of ELRS.
ELRS_BT_LIKE_MIN_HZ = 500.0  # well below real BT's ~1600Hz, well above ELRS's 125Hz max

# HOP_FREQ_TOL_MHZ / HOP_POWER_TOL_DB: how close a reappearing peak's
# frequency/power has to be to the PREVIOUS hit to count as "the same
# emitter reappearing" rather than an unrelated new signal. Frequency
# tolerance is loose (a hopping link legitimately lands at a different
# in-band frequency each time) -- what actually gets compared here is
# POWER (a real emitter at a stable range/orientation should reappear at
# roughly the same RSSI) and TIMING (the interval since it last appeared).
HOP_POWER_TOL_DB = 8.0
HOP_CONFIRM_CYCLES = 3  # require this many consecutive real-data reappearances
                         # classified "elrs_consistent" before treating the
                         # hop-interval pattern as corroborating evidence --
                         # same CONFIRM_CYCLES-style gating used elsewhere in
                         # this file, so a one-off spike/coincidental timing
                         # match can never trigger this on its own.
HOP_TRACKED_BANDS = ("LRS-433", "SRD-868")  # additive only -- see module docstring

# --- 902-928 MHz (SiK-915 band) hop tracking, GUARDED -----------------------
# US ELRS-900 / TBS Crossfire-915 long-range control links share the SiK-915
# band (902-928 MHz) with SiK/MAVLink telemetry and low-duty LoRa/consumer-IoT
# traffic. Extending the ELRS/Crossfire hop-interval-consistency heuristic to
# this band lets a genuinely hopping LRS emitter be corroborated, but it MUST
# be guarded so the band's OTHER occupants are never mislabeled as hopping:
#   1. LoRa guard: if the SiK-915 low-duty-cycle exclusion (likely_low_duty_
#      cycle_device, see LORA_WINDOW_CYCLES) fired this cycle, the hop tracker
#      is reset and never contributes evidence -- a low-duty LoRa burst can't
#      become an "LRS hopping" call.
#   2. Continuous-carrier guard: a real ELRS-900/Crossfire link is wideband
#      FHSS -- it lands on a DIFFERENT in-band frequency across observations.
#      A fixed-frequency continuous telemetry carrier does not. So a cycle
#      only counts toward hop-consistency when the peak actually MOVED in
#      frequency by more than HOP_FREQ_MIN_MOVE_MHZ (require_freq_hop=True in
#      update_hop_track). This is corroboration in ADDITION to the existing
#      timing check (classify_hop_interval already rejects the several-second
#      sweep-cadence reappearance of a continuous link as "no_match").
# Labels stay family-level "Sub-GHz LRS / ELRS-Crossfire-class" -- never an
# ELRS-vs-Crossfire-vs-SiK determination.
SUBGHZ_900_HOP_BAND = "SiK-915"
HOP_FREQ_MIN_MOVE_MHZ = 3.0  # a wideband-FHSS LRS link lands far apart across
                              # the 26 MHz band; a continuous carrier stays put
                              # (well under this, and under BT_MOVE_TOL_MHZ's slop).


def classify_hop_interval(interval_s: Optional[float],
                           hop_range_hz: Tuple[float, float] = ELRS_HOP_RATE_RANGE_HZ,
                           bt_like_min_hz: float = ELRS_BT_LIKE_MIN_HZ) -> str:
    """Classify an observed peak-reappearance interval (seconds, wall-clock
    time since the same-power peak last appeared in-band) against ELRS's
    real hop-rate span.

    Returns one of:
      "insufficient"    -- no prior sample to compare against yet.
      "bt_like"         -- implied rate is far faster than any ELRS air rate
                           (>= bt_like_min_hz) -- consistent with the existing
                           Bluetooth non-persistence signature, NOT ELRS; the
                           caller must not flag this as ELRS-consistent so as
                           not to double-flag the same underlying pattern.
      "elrs_consistent" -- implied rate falls within hop_range_hz.
      "no_match"         -- implied rate is outside both of the above (e.g. a
                           stationary/non-hopping signal, whose reappearance
                           interval is dominated by this bridge's own several-
                           second sweep cadence rather than any real hopping).

    Pure function, deliberately hardware/timing-source agnostic, so it can be
    unit-tested with synthetic interval sequences without a real HackRF.
    """
    if interval_s is None or interval_s <= 0:
        return "insufficient"
    rate_hz = 1.0 / interval_s
    if rate_hz >= bt_like_min_hz:
        return "bt_like"
    lo, hi = hop_range_hz
    if lo <= rate_hz <= hi:
        return "elrs_consistent"
    return "no_match"


def update_hop_track(track: Dict, now: float, peak_dbm: float, is_hit: bool,
                      is_real_data: bool, peak_freq_mhz: Optional[float] = None,
                      require_freq_hop: bool = False,
                      freq_hop_min_move_mhz: float = HOP_FREQ_MIN_MOVE_MHZ
                      ) -> Tuple[bool, Optional[float]]:
    """Update a per-band ELRS hop-interval-consistency tracker in place (same
    shape/pattern as wifi_persist/bt_track/sik_hit_window above) and return
    (hop_consistent, last_rate_hz).

    `track` keys: last_time (float|None), last_power_dbm (float|None),
    consistent_cycles (int); when require_freq_hop is used, also
    last_freq_mhz (float|None).

    require_freq_hop / peak_freq_mhz (default off, so existing LRS-433/SRD-868
    callers are byte-for-byte unchanged): when True, an otherwise timing-
    consistent cycle only counts if the peak also MOVED in frequency by more
    than freq_hop_min_move_mhz since the previous hit. This is the
    continuous-carrier guard used for the SiK-915 (902-928 MHz) band, where a
    fixed-frequency continuous telemetry carrier must never be mislabeled as a
    wideband-FHSS ELRS-900/Crossfire hopper (see SUBGHZ_900_HOP_BAND above).

    Only called for HOP_TRACKED_BANDS (LRS-433/SRD-868). Only real-data hit
    cycles (is_real_data and is_hit) advance/verify the pattern -- a wedge/
    fallback filler cycle leaves the tracker untouched (same is_real_data-
    aware convention as the Wi-Fi/Bluetooth/LoRa trackers above), and a
    real-data cycle with no hit resets the pattern (nothing reappeared).

    IMPORTANT, stated plainly: this bridge's sweep cadence (several seconds
    per full band cycle -- see args.interval_s / SWEEPS_PER_CYCLE) is far
    coarser than the true ELRS hop period (8-80ms, per ELRS_HOP_RATE_RANGE_HZ
    above). This function computes what it can actually observe -- the
    wall-clock interval between this bridge's own successive detections of a
    similar-power peak in-band -- and classifies THAT against ELRS's real
    hop-rate span. It is a power-spectrum-sweep heuristic, not a hop-rate
    measurement in the strict sense: a genuinely continuous/non-hopping
    emitter will reappear at roughly this bridge's own sweep cadence, which
    is far below ELRS_HOP_RATE_RANGE_HZ and will correctly classify as
    "no_match", not "elrs_consistent". See classify_hop_interval()'s
    docstring for the full classification contract.
    """
    if not is_real_data:
        return track["consistent_cycles"] >= HOP_CONFIRM_CYCLES, track.get("last_rate_hz")

    if not is_hit:
        track["last_time"] = None
        track["last_power_dbm"] = None
        track["last_freq_mhz"] = None
        track["consistent_cycles"] = 0
        track["last_rate_hz"] = None
        return False, None

    prev_time = track.get("last_time")
    prev_power = track.get("last_power_dbm")
    prev_freq = track.get("last_freq_mhz")
    classification = "insufficient"
    rate_hz = None
    if prev_time is not None and prev_power is not None:
        interval_s = now - prev_time
        if abs(peak_dbm - prev_power) <= HOP_POWER_TOL_DB:
            classification = classify_hop_interval(interval_s)
            if classification == "elrs_consistent":
                rate_hz = 1.0 / interval_s if interval_s > 0 else None
        else:
            classification = "no_match"  # power jumped too much -- likely a different emitter

    # Continuous-carrier guard (SiK-915 band, require_freq_hop=True): a wideband
    # FHSS LRS link lands on a DIFFERENT in-band frequency each observation; a
    # fixed-frequency continuous telemetry carrier does not. Downgrade an
    # otherwise timing-consistent cycle to "no_match" when the peak did NOT move
    # far enough in frequency, so a continuous link is never called hopping.
    if (classification == "elrs_consistent" and require_freq_hop
            and prev_freq is not None and peak_freq_mhz is not None
            and abs(peak_freq_mhz - prev_freq) <= freq_hop_min_move_mhz):
        classification = "no_match"
        rate_hz = None

    if classification == "elrs_consistent":
        track["consistent_cycles"] = track.get("consistent_cycles", 0) + 1
        track["last_rate_hz"] = rate_hz
    else:
        track["consistent_cycles"] = 0
        track["last_rate_hz"] = None

    track["last_time"] = now
    track["last_power_dbm"] = peak_dbm
    track["last_freq_mhz"] = peak_freq_mhz

    return track["consistent_cycles"] >= HOP_CONFIRM_CYCLES, track.get("last_rate_hz")


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
# --- Bluetooth exclusion heuristic (DJI-2G4 only) ---------------------------
# ADDED 2026-07-22: DJI-2G4 (2400-2483MHz) overlaps the classic Bluetooth band
# (2.402-2.480GHz, 79x1MHz FHSS channels hopping ~1600 times/sec; BLE hops
# across 40 channels). This is a SEPARATE exclusion reason from the Wi-Fi-AP
# heuristic above, checked independently on the same band/peak.
#
# Physical reasoning: WiFi sits STILL on one fixed channel for a long time --
# that's exactly why the persistence heuristic above works for it. Bluetooth
# does the OPPOSITE: it hops continuously and essentially never settles on
# one channel. A drone OcuSync/video link is also frequency-agile/bursty, but
# its occupied bandwidth is much wider (several MHz of video/control signal)
# than a single ~1MHz Bluetooth hop channel.
#
# HARD HARDWARE LIMITATION (stated plainly, not hidden): this system's
# hackrf_sweep cycle re-scans the whole configured range roughly once every
# --interval-s seconds (SWEEPS_PER_CYCLE=2 passes per cycle). It CANNOT
# resolve genuine microsecond-scale Bluetooth hop timing -- that would require
# IQ capture/demodulation, which is out of scope here (same caveat as the
# Wi-Fi heuristic: no protocol classification, energy-detection only). What
# IS observable at this sweep rate is a much coarser proxy: does the
# strongest peak's frequency location keep moving to a materially different
# spot on almost every consecutive REAL-DATA cycle (never settling anywhere
# for BT_NONPERSIST_CYCLES in a row), AND is the peak narrow enough
# (<= BT_MAX_BANDWIDTH_MHZ) to be consistent with a single ~1MHz BT channel
# rather than a several-MHz-wide OcuSync/video carrier.
#
# KNOWN LIMITATION -- read before trusting this in the field: this is a MUCH
# CRUDER signal than the Wi-Fi persistence heuristic. At a ~3s+ sweep
# interval we are aliasing thousands of real Bluetooth hops per cycle down to
# a single peak-held reading, so "moves every cycle" is at best a weak
# correlate of real FHSS behavior -- plenty of things (multiple simultaneous
# emitters, noise, a genuinely hopping drone link) could also produce a
# peak that appears to move cycle-to-cycle. Expect a HIGHER false-positive
# AND false-negative rate than the Wi-Fi case:
#   - False positive: a real, weak/marginal drone link whose peak estimate
#     jitters between adjacent bins cycle-to-cycle (due to noise near
#     threshold, not real hopping) could be misclassified as Bluetooth and
#     suppressed.
#   - False negative: a drone link that happens to be narrowband and briefly
#     "settles" for a few cycles (e.g. hovering, momentarily locked video
#     channel) will simply fail the non-persistence check and pass through
#     un-excluded -- i.e. this heuristic will often just do nothing, which is
#     the safer failure mode but means it should not be read as "we detect
#     Bluetooth reliably."
# This is accepted as a best-effort coarse filter, not real BT/BLE
# classification, given the single-HackRF/no-IQ-demod constraint.
BT_NONPERSIST_CYCLES = 5  # consecutive real-data cycles the peak must keep moving
                           # (never settling) before we call it likely-Bluetooth.
                           # Matches WIFI_PERSIST_CYCLES in magnitude so both
                           # heuristics require a comparably sustained pattern
                           # before acting, even though the patterns are inverse.
BT_MOVE_TOL_MHZ = 2.0  # peak must move by more than this between cycles to count
                       # as "did not settle" (a little slack above 1 bin-width
                       # avoids treating ordinary bin-to-bin measurement noise
                       # as movement).
BT_MAX_BANDWIDTH_MHZ = 2.0  # a single BT/BLE channel is ~1MHz; allow some sweep
                             # slop but keep this well under a multi-MHz OcuSync
                             # video carrier's occupied bandwidth.
#
# --- LoRa / low-duty-cycle exclusion heuristic (SiK-915 only) ---------------
# ADDED 2026-07-22: SiK-915 (902-928MHz) currently has ZERO exclusion logic --
# this is new logic for that band, not an extension of an existing check.
#
# Physical reasoning: a real SiK telemetry radio (what this pipeline's
# "SIK_RADIO" classification represents) is a continuous/near-continuous
# real-time control/telemetry link while the drone is airborne -- it should
# show up as a hit on most/all cycles. LoRa/LoRaWAN devices sharing this ISM
# band, by contrast, are regulatorily constrained to a very low duty cycle
# (commonly under 1% airtime) -- a LoRa sensor/gateway sends a brief packet
# then goes silent for seconds-to-minutes. So: track, over a rolling window
# of the last LORA_WINDOW_CYCLES REAL-DATA cycles (using is_real_data from
# sweep_band() to correctly ignore wedge/filler cycles, same pattern as the
# Wi-Fi heuristic), what fraction of cycles had a genuine above-threshold hit.
# Sparse/intermittent hits (isolated, never sustained) => likely a low-duty-
# cycle device (LoRa-like) => exclude from drone-detection ingest for that
# cycle. Sustained/continuous hits => leave it alone, consistent with a real
# SiK link.
#
# KNOWN LIMITATION (explicitly not hidden, same class of tradeoff as the
# Wi-Fi false-negative risk documented above): a real drone with a marginal
# or weak SiK link -- e.g. at the edge of range, partially obstructed, or
# suffering interference -- could also produce intermittent/sparse hits and
# get misclassified as "likely_low_duty_cycle_device", suppressing a genuine
# contact. This tradeoff is accepted because a LoRa sensor/gateway being
# reported as a "MAVLink craft (candidate)" drone is the more visible/costly
# failure mode to avoid; there is no IQ/demodulation-based way to tell a weak
# real SiK link from a genuine LoRa packet apart from this duty-cycle proxy
# given current hardware.
LORA_WINDOW_CYCLES = 8  # rolling window size (in REAL-DATA cycles) over which
                         # duty cycle is evaluated for SiK-915.
LORA_MIN_REAL_CYCLES = 4  # require at least this many real-data samples in the
                           # window before making a duty-cycle judgement at all
                           # (avoid snap judgements off 1-2 samples right after
                           # startup or a run of wedged cycles).
LORA_MAX_HIT_RATIO = 0.5  # if the fraction of real-data cycles in the window
                           # that were above-threshold hits is AT or BELOW this,
                           # treat it as sparse/intermittent (LoRa-like) rather
                           # than a persistent/continuous SiK telemetry link.

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


# Identifies this bridge in the backend's ingest_health tracking (task #74) --
# see GET /api/health's ingest_sources / preflight.sh.
BRIDGE_NAME = "hackrf_rx"


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5,
                       bridge_name: str = None) -> "requests.Response":
    """POST to the backend, auto-recovering from an expired JWT.

    The backend's JWT TTL is 12h (create_access_token() in backend/server.py)
    and this bridge is meant to run forever under systemd Restart=always, so
    a token obtained once at startup WILL expire mid-run. Before this helper
    existed, that produced a silent, PERMANENT 401 on every subsequent
    ingest call until someone noticed and restarted the service by hand --
    this is exactly the bug that hit hackrf_rx.py/ml_classify_bridge.py/
    fpv_video_bridge.py in production (real detections computed, never
    ingested, only a 401 buried in each service's own log).

    On a 401, re-login ONCE and retry the same request. `headers` is
    mutated in place (same dict object the caller holds), so the caller's
    cached token is transparently refreshed for all future calls too. If
    the retry ALSO 401s, that's a real auth problem (bad/rotated
    credentials), not just an expired token -- log it clearly and return
    the failing response rather than looping forever or crashing the
    bridge's main loop.
    """
    url = f"{console_url}{path}"
    # X-Bridge-Name identifies this bridge to the backend's ingest_health
    # tracking (task #74) so a recurrence of the silent-401-loop failure
    # mode this docstring describes shows up in GET /api/health /
    # preflight.sh instead of going unnoticed for hours again.
    # bridge_name lets other bridges that import this shared helper directly
    # (rather than duplicating it) identify themselves correctly instead of
    # reporting as "hackrf_rx" -- see ml_classify_bridge.py/
    # droneid_decode_bridge.py callers.
    headers.setdefault("X-Bridge-Name", bridge_name or BRIDGE_NAME)
    r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        print(f"[auth] 401 from POST {path} -- token expired, re-authenticating as {email}",
              file=sys.stderr)
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[auth] re-login failed ({e}) -- leaving stale token in place, "
                  f"will retry re-login on the next call", file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
        if r.status_code == 401:
            print(f"[auth] still 401 for POST {path} after re-authenticating -- this is a "
                  f"real auth problem (check credentials for {email}), not just an expired "
                  f"token. Continuing the normal loop rather than retrying forever.",
                  file=sys.stderr)
    return r


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


def _one_sweep(low_mhz: int, high_mhz: int, bin_width_khz: int,
               serial: Optional[str] = HACKRF_RX_SERIAL) -> List[float]:
    cmd = [
        "hackrf_sweep",
        "-f", f"{low_mhz}:{high_mhz}",
        "-w", str(bin_width_khz * 1000),
        "-1",  # one-shot
    ]
    if serial:
        cmd += [DEVICE_SELECT_FLAG, serial]
    try:
        # Device-access coordination: hackrf_rx.py's own sweep loop (this
        # function) and ml_classify_bridge.py's gate-check sweeps + IQ
        # captures (iq_capture.py's capture_iq()) both drive the SAME single
        # physical HackRF, which only supports one open handle at a time.
        # Previously these two processes relied purely on timing separation
        # (12s vs 3s interval) with no actual coordination -- a latent risk
        # of a collision if both happened to try to open the device at once.
        # This lock (see hackrf_device_lock.py) makes that mutual exclusion
        # explicit and process-safe: only one of the two processes can be
        # mid-subprocess-call against the device at a time; the other waits
        # briefly (bounded) or skips this pass rather than colliding.
        # `serial` is passed through so the lock is scoped to THIS specific
        # physical device once HACKRF_RX_SERIAL is configured -- with no
        # serial (default), this is the original shared-lockfile behavior.
        with hackrf_device_lock(serial=serial):
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=SWEEP_TIMEOUT_S)
    except FileNotFoundError:
        print("ERROR: hackrf_sweep not found. Install the `hackrf` package (brew/apt).", file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"WARN: hackrf_sweep wedged on {low_mhz}-{high_mhz}MHz (device busy/USB hang) — "
              f"killed and skipping this pass.", file=sys.stderr)
        return []
    except HackrfDeviceBusy as e:
        # The other HackRF-using process (hackrf_rx.py's sweep loop or
        # ml_classify_bridge.py's gate-check/IQ-capture) is currently holding
        # the device. Treat this exactly like a wedged/skipped pass rather
        # than crashing -- the next scheduled sweep will simply try again.
        print(f"WARN: hackrf_sweep skipped on {low_mhz}-{high_mhz}MHz — {e}", file=sys.stderr)
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
               sweeps: int = SWEEPS_PER_CYCLE,
               serial: Optional[str] = HACKRF_RX_SERIAL) -> Tuple[List[float], float, bool]:
    """Run several hackrf_sweep passes over [low, high] MHz and take the per-bin max,
    since frequency-hopping links (e.g. DJI OcuSync) are only in-band intermittently.
    Returns (peak-held power_dbm_bins, center_freq_mhz, is_real_data).

    is_real_data is False when every attempted pass in this cycle failed (device
    wedged/not connected/permissions issue) and the returned bins are synthetic
    noise-floor filler rather than an actual sweep. Callers that track persistence
    across cycles (e.g. the Wi-Fi-AP exclusion heuristic in main()) need this
    distinction: a filler cycle is uninformative and must not be treated the same
    as a genuine "no signal this cycle" reading.

    `serial`, if given, pins every pass in this cycle to one specific
    physical HackRF (see MULTI-DEVICE NOTE at the top of this file);
    defaults to HACKRF_RX_SERIAL (env var), which defaults to None/unset --
    i.e. unchanged "whichever HackRF responds first" behavior."""
    held: List[float] = []
    for _ in range(sweeps):
        powers = _one_sweep(low_mhz, high_mhz, bin_width_khz, serial=serial)
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
    # Bluetooth non-persistence tracker for DJI-2G4 only -- see BT_NONPERSIST_CYCLES
    # comment block above. Tracks the last real-data cycle's peak freq and how many
    # consecutive real-data cycles in a row the peak has kept moving (never settled).
    # advisory_posted latches True once a low-severity Bluetooth advisory has
    # been posted for the CURRENT non-persistence episode, so we POST ONCE per
    # episode (not once per cycle for the whole time BT_NONPERSIST_CYCLES stays
    # satisfied) -- reset back to False whenever moving_cycles drops (pattern
    # broke), so a genuinely new episode can post again later.
    bt_track = {"freq_mhz": None, "moving_cycles": 0, "advisory_posted": False}
    # LoRa/low-duty-cycle rolling window for SiK-915 only -- see LORA_WINDOW_CYCLES
    # comment block above. Only real-data cycles (is_real_data) are pushed into this
    # deque; wedge/filler cycles are skipped entirely, same pattern as the Wi-Fi fix.
    sik_hit_window: "collections.deque[bool]" = collections.deque(maxlen=LORA_WINDOW_CYCLES)
    # ELRS/Crossfire-class hop-interval-consistency trackers, LRS-433/SRD-868
    # only -- see update_hop_track()/classify_hop_interval() above (task #88).
    hop_track = {name: {"last_time": None, "last_power_dbm": None,
                         "last_freq_mhz": None,
                         "consistent_cycles": 0, "last_rate_hz": None}
                 for name in HOP_TRACKED_BANDS + (SUBGHZ_900_HOP_BAND,)}
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
                _post_with_reauth(
                    args.console_url, "/api/spectrum/ingest",
                    {"bins": len(powers), "rows": [powers]},
                    headers, args.email, args.password, timeout=5,
                )
            except requests.RequestException as e:
                print(f"spectrum ingest failed: {e}", file=sys.stderr)
            peak = max(powers)
            # Real per-detection peak frequency (bin_width_mhz = 1.0 matches
            # sweep_band's default bin width of 1000 kHz), computed once per
            # band per cycle, unconditionally -- this is what actually gets
            # threaded into every detection-ingest payload below (as
            # center_freq_ghz), NOT the fixed band midpoint center_mhz. See
            # ml_classify_bridge.py's peak_freq_mhz fix (task #77) for the
            # same rationale. Previously this was only computed inside the
            # Wi-Fi/Bluetooth exclusion branches below (task #99 fix).
            bin_width_mhz = 1.0
            peak_idx = int(np.argmax(powers)) if powers else 0
            peak_freq_mhz = low + (peak_idx + 0.5) * bin_width_mhz
            floor = BAND_NOISE_FLOOR_DBM.get(name, DEFAULT_NOISE_FLOOR_DBM)
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
                    # peak_freq_mhz/bin_width_mhz computed unconditionally above.
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

            # --- Bluetooth exclusion (DJI-2G4 only) -- see BT_NONPERSIST_CYCLES
            # comment block above for full rationale and honestly-stated limitations.
            # This is an ADDITIONAL, independent exclusion reason alongside the
            # Wi-Fi-AP check above -- a DJI-2G4 peak can be excluded as either
            # likely-WiFi OR likely-Bluetooth (or neither), both suppress ingest.
            likely_bluetooth = False
            if name == "DJI-2G4":
                if peak > floor + DETECT_THRESHOLD_DB and powers:
                    # peak_freq_mhz/bin_width_mhz computed unconditionally above.
                    # crude occupied-bandwidth estimate: width of the contiguous
                    # run of bins within DETECT_THRESHOLD_DB/2 of the peak, centered
                    # on peak_idx. Coarse by design -- no real spectral-mask analysis.
                    half_thresh = floor + DETECT_THRESHOLD_DB / 2.0
                    lo_idx = peak_idx
                    while lo_idx > 0 and powers[lo_idx - 1] >= half_thresh:
                        lo_idx -= 1
                    hi_idx = peak_idx
                    while hi_idx < len(powers) - 1 and powers[hi_idx + 1] >= half_thresh:
                        hi_idx += 1
                    occupied_bw_mhz = (hi_idx - lo_idx + 1) * bin_width_mhz

                    prev_bt_freq = bt_track["freq_mhz"]
                    moved = prev_bt_freq is None or abs(peak_freq_mhz - prev_bt_freq) > BT_MOVE_TOL_MHZ
                    narrow = occupied_bw_mhz <= BT_MAX_BANDWIDTH_MHZ
                    if is_real_data:
                        if moved and narrow:
                            bt_track["moving_cycles"] += 1
                        else:
                            bt_track["moving_cycles"] = 0
                            bt_track["advisory_posted"] = False  # episode broke, allow a future re-post
                        bt_track["freq_mhz"] = peak_freq_mhz
                    if bt_track["moving_cycles"] >= BT_NONPERSIST_CYCLES:
                        likely_bluetooth = True
                        print(f"[{label}] excluded likely-Bluetooth at {peak_freq_mhz:.1f}MHz "
                              f"(non-persistent {bt_track['moving_cycles']} cycles, "
                              f"~{occupied_bw_mhz:.0f}MHz wide) — coarse heuristic, see "
                              f"BT_NONPERSIST_CYCLES limitations")
                elif is_real_data:
                    bt_track["advisory_posted"] = False
                    # No hit this cycle (real sweep, genuinely below threshold) --
                    # nothing to track movement against; reset like the Wi-Fi case.
                    bt_track["moving_cycles"] = 0
                    bt_track["freq_mhz"] = None
                # else: wedge/fallback filler cycle -- leave bt_track untouched,
                # same rationale as the Wi-Fi persistence fix above.

            # --- LoRa / low-duty-cycle exclusion (SiK-915 only) -- see
            # LORA_WINDOW_CYCLES comment block above for full rationale and
            # honestly-stated limitations. This band had NO exclusion logic before;
            # this is new, not an extension of the Wi-Fi/Bluetooth checks.
            likely_low_duty_cycle_device = False
            if name == "SiK-915" and is_real_data:
                is_hit = peak > floor + DETECT_THRESHOLD_DB
                sik_hit_window.append(is_hit)
                real_count = len(sik_hit_window)
                if real_count >= LORA_MIN_REAL_CYCLES:
                    hit_ratio = sum(sik_hit_window) / real_count
                    if is_hit and hit_ratio <= LORA_MAX_HIT_RATIO:
                        likely_low_duty_cycle_device = True
                        print(f"[{label}] excluded likely_low_duty_cycle_device (LoRa-like) at "
                              f"peak {peak:.1f}dBm — hit rate {hit_ratio:.2f} over last "
                              f"{real_count} real-data cycles (<= {LORA_MAX_HIT_RATIO}) — "
                              f"coarse heuristic, see LORA_WINDOW_CYCLES limitations")
            # else (wedge/filler cycle): sik_hit_window is intentionally NOT
            # appended to, so a run of wedged cycles doesn't get counted as
            # "silence" and doesn't dilute the real-data duty-cycle estimate --
            # same is_real_data-aware pattern as the Wi-Fi/Bluetooth checks above.

            # --- ELRS/Crossfire-class hop-interval-consistency heuristic
            # (LRS-433/SRD-868 only) -- see update_hop_track()/
            # classify_hop_interval()/ELRS_HOP_RATE_RANGE_HZ above (task #88).
            # This is NOT an exclusion filter like the three checks above --
            # it's supporting evidence attached to the existing LRS/telemetry
            # detection for these two bands when it fires, gated by its own
            # HOP_CONFIRM_CYCLES so a one-off timing coincidence never
            # triggers it alone.
            hop_consistent = False
            hop_rate_hz = None
            if name in HOP_TRACKED_BANDS:
                is_hit = peak > floor + DETECT_THRESHOLD_DB
                hop_consistent, hop_rate_hz = update_hop_track(
                    hop_track[name], time.time(), peak, is_hit, is_real_data)
            elif name == SUBGHZ_900_HOP_BAND:
                # 902-928 MHz hosts SiK/MAVLink telemetry, low-duty LoRa/IoT AND
                # ELRS-900/Crossfire LRS. Track hop-consistency here too, but
                # GUARDED (see SUBGHZ_900_HOP_BAND): a low-duty LoRa burst resets
                # the tracker outright, and the require_freq_hop guard means only
                # a peak that actually MOVES across the band (wideband FHSS)
                # corroborates -- a fixed-frequency continuous carrier can't.
                is_hit = peak > floor + DETECT_THRESHOLD_DB
                if likely_low_duty_cycle_device:
                    hop_track[name].update(
                        {"last_time": None, "last_power_dbm": None,
                         "last_freq_mhz": None, "consistent_cycles": 0,
                         "last_rate_hz": None})
                else:
                    hop_consistent, hop_rate_hz = update_hop_track(
                        hop_track[name], time.time(), peak, is_hit, is_real_data,
                        peak_freq_mhz=peak_freq_mhz, require_freq_hop=True)

            likely_excluded = likely_wifi_ap or likely_bluetooth or likely_low_duty_cycle_device

            # --- Bluetooth advisory ingest (NOT a threat detection) ----------------
            # ADDED 2026-07-23: the likely_bluetooth exclusion above is intentionally
            # a SUPPRESSION filter -- it correctly prevents a BT-pattern signal from
            # ever being posted as a drone/WiFi contact, and that behavior is
            # unchanged by this block. But suppressing it entirely means an operator
            # gets zero visibility that something (probably BT) is sitting in the
            # 2.4GHz band. This posts a SEPARATE, clearly-labeled, low-severity
            # advisory record so it shows up in the Dashboard/Detection History for
            # situational awareness only -- never folded into the drone/WiFi
            # ingest path above, never affecting likely_excluded/consecutive_hits.
            #
            # Only fire when Bluetooth is the SOLE reason this cycle was excluded --
            # if likely_wifi_ap or likely_low_duty_cycle_device also fired on the
            # same band/cycle, skip the advisory to avoid a confusing double-ingest
            # (that would already be reflected as excluded for a different, more
            # specific reason).
            #
            # Paced the same way CONFIRM_CYCLES paces the drone ingest above: post
            # once when the BT_NONPERSIST_CYCLES threshold is first reached for this
            # episode (bt_track["advisory_posted"] latches True so we don't spam one
            # advisory per cycle for as long as the pattern persists), and reset that
            # latch whenever the non-persistence pattern breaks (see moving_cycles
            # reset points above) so a later, distinct BT episode can post again.
            if (name == "DJI-2G4" and likely_bluetooth and not likely_wifi_ap
                    and not likely_low_duty_cycle_device and not bt_track["advisory_posted"]):
                bt_track["advisory_posted"] = True
                bt_det = {
                    "model": "Bluetooth device (advisory)",
                    "protocol": "Bluetooth (2.4GHz ISM, rapid-hop signature)",
                    "threat_level": "LOW",
                    "center_freq_ghz": peak_freq_mhz / 1000.0,
                    "bandwidth_mhz": occupied_bw_mhz,
                    "rssi_dbm": peak,
                    "snr_db": peak - floor,
                    "bearing_deg": None,  # honest: no DF array -> direction unknown (was 0.0 fake)
                    "bearing_available": False,
                    "distance_m": 0.0,
                    "distance_estimated": False,  # not a range estimate -- advisory only
                    "source": "HACKRF",
                    "confidence_type": "advisory_only",  # presence heuristic, not an identity/threat claim
                }
                try:
                    _post_with_reauth(args.console_url, "/api/detections/ingest", bt_det,
                                       headers, args.email, args.password, timeout=5)
                    print(f"[{label}] posted low-severity Bluetooth advisory at "
                          f"{peak_freq_mhz:.1f}MHz ({bt_track['moving_cycles']} non-persistent cycles)")
                except requests.RequestException as e:
                    print(f"bluetooth advisory ingest failed: {e}", file=sys.stderr)

            if consecutive_hits[name] >= CONFIRM_CYCLES and not likely_excluded:
                # Coarse RSSI-based distance ESTIMATE (log-distance path-loss model,
                # no site calibration) — see estimate_distance_m() above. Flagged
                # via distance_estimated so the console/operators know this is not
                # a precise range measurement.
                est_distance_m = estimate_distance_m(name, peak)
                meta = BAND_DETECTION_META.get(name, DEFAULT_DETECTION_META)
                det = {
                    "model": meta["model"],
                    "protocol": meta["protocol"],
                    "threat_level": "MEDIUM",
                    "center_freq_ghz": peak_freq_mhz / 1000.0,
                    "bandwidth_mhz": high - low,  # whole SWEEP-BAND width (backward-compat)
                    # OCCUPIED bandwidth: width of the contiguous run of bins
                    # within DETECT_THRESHOLD_DB/2 of the peak (rf_features.
                    # compute_bandwidth_mhz -- the SAME coarse estimate the
                    # inline Bluetooth-exclusion block uses, now emitted for
                    # every confirmed detection). This, NOT the band width, is
                    # what the control-link classifier's wide/narrow divider
                    # needs so a narrowband 2.4 GHz hobby-RC link is not lumped
                    # in with wideband video. Coarse by design -- not a
                    # spectral-mask measurement.
                    "occupied_bw_mhz": round(
                        compute_bandwidth_mhz(powers, floor, DETECT_THRESHOLD_DB, bin_width_mhz), 1),
                    "rssi_dbm": peak,
                    "snr_db": peak - floor,
                    "bearing_deg": None,  # honest: no DF array -> direction unknown (was 0.0 fake)
                    "bearing_available": False,
                    "distance_m": round(est_distance_m, 1),
                    "distance_estimated": True,  # RSSI path-loss model, not a real range measurement
                    "source": meta["source"],
                    "confidence_type": "heuristic_binary",  # persistence-confirmed, no real-valued probability
                }
                # ELRS/Crossfire-class hop-interval-consistency evidence, LRS-433/
                # SRD-868 only (task #88) -- attached ONLY when HOP_CONFIRM_CYCLES
                # consecutive reappearances classified as "elrs_consistent" (see
                # update_hop_track() above). Labeled honestly per the design
                # review: rf_signature_only marks this as power-spectrum-sweep
                # evidence, NOT a protocol decode and NOT below-noise-floor
                # detection (that would require real IQ-level LoRa correlation,
                # out of scope -- see ELRS_HOP_RATE_RANGE_HZ comment block).
                if hop_consistent:  # LRS-433/SRD-868/SiK-915 hop-corroborated
                    det["rf_signature_only"] = True
                    det["hop_rate_hz"] = round(hop_rate_hz, 1) if hop_rate_hz is not None else None
                    det["notes"] = (
                        "Hop-interval-consistency heuristic from power-spectrum sweeps: "
                        f"this band's peak reappeared at a consistent power (within "
                        f"{HOP_POWER_TOL_DB}dB) at an interval implying a "
                        f"~{hop_rate_hz:.1f}Hz reappearance rate, within ELRS/Crossfire-"
                        f"class LRS's real supported hop-rate span "
                        f"({ELRS_HOP_RATE_RANGE_HZ[0]}-{ELRS_HOP_RATE_RANGE_HZ[1]}Hz, "
                        "derived from ExpressLRS firmware source, see hackrf_rx.py "
                        "comments). This is NOT a protocol decode (no CRSF/ELRS packet "
                        "was demodulated or parsed) and NOT a below-noise-floor "
                        "detection (no IQ-level LoRa correlation was performed) -- it "
                        "is circumstantial RF-signature evidence only, from energy "
                        "detection across sweep cycles."
                    ) if hop_rate_hz is not None else (
                        "Hop-interval-consistency heuristic from power-spectrum sweeps "
                        "flagged this band's peak-reappearance pattern as consistent "
                        "with ELRS/Crossfire-class LRS hop timing. This is NOT a "
                        "protocol decode and NOT a below-noise-floor detection -- "
                        "circumstantial RF-signature evidence only."
                    )
                # --- Consumer-IoT (non-drone) signature annotation, LRS-433/
                # SRD-868 only (task #72). Purely additive/parallel to the
                # ELRS/Crossfire hop-consistency heuristic directly above --
                # does not touch hop_consistent/update_hop_track/
                # classify_hop_interval at all. Only fires when this
                # detection did NOT show FHSS hop-consistency evidence (i.e.
                # `not hop_consistent`): a real ELRS/Crossfire-class link
                # gets its dedicated LRS/telemetry labeling above and is left
                # alone here to avoid conflicting/double labeling. When
                # non-hopping, this band is at least as plausibly one of the
                # catalogued ambient consumer-IoT devices (garage/gate
                # remotes, weather stations, utility meters, etc.) as a
                # genuine control link, so it gets annotated/relabeled with
                # that honest, catalogue-grounded alternative explanation
                # (see consumer_iot_signatures.py for the full rationale and
                # its "advisory_only" confidence caveat).
                if name in HOP_TRACKED_BANDS and not hop_consistent:
                    iot = consumer_iot_annotation(name)
                    if iot is not None:
                        det["consumer_iot_match"] = iot
                        # Soften the outward-facing label/threat level to
                        # reflect that this is now flagged as at least as
                        # likely ordinary ambient IoT traffic as a control
                        # link -- annotate, don't silently keep calling it an
                        # "LRS/telemetry craft (candidate)" at MEDIUM threat.
                        det["model"] = f"{meta['model']} / {iot['label']}"
                        det["threat_level"] = "LOW"
                try:
                    _post_with_reauth(args.console_url, "/api/detections/ingest", det,
                                       headers, args.email, args.password, timeout=5)
                    print(f"[{label}] CONFIRMED contact: peak {peak:.1f} dBm ({peak - floor:.1f} dB above floor, "
                          f"{consecutive_hits[name]} consecutive cycles)")
                except requests.RequestException as e:
                    print(f"ingest failed: {e}", file=sys.stderr)
            elif consecutive_hits[name] > 0 and not likely_excluded:
                print(f"[{label}] possible contact: peak {peak:.1f} dBm — awaiting confirmation "
                      f"({consecutive_hits[name]}/{CONFIRM_CYCLES} cycles)")

        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
