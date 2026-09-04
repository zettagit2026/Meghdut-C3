#!/usr/bin/env python3
"""LIVE Remote ID consumer: Kismet REST feed -> ODID decode -> backend ingest.

RX-ONLY. Passive. No transmit path anywhere.

=============================================================================
WHAT THIS IS
=============================================================================
This is the LIVE wiring that turns field-bridge/remoteid_decode_bridge.py's
verified ASTM F3411 / ASD-STAN Remote ID decoder into a running service:

  Kismet (already running, monitor-mode NIC + BLE dongle)
      -> its REST device list (same data path kismet_bridge.py already polls)
      -> extract the raw OpenDroneID (ODID) payload a drone broadcasts in its
         802.11 Beacon/NAN vendor IE or its BLE ASTM Service Data AD structure
      -> decode with remoteid_decode_bridge.py's UNMODIFIED, reference-verified
         decode_message()/decode_message_pack()/decode_bluetooth_service_data()
      -> aggregate the message set into {id, serial, position, operator}
      -> POST /api/remoteid/ingest (and a per-cycle /api/protocols/heartbeat).

It reuses kismet_bridge.fetch_kismet_devices() for the Kismet REST call and
remoteid_decode_bridge for ALL decode logic -- this file adds only the glue:
ODID-payload extraction from a Kismet device record, message aggregation, and
the ingest/heartbeat POSTs.

=============================================================================
HONEST STATUS -- READ THIS
=============================================================================
The decoder is real and reference-verified. Whether this service ever emits a
decode depends entirely on whether the Kismet instance it polls actually
surfaces ODID payload bytes:

  * If Kismet is running with a monitor-mode Wi-Fi NIC and/or a BLE5-capable
    dongle AND is configured to expose the drone/OpenDroneID IE bytes, a real
    compliant-drone broadcast in range will decode -> the remoteid protocol
    shows LIVE on the Protocol Library board.
  * If Kismet exposes no ODID payloads (no monitor-mode source, no compliant
    drone in range, or a Kismet build that does not surface the raw IE), this
    service runs and heartbeats every cycle but decodes nothing -> the board
    honestly shows READY (running, awaiting a matching broadcast), NOT a fake
    LIVE. No telemetry is ever fabricated.

Remote ID is a DECONFLICTION aid (positively identify KNOWN, LEGAL traffic),
NOT a threat detector -- a hostile/non-compliant drone need not broadcast it.
See remoteid_decode_bridge.py's module docstring for the full doctrine.

=============================================================================
CONFIG (env vars, same convention as the other field-bridge scripts)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
KISMET_URL          Kismet REST base (default http://127.0.0.1:2501)
KISMET_APIKEY       Kismet API key (optional; query-param auth)
REMOTEID_POLL_INTERVAL_S   seconds between Kismet polls (default 5)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import remoteid_decode_bridge as rid
from kismet_bridge import fetch_kismet_devices  # reuse the exact Kismet REST data path

BRIDGE_NAME = "remoteid_kismet_bridge"
PROTOCOL_ID = "remoteid"

# Device-record key substrings whose value MIGHT carry a raw ODID payload (hex
# string) that Kismet surfaced from a drone's 802.11 vendor IE or BLE Service
# Data AD. We ONLY attempt a decode on values under keys that clearly indicate
# OpenDroneID/RemoteID/DroneID content -- never on arbitrary device bytes -- so
# a decode success is genuine (the decoder validates message type + protocol
# version and RAISES on anything else; it never guesses).
_ODID_KEY_HINTS = (
    "opendroneid", "open_drone_id", "odid", "remoteid", "remote_id",
    "droneid", "drone_id", "uav.message", "servicedata", "service_data",
)
# Kismet BLE advertising Service Data can arrive as a full AD payload rather
# than the inner ODID bytes; try the BLE outer-framing path for these.
_BLE_KEY_HINTS = ("ble", "bluetooth", "advertised", "advertising", "servicedata")


def _hex_to_bytes(value) -> Optional[bytes]:
    """Coerce a hex-ish string to bytes, or None if it is not clean hex.
    Accepts optional '0x' prefix and ':'/' ' separators (common Kismet IE
    hex-dump styles). Never raises."""
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace("0x", "").replace(":", "").replace(" ", "")
    if len(s) < 2 or len(s) % 2 != 0:
        return None
    try:
        return bytes.fromhex(s)
    except ValueError:
        return None


def _iter_candidate_payloads(obj, key_path: str = ""):
    """Walk a Kismet device record recursively, yielding (key_path, bytes) for
    every hex-string value found under a key whose name hints at ODID/RemoteID
    content. Robust to Kismet schema differences across versions/builds."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{key_path}.{k}" if key_path else str(k)
            kl = str(k).lower()
            if any(h in kl for h in _ODID_KEY_HINTS):
                raw = _hex_to_bytes(v)
                if raw is not None:
                    yield kp, raw
            yield from _iter_candidate_payloads(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_candidate_payloads(v, f"{key_path}[{i}]")


def decode_device_odid(device: Dict) -> List[Dict]:
    """Extract and decode every ODID message this Kismet device record carries.

    Tries, for each ODID-hinted hex payload found:
      1. the Wi-Fi/Beacon path: decode_message() / decode_message_pack()
      2. the BLE path: decode_bluetooth_service_data() (outer AD framing)
    Returns the flat list of successfully-decoded ODID message dicts (may be
    empty). Never fabricates: a payload that does not cleanly decode is skipped.
    """
    decoded: List[Dict] = []
    for key_path, raw in _iter_candidate_payloads(device):
        is_ble = any(h in key_path.lower() for h in _BLE_KEY_HINTS)
        # Try the transport most likely for this key first, then the other.
        attempts = ([_try_ble, _try_wifi] if is_ble else [_try_wifi, _try_ble])
        for attempt in attempts:
            msgs = attempt(raw)
            if msgs:
                decoded.extend(msgs)
                break
    return decoded


def _try_wifi(raw: bytes) -> List[Dict]:
    try:
        if rid.peek_message_type(raw) == rid.MESSAGETYPE_PACKED:
            return [m for m in rid.decode_message_pack(raw) if "error" not in m]
        return [rid.decode_message(raw)]
    except (rid.RemoteIDDecodeError, Exception):
        return []


def _try_ble(raw: bytes) -> List[Dict]:
    try:
        return rid.decode_bluetooth_service_data(raw)
    except (rid.BluetoothFrameError, rid.RemoteIDDecodeError, Exception):
        return []


def aggregate_messages(messages: List[Dict], *, source_mac: Optional[str] = None,
                        transport: Optional[str] = None,
                        rssi_dbm: Optional[float] = None) -> Optional[Dict]:
    """Aggregate a decoded ODID message set for ONE sender into the
    /api/remoteid/ingest body shape {id, serial, position, operator}. Returns
    None if the message set carries no usable identity/position/operator field
    (nothing worth ingesting). Every output field is populated ONLY from a real
    decoded message -- absent message types leave their fields null."""
    if not messages:
        return None
    body: Dict = {
        "uas_id": None, "id_type": None, "ua_type": None,
        "latitude_deg": None, "longitude_deg": None, "altitude_geo_m": None,
        "height_m": None, "speed_horizontal_mps": None,
        "operator_id": None, "operator_latitude_deg": None,
        "operator_longitude_deg": None, "description": None,
        "source_mac": source_mac, "transport": transport, "rssi_dbm": rssi_dbm,
        "message_types": [], "caveats": list(rid_caveats()),
    }
    seen_types: List[str] = []
    for m in messages:
        mt = m.get("message_type")
        if mt and mt not in seen_types:
            seen_types.append(mt)
        if mt == "BASIC_ID":
            body["uas_id"] = body["uas_id"] or m.get("uas_id")
            body["id_type"] = body["id_type"] or m.get("id_type")
            body["ua_type"] = body["ua_type"] or m.get("ua_type")
        elif mt == "LOCATION":
            body["latitude_deg"] = m.get("latitude_deg")
            body["longitude_deg"] = m.get("longitude_deg")
            body["altitude_geo_m"] = m.get("altitude_geo_m")
            body["height_m"] = m.get("height_m")
            body["speed_horizontal_mps"] = m.get("speed_horizontal_mps")
        elif mt == "SYSTEM":
            body["operator_latitude_deg"] = m.get("operator_latitude_deg")
            body["operator_longitude_deg"] = m.get("operator_longitude_deg")
        elif mt == "OPERATOR_ID":
            body["operator_id"] = m.get("operator_id")
        elif mt == "SELF_ID":
            body["description"] = m.get("description")
    body["message_types"] = seen_types
    # Worth ingesting only if we recovered at least an identity, a position, or
    # an operator field -- otherwise there is nothing to report.
    if not (body["uas_id"] or body["operator_id"] or body["latitude_deg"] is not None):
        return None
    return body


def rid_caveats() -> List[str]:
    return [
        "Remote ID is a DECONFLICTION aid (identifies KNOWN/legal traffic), not a threat detector",
        "a hostile/non-compliant drone need not broadcast Remote ID at all",
        "no cryptographic authentication of the broadcast beyond the optional Auth message",
    ]


# ---------------------------------------------------------------------------
# Console auth (same convention as every other field-bridge script).
# ---------------------------------------------------------------------------
def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5) -> "requests.Response":
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", BRIDGE_NAME)
    r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        try:
            headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] re-login failed ({e})", file=sys.stderr)
            return r
        r = requests.post(url, json=json_body, headers=headers, timeout=timeout)
    return r


def _device_mac(device: Dict) -> Optional[str]:
    return device.get("kismet.device.base.macaddr")


def _device_rssi(device: Dict) -> Optional[float]:
    sig = device.get("kismet.device.base.signal") or {}
    v = sig.get("kismet.common.signal.last_signal")
    return float(v) if isinstance(v, (int, float)) else None


def _device_transport(device: Dict) -> str:
    phy = str(device.get("kismet.device.base.phyname", "")).lower()
    return "bluetooth" if "blue" in phy or "bt" in phy else "wifi"


def poll_once(kismet_url: str, apikey: Optional[str], console_url: str,
              headers: dict, email: str, password: str,
              since_time_t: Optional[int]) -> int:
    """One poll cycle: fetch Kismet devices, decode any ODID broadcasts, ingest
    each aggregated sender. Returns the number of Remote ID senders ingested
    this cycle (0 is a normal, honest result). Always posts a heartbeat so the
    board shows READY even when nothing decoded."""
    ingested = 0
    try:
        devices = fetch_kismet_devices(kismet_url, apikey, since_time_t)
    except requests.RequestException as e:
        print(f"[{BRIDGE_NAME}] Kismet poll failed: {e}", file=sys.stderr)
        devices = []

    for device in devices:
        messages = decode_device_odid(device)
        if not messages:
            continue
        body = aggregate_messages(
            messages,
            source_mac=_device_mac(device),
            transport=_device_transport(device),
            rssi_dbm=_device_rssi(device),
        )
        if body is None:
            continue
        try:
            r = _post_with_reauth(console_url, "/api/remoteid/ingest", body,
                                   headers, email, password, timeout=8)
            if r.status_code == 200:
                ingested += 1
                print(f"[{BRIDGE_NAME}] REAL Remote ID decode: id={body['uas_id']} "
                      f"operator={body['operator_id']} pos=({body['latitude_deg']},"
                      f"{body['longitude_deg']}) via {body['transport']} {body['source_mac']}")
            else:
                print(f"[{BRIDGE_NAME}] ingest HTTP {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] ingest failed: {e}", file=sys.stderr)

    # Per-cycle liveness heartbeat regardless of decode outcome (READY vs LIVE
    # is then derived purely from decode recency on the backend).
    try:
        _post_with_reauth(console_url, "/api/protocols/heartbeat",
                          {"protocol": PROTOCOL_ID,
                           "note": f"polled {len(devices)} kismet devices"},
                          headers, email, password, timeout=5)
    except requests.RequestException:
        pass
    return ingested


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--kismet-url", default=os.environ.get("KISMET_URL", "http://127.0.0.1:2501"))
    ap.add_argument("--kismet-apikey", default=os.environ.get("KISMET_APIKEY"))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("REMOTEID_POLL_INTERVAL_S", "5.0")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[{BRIDGE_NAME}] logged in. Polling Kismet at {args.kismet_url} every "
          f"{args.interval_s}s for Remote ID broadcasts. RX ONLY.")

    i = 0
    since_time_t: Optional[int] = None
    while args.iterations == 0 or i < args.iterations:
        n = poll_once(args.kismet_url, args.kismet_apikey, args.console_url,
                      headers, args.email, args.password, since_time_t)
        if n == 0:
            print(f"[{BRIDGE_NAME}] cycle complete: no Remote ID broadcast decoded "
                  "(expected/honest if no compliant drone is broadcasting in range, "
                  "or Kismet is not surfacing ODID payloads).")
        since_time_t = int(time.time())
        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
