#!/usr/bin/env python3
"""Passive MAVLink HEARTBEAT sniffer — REAL protocol-level ArduPilot detection.

=============================================================================
WHAT THIS IS, AND WHY IT IS DIFFERENT FROM hackrf_rx.py
=============================================================================
Every other passive bridge in this repo (hackrf_rx.py, hackrf_active_only.py,
monitor_band.py/monitor_freq.py) does RF ENERGY-DETECTION only: it measures
power in a band and applies heuristics (RSSI threshold, persistence-vs-Wi-Fi
filtering, etc.) to GUESS that "something drone-shaped" is probably there.
The resulting "model" labels (e.g. "DJI Mini (candidate)") are exactly that
— candidates, not identifications. There is no protocol decode anywhere in
that path.

This script is different in kind, not just degree. It opens the SiK/RFD900
telemetry radio's serial link in READ-ONLY mode and hands the raw byte
stream to `pymavlink`'s own mature MAVLink parser (mavutil.mavlink_connection
+ recv_match) — the same standard library already used elsewhere in this
repo (backend/mavlink_codec.py's TX side, rf-bridge/mavlink_bridge.py's RX
side). When pymavlink genuinely decodes a real HEARTBEAT message (MAVLink
message ID 0, which every real MAVLink autopilot broadcasts roughly once per
second) off the wire, its `autopilot` field is read directly from those real,
intercepted bytes. If autopilot == MAV_AUTOPILOT_ARDUPILOTMEGA (3), that is a
DEFINITIVE, POSITIVE, protocol-level identification of a real ArduPilot
platform — not RF-energy heuristics, not a label guess.

STANDING PROJECT RULE — ALL DATA MUST BE REAL. NO SIMULATION, ANYWHERE:
  * There is NO synthetic/fallback/filler data path in this script, unlike
    hackrf_rx.py's noise-floor filler (which exists there for a different,
    energy-detection purpose and is explicitly NOT replicated here).
  * If there is no real MAVLink traffic arriving on this exact serial link,
    at this exact baud rate, from a real MAVLink-speaking radio within RF
    range, this script posts NOTHING. Silence on the link means silence in
    the console. That is by design, not a bug or an incomplete feature.
  * system_id posted to the console is the REAL system_id field decoded out
    of the actual HEARTBEAT bytes — never a random/default/assumed value.

RX-ONLY / NO TRANSMISSION:
  This script never writes to the serial port. mavutil.mavlink_connection()
  is used purely as a byte-stream parser here; .write()/.mav.*_send() are
  never called. This is intentionally kept separate from
  field-bridge/sik_mavlink_bridge.py (which DOES transmit, under an explicit
  safety gate) and from rf-bridge/mavlink_bridge.py (which is a combined
  TX+RX bridge for kinetic command delivery). Do not add TX capability here.

IMPORTANT — SERIAL PORT EXCLUSIVITY:
  Only one process can hold a serial device open at a time. Do not run this
  script concurrently against the same /dev/ttyUSB* that rf-bridge's
  mavlink_bridge.py (or any other bridge) already has open. This script is
  meant for deployments where a SiK/RFD900 radio is dedicated to PASSIVE
  intercept/recon (e.g. a second radio, or a period with no command link
  active yet) — not for bolting onto an already-in-use command-link radio.

Requires: pyserial, pymavlink, requests  (see field-bridge/requirements.txt).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, Optional

import requests

try:
    from pymavlink import mavutil
except ImportError:
    print("ERROR: pymavlink not installed. pip install pymavlink (see "
          "field-bridge/requirements.txt).", file=sys.stderr)
    sys.exit(1)

# MAV_AUTOPILOT_ARDUPILOTMEGA is enum value 3 in the MAVLink common dialect.
# Pulled from pymavlink's own generated dialect module rather than
# hand-copied, so if pymavlink's dialect ever changes this stays correct.
MAV_AUTOPILOT_ARDUPILOTMEGA = mavutil.mavlink.MAV_AUTOPILOT_ARDUPILOTMEGA

# Human-readable names for a few other MAV_AUTOPILOT values, purely for
# logging/visibility when a real HEARTBEAT is seen from a non-ArduPilot
# autopilot (e.g. PX4). We still only INGEST a detection for ArduPilot
# per the task scope, but logging what else is really out there is useful
# for the operator and costs nothing.
_AUTOPILOT_NAMES: Dict[int, str] = {
    getattr(mavutil.mavlink, name): name.replace("MAV_AUTOPILOT_", "")
    for name in dir(mavutil.mavlink)
    if name.startswith("MAV_AUTOPILOT_")
}


def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5) -> "requests.Response":
    """POST to the backend, auto-recovering from an expired JWT by re-login
    ONCE and retrying. Duplicated per-file (same convention as login() above
    -- no shared auth module exists in field-bridge/); canonical copy +
    rationale lives in hackrf_rx.py. This bridge runs Restart=always
    indefinitely, so without this it would silently and permanently 401 on
    every ingest past the backend's 12h JWT TTL (create_access_token() in
    backend/server.py) until manually restarted -- the exact bug this fixes."""
    url = f"{console_url}{path}"
    # Identifies this bridge in the backend's ingest_health tracking (task
    # #74) -- see GET /api/health's ingest_sources / preflight.sh.
    headers.setdefault("X-Bridge-Name", "mavlink_sniffer")
    r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        print(f"[auth] 401 from POST {path} -- token expired, re-authenticating as {email}",
              file=sys.stderr)
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[auth] re-login failed ({e})", file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
        if r.status_code == 401:
            print(f"[auth] still 401 for POST {path} after re-authenticating -- real auth "
                  f"problem (check credentials for {email}), not just an expired token.",
                  file=sys.stderr)
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Credentials/console URL: CLI args take precedence over env vars, same
    # convention as field-bridge/hackrf_rx.py, so a systemd unit can supply
    # them via EnvironmentFile= without hardcoding secrets on ExecStart.
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--serial", default=os.environ.get("MAVLINK_SNIFF_SERIAL", "/dev/ttyUSB0"),
                    help="SiK/RFD900 radio serial device (READ-ONLY; never transmits).")
    ap.add_argument("--baud", type=int,
                    default=int(os.environ.get("MAVLINK_SNIFF_BAUD", "57600")))
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Opening {args.serial} @ {args.baud} baud in RX-ONLY mode "
          f"(no transmission will occur).")
    print("Passive MAVLink HEARTBEAT sniffer running. This posts a detection "
          "ONLY when a REAL, decoded MAVLink HEARTBEAT with "
          "autopilot=ARDUPILOTMEGA is received on this link. If no such "
          "traffic ever arrives, nothing is ever posted — there is no "
          "synthetic fallback, by design.")

    # source_system/source_component here identify US as a MAVLink participant
    # if we ever needed to reply (we never send), matching the convention used
    # by rf-bridge/mavlink_bridge.py's RX path (255/190 = a GCS-class node).
    mav = mavutil.mavlink_connection(
        args.serial,
        baud=args.baud,
        source_system=255,
        source_component=190,
    )

    # sysid -> last-confirmed-ts, so we don't spam /detections/ingest on every
    # single HEARTBEAT (~1Hz) from the same real airframe. The backend itself
    # also merges re-ingests of the same (source, model, protocol) within its
    # own DETECTION_MERGE_WINDOW_S, so this is a light client-side throttle on
    # top of that, not a replacement for it.
    last_posted: Dict[int, float] = {}
    REPOST_INTERVAL_S = 10.0

    while True:
        try:
            m = mav.recv_match(blocking=True, timeout=1)
        except Exception as e:
            print(f"WARN: serial/mavlink recv error: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        if m is None:
            # Genuinely nothing on the link this second. No fallback data —
            # this is expected and correct behaviour absent real traffic.
            continue
        if m.get_type() in ("BAD_DATA", "UNKNOWN"):
            continue
        if m.get_type() != "HEARTBEAT":
            continue

        sysid = m.get_srcSystem()
        compid = m.get_srcComponent()
        autopilot = getattr(m, "autopilot", None)
        autopilot_name = _AUTOPILOT_NAMES.get(autopilot, f"UNKNOWN({autopilot})")

        if autopilot != MAV_AUTOPILOT_ARDUPILOTMEGA:
            # Real HEARTBEAT, genuinely decoded, but not ArduPilot (e.g. PX4,
            # or another autopilot type). Log for visibility; do not ingest —
            # this task is scoped to positive ArduPilot identification.
            print(f"Real HEARTBEAT decoded: sysid={sysid} compid={compid} "
                  f"autopilot={autopilot_name} (not ArduPilot — not ingested)")
            continue

        now = time.time()
        if now - last_posted.get(sysid, 0.0) < REPOST_INTERVAL_S:
            continue
        last_posted[sysid] = now

        detection = {
            "callsign": f"ARDUPILOT-{sysid}",
            "model": "ArduPilot autopilot (protocol-confirmed)",
            # Explicitly ArduPilot, not a generic "MAVLink craft" guess — this
            # came from a real decoded HEARTBEAT.autopilot field, not a label
            # picked by an energy-detection heuristic.
            "protocol": "MAVLink/ArduPilot",
            "threat_level": "HIGH",
            # SiK/RFD900 telemetry radios operate at 915MHz (or 433MHz in some
            # regions); we don't have a real per-message frequency measurement
            # from pymavlink's parser (it only demuxes bytes, it isn't an SDR),
            # so this reflects the known band of the physical link, not a
            # measured center frequency of this specific packet.
            "center_freq_ghz": 0.915,
            "bandwidth_mhz": 0.25,
            # No real RSSI/SNR is available from a plain serial UART link (no
            # SDR/IQ capture here) — pymavlink gives us decoded messages, not
            # signal power. Left at the schema defaults rather than fabricated;
            # this detection's value is the protocol confirmation, not RF
            # characterization.
            "system_id": int(sysid),
            "component_id": int(compid),
            "encrypted": False,
            "source": "SIK_RADIO",
            # The whole point of this script: this is a REAL, decoded,
            # protocol-level identification (MAVLink HEARTBEAT.autopilot ==
            # ARDUPILOTMEGA), not an RF-energy heuristic guess.
            "protocol_confirmed": True,
        }
        try:
            r = _post_with_reauth(args.console_url, "/api/detections/ingest",
                                   detection, headers, args.email, args.password, timeout=5)
            r.raise_for_status()
            print(f"CONFIRMED ArduPilot contact: sysid={sysid} compid={compid} "
                  f"(real decoded HEARTBEAT, autopilot=ARDUPILOTMEGA) -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"detection ingest failed for sysid={sysid}: {e}", file=sys.stderr)

    return 0  # unreachable; loop runs until process is killed/stopped


if __name__ == "__main__":
    sys.exit(main())
