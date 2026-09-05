#!/usr/bin/env python3
"""LIVE Wi-Fi drone SSID/OUI fingerprint: Kismet REST feed -> SSID+OUI match ->
backend ingest.

RX-ONLY. Passive. No transmit path anywhere.

=============================================================================
WHAT THIS IS
=============================================================================
This rides the EXISTING Kismet feed (the same REST device list
remoteid_kismet_bridge.py / parrot_arsdk_ingest_bridge.py already poll -- no
new radio) and flags a Wi-Fi device as a DRONE CANDIDATE when either:

  * its broadcast SSID matches a known drone softAP pattern
    (^TELLO-, ^ANAFI-, ^Autel..., or the generic ^DIRECT- Wi-Fi Direct softAP), or
  * its MAC's OUI (first 3 octets) is a known drone-manufacturer prefix
    (kismet_bridge.DRONE_MANUFACTURER_OUIS -- DJI/Parrot/Autel), or
  * Kismet's own manufacturer string names a drone vendor.

On a match it POSTs /api/wifi-drone/ingest {ssid, oui, manuf, make_candidate,
channel, signal} and, each cycle the Kismet feed is up, a
/api/protocols/heartbeat for the `wifi_drone` protocol.

It reuses kismet_bridge.fetch_kismet_devices() for the exact Kismet REST data
path and kismet_bridge.match_drone_oui()/DRONE_MANUFACTURER_OUIS for the OUI
table -- this file adds only the SSID/manuf matching and the ingest/heartbeat.

=============================================================================
HONEST STATUS -- READ THIS
=============================================================================
An SSID and a MAC OUI are BOTH trivially SPOOFABLE. So a hit here is a
manufacturer/model CANDIDATE, NOT a serial and NOT an exact-confirmed
identity:

  * the ^DIRECT- Wi-Fi Direct softAP pattern is used by MANY non-drone devices
    (printers, Chromecast/Miracast, phones) -- a DIRECT- match alone is a WEAK,
    generic-softAP signal and is labeled as such (make_candidate stays null
    unless a drone OUI also matches).
  * a drone's operator can rename the SSID or randomize the MAC, defeating both
    checks; conversely a non-drone device can be given a drone-looking SSID or
    a cloned OUI. This is a cueing/triage aid, never a positive ID.

This is COMPLEMENTARY to (and may overlap) the Threat-Library Wi-Fi match and
the RemoteID decoder -- overlap is fine and expected.

OFFLINE/READY/LIVE is derived on the backend PURELY from the heartbeat/decode
this bridge posts. If Kismet is DOWN (the REST poll fails) there is no feed to
be READY about, so this bridge does NOT heartbeat -- it logs an honest OFFLINE
and retries. A heartbeat with no feed behind it would be a fake-READY lie.

=============================================================================
CONFIG (env vars, same convention as the other field-bridge scripts)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
KISMET_URL          Kismet REST base (default http://127.0.0.1:2501)
KISMET_APIKEY       Kismet API key (optional; query-param auth)
WIFI_DRONE_POLL_INTERVAL_S   seconds between Kismet polls (default 5)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kismet_bridge import (  # reuse the exact Kismet REST data path + OUI table
    DRONE_MANUFACTURER_OUIS,
    fetch_kismet_devices,
    match_drone_oui,
)

BRIDGE_NAME = "wifi_drone_bridge"
PROTOCOL_ID = "wifi_drone"

# Drone softAP SSID patterns -> the make/model each names. These are the
# well-known factory-default softAP SSIDs a drone broadcasts when acting as its
# own Wi-Fi AP. Anchored at start (^) and case-insensitive. SPOOFABLE (see the
# module docstring) -- a match is a CANDIDATE make, never a confirmed identity.
DRONE_SSID_PATTERNS: List[Tuple["re.Pattern[str]", str]] = [
    (re.compile(r"^TELLO-", re.IGNORECASE), "DJI/Ryze Tello"),
    (re.compile(r"^ANAFI-", re.IGNORECASE), "Parrot Anafi"),
    (re.compile(r"^Autel", re.IGNORECASE), "Autel"),
]
# Generic Wi-Fi Direct softAP. HONEST: used by a huge range of NON-drone
# devices too (printers, Chromecast, Miracast, phone hotspots), so on its own
# this is only a WEAK generic-softAP signal -- it yields a null make_candidate
# unless a drone OUI/manuf also matches.
GENERIC_SOFTAP_PATTERN = re.compile(r"^DIRECT-", re.IGNORECASE)

# Kismet's own resolved manufacturer string (from the IEEE OUI DB) naming a
# drone vendor -- an additional honest signal beyond our small OUI table.
# Substring, case-insensitive.
DRONE_MANUF_HINTS = ("dji", "parrot", "autel", "skydio", "yuneec", "hubsan", "ryze")


def _extract_ssids(device: Dict) -> List[str]:
    """Collect candidate SSID strings from a Kismet device record, schema-
    robustly. Kismet surfaces an AP's SSID under kismet.device.base.name and,
    depending on build, under dot11 last-beaconed/advertised-SSID keys. We walk
    for any non-empty string value under a key whose name contains 'ssid', plus
    base.name. Never raises."""
    found: List[str] = []
    name = device.get("kismet.device.base.name")
    if isinstance(name, str) and name.strip():
        found.append(name.strip())

    def _walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "ssid" in str(k).lower() and isinstance(v, str) and v.strip():
                    found.append(v.strip())
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(device)
    # de-dup, preserve order
    seen = set()
    out = []
    for s in found:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _match_ssid(ssid: str) -> Tuple[Optional[str], bool]:
    """(make_candidate, is_generic_softap) for one SSID. make_candidate is a
    specific make when a drone softAP pattern matches; is_generic_softap is True
    for a bare ^DIRECT- (weak) match with no specific make."""
    for pat, make in DRONE_SSID_PATTERNS:
        if pat.search(ssid):
            return make, False
    if GENERIC_SOFTAP_PATTERN.search(ssid):
        return None, True
    return None, False


def _match_manuf(manuf: Optional[str]) -> Optional[str]:
    """Kismet-reported manufacturer string -> drone vendor name, or None."""
    if not isinstance(manuf, str):
        return None
    low = manuf.lower()
    for hint in DRONE_MANUF_HINTS:
        if hint in low:
            return manuf.strip()
    return None


def match_wifi_drone(ssids: List[str], mac: Optional[str],
                     manuf: Optional[str]) -> Optional[Dict]:
    """Decide whether a Wi-Fi device is a drone CANDIDATE from its SSID(s), MAC
    OUI, and Kismet manufacturer string. Returns a match dict (the ingest body
    core, minus channel/signal) or None if nothing droney matched.

    A specific SSID make or a drone OUI/manuf yields a real make_candidate; a
    bare generic ^DIRECT- softAP with no OUI/manuf corroboration yields a match
    with make_candidate=None (weak generic-softAP signal, honestly labeled)."""
    oui_vendor = match_drone_oui(mac) if mac else None
    manuf_vendor = _match_manuf(manuf)

    ssid_make: Optional[str] = None
    matched_ssid: Optional[str] = None
    generic_softap = False
    for ssid in ssids:
        make, is_generic = _match_ssid(ssid)
        if make:
            ssid_make = make
            matched_ssid = ssid
            break
        if is_generic and matched_ssid is None:
            generic_softap = True
            matched_ssid = ssid  # remember the DIRECT- SSID, keep scanning for a stronger make

    if not (ssid_make or oui_vendor or manuf_vendor or generic_softap):
        return None

    basis_parts: List[str] = []
    if ssid_make:
        basis_parts.append("ssid")
    elif generic_softap:
        basis_parts.append("ssid(generic softAP)")
    if oui_vendor:
        basis_parts.append("oui")
    if manuf_vendor and not oui_vendor:
        basis_parts.append("manuf")

    # Prefer the most specific make available; a bare generic softAP stays null.
    make_candidate = ssid_make or oui_vendor or manuf_vendor

    return {
        "ssid": matched_ssid,
        "oui": _mac_oui(mac) if mac else None,
        "manuf": manuf if isinstance(manuf, str) else None,
        "make_candidate": make_candidate,
        "match_basis": "+".join(basis_parts) if basis_parts else None,
        "source_mac": mac,
        "source": "WIFI_DRONE_KISMET",
        "caveats": wifi_drone_caveats(),
    }


def wifi_drone_caveats() -> List[str]:
    return [
        "SSID and MAC OUI are BOTH spoofable -- this is a make/model CANDIDATE, not a serial or confirmed ID",
        "the generic ^DIRECT- Wi-Fi Direct softAP is used by many non-drone devices (printers/casting/phones)",
        "complementary to (and may overlap) the Threat-Library Wi-Fi match and the RemoteID decoder -- overlap is expected",
    ]


def _mac_oui(mac: str) -> str:
    return ":".join(mac.upper().split(":")[:3])


def _device_mac(device: Dict) -> Optional[str]:
    v = device.get("kismet.device.base.macaddr")
    return v if isinstance(v, str) and v else None


def _device_manuf(device: Dict) -> Optional[str]:
    v = device.get("kismet.device.base.manuf")
    return v if isinstance(v, str) and v else None


def _device_channel(device: Dict) -> Optional[int]:
    """Leading integer of Kismet's channel string (e.g. '6HT40+' -> 6), or None."""
    v = device.get("kismet.device.base.channel")
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.match(r"\s*(\d+)", v)
        if m:
            return int(m.group(1))
    return None


def _device_signal(device: Dict) -> Optional[float]:
    sig = device.get("kismet.device.base.signal") or {}
    v = sig.get("kismet.common.signal.last_signal")
    return float(v) if isinstance(v, (int, float)) else None


def scan_device(device: Dict) -> Optional[Dict]:
    """Build a full /api/wifi-drone/ingest body for one device if it is a drone
    candidate, else None. Only IEEE802.11 (Wi-Fi) devices are considered."""
    phy = str(device.get("kismet.device.base.phyname", "")).lower()
    if "802.11" not in phy and "wifi" not in phy and "wi-fi" not in phy:
        return None
    mac = _device_mac(device)
    match = match_wifi_drone(_extract_ssids(device), mac, _device_manuf(device))
    if match is None:
        return None
    match["channel"] = _device_channel(device)
    match["signal_dbm"] = _device_signal(device)
    return match


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
              since_time_t: Optional[int]) -> int:
    """One poll cycle. Returns the number of drone candidates ingested (0 is a
    normal, honest result). Heartbeats ONLY when the Kismet feed is genuinely
    reachable -- a failed poll is an honest OFFLINE (no fake-READY heartbeat)."""
    try:
        devices = fetch_kismet_devices(kismet_url, apikey, since_time_t)
    except requests.RequestException as e:
        # No feed -> OFFLINE. Do NOT heartbeat (a heartbeat here would be a
        # fake-READY with no Kismet behind it). Same doctrine as adsb_ingest_bridge.
        print(f"[{BRIDGE_NAME}] Kismet feed OFFLINE ({kismet_url}: {e}). "
              "Not heartbeating (no feed = OFFLINE, not fake-READY). Retrying.",
              file=sys.stderr)
        return -1  # sentinel: feed down this cycle

    ingested = 0
    for device in devices:
        body = scan_device(device)
        if body is None:
            continue
        try:
            r = _post_with_reauth(console_url, "/api/wifi-drone/ingest", body,
                                   headers, email, password, timeout=8)
            if r.status_code == 200:
                ingested += 1
                print(f"[{BRIDGE_NAME}] Wi-Fi drone CANDIDATE: "
                      f"make={body['make_candidate']} ssid={body['ssid']} "
                      f"oui={body['oui']} basis={body['match_basis']} "
                      f"mac={body['source_mac']} ch={body['channel']} "
                      f"(SSID+OUI spoofable -- candidate, not a serial)")
            else:
                print(f"[{BRIDGE_NAME}] ingest HTTP {r.status_code}: {r.text[:200]}",
                      file=sys.stderr)
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] ingest failed: {e}", file=sys.stderr)

    # Feed is up -> honest heartbeat every cycle (READY when nothing matched,
    # LIVE when a candidate was ingested this/recent cycle).
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
                     default=float(os.environ.get("WIFI_DRONE_POLL_INTERVAL_S", "5.0")))
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
          f"{args.interval_s}s for Wi-Fi drone SSID/OUI fingerprints. RX ONLY. "
          f"({len(DRONE_MANUFACTURER_OUIS)} drone OUIs, "
          f"{len(DRONE_SSID_PATTERNS)} SSID patterns loaded).")

    i = 0
    since_time_t: Optional[int] = None
    while args.iterations == 0 or i < args.iterations:
        n = poll_once(args.kismet_url, args.kismet_apikey, args.console_url,
                      headers, args.email, args.password, since_time_t)
        if n == 0:
            print(f"[{BRIDGE_NAME}] cycle complete: no Wi-Fi drone candidate "
                  "(feed up, awaiting a matching SSID/OUI -> READY).")
        since_time_t = int(time.time())
        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
