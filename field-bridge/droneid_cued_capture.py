#!/usr/bin/env python3
"""CUED (best-effort) DJI DroneID capture -- non-contending with detection.

RECEIVE ONLY. No transmission anywhere.

=============================================================================
WHY THIS EXISTS (AND WHY IT IS "CUED, BEST-EFFORT", NOT AN ALWAYS-ON SWEEP)
=============================================================================
field-bridge/droneid_decode_bridge.py is a REAL, reference-verified DJI DroneID
decode chain, but its stock main() blindly sweeps 16 candidate OcuSync
frequencies every 30 s, each doing a ~1.3 s HackRF IQ capture. On a single-
HackRF deployment that radio is ALSO the live detection sweep's radio
(hackrf_rx.py). An unconditional 16-frequency capture sweep would repeatedly
seize the radio and starve the detection plane -- unacceptable.

This module makes DroneID capture CUED and NON-CONTENDING:

  1. CUED: it only captures when the LIVE detection plane has actually flagged
     a DJI/OcuSync-band candidate (GET /api/detections). No candidate -> no
     capture, the radio is left entirely to the detection sweep.

  2. DEVICE-LOCK GUARDED: the one short capture it does fire is wrapped in
     hackrf_device_lock() with a SHORT timeout. If hackrf_rx.py is mid-sweep
     and holds the lock, this RAISES HackrfDeviceBusy and SKIPS this cue
     honestly -- it never blocks the detection sweep. The lock is the exact
     same cross-process mutex hackrf_rx.py / ml_classify_bridge.py already use.

  3. COOLDOWN: at most one capture per COOLDOWN_S, and only ONE frequency (the
     cued candidate's nearest OcuSync channel) per cue -- not the full 16-freq
     sweep. Worst-case radio occupancy is ~1 capture per cooldown window, a few
     percent of the time, which does not starve detection.

Because non-contention is GUARANTEED by the lock (skip-on-busy) but a real
decode still depends on unavailable DJI hardware + an untested HackRF-rate
decode (see droneid_decode_bridge.py's disclosures), DroneID is honestly a
CUED (BEST-EFFORT) capability: the service shows READY (running, cueing) on the
Protocol Library board, and only goes LIVE if a real CRC-verified DroneID frame
is actually decoded and ingested.

All decode logic is REUSED from droneid_decode_bridge.py (capture_iq /
load_sigmf / decode_capture / _load_droneid_modules) -- this file adds only the
cue selection, the device-lock guard, the cooldown, and the heartbeat.

=============================================================================
CONFIG (env vars)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
DRONEID_SRC_DIR             DroneSecurity src/ dir (see droneid_decode_bridge.py)
CEMA_DRONEID_SAMPLE_RATE_HZ IQ capture rate (default 16e6; 15.36e6-20e6 window)
CEMA_DRONEID_CAPTURE_S      capture seconds per cued freq (default 1.0 -- short)
CEMA_DRONEID_POLL_INTERVAL_S seconds between cue polls (default 10)
CEMA_DRONEID_COOLDOWN_S     min seconds between actual captures (default 30)
CEMA_DRONEID_LOCK_TIMEOUT_S device-lock acquire timeout, short (default 2.0)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BRIDGE_NAME = "droneid_cued_capture"
PROTOCOL_ID = "droneid"

# DJI OcuSync candidate channels (MHz). Mirrors
# droneid_decode_bridge.CANDIDATE_FREQS_MHZ verbatim, duplicated here so this
# module's pure cue-selection logic is importable/testable WITHOUT triggering
# droneid_decode_bridge's heavy hardware imports (iq_capture/hackrf_rx). Kept
# in sync by the test that asserts equality against the source list.
CANDIDATE_FREQS_MHZ: List[float] = [
    2414.5, 2429.502441, 2434.5, 2444.5, 2459.5, 2474.5,
    5721.5, 5731.5, 5741.5, 5756.5, 5761.5, 5771.5, 5786.5, 5801.5, 5816.5, 5831.5,
]

# DJI OcuSync bands (GHz). A cue candidate must be a drone-ish contact whose
# center frequency falls in one of these -- ordinary out-of-band contacts never
# trigger a capture.
_DJI_BANDS_GHZ = ((2.400, 2.4835), (5.650, 5.950))
_DJI_HINTS = ("dji", "ocusync", "droneid", "mavic", "phantom", "mini", "air", "avata")


def _in_dji_band(center_freq_ghz: Optional[float]) -> bool:
    if center_freq_ghz is None:
        return False
    return any(lo <= center_freq_ghz <= hi for lo, hi in _DJI_BANDS_GHZ)


def is_dji_ocusync_candidate(det: Dict) -> bool:
    """True if a detection doc is a plausible DJI/OcuSync cue: a drone-ish
    contact (model/protocol hint or ML 'drone' label) sitting in a DJI band.
    Deliberately conservative -- an in-band contact with NO drone hint (e.g. a
    Wi-Fi AP) is NOT cued, so we don't fire captures at ambient 2.4 GHz."""
    if det.get("status") not in (None, "ACTIVE"):
        return False
    if not _in_dji_band(det.get("center_freq_ghz")):
        return False
    text = f"{det.get('model','')} {det.get('protocol','')}".lower()
    hinted = any(h in text for h in _DJI_HINTS)
    ml_drone = (det.get("ml_label") == "drone")
    return hinted or ml_drone


def nearest_candidate_mhz(center_freq_ghz: float) -> float:
    """Nearest DJI OcuSync candidate channel (MHz) to a contact's frequency."""
    target_mhz = center_freq_ghz * 1000.0
    return min(CANDIDATE_FREQS_MHZ, key=lambda f: abs(f - target_mhz))


def select_cue_frequency_mhz(detections: List[Dict]) -> Optional[float]:
    """Pick ONE OcuSync channel to capture this cue, from the live detection
    list, or None if nothing qualifies. Chooses the nearest candidate channel
    to the STRONGEST (highest RSSI) DJI candidate, so a single cued capture
    targets the most promising contact rather than sweeping everything."""
    candidates = [d for d in detections if is_dji_ocusync_candidate(d)]
    if not candidates:
        return None
    best = max(candidates, key=lambda d: (d.get("rssi_dbm") if d.get("rssi_dbm") is not None else -999))
    return nearest_candidate_mhz(best["center_freq_ghz"])


# ---------------------------------------------------------------------------
# Console auth (same convention as every other field-bridge script).
# ---------------------------------------------------------------------------
def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _reauth(console_url: str, headers: dict, email: str, password: str) -> None:
    try:
        headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
    except requests.RequestException as e:
        print(f"[{BRIDGE_NAME}] re-login failed ({e})", file=sys.stderr)


def _post(console_url: str, path: str, json_body: dict, headers: dict,
          email: str, password: str, timeout: float = 8) -> "requests.Response":
    headers.setdefault("X-Bridge-Name", BRIDGE_NAME)
    r = requests.post(f"{console_url}{path}", json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        _reauth(console_url, headers, email, password)
        r = requests.post(f"{console_url}{path}", json=json_body, headers=headers, timeout=timeout)
    return r


def _get(console_url: str, path: str, headers: dict, email: str, password: str,
         timeout: float = 8) -> "requests.Response":
    r = requests.get(f"{console_url}{path}", headers=headers, timeout=timeout)
    if r.status_code == 401:
        _reauth(console_url, headers, email, password)
        r = requests.get(f"{console_url}{path}", headers=headers, timeout=timeout)
    return r


def cued_capture_once(freq_mhz: float, *, console_url: str, headers: dict,
                       email: str, password: str, sample_rate_hz: float,
                       capture_s: float, modules, tmp_dir: str,
                       lock_timeout_s: float) -> bool:
    """Fire ONE short, device-lock-guarded IQ capture at `freq_mhz` and decode
    it. Reuses droneid_decode_bridge's real capture + decode chain. Returns
    True only if a CRC-valid DroneID frame was decoded. Returns False (and does
    NOT block the detection sweep) if the HackRF is busy with the RX sweep."""
    # Heavy imports (iq_capture/hackrf_rx/DroneSecurity) are done lazily here so
    # this module's pure cue logic stays importable without hardware deps.
    import droneid_decode_bridge as did
    from hackrf_device_lock import hackrf_device_lock, HackrfDeviceBusy

    center_hz = freq_mhz * 1e6
    out_path = os.path.join(tmp_dir, f"droneid_cued_{freq_mhz:.1f}.sigmf-data")
    samples = sr = None
    try:
        # ONLY the radio-touching capture_iq() is inside the lock; keep the
        # critical section as short as possible so a waiting RX sweep is held
        # up no longer than the capture itself.
        with hackrf_device_lock(timeout_s=lock_timeout_s, serial=did.HACKRF_SERIAL):
            meta_path = did.capture_iq(
                center_freq_hz=center_hz,
                sample_rate_hz=sample_rate_hz,
                duration_s=capture_s,
                out_path=out_path,
                serial=did.HACKRF_SERIAL,
                description=f"droneid_cued_capture @ {freq_mhz} MHz",
            )
        samples, sr, _freq, _dtype = did.load_sigmf(meta_path, out_path)
    except HackrfDeviceBusy:
        print(f"[{BRIDGE_NAME}] HackRF busy (RX sweep active) -- yielding, cue skipped "
              f"(detection NOT starved).")
        return False
    except Exception as e:  # noqa: BLE001 -- capture failure must never crash the loop
        print(f"[{BRIDGE_NAME}] capture failed @ {freq_mhz} MHz: {e}", file=sys.stderr)
        return False
    finally:
        for p in (out_path, out_path.replace(".sigmf-data", ".sigmf-meta")):
            try:
                os.remove(p)
            except OSError:
                pass

    try:
        results = did.decode_capture(samples, sr, modules)
    except Exception as e:  # noqa: BLE001
        print(f"[{BRIDGE_NAME}] decode failed @ {freq_mhz} MHz: {e}", file=sys.stderr)
        return False

    if not results:
        return False

    for rec in results:
        det = {
            "model": "DJI DroneID (decoded)",
            "protocol": "OcuSync 2.0",
            "threat_level": "HIGH",
            "center_freq_ghz": freq_mhz / 1000.0,
            "bandwidth_mhz": sample_rate_hz / 1e6,
            "source": "HACKRF",
            "protocol_confirmed": True,
            "decoded_serial_number": rec["serial_number"],
            "decoded_device_type": rec["device_type"],
            "drone_lat": rec["drone_lat"],
            "drone_lon": rec["drone_lon"],
            "app_lat": rec["app_lat"],
            "app_lon": rec["app_lon"],
            "height_m": rec["height"],
            "gps_time_ms": rec["gps_time"],
            "distance_estimated": False,
            "notes": "Cued, device-lock-guarded CRC-verified DroneID decode.",
            "confidence_type": "protocol_verified",
        }
        try:
            _post(console_url, "/api/detections/ingest", det, headers, email, password, timeout=10)
            print(f"[{BRIDGE_NAME}] REAL cued DroneID decode @ {freq_mhz} MHz: {rec}")
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] ingest failed: {e}", file=sys.stderr)
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--droneid-src-dir", default=os.environ.get("DRONEID_SRC_DIR"))
    ap.add_argument("--sample-rate-hz", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_SAMPLE_RATE_HZ", "16e6")))
    ap.add_argument("--capture-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_CAPTURE_S", "1.0")))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_POLL_INTERVAL_S", "10.0")))
    ap.add_argument("--cooldown-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_COOLDOWN_S", "30.0")))
    ap.add_argument("--lock-timeout-s", type=float,
                     default=float(os.environ.get("CEMA_DRONEID_LOCK_TIMEOUT_S", "2.0")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    import tempfile
    import droneid_decode_bridge as did
    src_dir = args.droneid_src_dir or did.DEFAULT_SRC_DIR
    modules = did._load_droneid_modules(src_dir)

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[{BRIDGE_NAME}] logged in. CUED DroneID capture: poll every "
          f"{args.interval_s}s, capture only on a DJI/OcuSync cue, device-lock "
          f"guarded (timeout {args.lock_timeout_s}s), cooldown {args.cooldown_s}s. "
          "RX ONLY. Non-contending with the detection sweep.")

    i = 0
    last_capture_t = 0.0
    with tempfile.TemporaryDirectory(prefix="cema_droneid_cued_") as tmp_dir:
        while args.iterations == 0 or i < args.iterations:
            try:
                r = _get(args.console_url, "/api/detections", headers, args.email, args.password)
                detections = r.json() if r.status_code == 200 else []
            except (requests.RequestException, ValueError) as e:
                print(f"[{BRIDGE_NAME}] detections poll failed: {e}", file=sys.stderr)
                detections = []

            cue_mhz = select_cue_frequency_mhz(detections)
            now = time.monotonic()
            if cue_mhz is not None and (now - last_capture_t) >= args.cooldown_s:
                print(f"[{BRIDGE_NAME}] DJI/OcuSync cue -> capturing @ {cue_mhz} MHz (best-effort).")
                cued_capture_once(
                    cue_mhz, console_url=args.console_url, headers=headers,
                    email=args.email, password=args.password,
                    sample_rate_hz=args.sample_rate_hz, capture_s=args.capture_s,
                    modules=modules, tmp_dir=tmp_dir, lock_timeout_s=args.lock_timeout_s,
                )
                last_capture_t = time.monotonic()

            try:
                note = (f"cue @ {cue_mhz} MHz" if cue_mhz is not None
                        else "no DJI/OcuSync cue this cycle")
                _post(args.console_url, "/api/protocols/heartbeat",
                      {"protocol": PROTOCOL_ID, "note": note},
                      headers, args.email, args.password, timeout=5)
            except requests.RequestException:
                pass

            i += 1
            time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
