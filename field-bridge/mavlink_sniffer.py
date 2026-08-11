#!/usr/bin/env python3
"""Passive MAVLink HEARTBEAT sniffer — REAL protocol-level ArduPilot detection.

TASK #114 EXTENSION (2026-07-25) — ArduPilot-specific protocol layers:
  In addition to the original HEARTBEAT-only decode described below, this
  script now also RECOGNIZES (passively, RX-only, log-only — no ingest, no
  new detection schema) three additional real MAVLink protocol layers that
  ArduPilot GCS<->vehicle links use constantly:
    * Parameter protocol   -- PARAM_REQUEST_READ / PARAM_REQUEST_LIST /
      PARAM_VALUE / PARAM_SET. Seeing this on the link means a real GCS is
      actively reading or changing a real vehicle's configuration right now.
    * Mission protocol      -- MISSION_REQUEST(_INT/_LIST), MISSION_ITEM(_INT),
      MISSION_COUNT, MISSION_ACK, MISSION_CURRENT, MISSION_SET_CURRENT. Seeing
      this means a real waypoint/mission upload or download sequence is in
      progress between a real GCS and a real vehicle -- a materially
      different signal from routine 1Hz heartbeat telemetry (e.g. "adversary
      is re-tasking this airframe right now").
    * MAVLink-FTP           -- FILE_TRANSFER_PROTOCOL, ArduPilot's file
      transfer sub-protocol (used for parameter files, dataflash logs,
      firmware). This script decodes the well-known MAVFTP payload header
      (seq/session/opcode/size/req_opcode/offset -- see
      https://mavlink.io/en/services/ftp.html) directly out of the real,
      decoded FILE_TRANSFER_PROTOCOL.payload bytes; it does not invent this
      layout, it's the published ArduPilot/MAVLink FTP wire format.
  All of this is passive recognition of REAL traffic already on the wire,
  using pymavlink's own generated message classes for every field access --
  no new message parsing is reimplemented here. Nothing here transmits,
  requests, or injects anything (see RX-ONLY / NO TRANSMISSION below, which
  applies equally to this extension). These messages are printed for
  operator visibility only; they are deliberately NOT posted to
  /api/detections/ingest (that endpoint's schema is scoped to platform
  detections, e.g. the ArduPilot HEARTBEAT confirmation below -- a
  PARAM_VALUE or MISSION_ITEM sighting isn't a new "platform detected"
  event, it's link-activity intelligence about an already-identified link).

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

# Errors that mean "the serial device is absent / not openable / went away"
# (radio unplugged, udev path not created yet, USB hang) -- as opposed to a
# real misconfiguration. pyserial's SerialException subclasses OSError, and a
# missing /dev node raises FileNotFoundError (also an OSError), but we import
# and name SerialException explicitly so the intent is obvious. A device that
# is merely ABSENT must make this process idle-and-retry, never exit(1) -- see
# _open_serial_with_retry() and the reconnect loop in main(). This mirrors the
# rest of the field-bridge fleet, which tolerates absent hardware and keeps the
# service alive rather than crash-looping under systemd Restart=always (e.g.
# hackrf_rx.py treats a busy/absent HackRF as a skipped pass and loops on).
try:
    import serial  # pyserial (a pymavlink dependency; see requirements.txt)
    _SERIAL_ABSENT_ERRORS = (serial.SerialException, FileNotFoundError, OSError)
except ImportError:  # pragma: no cover - pyserial ships with pymavlink
    _SERIAL_ABSENT_ERRORS = (FileNotFoundError, OSError)

# Bounded backoff between attempts to (re)open an absent serial device.
DEVICE_RETRY_BACKOFF_S = 5.0

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


# -----------------------------------------------------------------------
# Task #114: ArduPilot-specific protocol layer recognition (passive, RX-only)
# -----------------------------------------------------------------------
# Message type names as reported by pymavlink's Message.get_type() -- these
# are the real generated dialect class names, not made up.
PARAM_PROTOCOL_MSG_TYPES = (
    "PARAM_REQUEST_READ",
    "PARAM_REQUEST_LIST",
    "PARAM_VALUE",
    "PARAM_SET",
)
MISSION_PROTOCOL_MSG_TYPES = (
    "MISSION_REQUEST",
    "MISSION_REQUEST_INT",
    "MISSION_REQUEST_LIST",
    "MISSION_ITEM",
    "MISSION_ITEM_INT",
    "MISSION_COUNT",
    "MISSION_ACK",
    "MISSION_CURRENT",
    "MISSION_SET_CURRENT",
)
FTP_PROTOCOL_MSG_TYPES = ("FILE_TRANSFER_PROTOCOL",)

# MAVLink-FTP (MAVFTP) opcode names, per the published ArduPilot/MAVLink FTP
# spec (https://mavlink.io/en/services/ftp.html) -- this is the real,
# documented wire layout of FILE_TRANSFER_PROTOCOL.payload, not invented
# here. Header layout (little-endian): seq_number(u16) session(u8) opcode(u8)
# size(u8) req_opcode(u8) burst_complete(u8) padding(u8) offset(u32) [data...]
_MAVFTP_OPCODE_NAMES: Dict[int, str] = {
    0: "None", 1: "TerminateSession", 2: "ResetSessions", 3: "ListDirectory",
    4: "OpenFileRO", 5: "ReadFile", 6: "CreateFile", 7: "WriteFile",
    8: "RemoveFile", 9: "CreateDirectory", 10: "RemoveDirectory",
    11: "OpenFileWO", 12: "TruncateFile", 13: "Rename", 14: "CalcFileCRC32",
    15: "BurstReadFile", 128: "Ack", 129: "Nak",
}
_MAVFTP_HEADER_LEN = 12  # seq(2)+session(1)+opcode(1)+size(1)+req_opcode(1)+burst(1)+pad(1)+offset(4)


def describe_param_activity(m) -> str:
    """Format a one-line, human-readable summary of a real, decoded
    parameter-protocol message. Fields are read directly off the pymavlink
    message object -- no guessing/reimplementation of the wire format."""
    mtype = m.get_type()
    sysid, compid = m.get_srcSystem(), m.get_srcComponent()
    if mtype == "PARAM_VALUE":
        param_id = getattr(m, "param_id", "")
        if isinstance(param_id, (bytes, bytearray)):
            param_id = param_id.split(b"\x00")[0].decode("ascii", "replace")
        return (f"PARAM protocol: PARAM_VALUE from sysid={sysid} compid={compid} "
                f"param_id={param_id!r} value={getattr(m, 'param_value', None)} "
                f"index={getattr(m, 'param_index', None)}/{getattr(m, 'param_count', None)} "
                f"-- a real GCS<->vehicle parameter is being read/changed right now.")
    if mtype == "PARAM_REQUEST_READ":
        param_id = getattr(m, "param_id", "")
        if isinstance(param_id, (bytes, bytearray)):
            param_id = param_id.split(b"\x00")[0].decode("ascii", "replace")
        return (f"PARAM protocol: PARAM_REQUEST_READ from sysid={sysid} compid={compid} "
                f"target=({getattr(m, 'target_system', None)},{getattr(m, 'target_component', None)}) "
                f"param_id={param_id!r} param_index={getattr(m, 'param_index', None)}")
    if mtype == "PARAM_REQUEST_LIST":
        return (f"PARAM protocol: PARAM_REQUEST_LIST from sysid={sysid} compid={compid} "
                f"target=({getattr(m, 'target_system', None)},{getattr(m, 'target_component', None)}) "
                f"-- a full parameter dump is being requested.")
    if mtype == "PARAM_SET":
        param_id = getattr(m, "param_id", "")
        if isinstance(param_id, (bytes, bytearray)):
            param_id = param_id.split(b"\x00")[0].decode("ascii", "replace")
        return (f"PARAM protocol: PARAM_SET from sysid={sysid} compid={compid} "
                f"target=({getattr(m, 'target_system', None)},{getattr(m, 'target_component', None)}) "
                f"param_id={param_id!r} value={getattr(m, 'param_value', None)}")
    return f"PARAM protocol: {mtype} from sysid={sysid} compid={compid}"


def describe_mission_activity(m) -> str:
    """Format a one-line summary of a real, decoded mission-protocol
    message (waypoint upload/download sequence between a real GCS and a
    real vehicle)."""
    mtype = m.get_type()
    sysid, compid = m.get_srcSystem(), m.get_srcComponent()
    if mtype in ("MISSION_ITEM", "MISSION_ITEM_INT"):
        return (f"MISSION protocol: {mtype} from sysid={sysid} compid={compid} "
                f"seq={getattr(m, 'seq', None)} cmd={getattr(m, 'command', None)} "
                f"frame={getattr(m, 'frame', None)} "
                f"x={getattr(m, 'x', None)} y={getattr(m, 'y', None)} z={getattr(m, 'z', None)} "
                f"-- a real waypoint is being uploaded/downloaded right now.")
    if mtype == "MISSION_COUNT":
        return (f"MISSION protocol: MISSION_COUNT from sysid={sysid} compid={compid} "
                f"count={getattr(m, 'count', None)} "
                f"-- a mission of this many waypoints is about to be transferred.")
    if mtype in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
        return (f"MISSION protocol: {mtype} from sysid={sysid} compid={compid} "
                f"seq={getattr(m, 'seq', None)}")
    if mtype == "MISSION_REQUEST_LIST":
        return f"MISSION protocol: MISSION_REQUEST_LIST from sysid={sysid} compid={compid}"
    if mtype == "MISSION_ACK":
        return (f"MISSION protocol: MISSION_ACK from sysid={sysid} compid={compid} "
                f"type={getattr(m, 'type', None)} "
                f"-- mission transfer sequence completed/acknowledged.")
    if mtype in ("MISSION_CURRENT", "MISSION_SET_CURRENT"):
        return (f"MISSION protocol: {mtype} from sysid={sysid} compid={compid} "
                f"seq={getattr(m, 'seq', None)}")
    return f"MISSION protocol: {mtype} from sysid={sysid} compid={compid}"


def describe_ftp_activity(m) -> str:
    """Format a one-line summary of a real, decoded FILE_TRANSFER_PROTOCOL
    message, decoding the MAVFTP header out of the real payload bytes per
    the published spec (https://mavlink.io/en/services/ftp.html)."""
    sysid, compid = m.get_srcSystem(), m.get_srcComponent()
    payload = bytes(getattr(m, "payload", b""))
    if len(payload) < _MAVFTP_HEADER_LEN:
        return (f"FTP protocol: FILE_TRANSFER_PROTOCOL from sysid={sysid} compid={compid} "
                f"(payload too short to decode MAVFTP header: {len(payload)} bytes)")
    seq = payload[0] | (payload[1] << 8)
    session = payload[2]
    opcode = payload[3]
    size = payload[4]
    req_opcode = payload[5]
    offset = int.from_bytes(payload[8:12], "little")
    opcode_name = _MAVFTP_OPCODE_NAMES.get(opcode, f"UNKNOWN({opcode})")
    req_opcode_name = _MAVFTP_OPCODE_NAMES.get(req_opcode, f"UNKNOWN({req_opcode})")
    return (f"FTP protocol: FILE_TRANSFER_PROTOCOL from sysid={sysid} compid={compid} "
            f"seq={seq} session={session} opcode={opcode_name} size={size} "
            f"req_opcode={req_opcode_name} offset={offset} "
            f"-- a real MAVLink-FTP file transfer (firmware/log/parameter file) "
            f"is in progress on this link.")


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


def _open_serial_with_retry(serial_path: str, baud: int,
                             idle_heartbeat_interval_s: float):
    """Open the SiK/RFD900 serial link for passive RX, tolerating an ABSENT
    device by idling and retrying forever instead of crashing.

    This is the fix for a real deploy regression: the connection used to be
    opened once, unguarded, so a missing device (radio unplugged, udev path
    not yet created) raised serial.SerialException/FileNotFoundError straight
    out of main() -> non-zero exit -> systemd Restart=always crash-loop. A
    merely-absent device is a normal, expected state here (the radio may be
    unplugged), so we log a clear "waiting for ... (sensor idle)" line on the
    same cadence as the idle heartbeat (not silent, not spammy), sleep a
    bounded backoff, and retry until the device appears. Returns the opened
    mavutil connection once the device is present. Never returns on absence --
    it keeps the process alive and idle, matching how the rest of the
    field-bridge fleet degrades gracefully when its hardware is missing."""
    last_idle_heartbeat = 0.0
    announced = False
    while True:
        try:
            mav = mavutil.mavlink_connection(
                serial_path,
                baud=baud,
                # source_system/source_component identify US as a MAVLink
                # participant if we ever needed to reply (we never send),
                # matching rf-bridge/mavlink_bridge.py's RX path (255/190 =
                # a GCS-class node).
                source_system=255,
                source_component=190,
            )
            print(f"Opened {serial_path} @ {baud} baud in RX-ONLY mode "
                  f"(no transmission will occur).")
            return mav
        except _SERIAL_ABSENT_ERRORS as e:
            now = time.time()
            if not announced or now - last_idle_heartbeat >= idle_heartbeat_interval_s:
                announced = True
                last_idle_heartbeat = now
                print(f"[heartbeat] waiting for MAVLink serial device {serial_path} "
                      f"(sensor idle) -- not present/openable yet ({e}); process "
                      f"alive, retrying every {DEVICE_RETRY_BACKOFF_S:.0f}s. This is "
                      f"expected when the SiK/RFD900 radio is unplugged; sniffing "
                      f"starts automatically once the device appears.",
                      file=sys.stderr)
            time.sleep(DEVICE_RETRY_BACKOFF_S)


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

    print("Passive MAVLink HEARTBEAT sniffer running. This posts a detection "
          "ONLY when a REAL, decoded MAVLink HEARTBEAT with "
          "autopilot=ARDUPILOTMEGA is received on this link. If no such "
          "traffic ever arrives, nothing is ever posted — there is no "
          "synthetic fallback, by design.")

    # Task #139: idle-loop heartbeat print. This process is legitimately
    # silent for long stretches whenever no real MAVLink traffic is on the
    # link (see comment below on `m is None`) -- by design, there is no
    # synthetic fallback data. That means log-file mtime alone can't
    # distinguish "alive, just nothing in range" from "hung" the way it can
    # for hackrf_rx.py (which produces spectrum-ingest/classification output
    # every cycle regardless of ambient traffic). Printing a cheap, real
    # "still listening" line on a fixed cadence -- independent of whether any
    # traffic was ever seen -- gives preflight.sh's log-freshness heartbeat
    # check (section 4) a genuine liveness signal for this bridge too,
    # without inventing any detection/telemetry data. The same cadence is
    # reused by _open_serial_with_retry() for the "waiting for device" line so
    # an absent radio is equally visible to preflight.
    IDLE_HEARTBEAT_INTERVAL_S = 60.0

    # Outer reconnect loop: (re)acquire the serial device -- idling+retrying if
    # it is absent -- then sniff until the device goes away (hot-unplug), at
    # which point we fall back here and wait for it to reappear rather than
    # crashing. This makes an unplug/replug (the radio is being plugged in
    # tomorrow while the service may already be running) survivable.
    while True:
        mav = _open_serial_with_retry(args.serial, args.baud,
                                      IDLE_HEARTBEAT_INTERVAL_S)

        # sysid -> last-confirmed-ts, so we don't spam /detections/ingest on
        # every single HEARTBEAT (~1Hz) from the same real airframe. The
        # backend itself also merges re-ingests of the same (source, model,
        # protocol) within its own DETECTION_MERGE_WINDOW_S, so this is a light
        # client-side throttle on top of that, not a replacement for it.
        last_posted: Dict[int, float] = {}
        REPOST_INTERVAL_S = 10.0
        last_idle_heartbeat = 0.0

        try:
            if not _sniff_loop(mav, args, headers, last_posted, REPOST_INTERVAL_S,
                               IDLE_HEARTBEAT_INTERVAL_S, last_idle_heartbeat):
                # _sniff_loop returned False -> the device disappeared mid-run;
                # loop back around to wait for it to come back.
                continue
        finally:
            try:
                mav.close()
            except Exception:
                pass

    return 0  # unreachable; loop runs until process is killed/stopped


def _sniff_loop(mav, args, headers: dict, last_posted: Dict[int, float],
                REPOST_INTERVAL_S: float, IDLE_HEARTBEAT_INTERVAL_S: float,
                last_idle_heartbeat: float) -> bool:
    """Run the passive sniff loop against an already-open connection.

    Returns False if the serial device disappeared mid-run (so the caller
    should close and fall back to waiting for the device). Otherwise loops
    forever (until the process is stopped). All present-device behaviour
    (idle heartbeat, ArduPilot/MAVLink parsing, reauth-on-401 ingest) is
    unchanged from before the graceful-degrade fix."""
    while True:
        try:
            m = mav.recv_match(blocking=True, timeout=1)
        except _SERIAL_ABSENT_ERRORS as e:
            # The device was unplugged / went away mid-run. Don't crash: bail
            # out so the outer loop can idle-and-wait for it to reappear.
            print(f"WARN: MAVLink serial device {args.serial} read error ({e}) -- "
                  f"device may have been unplugged; closing and falling back to "
                  f"wait-for-device (sensor idle).", file=sys.stderr)
            return False
        except Exception as e:
            print(f"WARN: serial/mavlink recv error: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue

        if m is None:
            # Genuinely nothing on the link this second. No fallback data —
            # this is expected and correct behaviour absent real traffic.
            now_idle = time.time()
            if now_idle - last_idle_heartbeat >= IDLE_HEARTBEAT_INTERVAL_S:
                last_idle_heartbeat = now_idle
                print(f"[heartbeat] still listening on {args.serial} — no "
                      f"MAVLink traffic decoded in the last "
                      f"{IDLE_HEARTBEAT_INTERVAL_S:.0f}s (process alive, "
                      f"nothing in range yet).")
            continue
        mtype = m.get_type()
        if mtype in ("BAD_DATA", "UNKNOWN"):
            continue

        # Task #114: passive recognition of ArduPilot parameter/mission/FTP
        # protocol traffic. Log-only -- no ingest, no TX, no injection. See
        # module docstring for why these are not posted to
        # /api/detections/ingest.
        if mtype in PARAM_PROTOCOL_MSG_TYPES:
            print(describe_param_activity(m))
            continue
        if mtype in MISSION_PROTOCOL_MSG_TYPES:
            print(describe_mission_activity(m))
            continue
        if mtype in FTP_PROTOCOL_MSG_TYPES:
            print(describe_ftp_activity(m))
            continue

        if mtype != "HEARTBEAT":
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
