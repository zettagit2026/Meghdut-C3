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

=============================================================================
TASK #105 -- DroneBridge WifiBroadcast RAW-FRAME SIGNATURE (WHY IT LIVES
HERE, AND WHY IT IS NOT WIRED INTO THE LIVE poll_once() PATH)
=============================================================================
DroneBridge's raw-injection protocol (~/Desktop/.../tool/DroneBridge,
Apache-2.0 core; common/db_protocol.h) is a distinctive, associationless,
raw-802.11-injection long-range link -- a real signature worth detecting.
Per this project's "cite constants as reference, don't copy source" pattern
(same as ExpressLRS/Betaflight timing constants elsewhere this session),
only the numeric constants/field offsets below are reproduced, not any
DroneBridge source code.

ARCHITECTURAL QUESTION THIS SECTION ANSWERS: does this bridge's existing
Kismet REST polling (fetch_kismet_devices(), /devices/all_devices.json /
/devices/last-time/.../devices.json, per devicetracker.cc:335,:378) expose
raw 802.11 frame payload bytes, or only per-device metadata?

VERIFIED ANSWER (by reading the real Kismet source checkout, not assumed):
NO -- devicetracker.cc's registered per-device fields (kismet.device.base.*,
see the field-name table above) are device-level metadata (MAC, phy, name,
type, manuf, channel, frequency, last-seen signal) built up by Kismet's own
internal dissectors; the JSON device objects returned by
/devices/all_devices.json never contain a raw frame/payload byte array.
Kismet DOES expose raw frames, but through a COMPLETELY DIFFERENT endpoint
and wire format: ../kismet/phy_80211.cc:1016 registers
  GET /phy/phy80211/pcap/by-bssid/:mac/packets.pcapng
which streams a live pcapng capture (binary, not JSON) of packets for a
given BSSID, via pcapng_stream_packetchain (phy_80211.cc:1024-1038). That is
a fundamentally different integration (binary pcapng framing over a
streaming HTTP response, requiring a pcapng/link-layer parser) from this
script's simple JSON device-list polling, and per-BSSID (DroneBridge's
associationless injection has no real BSSID/association to key a stream
off of in the first place, which is itself part of the signature).

CONCLUSION: this bridge's OWN polling path (fetch_kismet_devices/to_detection
/poll_once) genuinely cannot see raw frame bytes today, so the signature
match below is NOT wired into poll_once()'s live loop -- doing so honestly
would require a separate pcapng-stream consumer, which is out of scope for
this task (tracked as a TODO below, not silently skipped). What IS added
here is the actual, testable signature-matching function
(detect_db_raw_v2_signature()) plus its detection-ingest builder
(build_wifibroadcast_detection()), operating on raw frame bytes IF/WHEN a
caller has them (e.g. a future pcapng-stream bridge, or a raw
monitor-mode-socket reader) -- kept in this file because the detection
DOMAIN (associationless raw-802.11-injection signatures) belongs with this
script's Kismet-side situational-awareness layer, not with hackrf_rx.py's
power-spectrum-level SDR sweep (hackrf_rx.py never sees individual 802.11
frames at all, only RF energy/occupancy).

TODO (not implemented, HONESTY not silent scope-narrowing): wiring this to
a REAL live feed needs one of (a) a pcapng-stream consumer against Kismet's
/phy/phy80211/pcap/by-bssid/:mac/packets.pcapng (requires a real BSSID to
key off -- awkward for an associationless protocol, may need a broader
"all packets" stream if Kismet exposes one), or (b) a raw AF_PACKET monitor-
mode socket read of this bridge's own, independent of Kismet entirely. Both
are HARDWARE-BLOCKED the same way the rest of this file is (task #70, no
monitor-mode NIC on primary) and are not attempted here.

SIGNATURE DEFINITION (verified directly against DroneBridge's real source,
common/db_protocol.h and common/db_raw_send_receive.c -- NOT copied, only
the constants/offsets below are reproduced):
  - db_raw_v2_header_t sits immediately after the radiotap header (whose
    length is a variable, per-driver value carried in the radiotap header's
    own bytes[2:4] -- see get_db_payload() in db_raw_receive.c) and is
    exactly DB_RAW_V2_HEADER_LENGTH=10 bytes:
      [0:4]  fcf_duration   -- a fake 802.11 Frame-Control+Duration prefix.
                               DroneBridge only ever sends two values here
                               (db_raw_send_receive.c's frame_control_pre_*
                               tables): 0x08,0x00,0x00,0x00 for DATA frames,
                               or 0xB4,0x00,0x00,0x00 for RTS frames.
      [4]    direction      -- DB_DIREC_DRONE=0x01 or DB_DIREC_GROUND=0x03
      [5]    comm_id        -- arbitrary per-link ID, any byte value
      [6]    port           -- DB_PORT_* enum, valid range 0x01-0x07
      [7:9]  payload_length -- little-endian uint16 (receive_buffer[off+7] |
                               receive_buffer[off+8]<<8, per get_db_payload())
      [9]    seq_num        -- rolling sequence number, any byte value
  - IMPORTANT CORRECTION vs this task's original brief: the brief described
    an "ETHER_TYPE 0x88ab carried inside the fake 802.11 data frame
    payload". Reading db_raw_send_receive.c/db_raw_receive.c directly shows
    this is NOT accurate for DroneBridge's primary monitor-mode raw-v2
    protocol: ETHER_TYPE (0x88ab, db_protocol.h:52) is used ONLY as the
    sll_protocol value when bind()-ing an AF_PACKET socket in DroneBridge's
    separate, legacy "wifi" mode (db_raw_receive.c:110, `if (the_mode ==
    'w') sll.sll_protocol = htons(ETHER_TYPE);` vs `:112` which uses
    ETH_P_802_2 for monitor mode) -- it is an OS socket-filter value, not a
    literal byte sequence appearing at a fixed offset in the over-the-air
    monitor-mode frame. The literal, verifiable, over-the-air signature for
    the actual raw-v2 protocol is the db_raw_v2_header_t fixed-offset field
    pattern above (fcf_duration + direction + port range), which is what
    detect_db_raw_v2_signature() below actually checks -- flagging this
    discrepancy explicitly rather than silently matching bytes that would
    never really appear on the wire.
  - Associationless: DroneBridge's raw-v2 protocol is injected directly via
    a monitor-mode raw socket, with no 802.11 association/4-way-handshake
    ever performed (db_raw_send_receive.c's open_db_socket() only ever
    configures a monitor-mode AF_PACKET socket, never an association state
    machine) -- a caller with 802.11 state tracking (e.g. a future Kismet
    pcapng consumer, which WOULD have per-BSSID association state) can pass
    associationless=True/False/None; this function does not attempt to
    derive it itself since it has no frame-sequence/state context.

CONFIDENCE_TYPE CHOSEN: "heuristic_binary" (the same enum value used for
this file's OUI match above) when the full db_raw_v2_header_t field pattern
matches AND associationless is True or unknown (None) -- this is a genuine,
deterministic structural match against a real, documented protocol layout
(fcf_duration in {DATA, RTS} AND direction in {DRONE, GROUND} AND port in
1-7), not a probabilistic guess, so it earns the stronger existing category
per this project's confidence-model conventions (CONFIDENCE_MODEL.md). It
is explicitly NOT "protocol_verified" (no CRC/checksum exists in this header
to cryptographically confirm it, unlike CRSF/DroneID's CRC-gated frames) --
a coincidental byte pattern from unrelated traffic, though unlikely given
the port-range + direction-enum constraints, cannot be fully ruled out.
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
# Task #105: DroneBridge WifiBroadcast-style raw-v2 protocol signature.
# See the module docstring's "TASK #105" section above for full sourcing,
# the ETHER_TYPE-usage correction, and why this is NOT wired into
# poll_once()'s live path (Kismet's JSON device API has no raw frame bytes).
# Constants below are reproduced from DroneBridge's common/db_protocol.h /
# common/db_raw_send_receive.c (Apache-2.0) -- citing the numeric layout
# only, not copying source, per this project's standing citation pattern.
# ---------------------------------------------------------------------------
DB_RAW_V2_HEADER_LENGTH = 10  # db_protocol.h: DB_RAW_V2_HEADER_LENGTH
DB_FCF_DURATION_DATA = bytes([0x08, 0x00, 0x00, 0x00])  # frame_control_pre_data
DB_FCF_DURATION_RTS = bytes([0xB4, 0x00, 0x00, 0x00])   # frame_control_pre_rts
DB_DIREC_DRONE = 0x01
DB_DIREC_GROUND = 0x03
DB_VALID_DIRECTIONS = frozenset({DB_DIREC_DRONE, DB_DIREC_GROUND})
DB_PORT_MIN = 0x01  # DB_PORT_CONTROLLER
DB_PORT_MAX = 0x07  # DB_PORT_RC
DB_WIFIBROADCAST_ETHER_TYPE = 0x88AB  # db_protocol.h ETHER_TYPE -- see module
# docstring correction: only used as an AF_PACKET sll_protocol value in
# DroneBridge's legacy "wifi" bind mode, NOT a literal over-the-air byte
# sequence for the raw-v2 monitor-mode protocol this function matches.


def detect_db_raw_v2_signature(frame_bytes: bytes, radiotap_length: int,
                                associationless: Optional[bool] = None) -> Optional[Dict]:
    """Check whether bytes immediately following a radiotap header match
    DroneBridge's db_raw_v2_header_t fixed-offset layout (see module
    docstring's SIGNATURE DEFINITION for full sourcing).

    Args:
      frame_bytes: the full raw 802.11 frame, INCLUDING its leading radiotap
        header (this function does not fetch/parse radiotap itself -- a
        caller with real Kismet pcapng frames, or a raw monitor-mode socket,
        is expected to hand over radiotap_length already known/parsed).
      radiotap_length: length in bytes of the radiotap header prefix.
      associationless: True/False if the caller has 802.11 association
        state for this transmitter, None if unknown/not tracked. This
        function has no state-machine context of its own so it never
        derives this itself -- it only uses it as an input for the
        confidence_type decision in build_wifibroadcast_detection().

    Returns None if the byte pattern does not match. Returns a dict of the
    matched fields (never raises) if it does -- this is a pure, offline-
    testable function with no I/O.
    """
    header_start = radiotap_length
    header_end = header_start + DB_RAW_V2_HEADER_LENGTH
    if radiotap_length < 0 or len(frame_bytes) < header_end:
        return None

    fcf_duration = bytes(frame_bytes[header_start:header_start + 4])
    if fcf_duration == DB_FCF_DURATION_DATA:
        frame_kind = "data"
    elif fcf_duration == DB_FCF_DURATION_RTS:
        frame_kind = "rts"
    else:
        return None  # not one of DroneBridge's two documented fcf_duration values

    direction = frame_bytes[header_start + 4]
    if direction not in DB_VALID_DIRECTIONS:
        return None

    port = frame_bytes[header_start + 6]
    if not (DB_PORT_MIN <= port <= DB_PORT_MAX):
        return None

    payload_length = frame_bytes[header_start + 7] | (frame_bytes[header_start + 8] << 8)
    seq_num = frame_bytes[header_start + 9]
    comm_id = frame_bytes[header_start + 5]

    return {
        "frame_kind": frame_kind,
        "direction": "drone" if direction == DB_DIREC_DRONE else "ground",
        "comm_id": comm_id,
        "port": port,
        "payload_length": payload_length,
        "seq_num": seq_num,
        "associationless": associationless,
    }


def build_wifibroadcast_detection(match: Dict, mac: Optional[str] = None) -> Dict:
    """Translate a detect_db_raw_v2_signature() match into this project's
    /api/detections/ingest body. See module docstring's CONFIDENCE_TYPE
    CHOSEN section for the heuristic_binary-vs-advisory_only reasoning.

    confidence_type is "heuristic_binary" when associationless is True or
    None (unknown -- a structural field-pattern match on its own is already
    a genuine, deterministic fact about the bytes, per the same reasoning
    this file already applies to OUI matches), and "advisory_only" only if
    the caller has POSITIVELY confirmed a real 802.11 association exists
    for this transmitter (associationless is False) -- which would
    contradict DroneBridge's associationless design and make this a much
    weaker, likely-coincidental match.
    """
    is_strong_match = match.get("associationless") is not False
    callsign_suffix = mac or f"comm{match['comm_id']}-{match['direction']}"
    return {
        "callsign": f"WIFIBROADCAST-{callsign_suffix}",
        "model": f"Likely WifiBroadcast-style long-range link "
                 f"(DroneBridge raw-v2 {match['frame_kind']} frame, "
                 f"direction={match['direction']}, port={match['port']})",
        "protocol": "IEEE802.11",
        "threat_level": "MEDIUM",
        "center_freq_ghz": 2.437,  # not measured by this function -- see caller for real value
        "bandwidth_mhz": 20.0,
        "rssi_dbm": -90.0,  # not measured here -- caller should override with real radiotap RSSI
        "snr_db": 0.0,
        "encrypted": False,
        "source": "KISMET",
        "protocol_confirmed": False,  # a byte-pattern structural match, not a CRC-verified decode
        "confidence_type": "heuristic_binary" if is_strong_match else "advisory_only",
    }


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
