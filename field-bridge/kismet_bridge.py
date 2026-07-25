#!/usr/bin/env python3
"""Kismet REST API -> detection-ingest bridge (passive WiFi/Bluetooth device
presence -> situational-awareness layer). RECEIVE ONLY.

=============================================================================
WHAT THIS IS, AND WHAT IT IS NOT
=============================================================================
This is a TRANSLATION layer, not a detector. A real, full Kismet server
(https://www.kismetwireless.net, GPL-2.0 -- confirmed against the local
checkout's own LICENSE file: "UNLESS OTHERWISE NOTED ... KISMET IS RELEASED
UNDER THE GPL2 LICENSE") does the actual passive 802.11/Bluetooth capture,
frame parsing, and device fingerprinting (OUI manufacturer lookup, device
type classification, signal tracking) using its own mature C++ codebase.
This script does none of that -- it polls a RUNNING Kismet server's own
JSON REST API for devices it has already found and forwards them, in a
shape this project's /api/detections/ingest endpoint understands, into the
same Detection History pipeline the RF bridges feed.

The local checkout at ../kismet (sibling of this repo) was inspected and
confirmed to be the REAL, FULL Kismet project source tree (kismet_server.cc,
devicetracker.cc, ~150 capture_* datasource plugins including
capture_antsdr_droneid/ -- the DJI DroneID-specific datasource tracked
separately as task #81/#70) -- not merely the droneid capture plugin in
isolation.

This script deliberately does NOT reimplement:
  - 802.11/Bluetooth frame capture or parsing (Kismet's job, via its own
    datasource plugins and a real monitor-mode WiFi/BT radio)
  - OUI manufacturer lookup (Kismet's devicetracker.cc already does this;
    see "kismet.device.base.manuf", registered in
    ../kismet/devicetracker_component.cc:516)
  - device-type classification (Kismet's own "kismet.device.base.type")

=============================================================================
REAL KISMET REST API SURFACE THIS SCRIPT TARGETS (verified against source)
=============================================================================
Verified by reading the local Kismet checkout's own C++ source (not assumed
from memory of public docs):

  Endpoint (../kismet/devicetracker.cc:335, :378):
    GET/POST /devices/all_devices.json
    GET/POST /devices/last-time/:timestamp/devices.json
      -- used here with timestamp=<last poll's max last_time>, or -<seconds>
      relative form, so each poll only returns devices seen/updated since
      the previous poll (avoids re-ingesting the same device every cycle).

  Auth: ../kismet/kis_net_beast_httpd.cc:39 defines
    const std::string kis_net_beast_httpd::AUTH_COOKIE{"KISMET"};
  and ../kismet/devicetracker.cc registers /auth/apikey/generate,
  /auth/apikey/revoke, /auth/apikey/list under LOGON_ROLE. A Kismet apikey
  (generated via the Kismet web UI or `kismet_client`/curl against
  /auth/apikey/generate) is passed as the "KISMET" query parameter on every
  request, matching that AUTH_COOKIE name -- this is Kismet's own documented
  scheme, not invented here.

  Per-device field names (verified in
  ../kismet/devicetracker_component.cc, register_field() calls, line
  numbers as inspected):
    kismet.device.base.macaddr    (:473) -- MAC address
    kismet.device.base.phyname    (:474) -- "IEEE802.11" / "Bluetooth" / etc
    kismet.device.base.name       (:475) -- printable device name (SSID etc)
    kismet.device.base.type       (:480) -- printable device type string
    kismet.device.base.first_time (:487) -- first-seen unix time_t
    kismet.device.base.last_time  (:488) -- last-seen unix time_t
    kismet.device.base.channel    (:514) -- channel (phy-specific)
    kismet.device.base.frequency  (:515) -- frequency
    kismet.device.base.manuf      (:516) -- Kismet's own OUI-derived
                                             manufacturer string
    kismet.device.base.signal (dynamic, :511) -- nested signal-data object
      containing kismet.common.signal.last_signal (dBm, registered in
      devicetracker_component.cc:304 under the "kis_tracked_signal_data"
      sub-object)

This script's TEST_FIXTURE (see build_test_fixture() below) reproduces this
exact field-name schema for offline testing -- it is not fabricated drone
data, it is Kismet's own documented device-JSON shape, populated with
plausible WiFi/BT MAC/vendor values for a phone, a laptop, and (for the
OUI-filter test path) a DJI-OUI-prefixed MAC.

=============================================================================
SCOPE / HONESTY: THIS IS A SITUATIONAL-AWARENESS LAYER, NOT A DRONE CLASSIFIER
=============================================================================
The overwhelming majority of WiFi/Bluetooth devices Kismet will ever see in
a real deployment are phones, laptops, headphones, smart-home gadgets, etc.
-- NOT drones. Posting every Kismet-seen device as a "detection" with a
drone-flavored threat_level would be dishonest and would flood the
Detection History with noise. This bridge therefore does two separate
things with two separate confidence_type values (see backend/server.py's
DetectionIngestBody.confidence_type and CONFIDENCE_MODEL.md):

  1. DRONE-OUI-MATCHED devices (this device's MAC prefix matches a known
     drone-manufacturer OUI -- see DRONE_MANUFACTURER_OUIS below) are
     posted with confidence_type="heuristic_binary" (an OUI match is a
     genuine, binary, verifiable fact about the MAC address -- but it is
     still only a MANUFACTURER match, not a protocol-level identification
     of a flying drone; a MAC-cloned or non-flying device with the same
     OUI would match identically). threat_level is left at "MEDIUM".

  2. ALL OTHER devices Kismet reports are posted with
     confidence_type="advisory_only" (same enum value used by
     hackrf_rx.py's existing low-severity Bluetooth-presence advisory,
     field-bridge/hackrf_rx.py:1070) -- explicitly a presence-only signal,
     not a threat/identity claim. threat_level is fixed at "LOW".

  By default (--drone-oui-only), this script only forwards OUI-matched
  devices, to avoid flooding ingest with every phone Kismet sees. Pass
  --forward-all-devices to also forward the advisory_only stream for full
  situational awareness (expect high volume in any real environment).

DRONE_MANUFACTURER_OUIS below is a SMALL, BEST-EFFORT, NON-EXHAUSTIVE list
of IEEE-assigned OUI prefixes publicly registered to drone manufacturers
(DJI, Parrot, Autel). IMPORTANT HONESTY NOTE: this repo's task #69
(RF-Drone-Detection WiFi MAC-OUI work) was searched for at the time this
bridge was written and NO reusable OUI list file was found on disk in this
checkout -- if that work exists in a separate project/session, it was not
found here and this list was built fresh from public IEEE OUI assignments
instead of reusing it. If task #69's list is later located, it should
replace/merge with this one rather than maintaining two.

=============================================================================
HARDWARE STATUS -- HARDWARE-BLOCKED, LIKE THE UNDERLYING KISMET CAPTURE ITSELF
=============================================================================
This bridge only ever produces real output if the Kismet SERVER it polls
has real detections, which requires Kismet itself to have a real
monitor-mode-capable WiFi adapter and/or Bluetooth adapter attached and
configured as a Kismet datasource. This project's WiFi monitor-mode
capability is tracked as HARDWARE-BLOCKED under task #70 (no Alfa
AWUS036NHA-class monitor-mode NIC on primary as of this session) -- the
exact same hardware gap. No such hardware and no running Kismet server were
available this session. This script was built and tested only against
--use-test-fixture (an offline, hardcoded JSON payload matching Kismet's
real documented schema, see above) -- it has NEVER been run against a real
Kismet server. Do NOT enable a live systemd unit for this script until (a)
task #70's WiFi adapter exists and is verified in monitor mode, (b) a real
Kismet server is running against it and its REST API has been manually
smoke-tested (e.g. `curl "http://127.0.0.1:2501/devices/all_devices.json?KISMET=<apikey>"`),
and (c) this script has been run against that real server and its output
manually reviewed. No cema-kismet-bridge.service unit is included in this
commit -- see field-bridge/README.md's "DO NOT ENABLE UNTIL HARDWARE EXISTS"
pattern (same as CRSF/MSP/CANopen/DroneCAN).

RX-ONLY: this script only issues GET requests to the Kismet server and POST
requests to this project's own /api/detections/ingest. It never writes to
Kismet, never transmits RF, and never controls any datasource.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Best-effort, non-exhaustive drone-manufacturer OUI prefixes (first 3 octets
# of MAC, uppercase, colon-separated), sourced from public IEEE OUI
# assignments. See module docstring's HONESTY NOTE: task #69's own WiFi
# MAC-OUI work was searched for and not found reusable in this checkout, so
# this list was built independently and should be merged with task #69's
# work if/when that is located, rather than kept as a second copy.
DRONE_MANUFACTURER_OUIS = {
    "60:60:1F": "DJI",
    "34:D2:62": "DJI",
    "A0:14:3D": "DJI",
    "48:1C:B9": "DJI",
    "90:3A:E6": "Parrot",
    "00:12:1C": "Parrot",
    # VERIFIED 2026-07-25 (task #97): the previous "A0:14:3D:00" entry was a
    # malformed 4-octet key (a real OUI prefix is exactly 3 octets), so it was
    # silently dropped by the normalization filter below and Autel devices
    # were NEVER OUI-flagged. Replaced with a real, currently-registered
    # 3-octet OUI confirmed directly against the IEEE Registration Authority
    # database (standards-oui.ieee.org/oui28/mam.txt, MA-M/28-bit block):
    # "EC-5B-CD ... Autel Robotics USA LLC". Verified by fetching that file
    # directly, not guessed/assumed.
    "EC:5B:CD": "Autel",
}
# Normalize to strict 3-octet keys only (defensive against the placeholder
# above / any future accidental non-3-octet entry).
DRONE_MANUFACTURER_OUIS = {
    k: v for k, v in DRONE_MANUFACTURER_OUIS.items() if len(k.split(":")) == 3
}


def mac_oui(mac: str) -> str:
    return ":".join(mac.upper().split(":")[:3])


def match_drone_oui(mac: str) -> Optional[str]:
    return DRONE_MANUFACTURER_OUIS.get(mac_oui(mac))


# ---------------------------------------------------------------------------
# Console auth (same convention as every other field-bridge script --
# canonical copy + rationale in hackrf_rx.py).
# ---------------------------------------------------------------------------
def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _post_with_reauth(console_url: str, path: str, json_body: dict, headers: dict,
                       email: str, password: str, timeout: float = 5) -> "requests.Response":
    """POST to the backend, auto-recovering from an expired JWT by re-login
    ONCE and retrying. Duplicated per-file (same convention as every other
    field-bridge script -- no shared auth module exists in field-bridge/);
    canonical copy + rationale lives in hackrf_rx.py."""
    url = f"{console_url}{path}"
    headers.setdefault("X-Bridge-Name", "kismet_bridge")
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


# ---------------------------------------------------------------------------
# Kismet REST client
# ---------------------------------------------------------------------------
def fetch_kismet_devices(kismet_url: str, apikey: Optional[str],
                         since_time_t: Optional[int], timeout: float = 10) -> List[Dict]:
    """GET a device list from a real Kismet server's REST API.

    Uses /devices/last-time/:timestamp/devices.json when since_time_t is
    given (incremental poll, per devicetracker.cc:378), else
    /devices/all_devices.json (devicetracker.cc:335). Both are real,
    verified Kismet routes -- see module docstring.
    """
    if since_time_t is not None:
        path = f"/devices/last-time/{since_time_t}/devices.json"
    else:
        path = "/devices/all_devices.json"
    params = {}
    if apikey:
        params["KISMET"] = apikey  # Kismet's own AUTH_COOKIE name, as query param
    r = requests.get(f"{kismet_url}{path}", params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return []


def build_test_fixture() -> List[Dict]:
    """Offline test fixture matching Kismet's REAL documented device-JSON
    field-name schema (see module docstring), NOT fabricated drone data --
    field names, nesting, and value types mirror what devicetracker.cc's
    register_field() calls actually produce. Values are plausible sample
    WiFi/BT devices (a phone, a laptop, and one DJI-OUI-prefixed MAC to
    exercise the OUI-filter path), used only for --use-test-fixture.
    """
    now = int(time.time())
    return [
        {
            "kismet.device.base.macaddr": "3C:5A:B4:11:22:33",
            "kismet.device.base.phyname": "IEEE802.11",
            "kismet.device.base.name": "Galaxy-S23",
            "kismet.device.base.type": "Wi-Fi Client",
            "kismet.device.base.manuf": "Samsung Electronics Co.,Ltd",
            "kismet.device.base.first_time": now - 600,
            "kismet.device.base.last_time": now - 5,
            "kismet.device.base.channel": "6",
            "kismet.device.base.frequency": 2437000,
            "kismet.device.base.signal": {"kismet.common.signal.last_signal": -62},
        },
        {
            "kismet.device.base.macaddr": "F4:5C:89:AA:BB:CC",
            "kismet.device.base.phyname": "Bluetooth",
            "kismet.device.base.name": "MX Master 3",
            "kismet.device.base.type": "BT Device",
            "kismet.device.base.manuf": "Logitech, Inc.",
            "kismet.device.base.first_time": now - 300,
            "kismet.device.base.last_time": now - 2,
            "kismet.device.base.channel": "0",
            "kismet.device.base.frequency": 2402000,
            "kismet.device.base.signal": {"kismet.common.signal.last_signal": -71},
        },
        {
            # DJI-OUI-prefixed MAC (60:60:1F is a real DJI-registered OUI),
            # to exercise the drone-OUI-match path honestly -- this is a
            # test fixture value, not a claim that a real drone was seen.
            "kismet.device.base.macaddr": "60:60:1F:44:55:66",
            "kismet.device.base.phyname": "IEEE802.11",
            "kismet.device.base.name": "",
            "kismet.device.base.type": "Wi-Fi AP",
            "kismet.device.base.manuf": "Dji Innovations",
            "kismet.device.base.first_time": now - 120,
            "kismet.device.base.last_time": now - 1,
            "kismet.device.base.channel": "149",
            "kismet.device.base.frequency": 5745000,
            "kismet.device.base.signal": {"kismet.common.signal.last_signal": -55},
        },
    ]


def to_detection(device: Dict, drone_manuf: Optional[str]) -> Dict:
    """Translate one Kismet device-JSON object into this project's
    /api/detections/ingest body. Pure translation -- no new detection
    logic; drone_manuf (if not None) is the only thing this bridge itself
    decided (an OUI-prefix match), everything else is passed through from
    Kismet's own fields."""
    mac = device.get("kismet.device.base.macaddr", "UNKNOWN")
    phy = device.get("kismet.device.base.phyname", "unknown")
    name = device.get("kismet.device.base.name") or mac
    dtype = device.get("kismet.device.base.type", "unknown")
    manuf = device.get("kismet.device.base.manuf", "unknown")
    freq_khz = device.get("kismet.device.base.frequency") or 0
    signal_obj = device.get("kismet.device.base.signal") or {}
    last_signal = signal_obj.get("kismet.common.signal.last_signal")

    # Kismet's frequency field is in kHz (802.11) or absent/0 for some BT
    # entries; convert to GHz for this project's schema. Fall back to the
    # 2.4GHz ISM band center as a documented default (NOT a measurement)
    # when Kismet reports no frequency, since center_freq_ghz is a required
    # field on DetectionIngestBody.
    center_freq_ghz = (freq_khz / 1_000_000.0) if freq_khz else 2.437

    is_drone_oui = drone_manuf is not None

    detection = {
        "callsign": f"KISMET-{mac}",
        "model": (f"{drone_manuf}-OUI WiFi/BT device ({dtype})" if is_drone_oui
                 else f"{manuf} {dtype}".strip()),
        "protocol": phy,  # "IEEE802.11" or "Bluetooth", as Kismet reports it
        "threat_level": "MEDIUM" if is_drone_oui else "LOW",
        "center_freq_ghz": round(center_freq_ghz, 6),
        "bandwidth_mhz": 20.0 if phy == "IEEE802.11" else 2.0,
        "rssi_dbm": float(last_signal) if last_signal is not None else -90.0,
        "snr_db": 0.0,  # Kismet's device JSON does not expose a per-device SNR
        "encrypted": False,  # Kismet reports crypt info separately; not asserted here
        "source": "KISMET",
        # This is device PRESENCE forwarded from Kismet's own detection, not
        # a protocol decode performed by this bridge -- protocol_confirmed
        # stays False (its documented meaning is specifically "RF-energy
        # heuristic vs genuinely decoded protocol message", which does not
        # apply to Kismet's 802.11/BT frame parsing in the sense this field
        # was designed for; see mavlink_sniffer.py/crsf_parser.py for what
        # protocol_confirmed=True is meant to represent).
        "protocol_confirmed": False,
        # See module docstring's SCOPE/HONESTY section: a drone-OUI match is
        # a genuine binary fact about the MAC (heuristic_binary); everything
        # else is a bare presence advisory (advisory_only), matching the
        # existing enum used by hackrf_rx.py's Bluetooth-presence advisory.
        "confidence_type": "heuristic_binary" if is_drone_oui else "advisory_only",
    }
    return detection


def poll_once(console_url: str, headers: Dict, email: str, password: str,
             devices: List[Dict], forward_all: bool, seen_macs: Dict[str, float],
             repost_interval_s: float) -> int:
    posted = 0
    now = time.time()
    for device in devices:
        mac = device.get("kismet.device.base.macaddr")
        if not mac:
            continue
        drone_manuf = match_drone_oui(mac)
        if not forward_all and drone_manuf is None:
            continue  # default: only forward OUI-matched (likely-drone) devices
        if now - seen_macs.get(mac, 0.0) < repost_interval_s:
            continue
        seen_macs[mac] = now
        detection = to_detection(device, drone_manuf)
        try:
            r = _post_with_reauth(console_url, "/api/detections/ingest", detection,
                                  headers, email, password, timeout=5)
            r.raise_for_status()
            posted += 1
            print(f"[kismet_bridge] posted {detection['confidence_type']} detection for "
                  f"{mac} ({detection['model']}) -> {r.json().get('callsign')}")
        except requests.RequestException as e:
            print(f"detection ingest failed for {mac}: {e}", file=sys.stderr)
    return posted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--kismet-url", default=os.environ.get("KISMET_URL", "http://127.0.0.1:2501"),
                    help="Base URL of a running Kismet server's REST API.")
    ap.add_argument("--kismet-apikey", default=os.environ.get("KISMET_APIKEY"),
                    help="Kismet apikey (generate via Kismet's /auth/apikey/generate "
                         "or web UI). Passed as the 'KISMET' query param, matching "
                         "kis_net_beast_httpd::AUTH_COOKIE.")
    ap.add_argument("--poll-interval-s", type=float,
                    default=float(os.environ.get("KISMET_POLL_INTERVAL_S", "5")))
    ap.add_argument("--repost-interval-s", type=float, default=60.0,
                    help="Minimum seconds between re-posting the same MAC (client-side "
                         "throttle on top of the backend's own DETECTION_MERGE_WINDOW_S).")
    ap.add_argument("--forward-all-devices", action="store_true",
                    help="Forward every Kismet-seen device as an advisory_only "
                         "detection, not just drone-OUI matches. Expect high volume.")
    ap.add_argument("--use-test-fixture", action="store_true",
                    help="Run against build_test_fixture()'s hardcoded offline payload "
                         "instead of a real Kismet server -- for testing this bridge's "
                         "translation logic without live hardware. Runs exactly one "
                         "poll cycle and exits.")
    args = ap.parse_args()

    if args.use_test_fixture:
        # Offline self-test path: no console/Kismet network calls at all,
        # just prints what WOULD be posted. Useful for CI / no-hardware dev.
        devices = build_test_fixture()
        seen: Dict[str, float] = {}
        for device in devices:
            mac = device.get("kismet.device.base.macaddr")
            drone_manuf = match_drone_oui(mac)
            if not args.forward_all_devices and drone_manuf is None:
                continue
            detection = to_detection(device, drone_manuf)
            print(f"[test-fixture] would POST: {detection}")
        return 0

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var)")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Polling Kismet REST API at {args.kismet_url} every "
          f"{args.poll_interval_s}s. forward_all_devices={args.forward_all_devices}.")
    print("HARDWARE-BLOCKED NOTICE: this bridge only produces real output if the "
          "Kismet server it polls has a real monitor-mode WiFi/BT datasource "
          "attached -- see module docstring.")

    seen_macs: Dict[str, float] = {}
    last_time_t: Optional[int] = None
    while True:
        try:
            devices = fetch_kismet_devices(args.kismet_url, args.kismet_apikey, last_time_t)
        except requests.RequestException as e:
            print(f"WARN: Kismet REST fetch failed: {e}", file=sys.stderr)
            time.sleep(args.poll_interval_s)
            continue

        poll_once(args.console_url, headers, args.email, args.password,
                 devices, args.forward_all_devices, seen_macs, args.repost_interval_s)

        last_times = [d.get("kismet.device.base.last_time") for d in devices
                     if d.get("kismet.device.base.last_time")]
        if last_times:
            last_time_t = max(last_times)

        time.sleep(args.poll_interval_s)

    return 0  # unreachable; loop runs until process is killed/stopped


if __name__ == "__main__":
    sys.exit(main())
