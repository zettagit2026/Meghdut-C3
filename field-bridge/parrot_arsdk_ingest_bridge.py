#!/usr/bin/env python3
"""LIVE Parrot ARSDK3 consumer: EXISTING Kismet Wi-Fi feed -> ARSDK decode ->
backend ingest.

RX-ONLY. Passive. No transmit path anywhere. No radio of its own.

=============================================================================
WHAT THIS IS
=============================================================================
This is the LIVE wiring that turns field-bridge/parrot_arsdk_decode_bridge.py's
verified ARNetworkAL / ARCommand decoder into a running service, exactly the
way remoteid_kismet_bridge.py did for the Remote ID decoder -- and it RIDES THE
SAME Kismet NIC, opening no monitor-mode adapter of its own:

  Kismet (already running, the SAME monitor-mode Wi-Fi NIC remoteid uses)
      -> its REST device list (via kismet_bridge.fetch_kismet_devices, the
         exact data path remoteid_kismet_bridge.py already polls)
      -> find devices that look like a Parrot Bebop/Bebop2/Disco/ANAFI drone
         Wi-Fi link, and extract any raw ARNetworkAL frame bytes Kismet
         surfaced from that link's 802.11 data frames
      -> decode with parrot_arsdk_decode_bridge.py's UNMODIFIED, verified
         iter_frames()/decode_frame()/decode_arcommand()
      -> POST /api/parrot/ingest per observed ARSDK command frame
      -> POST /api/protocols/heartbeat every cycle the Kismet feed is up.

It reuses kismet_bridge.fetch_kismet_devices() for the REST call and
parrot_arsdk_decode_bridge for ALL decode logic -- this file adds only the
glue: ARSDK-payload extraction from a Kismet device record, Parrot-device
identification (for SSID/MAC enrichment), and the ingest/heartbeat POSTs.

=============================================================================
HONEST STATUS -- READY vs LIVE vs OFFLINE
=============================================================================
The decoder is real and verified (BSD-3-Clause-derived ARSDK3 wire format --
see parrot_arsdk_decode_bridge.py). What this service reports depends entirely
on what Kismet surfaces:

  * Kismet reachable + a Parrot drone's ARSDK Wi-Fi frames surfaced in range ->
    a real ARNetworkAL DATA frame with a valid ARCommand decodes -> POST
    /api/parrot/ingest -> the parrot protocol shows LIVE.
  * Kismet reachable but no Parrot ARSDK frames right now (no such drone in
    range, or the link's data-frame payloads are not exposed) -> the service
    heartbeats every cycle -> the board honestly shows READY. Nothing faked.
  * Kismet NOT reachable -> there is no pipeline to be READY about. The service
    does NOT heartbeat this cycle; it logs an honest OFFLINE and retries.

NOTE -- deliberate divergence from remoteid_kismet_bridge.py: that template
heartbeats even on a failed Kismet poll. This bridge does NOT: a heartbeat with
no reachable feed behind it would misreport OFFLINE as READY. This matches the
honesty posture adsb_ingest_bridge.py uses (no feed -> OFFLINE, not fake-READY).

Parrot ARSDK observation is IDENTIFICATION of a specific cooperative airframe
family broadcasting on its own Wi-Fi -- like Remote ID, it positively IDs known
traffic; it is not a universal threat detector (a non-Parrot or wired-link
drone need never appear here).

=============================================================================
CONFIG (env vars, same convention as the other field-bridge scripts)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
KISMET_URL          Kismet REST base (default http://127.0.0.1:2501)
KISMET_APIKEY       Kismet API key (optional; query-param auth)
PARROT_POLL_INTERVAL_S   seconds between Kismet polls (default 5)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parrot_arsdk_decode_bridge as pa  # reuse the VERIFIED ARSDK decoder unchanged
from kismet_bridge import fetch_kismet_devices  # reuse the exact Kismet REST data path

BRIDGE_NAME = "parrot_arsdk_ingest_bridge"
PROTOCOL_ID = "parrot"

# Device-record key substrings whose value MIGHT carry raw ARNetworkAL frame
# bytes (hex) that Kismet surfaced from a Parrot drone's 802.11 data frames. We
# ONLY attempt a decode on values under keys that clearly indicate ARSDK/Parrot
# content -- never arbitrary device bytes -- so a decode success is genuine (the
# decoder validates the frame header + ARCommand header and RAISES otherwise).
_ARSDK_KEY_HINTS = (
    "arsdk", "arnetworkal", "arnetwork", "parrot", "bebop", "anafi",
    "disco", "ardrone3", "pcmd", "arcommand",
)

# Parrot SA registered OUI prefixes (upper-case, no separators) + SSID name
# patterns, used to IDENTIFY a device as a Parrot airframe for SSID/MAC
# enrichment and honest logging. (Identification enriches the ingest body; the
# genuine decode guarantee comes from the ARSDK-hinted-key + structural
# validation above, exactly as remoteid scopes to ODID-hinted keys.)
_PARROT_OUIS = ("9003B7", "A0143D", "00121C", "00267E", "902B34", "907842")
_PARROT_SSID_PATTERNS = (
    "bebop", "anafi", "disco", "ardrone", "rs_", "mambo", "swing",
    "airborne", "parrot",
)

_PROJECT_NAMES = {
    pa.PROJECT_COMMON: "common",
    pa.PROJECT_ARDRONE3: "ardrone3",
}


def _hex_to_bytes(value) -> Optional[bytes]:
    """Coerce a hex-ish string to bytes, or None if it is not clean hex.
    Accepts optional '0x' prefix and ':'/' ' separators. Never raises."""
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
    every hex-string value under a key whose name hints at ARSDK/Parrot content.
    Robust to Kismet schema differences across versions/builds."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{key_path}.{k}" if key_path else str(k)
            kl = str(k).lower()
            if any(h in kl for h in _ARSDK_KEY_HINTS):
                raw = _hex_to_bytes(v)
                if raw is not None:
                    yield kp, raw
            yield from _iter_candidate_payloads(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_candidate_payloads(v, f"{key_path}[{i}]")


def _try_arsdk(raw: bytes) -> List[Dict]:
    """Decode ARSDK command observations from a candidate payload. Tries the
    stream form first (iter_frames, several back-to-back frames as a captured
    payload stream would carry), then a single-frame decode. Returns a list of
    {header, arcommand} for every DATA-family frame that yields a valid
    ARCommand. Never fabricates: anything that does not cleanly decode to a
    DATA frame with an ARCommand header is skipped."""
    observations: List[Dict] = []
    try:
        for header, payload in pa.iter_frames(raw):
            if header["frame_type"] in ("DATA", "DATA_LOW_LATENCY", "DATA_WITH_ACK") and payload:
                try:
                    cmd = pa.decode_arcommand(payload)
                except pa.ARSDKFrameError:
                    continue
                observations.append({"header": header, "arcommand": cmd})
    except pa.ARSDKFrameError:
        pass  # stream not cleanly framed -- fall through to single-frame attempt
    if observations:
        return observations
    try:
        d = pa.decode_frame(raw)
    except pa.ARSDKFrameError:
        return []
    if "arcommand" in d:
        observations.append({"header": d["frame"], "arcommand": d["arcommand"]})
    return observations


def decode_device_arsdk(device: Dict) -> List[Dict]:
    """Extract and decode every ARSDK command frame this Kismet device record
    carries under an ARSDK/Parrot-hinted key. Returns the flat list of
    {header, arcommand} observations (may be empty)."""
    decoded: List[Dict] = []
    for _key_path, raw in _iter_candidate_payloads(device):
        decoded.extend(_try_arsdk(raw))
    return decoded


def parrot_caveats() -> List[str]:
    return [
        "Parrot ARSDK observation IDENTIFIES a specific cooperative airframe family, it is not a universal threat detector",
        "a non-Parrot drone, or one on a wired/encrypted link Kismet cannot surface, need never appear here",
        "read from the EXISTING Kismet monitor NIC -- this bridge opens no radio/adapter of its own",
    ]


def arcommand_to_ingest_body(observation: Dict, *, source_mac: Optional[str] = None,
                             ssid: Optional[str] = None,
                             rssi_dbm: Optional[float] = None) -> Dict:
    """Map one decoded ARSDK observation into the /api/parrot/ingest body shape.
    project/drone_class/command are populated ONLY from the real decoded
    ARCommand header; the class name is taken from the dotted command string
    when the command is one of the decoder's known set, else reported by id."""
    cmd = observation["arcommand"]
    project = _PROJECT_NAMES.get(cmd["project_id"], f"project={cmd['project_id']}")
    command = cmd.get("command")
    drone_class: Optional[str] = None
    if command and command != "UNKNOWN/undecoded" and "." in command:
        parts = command.split(".")
        if len(parts) >= 2:
            drone_class = parts[1]
    if drone_class is None:
        drone_class = f"class={cmd['class_id']}"
    return {
        "project": project,
        "drone_class": drone_class,
        "command": command,
        "source_mac": source_mac,
        "ssid": ssid,
        "rssi_dbm": rssi_dbm,
        "source": "PARROT_ARSDK_KISMET",
        "caveats": parrot_caveats(),
    }


# ---------------------------------------------------------------------------
# Kismet device-record accessors (same keys as remoteid_kismet_bridge.py).
# ---------------------------------------------------------------------------
def _device_mac(device: Dict) -> Optional[str]:
    return device.get("kismet.device.base.macaddr")


def _device_rssi(device: Dict) -> Optional[float]:
    sig = device.get("kismet.device.base.signal") or {}
    v = sig.get("kismet.common.signal.last_signal")
    return float(v) if isinstance(v, (int, float)) else None


def _device_manuf(device: Dict) -> str:
    return str(device.get("kismet.device.base.manuf", ""))


def _device_ssid(device: Dict) -> Optional[str]:
    """Best-effort SSID for a Wi-Fi device across Kismet schema variants: try
    the common name/SSID keys, then any nested key whose name mentions 'ssid'."""
    for key in ("kismet.device.base.name",):
        v = device.get(key)
        if isinstance(v, str) and v:
            return v
    dot11 = device.get("dot11.device") or {}
    for key in ("dot11.device.last_beaconed_ssid", "dot11.device.last_beaconed_ssid_record"):
        v = dot11.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, dict):
            inner = v.get("dot11.advertisedssid.ssid")
            if isinstance(inner, str) and inner:
                return inner
    for kp, val in _iter_str_values(device):
        if "ssid" in kp.lower() and isinstance(val, str) and val:
            return val
    return None


def _iter_str_values(obj, key_path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            kp = f"{key_path}.{k}" if key_path else str(k)
            if isinstance(v, str):
                yield kp, v
            else:
                yield from _iter_str_values(v, kp)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _iter_str_values(v, f"{key_path}[{i}]")


def is_parrot_device(device: Dict) -> bool:
    """True if this Kismet device looks like a Parrot airframe (manufacturer,
    OUI prefix, or a Parrot-style SSID). Used for SSID/MAC enrichment + logging;
    not the genuineness guard for a decode (that is the ARSDK structural
    validation on an ARSDK-hinted-key payload)."""
    if "parrot" in _device_manuf(device).lower():
        return True
    mac = (_device_mac(device) or "").upper().replace(":", "").replace("-", "")
    if len(mac) >= 6 and mac[:6] in _PARROT_OUIS:
        return True
    ssid = (_device_ssid(device) or "").lower()
    return any(p in ssid for p in _PARROT_SSID_PATTERNS)


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


def poll_once(kismet_url: str, apikey: Optional[str], console_url: str,
              headers: dict, email: str, password: str,
              since_time_t: Optional[int]) -> Optional[int]:
    """One poll cycle: fetch Kismet devices, decode any Parrot ARSDK frames,
    ingest each observation, then heartbeat. Returns the number of ARSDK frames
    ingested this cycle (0 is a normal, honest result), or None if the Kismet
    feed was UNREACHABLE this cycle -- in which case NO heartbeat is sent (no
    feed = OFFLINE, not fake-READY)."""
    try:
        devices = fetch_kismet_devices(kismet_url, apikey, since_time_t)
    except requests.RequestException as e:
        print(f"[{BRIDGE_NAME}] Kismet feed OFFLINE ({e}). Not heartbeating "
              "(no feed = OFFLINE, not fake-READY).", file=sys.stderr)
        return None

    ingested = 0
    for device in devices:
        observations = decode_device_arsdk(device)
        if not observations:
            continue
        source_mac = _device_mac(device)
        ssid = _device_ssid(device)
        rssi = _device_rssi(device)
        for obs in observations:
            body = arcommand_to_ingest_body(obs, source_mac=source_mac,
                                            ssid=ssid, rssi_dbm=rssi)
            try:
                r = _post_with_reauth(console_url, "/api/parrot/ingest", body,
                                       headers, email, password, timeout=8)
                if r.status_code == 200:
                    ingested += 1
                    print(f"[{BRIDGE_NAME}] REAL Parrot ARSDK observation: "
                          f"project={body['project']} class={body['drone_class']} "
                          f"command={body['command']} ssid={body['ssid']} "
                          f"mac={body['source_mac']}")
                else:
                    print(f"[{BRIDGE_NAME}] ingest HTTP {r.status_code}: {r.text[:200]}",
                          file=sys.stderr)
            except requests.RequestException as e:
                print(f"[{BRIDGE_NAME}] ingest failed: {e}", file=sys.stderr)

    # Per-cycle liveness heartbeat -- ONLY reached when the Kismet feed was
    # reachable this cycle (the OFFLINE early-return above skips it otherwise).
    try:
        _post_with_reauth(console_url, "/api/protocols/heartbeat",
                          {"protocol": PROTOCOL_ID,
                           "note": f"polled {len(devices)} kismet devices for Parrot ARSDK"},
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
                     default=float(os.environ.get("PARROT_POLL_INTERVAL_S", "5.0")))
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
          f"{args.interval_s}s for Parrot ARSDK Wi-Fi frames. RX ONLY -- rides the "
          "existing Kismet NIC, opens no radio.")

    i = 0
    since_time_t: Optional[int] = None
    while args.iterations == 0 or i < args.iterations:
        n = poll_once(args.kismet_url, args.kismet_apikey, args.console_url,
                      headers, args.email, args.password, since_time_t)
        if n is None:
            print(f"[{BRIDGE_NAME}] cycle complete: Kismet feed OFFLINE (no heartbeat).")
        elif n == 0:
            print(f"[{BRIDGE_NAME}] cycle complete: no Parrot ARSDK frame decoded "
                  "(READY -- feed up, awaiting a Parrot drone's ARSDK Wi-Fi frames "
                  "in range).")
        since_time_t = int(time.time())
        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
