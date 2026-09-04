"""Protocol-Library status derivation (over-the-air decoders vs forensic).

WHY THIS MODULE EXISTS
======================
The Protocol Library page used to show a single blanket
"STAGED -- TESTED, NOT LIVE" label for every decoder. That was dishonest in
both directions: it undersold the decoders that DO run live against real
signals (RemoteID via Kismet, control-link RF classification over real
detections) and it oversold the wire-tap decoders (CRSF/MSP/CANopen/DroneCAN)
by implying they were one step from being fielded when in fact they need
PHYSICAL contact with a recovered airframe and are useless against an airborne
target.

This module holds the SINGLE SOURCE OF TRUTH for the per-protocol status
board, derived ONLY from OBSERVABLE state (is the decoder service posting a
heartbeat? has it produced a real decode recently?) -- never a hardcoded
optimistic "LIVE". It is deliberately a small, dependency-free, pure module so
its derivation logic is unit-testable in-process without booting FastAPI/Mongo
(same factoring convention as mavlink_codec.py / track_manager.py).

DOCTRINE (from the operator, 2026-09-04)
========================================
The Protocol Library IDENTIFIES a drone over the air. The DEFEAT is
jam / GNSS-spoof / SDR-MAVLink-injection, NOT the wire decoders. So the board
is split into two clearly-separated groups:

  OPERATIONAL -- over-the-air, fielded against airborne targets, no physical
                 access to the target required:
                   * remoteid     (Wi-Fi/BLE ASTM F3411 broadcast, via Kismet)
                   * droneid       (DJI OcuSync RF, cued SDR capture)
                   * control_link  (ELRS/CRSF/DSMX/DJI/MAVLink emission class)
                   * fpv_osd       (analog FPV video OSD telemetry -- the
                                    over-the-air "MSP-class" data on an enemy)
                   * adsb          (1090 MHz Mode-S DF17, via an EXISTING
                                    dump1090/readsb feed -- not the primary SDR)
                   * parrot        (Parrot ARSDK3 over Wi-Fi, via the EXISTING
                                    Kismet monitor NIC)

  FORENSIC    -- recovered / own airframe, bench, requires physical access
                 (USB-UART / CAN electrical tap). NOT for airborne engagement:
                   * crsf, msp, canopen, dronecan, sik_mavlink_wire,
                     ltm, dshot, frsky_smartport, graupner_hott (wire taps)
                   * flysky_afhds, frsky_accst, spektrum_dsm -- chip-level RC
                     control-link parsers. HONEST: their over-the-air presence
                     is surfaced at FAMILY level by the operational Control-Link
                     RF Classification (hobby_rc_2g4); the per-chip frame decode
                     needs a dedicated receiver IC (A7105 / CC2500 / CYRF6936)
                     and is NOT decodable from the wideband HackRF sweep. They
                     are bench parsers, never phantom airborne radios.

STATUS VALUES
=============
  LIVE          service is posting a recent heartbeat AND has produced a real
                decode within the decode-recency window.
  READY         service is posting a recent heartbeat but has NOT decoded a
                matching signal yet (running, awaiting a real emission). This
                is the honest state for a decoder whose radio source is quiet
                -- NOT faked as "decoding".
  OFFLINE       no recent heartbeat -- the service is not running yet (e.g. the
                systemd unit has not been installed/started by the SRE deploy).
  FORENSIC      wire/bench decoder; requires physical access. Static -- there
                is no over-the-air source, so it can never be LIVE/READY here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

# Heartbeat recency window: a running decoder service POSTs a heartbeat once
# per poll/sweep cycle. Cycle intervals for these services are a handful of
# seconds (Kismet poll ~5s, control-link poll ~5s, FPV OSD poll ~5s, DroneID
# cued poll ~10s), so 45s is comfortably >3x the slowest and still flags a
# stopped/crashed service within under a minute -- same rationale as
# ml_classify_bridge_live's 40s window in server.py.
DEFAULT_LIVE_WINDOW_S = 45.0

# Decode recency window: how recently a real decoded message must have arrived
# for a service to count as LIVE (vs merely READY). Generous enough that an
# intermittently-transmitting contact (Remote ID beacons ~1 Hz, a DJI hop, an
# FPV OSD refresh) keeps the badge LIVE between bursts, without pinning it LIVE
# forever after a single stale decode.
DEFAULT_DECODE_WINDOW_S = 120.0

# The four over-the-air operational protocols. `id` is the key each field
# bridge reports under (POST /api/protocols/heartbeat and the per-protocol
# ingest endpoints). Order here is the display order on the board.
OPERATIONAL_PROTOCOLS: List[Dict[str, str]] = [
    {
        "id": "remoteid",
        "name": "RemoteID (Wi-Fi / BLE)",
        "aka": "ASTM F3411 / ASD-STAN Direct Remote ID broadcast",
        "over_the_air": "Passive 802.11 Beacon/NAN + BLE4/5 advertising, via Kismet",
        "service": "remoteid_kismet_bridge.py",
        "identifies": "Registered/compliant drone ID, serial, live position, operator location",
    },
    {
        "id": "droneid",
        "name": "DJI DroneID (RF)",
        "aka": "OcuSync 2.0 DroneID beacon",
        "over_the_air": "Cued short IQ capture on the RX HackRF (device-lock guarded)",
        "service": "droneid_cued_capture.py",
        "identifies": "DJI serial, device type, aircraft + operator GPS (CRC-verified decode)",
    },
    {
        "id": "control_link",
        "name": "Control-Link RF Classification",
        "aka": "ELRS / CRSF / DSMX / DJI / MAVLink-SiK emission class",
        "over_the_air": "Band + signature heuristic over live detection-plane contacts",
        "service": "control_link_bridge.py",
        "identifies": "Likely control-link family attached to a contact (heuristic, not a decode)",
    },
    {
        "id": "fpv_osd",
        "name": "FPV OSD Telemetry (analog)",
        "aka": "MAX7456-class analog video OSD -- the over-the-air 'MSP-class' path",
        "over_the_air": "OCR of the drone's own analog FPV video downlink OSD overlay",
        "service": "fpv_osd_bridge.py",
        "identifies": "Craft name, battery, altitude, GPS, sats, RSSI read off the video (analog only)",
    },
    {
        "id": "adsb",
        "name": "ADS-B (1090 MHz Mode-S DF17)",
        "aka": "Mode-S Extended Squitter (DF17/18) 1090 MHz cooperative surveillance",
        "over_the_air": "Passive 1090 MHz feed from an EXISTING dump1090/readsb receiver "
                        "(Beast/SBS output) -- NOT the primary detection HackRF",
        "service": "adsb_ingest_bridge.py",
        "identifies": "ICAO24 address, callsign, live position, altitude + velocity of a "
                      "transponder-equipped aircraft (cooperative broadcast, decoded)",
    },
    {
        "id": "parrot",
        "name": "Parrot ARSDK3 (Wi-Fi)",
        "aka": "Parrot ARSDK3 command/telemetry over the drone's own Wi-Fi",
        "over_the_air": "Passive 802.11 capture on the EXISTING Kismet monitor-mode NIC "
                        "(no new radio) -- reads a Parrot drone's own Wi-Fi link",
        "service": "parrot_arsdk_ingest_bridge.py",
        "identifies": "ARSDK project / class / command id observed off a Parrot drone's Wi-Fi",
    },
]

# Wire / bench decoders. These require PHYSICAL contact with a recovered or
# own airframe's flight controller (USB-UART) or CAN bus -- they are USELESS
# against an airborne enemy and are NEVER shown next to the operational board.
FORENSIC_PROTOCOLS: List[Dict[str, str]] = [
    {
        "id": "crsf",
        "name": "CRSF (Crossfire / ExpressLRS)",
        "requires": "USB-UART tap on the RX (420k baud serial)",
        "source": "field-bridge/crsf_parser.py",
    },
    {
        "id": "msp",
        "name": "MSP v2 (MultiWii Serial Protocol)",
        "requires": "USB-UART tap on the flight controller",
        "source": "field-bridge/msp_parser.py",
    },
    {
        "id": "canopen",
        "name": "CANopen (CiA 301)",
        "requires": "Electrical CAN bus adapter (PCAN-USB / CANable)",
        "source": "field-bridge/canopen_parser.py",
    },
    {
        "id": "dronecan",
        "name": "DroneCAN (UAVCAN v0)",
        "requires": "Electrical CAN bus adapter (PCAN-USB / CANable)",
        "source": "field-bridge/dronecan_parser.py",
    },
    {
        "id": "sik_mavlink_wire",
        "name": "MAVLink over SiK (paired telemetry-radio tap)",
        "requires": "Paired SiK 915 MHz radio + UART tap (bench / range-authorized)",
        "source": "field-bridge/sik_mavlink_bridge.py",
    },
    {
        "id": "ltm",
        "name": "LTM (Light Telemetry Protocol)",
        "requires": "USB-UART tap on the flight controller telemetry port",
        "source": "field-bridge/ltm_parser.py",
    },
    {
        "id": "dshot",
        "name": "DShot ESC telemetry (bidirectional)",
        "requires": "Logic-level tap on the ESC signal wire (bench capture)",
        "source": "field-bridge/dshot_telemetry_parser.py",
    },
    {
        "id": "frsky_smartport",
        "name": "FrSky S.Port (SmartPort telemetry)",
        "requires": "Inverted-UART tap on the FrSky S.Port telemetry wire",
        "source": "field-bridge/frsky_smartport_parser.py",
    },
    {
        "id": "graupner_hott",
        "name": "Graupner HoTT telemetry",
        "requires": "USB-UART tap on the HoTT telemetry bus",
        "source": "field-bridge/graupner_hott_parser.py",
    },
    # Chip-level RC control-link parsers. These decode the on-air FRAME FORMAT of
    # a specific 2.4 GHz hobby-RC receiver chip -- but ONLY from a dedicated
    # receiver IC's baseband, NOT from the wideband HackRF sweep. Their over-the-
    # air PRESENCE is surfaced at family level by the operational Control-Link RF
    # Classification (hobby_rc_2g4); the per-chip frame decode is a BENCH parser
    # against a matching receiver. `ota_family` states that honest linkage and
    # `requires` names the dedicated receiver chip -- never a phantom airborne
    # radio on the primary SDR.
    {
        "id": "flysky_afhds",
        "name": "Flysky AFHDS/AFHDS2A (RC control link)",
        "requires": "A7105 2.4 GHz receiver IC -- NOT decodable from the wideband HackRF sweep",
        "ota_family": "OTA presence surfaced at family level by Control-Link RF "
                      "Classification (hobby_rc_2g4); per-chip frame decode is bench-only",
        "source": "field-bridge/flysky_afhds_parser.py",
    },
    {
        "id": "frsky_accst",
        "name": "FrSky ACCST/ACCESS (RC control link)",
        "requires": "CC2500 2.4 GHz receiver IC -- NOT decodable from the wideband HackRF sweep",
        "ota_family": "OTA presence surfaced at family level by Control-Link RF "
                      "Classification (hobby_rc_2g4); per-chip frame decode is bench-only",
        "source": "field-bridge/frsky_accst_parser.py",
    },
    {
        "id": "spektrum_dsm",
        "name": "Spektrum DSM2/DSMX (RC control link)",
        "requires": "CYRF6936 2.4 GHz receiver IC -- NOT decodable from the wideband HackRF sweep",
        "ota_family": "OTA presence surfaced at family level by Control-Link RF "
                      "Classification (hobby_rc_2g4); per-chip frame decode is bench-only",
        "source": "field-bridge/spektrum_dsm_parser.py",
    },
]

_OPERATIONAL_IDS = {p["id"] for p in OPERATIONAL_PROTOCOLS}


def _age_s(ts_iso: Optional[str], now: datetime) -> Optional[float]:
    """Seconds since an ISO-8601 timestamp, or None if absent/unparseable.
    Never raises -- an unparseable timestamp is treated as 'no data'."""
    if not ts_iso:
        return None
    try:
        ts = datetime.fromisoformat(ts_iso)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds()


def derive_operational_status(
    report: Optional[Dict],
    now: datetime,
    live_window_s: float = DEFAULT_LIVE_WINDOW_S,
    decode_window_s: float = DEFAULT_DECODE_WINDOW_S,
) -> str:
    """Pure status derivation for one over-the-air protocol from its report.

    `report` is the runtime record the backend keeps per protocol id:
        {"last_heartbeat_ts": iso|None, "last_decode_ts": iso|None, ...}
    or None if the service has never reported. Returns one of
    "LIVE" / "READY" / "OFFLINE" -- see module docstring.
    """
    if not report:
        return "OFFLINE"
    hb_age = _age_s(report.get("last_heartbeat_ts"), now)
    if hb_age is None or hb_age > live_window_s:
        return "OFFLINE"
    dec_age = _age_s(report.get("last_decode_ts"), now)
    if dec_age is not None and dec_age <= decode_window_s:
        return "LIVE"
    return "READY"


def build_board(
    reports: Dict[str, Dict],
    now: Optional[datetime] = None,
    live_window_s: float = DEFAULT_LIVE_WINDOW_S,
    decode_window_s: float = DEFAULT_DECODE_WINDOW_S,
) -> Dict:
    """Build the full, truthful status board the frontend renders.

    `reports` maps protocol id -> its runtime report record. Missing ids are
    reported OFFLINE (honest: service not running yet). Returns a dict with two
    clearly-separated groups plus the windows used, so the UI never has to
    re-derive or guess any status.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    operational = []
    for meta in OPERATIONAL_PROTOCOLS:
        report = reports.get(meta["id"])
        status = derive_operational_status(report, now, live_window_s, decode_window_s)
        hb_age = _age_s((report or {}).get("last_heartbeat_ts"), now)
        dec_age = _age_s((report or {}).get("last_decode_ts"), now)
        operational.append({
            **meta,
            "group": "OPERATIONAL",
            "status": status,
            "last_heartbeat_age_s": round(hb_age, 1) if hb_age is not None else None,
            "last_decode_age_s": round(dec_age, 1) if dec_age is not None else None,
            "decode_count": (report or {}).get("decode_count", 0),
            "last_decode_summary": (report or {}).get("last_decode_summary"),
            "note": (report or {}).get("note"),
        })

    forensic = []
    for meta in FORENSIC_PROTOCOLS:
        forensic.append({
            **meta,
            "group": "FORENSIC",
            "status": "FORENSIC",
        })

    return {
        "generated_at": now.isoformat(),
        "live_window_s": live_window_s,
        "decode_window_s": decode_window_s,
        "operational": operational,
        "forensic": forensic,
        "doctrine": (
            "The Protocol Library IDENTIFIES a drone over the air. The DEFEAT is "
            "jam / GNSS-spoof / SDR-MAVLink-injection, not the wire decoders. "
            "FORENSIC decoders require physical access to a recovered/own airframe "
            "and are not a fielded counter-UAS capability against an airborne target."
        ),
    }
