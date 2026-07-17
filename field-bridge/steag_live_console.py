#!/usr/bin/env python3
"""STEAG live-fire operator console. Run this ON THE SERVER, at STEAG, under
Army Signals spectrum authorization only.

Single menu wrapping the already-tested field-bridge scripts:
  - passive detection status (hackrf_rx.py, already running as a systemd service)
  - MAVLink command injection over SiK (sik_mavlink_bridge.py)
  - HackRF link-disruption / jam burst (hackrf_jam.py), band presets from
    /CEMA/drone-kit/dronev5/cema/cema_{433,915,24,58}.py

This does NOT bypass any of those scripts' own safety gates — it just saves
retyping long commands under time pressure. You still need:
  export CEMA_AUTHORIZED_RANGE=1
before anything here will transmit, and each transmit still requires you to
type TRANSMIT at the final prompt.
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

MAVLINK_ACTIONS = ["land", "rth", "disarm", "flight_termination", "propeller_stop", "reboot", "gnss_denial"]
JAM_BANDS = ["433", "915", "2g4", "5g8"]


def check_authorized() -> bool:
    return os.environ.get("CEMA_AUTHORIZED_RANGE") == "1"


def menu() -> None:
    authorized = check_authorized()
    print("=" * 60)
    print("CEMA cUAS — STEAG Live-Fire Console")
    print(f"CEMA_AUTHORIZED_RANGE = {'1 (ARMED)' if authorized else 'unset (SAFE — transmit scripts will refuse)'}")
    print("=" * 60)
    print("1) Show detection status (passive, always safe)")
    print("2) Inject MAVLink command over SiK radio [TRANSMITS]")
    print("3) HackRF jam burst on a band preset [TRANSMITS]")
    print("4) Arm this session (export CEMA_AUTHORIZED_RANGE=1) — only do this at STEAG")
    print("0) Exit")


def show_detections() -> None:
    subprocess.run([sys.executable, os.path.join(HERE, "hackrf_active_only.py")])


def inject_mavlink() -> None:
    print("Actions:", ", ".join(MAVLINK_ACTIONS))
    action = input("action: ").strip()
    if action not in MAVLINK_ACTIONS:
        print("Unknown action.")
        return
    port = input("SiK serial port [/dev/ttyUSB0]: ").strip() or "/dev/ttyUSB0"
    target_sys = input("target system id [1]: ").strip() or "1"
    console_url = input("console URL [http://localhost:8001]: ").strip() or "http://localhost:8001"
    email = input("console email [operator@cema.mil]: ").strip() or "operator@cema.mil"
    password = input("console password [cema@2026]: ").strip() or "cema@2026"
    cmd = [
        sys.executable, os.path.join(HERE, "sik_mavlink_bridge.py"),
        "--port", port, "--action", action, "--target-sys", target_sys,
        "--console-url", console_url, "--email", email, "--password", password,
        "--i-confirm-authorized-range",
    ]
    subprocess.run(cmd)


def jam_burst() -> None:
    print("Bands:", ", ".join(JAM_BANDS))
    band = input("band: ").strip()
    if band not in JAM_BANDS:
        print("Unknown band.")
        return
    duration = input("duration seconds [5, max 10]: ").strip() or "5"
    cmd = [
        sys.executable, os.path.join(HERE, "hackrf_jam.py"),
        "--band", band, "--duration-s", duration,
        "--i-confirm-authorized-range",
    ]
    subprocess.run(cmd)


def main() -> None:
    while True:
        menu()
        choice = input("> ").strip()
        if choice == "1":
            show_detections()
        elif choice == "2":
            inject_mavlink()
        elif choice == "3":
            jam_burst()
        elif choice == "4":
            print("Run this in your shell BEFORE launching this console, not from inside it:")
            print("  export CEMA_AUTHORIZED_RANGE=1")
            print("(A child process can't set an env var for its parent shell — this")
            print(" console can only read the flag, not set it for you.)")
        elif choice == "0":
            break
        else:
            print("Unknown option.")
        print()


if __name__ == "__main__":
    main()
