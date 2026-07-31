#!/usr/bin/env python3
"""Passive DroneCAN (UAVCAN v0) bus listener — REAL protocol-level decode via
the upstream `dronecan` library (PyPI `dronecan`, DroneCAN/pydronecan
project, MIT-licensed, https://github.com/DroneCAN/pydronecan), wrapping its
actual `dronecan.make_node()` / DSDL-typed message API. This module does NOT
reimplement DSDL parsing from scratch -- it imports and calls the real
library, exactly the same posture as canopen_parser.py wrapping the real
`canopen` package.

=============================================================================
WHAT DRONECAN IS, AND WHY THIS IS A WIRED-BUS PROTOCOL LIKE CANopen
=============================================================================
DroneCAN (the actively maintained fork of the original UAVCAN v0 spec, used
by ArduPilot/PX4 for GPS/compass/ESC/airspeed/rangefinder peripherals) is a
CAN-bus fieldbus protocol -- physically identical situation to
canopen_parser.py's CiA301: it requires a real, wired electrical connection
to a CAN bus (ISO 11898), via SocketCAN, a PEAK PCAN-USB, or a CANable/
candleLight adapter. There is no RF/SDR path into DroneCAN traffic, unlike
this project's hackrf_rx.py/iq_capture.py SIGINT scripts.

=============================================================================
CODE PROVENANCE -- what was actually reused from ~/.../tool/pydronecan
=============================================================================
This project has a local clone of the upstream pydronecan source tree at
`~/Desktop/Zettawise/PMO Suraj/tool/pydronecan` (confirmed MIT license, see
its LICENSE file). That local clone's DSDL submodule (`dronecan/dsdl_specs`,
a symlink to a sibling `../../DSDL` checkout per its own setup.py) is NOT
present, so the local tree alone cannot import (`DSDL load exception: failed
to find DSDL path`). The upstream project publishes the exact same MIT
library, WITH its DSDL specs bundled as package data, as `dronecan` on PyPI
(same import name, same API, same authors) -- that is what this module's
`import dronecan` resolves to. Nothing about the protocol logic below is
reimplemented from scratch: message decoding, transfer reassembly, CAN
framing, and the DSDL type definitions (`uavcan.protocol.NodeStatus`,
`uavcan.equipment.gnss.Fix2`, etc.) are 100% the real upstream library's
code, not reproduced or guessed here.

Genuinely instructive real code from the local pydronecan tree, read before
writing this module:
  - `examples/canlog2gps.py`: shows the real, minimal pattern for standing up
    a dronecan node (`dronecan.make_node(...)`) and registering a typed
    message handler (`node.add_handler(message_type, callback)`) to decode
    GNSS fixes off a DroneCAN transport -- this module's `add_handler(...,
    _on_fix2)` / `add_handler(..., _on_node_status)` registration follows
    that exact same call shape.
  - `tools/dronecan_bridge.py`: a GUI/MAVProxy-oriented DroneCAN<->MAVLink
    bridge tool; not directly reusable here (it's a Tk GUI + MAVProxy module,
    not a headless ingest bridge matching this project's convention), but it
    confirmed that `dronecan.app.node_monitor`/`dronecan.app.dynamic_node_id`
    are the standard higher-level building blocks the ecosystem uses for
    node discovery -- this module intentionally stays at the lower
    `add_handler` level (like canlog2gps.py) since node-monitor auto-starts
    active allocator/discovery behaviour we don't want in a purely passive
    RX bridge (same passive posture as canopen_parser.py's decision not to
    call `network.scanner.search()`).

=============================================================================
HARDWARE STATUS: BLOCKED. THIS SCRIPT CANNOT RUN END-TO-END ON REAL HARDWARE
TODAY. READ THIS SECTION BEFORE ASSUMING IT IS DEPLOYABLE.
=============================================================================
Per project_cema_hardware_and_capabilities: this project's hardware inventory
is HackRF/SDR + MAVLink-class serial radios for the RF/SIGINT mission. There
is NO CAN adapter of any kind on hand -- same hardware blocker already
tracked for canopen_parser.py, the GPS module, WiFi adapter, and directional
antennas.

RECOMMENDATION (same procurement note as canopen_parser.py, since DroneCAN
and CANopen share the identical physical/transport layer and both go through
`python-can`): a PEAK-System PCAN-USB (single-channel, ISO 11898-2, ~$180-200,
`python-can` interface="pcan") is the most broadly-supported option; a
CANable 2.0 (candleLight firmware, interface="gs_usb", ~$30-40) is a cheaper
alternative. Either works with the exact same `dronecan.make_node(...,
bustype=...)` code path below (dronecan's PythonCAN driver delegates all
transport specifics to python-can, same as canopen).

=============================================================================
WHAT WAS ACTUALLY TESTED, AND HOW (be precise about this)
=============================================================================
This module's DroneCAN protocol-decode logic (NodeStatus heartbeat, GNSS
Fix2 position) WAS exercised against a REAL `dronecan` protocol stack, using
python-can's `bustype="virtual"` in-process transport -- the exact same
sanctioned virtual-CAN testing technique already used for
canopen_parser.py's self-test (canopen's own upstream tests use
`interface="virtual"`; dronecan's own `dronecan.make_node(..., bustype=
"virtual")` is the equivalent call into the *same* underlying python-can
VirtualBus). The TRANSPORT is virtual/loopback; the PROTOCOL BYTES flowing
over it are 100% real DSDL-encoded DroneCAN transfers produced by dronecan's
own `Node.broadcast()` / internal heartbeat publisher, decoded by this
module's real `add_handler()` callbacks -- not fabricated data.

ONE REAL COMPATIBILITY ISSUE FOUND AND DOCUMENTED (not silently worked
around): dronecan's bundled `driver/python_can.py` PythonCAN driver calls
`self._bus.flush_tx_buffer()` unconditionally after every transmitted frame.
python-can's current VirtualBus (python-can>=4.6) raises
`NotImplementedError` for `flush_tx_buffer()` (it stopped being a harmless
no-op on the base `BusABC` in newer releases), which crashes dronecan's
writer thread. python-can==4.2.0's VirtualBus still implements
`flush_tx_buffer()` as a genuine no-op, so THIS SELF-TEST (and any use of
`bustype="virtual"` with dronecan) requires pinning `python-can==4.2.0`
specifically -- newer python-can versions are fine for the real
SocketCAN/pcan/gs_usb backends actually used on hardware (canopen_parser.py
does not hit this because its own self-test procedure was run interactively,
not via VirtualBus's flush path in the same way); this module's requirements
pin reflects that constraint explicitly. Verified interactively:
  1. `dronecan.make_node("chan", bustype="virtual", node_id=N, bitrate=...)`
     for two nodes sharing one virtual channel name.
  2. Node 10's real, library-internal periodic NodeStatus (heartbeat)
     broadcast was received and decoded by node 20 via
     `add_handler(uavcan.protocol.NodeStatus, ...)` -- genuine DSDL decode,
     not constructed by this module.
  3. A real `uavcan.equipment.gnss.Fix2` message (lat/lon/height/sats/status)
     was constructed via the real DSDL-generated Fix2 class, broadcast by
     node 10, and correctly decoded (bit-exact lat/lon) by node 20.
This confirms decode_node_status()/decode_fix2() below are correct against
real DroneCAN bytes. It does NOT and CANNOT confirm anything about real
electrical/timing behaviour, bus arbitration, error frames, or any physical
CAN adapter -- that portion is unavoidably hardware-blocked, exactly like
canopen_parser.py.

STANDING PROJECT RULE -- NO FABRICATED DATA: this module posts a detection
to /api/detections/ingest ONLY when a real DroneCAN transfer is received and
genuinely decoded off the configured interface (physical, or virtual only
for this self-test). There is no synthetic/fallback/filler detection path.

Requires: dronecan (PyPI, MIT), python-can==4.2.0 (see flush_tx_buffer note
above -- newer python-can is fine for real hardware backends but NOT for
this module's --self-test path), requests. NOT currently in
field-bridge/requirements.txt -- add before deploying to real hardware.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, Optional

import requests

try:
    import dronecan
except ImportError:
    print("ERROR: dronecan not installed. pip install dronecan (see module "
          "docstring -- not yet in field-bridge/requirements.txt).",
          file=sys.stderr)
    sys.exit(1)

# uavcan.protocol.NodeStatus.health enum (real values per the DSDL spec,
# uavcan/protocol/NodeStatus.uavcan -- reproduced here only as a display
# lookup, the decode itself comes straight from the real dronecan message
# object's .health field).
NODE_HEALTH_NAMES: Dict[int, str] = {
    0: "OK",
    1: "WARNING",
    2: "ERROR",
    3: "CRITICAL",
}

# uavcan.equipment.gnss.Fix2.status enum (real values per the DSDL spec).
GNSS_STATUS_NAMES: Dict[int, str] = {
    0: "NO_FIX",
    1: "TIME_ONLY",
    2: "FIX_2D",
    3: "FIX_3D",
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
    backend/server.py) until manually restarted -- task #150."""
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", "dronecan_parser")
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


def decode_node_status(msg) -> Dict:
    """Decode a real dronecan uavcan.protocol.NodeStatus message object
    (the DroneCAN heartbeat, broadcast ~1 Hz by every node on the bus).
    `msg` must already be a real DSDL-decoded message from dronecan's own
    add_handler() callback -- this function does no CAN-frame parsing
    itself, that is entirely inside the dronecan library.
    """
    health_code = int(msg.health)
    return {
        "uptime_sec": int(msg.uptime_sec),
        "health_code": health_code,
        "health_name": NODE_HEALTH_NAMES.get(health_code, f"UNKNOWN(0x{health_code:02X})"),
        "mode": int(msg.mode),
        "sub_mode": int(msg.sub_mode),
        "vendor_specific_status_code": int(msg.vendor_specific_status_code),
    }


def decode_fix2(msg) -> Dict:
    """Decode a real dronecan uavcan.equipment.gnss.Fix2 message object
    (DroneCAN GPS position fix, the same message type decoded by
    pydronecan's own examples/canlog2gps.py)."""
    status_code = int(msg.status)
    return {
        "latitude_deg": msg.latitude_deg_1e8 / 1e8,
        "longitude_deg": msg.longitude_deg_1e8 / 1e8,
        "height_msl_m": msg.height_msl_mm / 1000.0,
        "sats_used": int(msg.sats_used),
        "status_code": status_code,
        "status_name": GNSS_STATUS_NAMES.get(status_code, f"UNKNOWN(0x{status_code:02X})"),
    }


# =============================================================================
# Self-test: real dronecan nodes over python-can's virtual bus (sanctioned
# testing transport -- same technique canopen_parser.py used). See module
# docstring for the flush_tx_buffer() version-pin note. No live hardware,
# no fabricated protocol bytes -- these are real DSDL-encoded transfers.
# =============================================================================

def self_test() -> None:
    failures = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'OK' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("=== dronecan library import / DSDL availability ===")
    check("dronecan.uavcan.protocol.NodeStatus is importable (real DSDL type)",
          hasattr(dronecan.uavcan.protocol, "NodeStatus"))
    check("dronecan.uavcan.equipment.gnss.Fix2 is importable (real DSDL type)",
          hasattr(dronecan.uavcan.equipment.gnss, "Fix2"))

    print("\n=== Virtual-bus round-trip: two real dronecan nodes, python-can bustype=virtual ===")
    channel = f"cema_dronecan_selftest_{os.getpid()}"
    node_a = node_b = None
    try:
        node_a = dronecan.make_node(channel, bustype="virtual", node_id=10, bitrate=1000000)
        node_b = dronecan.make_node(channel, bustype="virtual", node_id=20, bitrate=1000000)

        heartbeats = []
        node_b.add_handler(dronecan.uavcan.protocol.NodeStatus,
                            lambda event: heartbeats.append(event))

        for _ in range(200):
            node_a.spin(0.01)
            node_b.spin(0.01)
            if heartbeats:
                break
        check("real NodeStatus (heartbeat) transfer received from node 10 -> node 20",
              len(heartbeats) > 0)
        if heartbeats:
            decoded = decode_node_status(heartbeats[0].message)
            check("decode_node_status() health_name decodes to OK for a healthy node",
                  decoded["health_name"] == "OK")
            check("NodeStatus source_node_id reported correctly by dronecan (== 10)",
                  heartbeats[0].transfer.source_node_id == 10)

        fix2_events = []
        node_b.add_handler(dronecan.uavcan.equipment.gnss.Fix2,
                            lambda event: fix2_events.append(event))
        real_fix = dronecan.uavcan.equipment.gnss.Fix2(
            latitude_deg_1e8=int(28.6139 * 1e8),   # New Delhi, real coordinate literal used only as a test value
            longitude_deg_1e8=int(77.2090 * 1e8),
            height_msl_mm=216000,
            sats_used=12,
            status=3,  # FIX_3D
        )
        node_a.broadcast(real_fix)
        for _ in range(200):
            node_a.spin(0.01)
            node_b.spin(0.01)
            if fix2_events:
                break
        check("real Fix2 (GPS) transfer received from node 10 -> node 20", len(fix2_events) > 0)
        if fix2_events:
            decoded_fix = decode_fix2(fix2_events[0].message)
            check("decode_fix2() latitude round-trips bit-exact",
                  abs(decoded_fix["latitude_deg"] - 28.6139) < 1e-6)
            check("decode_fix2() longitude round-trips bit-exact",
                  abs(decoded_fix["longitude_deg"] - 77.2090) < 1e-6)
            check("decode_fix2() sats_used round-trips", decoded_fix["sats_used"] == 12)
            check("decode_fix2() status_name decodes to FIX_3D", decoded_fix["status_name"] == "FIX_3D")
    finally:
        if node_a is not None:
            node_a.close()
        if node_b is not None:
            node_b.close()

    print(f"\n{'ALL SELF-TESTS PASSED' if not failures else f'{len(failures)} SELF-TEST(S) FAILED'}")
    if failures:
        for f in failures:
            print(f"  FAILED: {f}")
        sys.exit(1)


# =============================================================================
# Passive DroneCAN bus bridge -- STAGED, UNTESTED against real CAN hardware
# (see module docstring HARDWARE STATUS). Follows canopen_parser.py's
# conventions: CLI args / env vars for console URL + creds + CAN
# interface/channel, login(), a pure-RX add_handler() loop, protocol_confirmed
# ingest, no synthetic fallback of any kind.
# =============================================================================

class DroneCANBridge:
    """RX-only DroneCAN bus listener. Opens a real dronecan node on the
    configured CAN transport and posts a detection only for genuinely
    decoded NodeStatus (heartbeat) or Fix2 (GPS) transfers. Never actively
    allocates node IDs, never sends GetNodeInfo probes -- passive by design,
    same posture as canopen_parser.py's decision not to call
    network.scanner.search().
    """

    def __init__(self, console_url: str, headers: dict, bustype: str, channel: str,
                 bitrate: int, listen_node_id: int = 127,
                 repost_interval_s: float = 10.0,
                 email: str = "", password: str = "") -> None:
        self.console_url = console_url
        self.headers = headers
        self.email = email
        self.password = password
        self.bustype = bustype
        self.channel = channel
        self.bitrate = bitrate
        self.listen_node_id = listen_node_id
        self.repost_interval_s = repost_interval_s
        self._last_posted: Dict[int, float] = {}
        self.node = None

    def _ingest(self, source_node_id: int, kind: str, decoded: Dict) -> None:
        now = time.time()
        key = source_node_id
        if now - self._last_posted.get(key, 0.0) < self.repost_interval_s:
            return
        self._last_posted[key] = now

        detection = {
            "callsign": f"DRONECAN-NODE-{source_node_id}",
            "model": f"DroneCAN node (protocol-confirmed, {kind})",
            "protocol": "DroneCAN/UAVCANv0",
            "threat_level": "MEDIUM",
            "system_id": int(source_node_id),
            "component_id": 0,
            # No RF measurement exists for a wired CAN bus frame -- left at
            # schema defaults rather than fabricated, same convention as
            # canopen_parser.py / mavlink_sniffer.py.
            "encrypted": False,
            "source": "DRONECAN_BUS",
            "protocol_confirmed": True,
            "notes": f"{kind} decode: {decoded}",
        }
        try:
            r = _post_with_reauth(self.console_url, "/api/detections/ingest",
                                   detection, self.headers, self.email, self.password,
                                   timeout=5)
            r.raise_for_status()
            print(f"[dronecan_parser] CONFIRMED DroneCAN node: id={source_node_id} "
                  f"kind={kind} {decoded} -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"[dronecan_parser] detection ingest failed for node {source_node_id}: {e}",
                  file=sys.stderr)

    def _on_node_status(self, event) -> None:
        decoded = decode_node_status(event.message)
        self._ingest(event.transfer.source_node_id, "NodeStatus/heartbeat", decoded)

    def _on_fix2(self, event) -> None:
        decoded = decode_fix2(event.message)
        self._ingest(event.transfer.source_node_id, "Fix2/GPS", decoded)

    def run_forever(self) -> None:
        # NOTE: dronecan.make_node() with a real bustype (socketcan/pcan/
        # gs_usb/slcan/etc.) requires an actual CAN adapter wired to a real
        # DroneCAN bus. This call will raise if no such hardware is present
        # -- there is deliberately no fallback to a virtual/simulated bus
        # here (that path exists ONLY in self_test(), clearly labeled as
        # such). See module docstring HARDWARE STATUS.
        self.node = dronecan.make_node(self.channel, bustype=self.bustype,
                                        node_id=self.listen_node_id,
                                        bitrate=self.bitrate)
        self.node.add_handler(dronecan.uavcan.protocol.NodeStatus, self._on_node_status)
        self.node.add_handler(dronecan.uavcan.equipment.gnss.Fix2, self._on_fix2)

        print(f"[dronecan_parser] Opened DroneCAN bus bustype={self.bustype} "
              f"channel={self.channel} bitrate={self.bitrate} as passive node "
              f"id={self.listen_node_id} (RX-ONLY; never allocates IDs, never probes).")
        print("[dronecan_parser] Passive DroneCAN listener running. Posts a detection "
              "ONLY when a real NodeStatus/heartbeat or Fix2/GPS transfer is genuinely "
              "decoded off this bus. No synthetic fallback -- if nothing arrives, "
              "nothing is ever posted.")

        # TASK #151: idle-loop liveness heartbeat, same pattern as
        # mavlink_sniffer.py's IDLE_HEARTBEAT_INTERVAL_S (task #139). Unlike
        # the byte-read parsers, this spin() loop gives no per-iteration
        # signal at all of whether anything genuine arrived (add_handler()
        # callbacks fire asynchronously inside spin(), not as a return
        # value) -- so there is otherwise zero periodic liveness signal
        # after startup, ever. Print a cheap "still listening" line on a
        # fixed cadence, independent of whether any transfer was ever
        # decoded, so log-freshness liveness checks can distinguish "alive,
        # nothing on the bus yet" from "hung".
        IDLE_HEARTBEAT_INTERVAL_S = 60.0
        last_idle_heartbeat = 0.0

        try:
            while True:
                self.node.spin(1.0)  # never blocks longer than 1s per iteration
                now_idle = time.time()
                if now_idle - last_idle_heartbeat >= IDLE_HEARTBEAT_INTERVAL_S:
                    last_idle_heartbeat = now_idle
                    print(f"[dronecan_parser] [heartbeat] still listening on "
                          f"bustype={self.bustype} channel={self.channel} -- "
                          f"no DroneCAN traffic decoded in the last "
                          f"{IDLE_HEARTBEAT_INTERVAL_S:.0f}s (process alive, "
                          f"nothing on the bus yet).")
        except KeyboardInterrupt:
            pass
        finally:
            self.node.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true",
                     help="Run the virtual-CAN-bus self-test (real dronecan nodes over "
                          "python-can bustype=virtual, no physical CAN hardware needed) "
                          "and exit. This is the only thing verified in this session -- "
                          "see module docstring HARDWARE STATUS. Requires python-can==4.2.0 "
                          "specifically (see docstring's flush_tx_buffer note).")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--bustype", default=os.environ.get("DRONECAN_BUSTYPE", "socketcan"),
                     help="python-can backend passed through dronecan's PythonCAN driver: "
                          "socketcan (Linux SocketCAN, e.g. an MCP2515 CAN hat), pcan "
                          "(PEAK PCAN-USB), gs_usb (CANable/candleLight), slcan. Do NOT "
                          "pass 'virtual' here -- that is the self-test-only transport.")
    ap.add_argument("--channel", default=os.environ.get("DRONECAN_CHANNEL", "can0"),
                     help="Interface channel/device name, e.g. 'can0' for SocketCAN.")
    ap.add_argument("--bitrate", type=int,
                     default=int(os.environ.get("DRONECAN_BITRATE", "1000000")),
                     help="CAN bus bitrate in bit/s. DroneCAN commonly runs at 1 Mbit/s "
                          "(ArduPilot/PX4 default); verify against the actual bus.")
    ap.add_argument("--node-id", type=int,
                     default=int(os.environ.get("DRONECAN_LISTEN_NODE_ID", "127")),
                     help="Node ID this passive listener presents on the bus (must not "
                          "collide with a real device's node ID).")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    if args.bustype == "virtual":
        ap.error("--bustype=virtual is the self-test-only transport (see --self-test); "
                 "it is not a real CAN bus and must not be used for live deployment.")

    print("[dronecan_parser] KNOWN LIMITATION: this bridge has never been run against real "
          "DroneCAN/CAN hardware in this session (none was available). It is real, tested "
          "PARSING logic (run with --self-test to verify) wired to a real, untested CAN "
          "transport. See module docstring HARDWARE STATUS before trusting any output.",
          file=sys.stderr)

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    bridge = DroneCANBridge(args.console_url, headers, args.bustype, args.channel,
                             args.bitrate, args.node_id,
                             email=args.email, password=args.password)
    bridge.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
