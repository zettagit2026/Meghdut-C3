#!/usr/bin/env python3
"""Read-only Kismet Wi-Fi AP survey -> RF situational-awareness shape.

=============================================================================
WHAT THIS IS, AND WHAT IT IS NOT
=============================================================================
This module backs GET /api/wifi-environment (see backend/server.py). It is a
STRICTLY READ-ONLY, PASSIVE window onto a running Kismet server's own device
list -- so an operator can SEE the ambient Wi-Fi picture (every AP/device in
range). It is a *visibility* layer, completely separate from targeting:

  - It creates NO detections, writes NOTHING to the database, and has NO
    side effects.
  - It does NOT make any AP engageable and NEVER touches the target/engage
    lists, the wifi-defeat fire path, arm/IFF/range-auth, or any TX/safety-
    spine code. Nothing in this file can transmit or authorize a transmit.
  - It only ever READS from the Kismet REST API. The one request it makes is
    a field-projection query for the device list (Kismet's device-list route
    accepts a POST carrying a read-only "fields" projection dictionary -- the
    same convention Kismet's own web UI uses to keep responses small); it
    fetches devices and never writes to or mutates any Kismet state.

The real, full Kismet server (https://www.kismetwireless.net, GPL-2.0) does
the actual passive 802.11 capture, frame parsing, OUI/vendor lookup and
device fingerprinting. This module does none of that -- it polls a RUNNING
Kismet server's own JSON REST API for the devices it already found and
reshapes them into a compact AP-survey row for the console. The Kismet REST
surface and per-device field names below are the SAME ones documented (and
verified against the local Kismet source checkout) in
field-bridge/kismet_bridge.py -- see that file's module docstring for the
full source citations. This module deliberately reuses that same access
pattern rather than inventing a new one.

=============================================================================
KISMET FIELDS USED (verified against ../kismet source, see kismet_bridge.py)
=============================================================================
Base (per device):
  kismet.device.base.macaddr    -- MAC / BSSID
  kismet.device.base.phyname    -- "IEEE802.11" / "Bluetooth" / ...
  kismet.device.base.name       -- printable name (often SSID for an AP)
  kismet.device.base.type       -- printable device type ("Wi-Fi AP", ...)
  kismet.device.base.manuf      -- Kismet's own OUI-derived vendor string
  kismet.device.base.channel    -- channel (phy-specific)
  kismet.device.base.frequency  -- frequency in kHz (802.11) or 0
  kismet.device.base.first_time -- first-seen unix time_t
  kismet.device.base.last_time  -- last-seen unix time_t
  kismet.device.base.signal     -- nested; .kismet.common.signal.last_signal (dBm)

Dot11 (802.11 AP enrichment; verified in ../kismet/phy_80211_components.cc):
  dot11.device                          -- nested per-device 802.11 record
    dot11.device.num_associated_clients -- associated-client count
    dot11.device.last_beaconed_ssid_record -- nested advertised-SSID record:
      dot11.advertisedssid.ssid            -- beaconed SSID string
      dot11.advertisedssid.crypt_string    -- printable encryption info
      dot11.advertisedssid.wpa_mfp_required  -- PMF/802.11w required
      dot11.advertisedssid.wpa_mfp_supported -- PMF/802.11w supported

Every dot11 field is OPTIONAL here: Bluetooth devices, clients, and Kismet
field-view responses that omit them all degrade gracefully to None/unknown
rather than raising.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# Drone-vendor OUI + SSID hints -- for a NON-ACTIONABLE "possible UAS" visual
# tag ONLY. A hit here NEVER makes an AP engageable and NEVER creates a
# detection; real engagement stays entirely on the drone-contact target flow.
# This is a small, best-effort, non-exhaustive copy of the same public IEEE
# OUI assignments used by field-bridge/kismet_bridge.py's
# DRONE_MANUFACTURER_OUIS (kept self-contained here rather than importing
# across the backend/field-bridge boundary; reconcile with that list if it
# changes). SSID + OUI are SPOOFABLE, so a hit is a manufacturer CANDIDATE,
# never an identification.
# ---------------------------------------------------------------------------
DRONE_MANUFACTURER_OUIS: Dict[str, str] = {
    "60:60:1F": "DJI",
    "34:D2:62": "DJI",
    "A0:14:3D": "DJI",
    "48:1C:B9": "DJI",
    "90:3A:E6": "Parrot",
    "00:12:1C": "Parrot",
    "00:26:7E": "Parrot",
    "90:03:B7": "Parrot",
    "EC:5B:CD": "Autel",
}

# SSID substrings publicly known to be broadcast by consumer drones' own
# Wi-Fi APs. Case-insensitive substring match. Best-effort/non-exhaustive.
DRONE_SSID_PATTERNS = (
    "dji-",
    "mavic",
    "phantom",
    "spark",
    "tello",
    "parrot",
    "bebop",
    "anafi",
    "autel",
    "evo-",
    "skydio",
    "fimi",
    "hubsan",
)


def mac_oui(mac: str) -> str:
    return ":".join(str(mac).upper().split(":")[:3])


def match_drone_oui(mac: Optional[str]) -> Optional[str]:
    if not mac:
        return None
    return DRONE_MANUFACTURER_OUIS.get(mac_oui(mac))


def match_drone_ssid(ssid: Optional[str]) -> bool:
    if not ssid:
        return False
    low = ssid.lower()
    return any(p in low for p in DRONE_SSID_PATTERNS)


# ---------------------------------------------------------------------------
# The compact per-device field-view we ask Kismet to return. Requesting only
# these fields (via POST body {"fields": KISMET_SURVEY_FIELDS}) keeps the
# response small even when Kismet is tracking thousands of devices -- Kismet's
# own device JSON is otherwise very large per device.
#
# IMPORTANT (verified live against the field Kismet 2025-09 on meghdut-srv02):
# for a COMPOUND/nested field we must request the WHOLE parent tracked-component
# by its own top-level name (e.g. "kismet.device.base.signal", "dot11.device")
# and dig into the returned sub-object ourselves. The "parent/child" leaf-path
# projection syntax does NOT reliably return the leaf here -- Kismet flattens it
# to a bare child key whose value comes back None (so last_signal / the beaconed
# SSID record silently drop out, every AP gets RSSI=None, and the RSSI sort then
# ranks real APs below signal-0 noise and off the top-N). Requesting the parent
# object returns the full nested dict (signal last_signal in dBm, the dot11
# advertised-SSID record with crypt/PMF, associated-client count); parse_kismet_
# device already reads exactly that shape. The parent objects are small and the
# window is bounded to KISMET_SURVEY_WINDOW_S, so the response stays ~1s.
# ---------------------------------------------------------------------------
KISMET_SURVEY_FIELDS: List[Any] = [
    "kismet.device.base.macaddr",
    "kismet.device.base.phyname",
    "kismet.device.base.name",
    "kismet.device.base.type",
    "kismet.device.base.manuf",
    "kismet.device.base.channel",
    "kismet.device.base.frequency",
    "kismet.device.base.first_time",
    "kismet.device.base.last_time",
    # Full parent tracked-components (NOT "parent/child" leaf paths -- see note
    # above): the parser digs last_signal out of the signal object and the
    # associated-client count + last-beaconed-SSID record out of dot11.device.
    "kismet.device.base.signal",
    "dot11.device",
]

# How far back the survey looks, in seconds, expressed as Kismet's NEGATIVE
# relative "last-time" window. A SMALL window keeps the survey to what is
# CURRENTLY in range and keeps Kismet's response tiny even when it is tracking
# ~millions of historical devices (a 24h window against a busy Kismet times
# out). 10 minutes is recent enough to reflect the live RF picture.
KISMET_SURVEY_WINDOW_S = 600


def _get(device: Dict, *keys: str) -> Any:
    """Return the first present, non-None value for any of `keys` in `device`.

    Kismet returns nested objects for compound fields, but its simplefields/
    field-view responses can flatten a requested "parent/child" path into a
    single flat key whose name is the child path. This helper tolerates both:
    it checks each candidate key at the top level of `device`.
    """
    for k in keys:
        if k in device and device[k] is not None:
            return device[k]
    return None


def _nested(device: Dict, parent: str, child: str) -> Any:
    """Read `child` from a nested `parent` object if present, else try the
    flattened `child` key directly on `device`. Returns None if absent."""
    obj = device.get(parent)
    if isinstance(obj, dict) and obj.get(child) is not None:
        return obj[child]
    return device.get(child)


def _to_bool(val: Any) -> Optional[bool]:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on")
    return None


def parse_kismet_device(device: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape ONE Kismet device-JSON object into the compact survey row the
    /api/wifi-environment endpoint returns. Pure -- no I/O, never raises on a
    missing/oddly-shaped field (degrades to None/unknown).

    This is a read-only projection of Kismet's own fields plus a non-
    actionable "possible UAS" tag; it makes no threat/target claim.
    """
    mac = _get(device, "kismet.device.base.macaddr") or ""
    phy = _get(device, "kismet.device.base.phyname") or "unknown"
    base_name = _get(device, "kismet.device.base.name")
    dtype = _get(device, "kismet.device.base.type") or "unknown"
    vendor = _get(device, "kismet.device.base.manuf") or "unknown"
    channel = _get(device, "kismet.device.base.channel")
    freq_khz = _get(device, "kismet.device.base.frequency") or 0
    first_time = _get(device, "kismet.device.base.first_time")
    last_time = _get(device, "kismet.device.base.last_time")

    # Signal: nested object OR flattened "last_signal" key.
    signal_obj = _get(device, "kismet.device.base.signal")
    if isinstance(signal_obj, dict):
        rssi = signal_obj.get("kismet.common.signal.last_signal")
    else:
        rssi = _get(device, "kismet.common.signal.last_signal",
                    "kismet.device.base.signal/kismet.common.signal.last_signal")

    # 802.11 AP enrichment (all optional).
    dot11 = device.get("dot11.device")
    client_count = _nested(device, "dot11.device",
                           "dot11.device.num_associated_clients")
    ssid_record = _nested(device, "dot11.device",
                          "dot11.device.last_beaconed_ssid_record")

    ssid = None
    crypt_string = None
    pmf_required = None
    pmf_supported = None
    if isinstance(ssid_record, dict):
        ssid = ssid_record.get("dot11.advertisedssid.ssid")
        crypt_string = ssid_record.get("dot11.advertisedssid.crypt_string")
        pmf_required = _to_bool(ssid_record.get("dot11.advertisedssid.wpa_mfp_required"))
        pmf_supported = _to_bool(ssid_record.get("dot11.advertisedssid.wpa_mfp_supported"))

    # Fall back to the base device name as the SSID label for an AP, but
    # suppress Kismet's "name == MAC" placeholder so the UI never shows the
    # BSSID twice. (Same suppression kismet_bridge.to_wifi_reference does.)
    if not ssid and base_name:
        if str(base_name).replace(":", "").upper() != str(mac).replace(":", "").upper():
            ssid = base_name

    freq_ghz = round(freq_khz / 1_000_000.0, 6) if freq_khz else None

    drone_oui_vendor = match_drone_oui(mac)
    ssid_is_drone = match_drone_ssid(ssid)
    possible_uas = drone_oui_vendor is not None or ssid_is_drone

    return {
        "bssid": mac,
        "oui": mac_oui(mac) if mac else "",
        "phy": phy,
        "device_type": dtype,
        "ssid": ssid,
        "vendor": vendor,
        "channel": str(channel) if channel not in (None, "") else None,
        "frequency_khz": freq_khz or None,
        "frequency_ghz": freq_ghz,
        "rssi_dbm": float(rssi) if rssi is not None else None,
        "encryption": crypt_string,           # printable, from Kismet; None => unknown
        "pmf_required": pmf_required,          # 802.11w
        "pmf_supported": pmf_supported,
        "client_count": int(client_count) if client_count is not None else None,
        "first_seen": int(first_time) if first_time else None,
        "last_seen": int(last_time) if last_time else None,
        # NON-ACTIONABLE visual hint only. Never used for targeting/engagement.
        "possible_uas": possible_uas,
        "possible_uas_reason": (
            f"OUI {mac_oui(mac)} -> {drone_oui_vendor}" if drone_oui_vendor
            else ("SSID pattern" if ssid_is_drone else None)
        ),
    }


def build_survey(devices: List[Dict[str, Any]], top_n: int = 250,
                 wifi_only: bool = False) -> List[Dict[str, Any]]:
    """Reshape a list of Kismet devices into survey rows, sorted by RSSI
    (strongest first), capped to `top_n`. Pure -- no I/O.

    Devices with no RSSI sort last (they are the least useful for an RF
    picture) but are retained until the top_n cap. `wifi_only` filters to
    IEEE802.11 devices when set.
    """
    rows = [parse_kismet_device(d) for d in devices if isinstance(d, dict)]
    if wifi_only:
        rows = [r for r in rows if r["phy"] == "IEEE802.11"]

    def _sort_key(r: Dict[str, Any]):
        rssi = r.get("rssi_dbm")
        # A MEANINGFUL 802.11 signal reading is in dBm and therefore strictly
        # negative (typ. -30 strong .. -90 weak). Kismet reports last_signal = 0
        # for a device it is tracking but has no signal measurement for (the vast
        # majority of the device list -- passive BT/BTLE contacts etc.), and None
        # when the field is absent. Both 0 and None mean "no real signal", so they
        # must sort AFTER every real AP -- otherwise a flood of signal-0 noise
        # ranks above a -31 dBm office AP and pushes every real AP off the top-N.
        has_signal = isinstance(rssi, (int, float)) and rssi < 0
        return (has_signal, rssi if has_signal else -9999.0)

    rows.sort(key=_sort_key, reverse=True)
    if top_n is not None and top_n >= 0:
        rows = rows[:top_n]
    return rows


def fetch_kismet_devices(kismet_url: str, apikey: Optional[str],
                         timeout: float = 12.0) -> List[Dict[str, Any]]:
    """Fetch the recent device list from a running Kismet server's REST API,
    asking Kismet to project only the compact KISMET_SURVEY_FIELDS.

    READ-ONLY: this only ever READS from Kismet -- it fetches the device list
    and never mutates any Kismet state. It uses the SAME route + auth scheme as
    field-bridge/kismet_bridge.py (the "KISMET" query param carrying the apikey,
    matching Kismet's own AUTH_COOKIE name; the key stays server-side).

    WHY POST WITH A FIELD PROJECTION (not a bare GET of full device objects):
    Kismet's /devices/... endpoints accept a POST carrying a "command
    dictionary" as the `json` POST variable (Kismet's own documented REST
    convention, ../kismet/kis_net_beast_httpd -- the same form its web UI uses).
    A "fields" list in that dictionary tells Kismet to return ONLY those fields
    ("field simplification"), so the response stays tiny even when Kismet is
    tracking hundreds of thousands / millions of devices. A bare GET returns the
    FULL per-device object for every device and times out against a busy Kismet.
    Field simplification returns HTTP 200 in ~1s with just the survey fields.

    We bound the query to a RECENT window (KISMET_SURVEY_WINDOW_S, a small
    negative "last N seconds" value) so the survey reflects what is currently in
    range and the payload stays small. Kismet 2025-09 removed the older
    all_devices.json GET (now 404); the incremental
    /devices/last-time/<n>/devices.json route survives and takes the negative
    relative window. Falls back to all_devices.json for older Kismet builds.
    Raises requests.RequestException on network failure (caller degrades).
    """
    params = {}
    if apikey:
        params["KISMET"] = apikey

    # Kismet field-simplification: a POST body form var `json` holding a command
    # dictionary; the "fields" list projects the response to only these fields.
    payload = {"json": json.dumps({"fields": KISMET_SURVEY_FIELDS})}

    base = kismet_url.rstrip("/")
    # Prefer the surviving incremental route with a small recent window.
    routes = [
        f"/devices/last-time/-{KISMET_SURVEY_WINDOW_S}/devices.json",
        "/devices/all_devices.json",
    ]
    last_exc: Optional[Exception] = None
    for path in routes:
        try:
            r = requests.post(f"{base}{path}", params=params, data=payload,
                              timeout=timeout)
            if r.status_code == 404:
                continue  # try the next known route
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return []
        except requests.RequestException as e:
            last_exc = e
            continue
    if last_exc is not None:
        raise last_exc
    return []
