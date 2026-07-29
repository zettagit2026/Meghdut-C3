#!/usr/bin/env python3
"""Passive CANopen (CiA 301) bus listener — REAL protocol-level parsing via
the upstream `canopen` library (christiansandberg/canopen, MIT), wrapping its
actual `canopen.Network` / `NodeScanner` / NMT error-control API.

=============================================================================
HARDWARE STATUS: BLOCKED. THIS SCRIPT CANNOT RUN END-TO-END ON REAL HARDWARE
TODAY. READ THIS SECTION BEFORE ASSUMING IT IS DEPLOYABLE.
=============================================================================
CANopen (CiA 301) is a fieldbus protocol layered directly on top of a
physical/electrical CAN bus (ISO 11898, twisted-pair differential signalling,
typically 125k-1M bit/s). Unlike this repo's other passive bridges
(mavlink_sniffer.py over a SiK/RFD900 UART radio link, hackrf_rx.py over an
SDR), there is no RF path into CANopen traffic — it requires a WIRED
electrical connection to an actual CAN bus segment, via a CAN-capable
interface such as:
  - SocketCAN on Linux (native "canN"/"vcanN" net-devices), typically fed by
    an MCP2515 SPI-CAN hat, or a native CAN controller
  - A USB-CAN adapter recognised by python-can's `interface=` backends, e.g.
    a PEAK-System PCAN-USB (~$180-220) — python-can interface="pcan"
  - A cheaper SocketCAN-compatible USB adapter (e.g. CANable/candleLight,
    ~$30-60, running slcan or gs_usb firmware) — interface="socketcan" or
    "slcan"

This project's hardware inventory (per project_cema_hardware_and_capabilities
memory note) is HackRF/SDR + MAVLink-class serial radios for the RF/SIGINT
mission — there is NO CAN adapter of any kind on hand. This is therefore a
HARDWARE BLOCKER of the same class already tracked for the GPS module, WiFi
adapter, and directional antennas: the code below is real and correct against
canopen's actual API, but it has NEVER been run against a real CAN bus and
CANNOT be, until a CAN adapter is procured.

RECOMMENDATION: a PEAK-System PCAN-USB (single-channel, ISO 11898-2,
~$180-200, `python-can` interface="pcan") is the most broadly-supported and
best-documented option and is what this module's `--interface`/`--channel`
defaults are written against. A CANable 2.0 (candleLight firmware,
interface="gs_usb", ~$30-40) is a materially cheaper alternative if budget-
constrained; either works with the exact same `canopen`/`python-can` code
path below (only the `--interface` value changes) since canopen delegates all
transport specifics to python-can.

=============================================================================
WHAT WAS ACTUALLY TESTED, AND HOW (be precise about this)
=============================================================================
This module's CANopen protocol-decode logic (NMT error-control/heartbeat
state parsing) WAS exercised against a REAL `canopen` protocol stack, using
python-can's `interface="virtual"` transport — an in-process virtual CAN bus
that python-can itself ships specifically for testing (canopen's own upstream
test suite uses exactly this: see canopen/test/test_network.py,
test/test_nmt.py, test/test_node.py, all of which call
`network.connect(interface="virtual")`). This is a REAL, sanctioned canopen/
python-can testing technique — not fabricated data. It is directly analogous
to the socat virtual-serial-pair + real MAVLink frames technique used
elsewhere in this project: the TRANSPORT is virtual/loopback, but the
PROTOCOL BYTES flowing over it are 100% real CiA 301 frames produced by
canopen's own `LocalNode`/`NmtMaster` implementation (heartbeat producer,
NMT state machine), and decoded by this module's real NMT_STATES lookup.

Verified interactively (not part of an automated test file, since this repo
has no CI dependency on `canopen`/`python-can` being installed — see
requirements note at the bottom of this file):
  1. `canopen.Network().connect(interface="virtual", channel="test-canopen")`
     on both a producer and a listener Network in the same process.
  2. A real `canopen.LocalNode` was set to NMT state OPERATIONAL and its real
     heartbeat producer (`node.nmt.start_heartbeat(...)`) was started.
  3. The listener genuinely received a CAN frame with COB-ID 0x700+node_id
     (0x742 for node 0x42) carrying payload byte 0x05 — which IS the real
     CiA 301 NMT error-control encoding of state OPERATIONAL (see
     canopen/nmt.py's NMT_STATES = {..., 5: 'OPERATIONAL', ...}), decoded
     correctly by the same lookup table this module uses.
This confirms the parsing logic in `decode_heartbeat()` below is correct
against real CiA 301 bytes. It does NOT and CANNOT confirm anything about
real-world electrical/timing behaviour, bus arbitration, error frames, or
any physical CAN adapter — that portion is unavoidably hardware-blocked.

STANDING PROJECT RULE — NO FABRICATED DATA: this module posts a detection to
/api/detections/ingest ONLY when a real CAN frame is received and genuinely
decoded off the configured interface (physical or virtual-for-testing). There
is no synthetic/fallback/filler detection path. If nothing arrives on the
bus, nothing is posted — silence in, silence out, exactly like
mavlink_sniffer.py.

Requires: canopen>=2.0, python-can>=4.0, requests (NOT currently in
field-bridge/requirements.txt — add before deploying to real hardware; both
installed cleanly from PyPI in a scratch venv for the virtual-bus test
above, no vendoring/forking of canopen needed).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, Optional

import requests

try:
    import canopen
    from canopen.nmt import NMT_STATES
except ImportError:
    print("ERROR: canopen not installed. pip install canopen (see module "
          "docstring — not yet in field-bridge/requirements.txt).",
          file=sys.stderr)
    sys.exit(1)

# CiA 301 predefined connection set: NMT error-control (heartbeat/bootup)
# messages use COB-ID 0x700 + node-id. This is the same constant canopen's
# own NodeScanner uses internally (canopen/network.py NodeScanner.SERVICES).
NMT_ERROR_CONTROL_BASE = 0x700
EMCY_BASE = 0x80
SDO_TX_BASE = 0x580  # server -> client SDO responses


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
    backend/server.py) until manually restarted -- task #150."""
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", "canopen_parser")
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


def decode_heartbeat(can_id: int, data: bytearray) -> Optional[Dict]:
    """Decode a real CiA 301 NMT error-control (heartbeat/bootup) frame.

    Returns None if this can_id/data does not represent a genuine
    error-control frame (i.e. not in the 0x700-0x77F range, or empty
    payload) — never guesses/fabricates a decode.
    """
    if not (NMT_ERROR_CONTROL_BASE <= can_id < NMT_ERROR_CONTROL_BASE + 0x80):
        return None
    if not data:
        return None
    node_id = can_id - NMT_ERROR_CONTROL_BASE
    if node_id == 0:
        return None  # 0 is not a valid CANopen node id
    state_code = data[0]
    state_name = NMT_STATES.get(state_code)
    return {
        "node_id": node_id,
        "state_code": state_code,
        # A code canopen doesn't recognise is reported honestly as unknown,
        # not silently mapped to something plausible-looking.
        "state_name": state_name or f"UNKNOWN(0x{state_code:02X})",
        "bootup": state_code == 0,  # 0x00 payload on first heartbeat = bootup
    }


def run(console_url: str, email: str, password: str,
        interface: str, channel: str, bitrate: Optional[int]) -> int:
    token = login(console_url, email, password)
    headers = {"Authorization": f"Bearer {token}"}

    network = canopen.Network()
    connect_kwargs = {"interface": interface, "channel": channel}
    if bitrate:
        connect_kwargs["bitrate"] = bitrate
    network.connect(**connect_kwargs)

    # Real canopen node discovery: passively observes heartbeat/SDO/PDO/EMCY
    # traffic to build a list of node ids actually present on the bus (does
    # NOT send anything by default — .search() would actively probe, which
    # we deliberately do not call here to stay pure-RX like
    # mavlink_sniffer.py's passive posture).
    scanner = network.scanner

    last_posted: Dict[int, float] = {}
    REPOST_INTERVAL_S = 10.0

    def on_error_control(can_id: int, data: bytearray, timestamp: float) -> None:
        decoded = decode_heartbeat(can_id, data)
        if decoded is None:
            return
        node_id = decoded["node_id"]
        now = time.time()
        if now - last_posted.get(node_id, 0.0) < REPOST_INTERVAL_S:
            return
        last_posted[node_id] = now

        detection = {
            "callsign": f"CANOPEN-NODE-{node_id}",
            "model": f"CANopen node (protocol-confirmed, state={decoded['state_name']})",
            "protocol": "CANopen/CiA301",
            "threat_level": "MEDIUM",
            # No RF measurement exists for a wired CAN bus frame — left at
            # schema defaults rather than fabricated, same convention as
            # mavlink_sniffer.py's SIK_RADIO source.
            "system_id": int(node_id),
            "component_id": 0,
            "encrypted": False,
            "source": "CANOPEN_BUS",
            "protocol_confirmed": True,
        }
        try:
            r = _post_with_reauth(console_url, "/api/detections/ingest",
                                   detection, headers, email, password, timeout=5)
            r.raise_for_status()
            print(f"CONFIRMED CANopen node: id={node_id} state={decoded['state_name']} "
                  f"-> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"detection ingest failed for node {node_id}: {e}", file=sys.stderr)

    # Subscribe across the full NMT error-control COB-ID range (0x701-0x77F);
    # canopen's own network.subscribe() takes one exact can_id per callback,
    # so we register 127 real per-node subscriptions (cheap: dict lookups).
    for nid in range(1, 128):
        network.subscribe(NMT_ERROR_CONTROL_BASE + nid, on_error_control)

    print(f"Opening CANopen bus interface={interface} channel={channel} "
          f"bitrate={bitrate or '(interface default)'}")
    print("Passive CANopen NMT error-control (heartbeat/bootup) listener "
          "running. Posts a detection ONLY when a real CiA301 error-control "
          "frame is decoded off this bus. No synthetic fallback.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        network.disconnect()

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--interface", default=os.environ.get("CANOPEN_INTERFACE", "socketcan"),
                     help="python-can backend: socketcan (Linux SocketCAN, e.g. "
                          "an MCP2515 CAN hat), pcan (PEAK PCAN-USB), gs_usb "
                          "(CANable/candleLight), slcan, or 'virtual' for the "
                          "in-process test transport (NOT real hardware).")
    ap.add_argument("--channel", default=os.environ.get("CANOPEN_CHANNEL", "can0"),
                     help="Interface channel/device name, e.g. 'can0' for SocketCAN, "
                          "or a PCAN device string for interface=pcan.")
    ap.add_argument("--bitrate", type=int, default=int(os.environ.get("CANOPEN_BITRATE", "0")) or None,
                     help="CAN bus bitrate in bit/s (e.g. 250000, 500000). Omit to use "
                          "the interface's configured default (e.g. via `ip link` for "
                          "SocketCAN).")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    return run(args.console_url, args.email, args.password,
                args.interface, args.channel, args.bitrate)


if __name__ == "__main__":
    sys.exit(main())
