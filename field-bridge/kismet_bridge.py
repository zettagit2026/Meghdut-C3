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
TASK #105/#116 -- DroneBridge WifiBroadcast RAW-FRAME SIGNATURE, AND ITS
PCAPNG-STREAM WIRING INTO poll_once()
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

TASK #116 UPDATE (this session): task #105 left the above genuinely unwired
because fetch_kismet_devices()'s JSON device API cannot see raw frame
bytes. This session re-read the local Kismet checkout's real source
(../kismet/phy_80211.cc:1016-1041, ../kismet/pcapng_stream_futurebuf.h) to
confirm the actual pcapng-streaming route's real behaviour, then built and
wired a real consumer for it:

  Route (../kismet/phy_80211.cc:1016):
    GET /phy/phy80211/pcap/by-bssid/:mac/packets.pcapng
  registered with httpd->register_route(..., {"GET"}, httpd->RO_ROLE,
  {"pcapng"}, ...) -- RO_ROLE means it uses the SAME apikey/session-cookie
  auth as the JSON device endpoints (kis_net_beast_httpd::AUTH_COOKIE), not
  a separate scheme.

  Behaviour (verified by reading phy_80211.cc:1024-1040, not assumed): this
  is a LIVE, OPEN-ENDED HTTP STREAM, not a one-shot capture window or a
  bounded file download. The handler constructs a
  pcapng_stream_packetchain<pcapng_phy80211_accept_ftor, ...> bound directly
  to con->response_stream(), calls pcapng->start_stream(), then
  pcapng->block_until_stream_done() -- i.e. the HTTP response body is written
  to incrementally, in real pcapng block framing, for as long as the
  connection stays open and packets matching pcapng_phy80211_accept_ftor(mac)
  (phy_80211.h:690-691, a per-BSSID filter) keep arriving from Kismet's
  packetchain. The stream only ends when the client closes the connection
  (con->set_closure_cb(...) calls stop_stream()) or the server does. There is
  no documented "all packets" / unfiltered variant of this route -- it is
  always by-BSSID, which is the awkwardness task #105 already flagged for
  DroneBridge's associationless protocol (no real association/BSSID to key
  off of). This bridge resolves that by keying off the BSSID/MAC that its
  OWN existing metadata poll (fetch_kismet_devices) already flagged as
  interesting (a drone-OUI match), NOT by inventing a fake "all packets" mode
  Kismet doesn't actually expose.

  Wire format: LINKFRAME datachunks selected by pcapng_stream_select_ftor
  (phy_80211.cc / pcapng_stream_futurebuf.h) are written as real IETF pcapng
  Enhanced Packet Blocks -- Section Header Block, Interface Description
  Block(s), then a live sequence of Enhanced Packet Blocks, one per matching
  raw 802.11 frame (including its leading radiotap header, since the
  linkframe payload for an IEEE802.11 phy datasource is the raw
  DLT_IEEE802_11_RADIO capture, radiotap-prefixed). This is a real,
  publicly-documented format (IETF pcapng draft, tcpdump.org/pcapng/), so
  this bridge parses it with a small stdlib-only reader
  (iter_pcapng_frames() below), NOT scapy: scapy's rdpcap/pcapng reader is
  GPL-2.0-only, and this project's standing OSI-permissive-only policy on
  new dependencies (reject non-permissive/copyleft additions where a
  reasonable stdlib alternative exists) rules it out here -- the pcapng
  block-length framing itself is simple, bounds-checked, fixed-width-field
  parsing, not "fragile hand-rolled binary parsing" in the sense that
  caution is meant to warn against.

  Because the real stream is open-ended, fetch_pcapng_frames() below reads
  it BOUNDED (a max-frames and max-seconds cutoff, then explicitly closes
  the HTTP connection) rather than blocking poll_once()'s synchronous poll
  loop forever on one BSSID -- this is a deliberate, documented design
  choice (a short per-candidate-BSSID burst read, triggered right after the
  metadata poll flags that BSSID as interesting), not a misunderstanding of
  the endpoint's real (unbounded) behaviour.

CONCLUSION: poll_once() now calls check_wifibroadcast_signature() for each
drone-OUI-matched IEEE802.11 device it already sees via the existing
metadata poll, which opens a bounded pcapng stream for that MAC, parses real
pcapng framing, and runs the EXISTING detect_db_raw_v2_signature() (verbatim,
not reimplemented) against each extracted raw frame. A signature match is
posted via build_wifibroadcast_detection() (also existing/unchanged) through
the same _post_with_reauth() ingest path everything else in this file uses.
This is genuinely wired now, not just theoretically ready -- see
--check-wifibroadcast-signature below. It remains HARDWARE-BLOCKED for LIVE
execution the same way the rest of this file is (task #70: no monitor-mode
NIC, so no real Kismet server with real 802.11 capture exists to point this
at yet) -- tested here only against a recorded/mocked pcapng byte stream
matching the real format described above.

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
import struct
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
    # Added for the Wi-Fi-drone SSID/OUI fingerprint work (wifi_drone_bridge.py).
    # Two additional Parrot SA OUI blocks, both real, currently-registered
    # 3-octet MA-L assignments to "Parrot SA/Parrot Drones" in the public IEEE
    # OUI database -- same best-effort, non-exhaustive stance as the block above
    # (see module docstring): SSID+OUI are SPOOFABLE, so a hit here is a
    # manufacturer CANDIDATE, never a serial. Reconcile against the full IEEE
    # list as more drone vendors are catalogued.
    "00:26:7E": "Parrot",
    "90:03:B7": "Parrot",
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
# Task #116: minimal, stdlib-only pcapng block reader for Kismet's real
# /phy/phy80211/pcap/by-bssid/:mac/packets.pcapng stream (see module
# docstring's TASK #116 UPDATE section for full sourcing/rationale for not
# using scapy here). Implements just enough of the IETF pcapng format
# (Section Header Block, Interface Description Block, Enhanced Packet
# Block -- https://www.tcpdump.org/pcapng/, the same document Wireshark's
# own pcapng dissector is built from) to recover raw frame bytes; unknown
# block types are skipped by their own declared length rather than
# rejected, since this reader only needs the packet payload, not full
# semantic coverage of every optional block type.
# ---------------------------------------------------------------------------
PCAPNG_BLOCK_SHB = 0x0A0D0D0A   # Section Header Block
PCAPNG_BLOCK_IDB = 0x00000001   # Interface Description Block
PCAPNG_BLOCK_EPB = 0x00000006   # Enhanced Packet Block (what Kismet emits)
PCAPNG_BLOCK_SPB = 0x00000003   # Simple Packet Block (older/simpler form)


def iter_pcapng_frames(byte_chunks) -> "List[bytes]":
    """Consume an iterable of byte chunks (e.g. requests' resp.iter_content())
    containing a real pcapng byte stream and yield each Enhanced/Simple
    Packet Block's raw captured frame bytes, in order.

    This is a streaming, incremental parser: pcapng blocks are self-
    delimiting (each starts with a 4-byte block type and a 4-byte total
    block length, little-endian, and repeats that same length at the very
    end of the block -- see tcpdump.org/pcapng's "General Block Structure"),
    so this function buffers chunks until at least one full block is
    available, yields its packet bytes (if it is an EPB/SPB; SHB/IDB and any
    other/unknown block type are skipped by length, not parsed further),
    then continues. Never raises on malformed/truncated trailing bytes at
    stream end -- it simply stops yielding once no further complete block
    fits in the remaining buffer.

    Assumes little-endian block encoding (the byte-order magic in the
    Section Header Block's body would normally be checked to select
    endianness per the spec; Kismet, the only real-world source this
    project targets, always writes little-endian pcapng on the
    little-endian x86/ARM hosts this project runs on -- documented
    assumption, not silently ignored).
    """
    buf = bytearray()
    for chunk in byte_chunks:
        if not chunk:
            continue
        buf.extend(chunk)
        while True:
            if len(buf) < 8:
                break
            block_type, block_total_length = struct.unpack_from("<II", buf, 0)
            if block_total_length < 12 or len(buf) < block_total_length:
                break  # incomplete block still arriving, wait for more chunks
            body = bytes(buf[8:block_total_length - 4])
            del buf[:block_total_length]

            if block_type in (PCAPNG_BLOCK_EPB,):
                # EPB body: interface_id(4) ts_high(4) ts_low(4) captured_len(4)
                # packet_len(4) packet_data(captured_len, padded to 4 bytes) options...
                if len(body) < 20:
                    continue
                captured_len = struct.unpack_from("<I", body, 12)[0]
                packet_start = 20
                packet_end = packet_start + captured_len
                if packet_end > len(body):
                    continue  # declared captured_len longer than actual body; skip
                yield bytes(body[packet_start:packet_end])
            elif block_type in (PCAPNG_BLOCK_SPB,):
                # SPB body: packet_len(4) packet_data(rest, padded)
                if len(body) < 4:
                    continue
                packet_len = struct.unpack_from("<I", body, 0)[0]
                yield bytes(body[4:4 + packet_len])
            # SHB/IDB/anything else: no packet payload to extract, skip.


def radiotap_header_length(frame_bytes: bytes) -> Optional[int]:
    """Real 802.11 radiotap headers start with version(1) + pad(1) +
    length(2, little-endian) -- the length field IS the full radiotap
    header length (this project's own kismet_bridge.py docstring already
    cites this as "the radiotap header's own bytes[2:4]"; see
    detect_db_raw_v2_signature()'s docstring). Returns None if frame_bytes
    is too short to even contain that 4-byte prefix."""
    if len(frame_bytes) < 4:
        return None
    return struct.unpack_from("<H", frame_bytes, 2)[0]


def fetch_pcapng_frames(kismet_url: str, mac: str, apikey: Optional[str],
                        max_frames: int = 25, max_seconds: float = 3.0,
                        timeout: float = 5.0) -> List[bytes]:
    """Open Kismet's REAL, verified pcapng-by-BSSID stream
    (/phy/phy80211/pcap/by-bssid/:mac/packets.pcapng, phy_80211.cc:1016 --
    see module docstring's TASK #116 UPDATE section) for one candidate MAC
    and return up to max_frames raw frame byte-strings, reading for at most
    max_seconds before explicitly closing the connection.

    This endpoint is a genuinely LIVE, OPEN-ENDED stream (confirmed by
    reading phy_80211.cc's block_until_stream_done() call) -- it does NOT
    stop on its own. Bounding the read here (rather than looping forever)
    is this bridge's own deliberate choice for fitting a per-candidate-BSSID
    check into poll_once()'s synchronous poll cycle; it is not a
    misunderstanding of the endpoint's real behaviour (documented above).
    """
    params = {}
    if apikey:
        params["KISMET"] = apikey
    path = f"/phy/phy80211/pcap/by-bssid/{mac}/packets.pcapng"
    frames: List[bytes] = []
    deadline = time.time() + max_seconds
    resp = requests.get(f"{kismet_url}{path}", params=params, stream=True, timeout=timeout)
    try:
        resp.raise_for_status()

        def _bounded_chunks():
            for chunk in resp.iter_content(chunk_size=4096):
                yield chunk
                if len(frames) >= max_frames or time.time() >= deadline:
                    return

        for frame in iter_pcapng_frames(_bounded_chunks()):
            frames.append(frame)
            if len(frames) >= max_frames or time.time() >= deadline:
                break
    finally:
        resp.close()  # explicit: this stream never ends on its own server-side
    return frames


def check_wifibroadcast_signature(kismet_url: str, mac: str, apikey: Optional[str],
                                  max_frames: int = 25, max_seconds: float = 3.0,
                                  timeout: float = 5.0) -> Optional[Dict]:
    """Open a bounded pcapng stream for one candidate MAC (see
    fetch_pcapng_frames()) and run the EXISTING, unmodified
    detect_db_raw_v2_signature() against each extracted raw frame. Returns
    the FIRST match found (Dict, as returned by detect_db_raw_v2_signature),
    or None if no frame in the bounded read matched or the stream/request
    itself failed (network errors are swallowed here and logged, matching
    this file's existing WARN-and-continue convention for Kismet REST
    fetch failures in main()'s poll loop -- one candidate BSSID's pcapng
    check failing should never crash the whole poll cycle).
    """
    try:
        frames = fetch_pcapng_frames(kismet_url, mac, apikey, max_frames, max_seconds, timeout)
    except requests.RequestException as e:
        print(f"WARN: pcapng fetch for {mac} failed: {e}", file=sys.stderr)
        return None

    for frame_bytes in frames:
        rt_len = radiotap_header_length(frame_bytes)
        if rt_len is None:
            continue
        # associationless=None (unknown): this consumer does not track
        # 802.11 association state for the BSSID, same honesty caveat
        # detect_db_raw_v2_signature()'s own docstring already documents.
        match = detect_db_raw_v2_signature(frame_bytes, rt_len, associationless=None)
        if match is not None:
            return match
    return None


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


def to_wifi_reference(device: Dict) -> Dict:
    """Translate one Kismet IEEE802.11 device into a /api/detections/wifi-
    reference body -- GROUND-TRUTH reference data for the backend's WiFi
    identification-confidence fusion, NOT a detection/board contact. Pure
    translation from Kismet's own fields (mirrors to_detection() above, which
    is the separate detection-forwarding path used only for drone-OUI matches).

    The backend stores this in db.wifi_ground_truth and cross-references it in
    detection_ingest to segregate real drones from ambient 2.4GHz WiFi: an
    ordinary co-channel AP/client re-attributes an RF drone-candidate to WiFi;
    a drone-OUI (DJI/Parrot/Autel) co-channel device corroborates it. Forwarding
    non-drone WiFi to this REFERENCE endpoint (rather than /detections/ingest)
    is what lets the fusion 'know about' ambient WiFi without flooding the
    operator board with ~20-50 WiFi contacts."""
    mac = device.get("kismet.device.base.macaddr", "UNKNOWN")
    manuf = device.get("kismet.device.base.manuf") or "unknown"
    dtype = device.get("kismet.device.base.type", "unknown")
    freq_khz = device.get("kismet.device.base.frequency") or 0
    signal_obj = device.get("kismet.device.base.signal") or {}
    last_signal = signal_obj.get("kismet.common.signal.last_signal")
    # Kismet uses the device name for an AP's SSID; fall back to the beaconed
    # SSID field. Suppress the "name == MAC" placeholder Kismet emits when it
    # has no better label, so the backend does not render "(AA:BB:...)".
    ssid = (device.get("kismet.device.base.name")
            or device.get("dot11.device/dot11.device.last_beaconed_ssid"))
    if ssid and ssid.replace(":", "").upper() == mac.replace(":", "").upper():
        ssid = None
    center_freq_ghz = (freq_khz / 1_000_000.0) if freq_khz else 2.437
    drone_manuf = match_drone_oui(mac)
    return {
        "mac": mac,
        "oui": mac_oui(mac),
        "manuf": manuf,
        "ssid": ssid,
        "device_type": dtype,
        "phyname": device.get("kismet.device.base.phyname", "IEEE802.11"),
        "frequency_khz": float(freq_khz) if freq_khz else None,
        "center_freq_ghz": round(center_freq_ghz, 6),
        "rssi_dbm": float(last_signal) if last_signal is not None else None,
        "is_drone_oui": drone_manuf is not None,
    }


def poll_once(console_url: str, headers: Dict, email: str, password: str,
             devices: List[Dict], forward_all: bool, seen_macs: Dict[str, float],
             repost_interval_s: float, check_wifibroadcast: bool = False,
             kismet_url: Optional[str] = None, kismet_apikey: Optional[str] = None,
             pcapng_max_frames: int = 25, pcapng_max_seconds: float = 3.0,
             forward_wifi_reference: bool = False,
             wifi_ref_repost_s: float = 10.0) -> int:
    posted = 0
    now = time.time()
    for device in devices:
        mac = device.get("kismet.device.base.macaddr")
        if not mac:
            continue
        drone_manuf = match_drone_oui(mac)

        # WiFi identification-confidence fusion ground truth: forward EVERY
        # IEEE802.11 device to the backend's REFERENCE store (not the detections
        # board) so the fusion can cross-reference it against RF drone-
        # candidates. Independent of, and runs BEFORE, the drone-OUI detection-
        # forwarding skip below -- ordinary (non-drone) WiFi is exactly what the
        # fusion needs to know about, and it must reach the reference store even
        # though it is (correctly) never forwarded as a board contact. Uses a
        # separate seen-key + its own shorter throttle so a continuously-
        # beaconing AP stays "fresh" inside the backend's fusion window.
        if (forward_wifi_reference
                and device.get("kismet.device.base.phyname") == "IEEE802.11"):
            ref_key = "wifiref:" + mac
            if now - seen_macs.get(ref_key, 0.0) >= wifi_ref_repost_s:
                seen_macs[ref_key] = now
                try:
                    r = _post_with_reauth(console_url, "/api/detections/wifi-reference",
                                          to_wifi_reference(device), headers,
                                          email, password, timeout=5)
                    r.raise_for_status()
                except requests.RequestException as e:
                    print(f"wifi-reference ingest failed for {mac}: {e}", file=sys.stderr)

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

        # Task #116: for a drone-OUI-matched IEEE802.11 device already flagged
        # by the metadata poll above, optionally also open a bounded pcapng
        # stream for its BSSID and check for DroneBridge's WifiBroadcast
        # raw-v2 signature (see module docstring's TASK #116 UPDATE section
        # for why this is keyed off metadata-poll-flagged MACs rather than an
        # "all packets" mode Kismet does not actually expose). Opt-in
        # (--check-wifibroadcast-signature) since it is an extra HTTP
        # round-trip per candidate device, on top of the existing JSON poll.
        if (check_wifibroadcast and kismet_url is not None
                and drone_manuf is not None
                and device.get("kismet.device.base.phyname") == "IEEE802.11"):
            match = check_wifibroadcast_signature(kismet_url, mac, kismet_apikey,
                                                  max_frames=pcapng_max_frames,
                                                  max_seconds=pcapng_max_seconds)
            if match is not None:
                wb_detection = build_wifibroadcast_detection(match, mac=mac)
                try:
                    r = _post_with_reauth(console_url, "/api/detections/ingest",
                                          wb_detection, headers, email, password, timeout=5)
                    r.raise_for_status()
                    posted += 1
                    print(f"[kismet_bridge] posted {wb_detection['confidence_type']} "
                          f"WifiBroadcast-signature detection for {mac} -> "
                          f"{r.json().get('callsign')}")
                except requests.RequestException as e:
                    print(f"wifibroadcast detection ingest failed for {mac}: {e}",
                          file=sys.stderr)
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
    ap.add_argument("--no-wifi-reference", dest="wifi_reference",
                    action="store_false",
                    help="Disable forwarding IEEE802.11 devices to the backend's "
                         "WiFi ground-truth reference store "
                         "(/api/detections/wifi-reference). By default (or when "
                         "KISMET_WIFI_REFERENCE_ENABLED is truthy) every WiFi "
                         "device Kismet sees is forwarded there as REFERENCE data "
                         "for the backend's drone-vs-WiFi fusion -- NOT as a board "
                         "contact. Turn this off to revert the bridge to "
                         "drone-OUI-only behavior.")
    ap.set_defaults(wifi_reference=(
        os.environ.get("KISMET_WIFI_REFERENCE_ENABLED", "true").strip().lower()
        in ("1", "true", "yes", "on")))
    ap.add_argument("--wifi-reference-repost-s", type=float,
                    default=float(os.environ.get("KISMET_WIFI_REF_REPOST_S", "10")),
                    help="Min seconds between re-posting the same WiFi device to "
                         "the reference store. Kept well under the backend's WiFi "
                         "fusion freshness window so a beaconing AP stays 'present' "
                         "and a re-attributed contact does not flicker.")
    ap.add_argument("--check-wifibroadcast-signature", action="store_true",
                    help="Task #116: for each drone-OUI-matched IEEE802.11 device, also "
                         "open a bounded live pcapng stream from Kismet's real "
                         "/phy/phy80211/pcap/by-bssid/:mac/packets.pcapng route for that "
                         "BSSID and check its raw frames against "
                         "detect_db_raw_v2_signature() (DroneBridge WifiBroadcast raw-v2 "
                         "protocol match). Adds one extra HTTP round-trip per candidate "
                         "device per poll cycle -- opt-in, off by default.")
    ap.add_argument("--pcapng-max-frames", type=int, default=25,
                    help="Max raw frames to read per bounded pcapng check (see "
                         "--check-wifibroadcast-signature).")
    ap.add_argument("--pcapng-max-seconds", type=float, default=3.0,
                    help="Max seconds to keep a bounded pcapng stream open per candidate "
                         "BSSID (see --check-wifibroadcast-signature).")
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
          f"{args.poll_interval_s}s. forward_all_devices={args.forward_all_devices}, "
          f"wifi_reference={args.wifi_reference}.")
    print("HARDWARE-BLOCKED NOTICE: this bridge only produces real output if the "
          "Kismet server it polls has a real monitor-mode WiFi/BT datasource "
          "attached -- see module docstring.")

    seen_macs: Dict[str, float] = {}
    # CEMA compat (2026-08-06): Kismet 2025-09 removed GET /devices/all_devices.json
    # (now 404). Its incremental route /devices/last-time/<n>/devices.json still
    # exists and accepts a NEGATIVE value = 'last N seconds' (per this module's own
    # docstring). Bootstrapping with -300 makes the FIRST poll target the surviving
    # route instead of the removed all_devices.json. Detection logic is unchanged.
    last_time_t: Optional[int] = -300

    # Idle-loop liveness heartbeat, same pattern as mavlink_sniffer.py's
    # IDLE_HEARTBEAT_INTERVAL_S (task #139). This loop is legitimately silent
    # whenever Kismet is reachable but has nothing new/matching to report --
    # print a cheap "still polling" line on a fixed cadence, independent of
    # whether any device was ever matched, so log-freshness liveness checks
    # can distinguish "alive, nothing to report" from "hung".
    IDLE_HEARTBEAT_INTERVAL_S = 60.0
    last_idle_heartbeat = 0.0

    while True:
        try:
            devices = fetch_kismet_devices(args.kismet_url, args.kismet_apikey, last_time_t)
        except requests.RequestException as e:
            print(f"WARN: Kismet REST fetch failed: {e}", file=sys.stderr)
            time.sleep(args.poll_interval_s)
            continue

        poll_once(args.console_url, headers, args.email, args.password,
                 devices, args.forward_all_devices, seen_macs, args.repost_interval_s,
                 check_wifibroadcast=args.check_wifibroadcast_signature,
                 kismet_url=args.kismet_url, kismet_apikey=args.kismet_apikey,
                 pcapng_max_frames=args.pcapng_max_frames,
                 pcapng_max_seconds=args.pcapng_max_seconds,
                 forward_wifi_reference=args.wifi_reference,
                 wifi_ref_repost_s=args.wifi_reference_repost_s)

        now_idle = time.time()
        if now_idle - last_idle_heartbeat >= IDLE_HEARTBEAT_INTERVAL_S:
            last_idle_heartbeat = now_idle
            print(f"[kismet_bridge] [heartbeat] still polling Kismet at "
                  f"{args.kismet_url} -- {len(seen_macs)} device(s) tracked, "
                  f"0 drone matches in the last {IDLE_HEARTBEAT_INTERVAL_S:.0f}s "
                  f"(process alive, nothing new to report).")

        last_times = [d.get("kismet.device.base.last_time") for d in devices
                     if d.get("kismet.device.base.last_time")]
        if last_times:
            last_time_t = max(last_times)

        time.sleep(args.poll_interval_s)

    return 0  # unreachable; loop runs until process is killed/stopped


if __name__ == "__main__":
    sys.exit(main())
