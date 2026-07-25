// Curated, hand-verified entries for the "Drone Protocol Library" module
// (task #82). Each entry below is transcribed directly from the real
// parser/bridge source files in this repo (docstrings + frame-type tables),
// not invented — see the `source` field on each entry for the exact file
// this was read from. Status labels follow each source file's own stated
// hardware status, not an optimistic guess:
//
//   LIVE                — deployed and exercised against real hardware in
//                          the field today (e.g. hackrf_rx.py's passive
//                          RSSI/energy detection).
//   STAGED              — real, tested parsing/decode logic (self-tests
//                          pass against real reference captures/frames),
//                          but NOT wired into a live ingest bridge, or the
//                          ingest bridge itself is untested against live
//                          hardware.
//   HARDWARE_BLOCKED    — code is real and tested against a virtual/loopback
//                          transport, but categorically cannot run against
//                          real traffic without hardware this project does
//                          not currently own (e.g. a CAN adapter).
//
// Do not add entries here without reading the corresponding source file —
// this list intentionally does not attempt to cover every file in
// field-bridge/, only the ones that are genuinely protocol *parsers/decoders*
// (as opposed to jam/detection/ML-classification bridges, which belong on
// Signal Analysis / RF Barrage Jam instead).

export const PROTOCOL_PARSERS = [
  {
    id: "droneid-ocusync2",
    name: "DJI DroneID (OcuSync 2.0)",
    aka: "OcuSync 2.0 remote-ID beacon",
    band: "2.4 / 5.8 GHz (ISM, frequency-hopping)",
    modulation: "OFDM / QPSK, Zadoff-Chu sync sequence",
    decodes: [
      "Frame detection (packetizer + ZC sync)",
      "QPSK demod + descramble + turbo-decode",
      "DroneID struct unpack + CRC check",
      "Serial number, device type, operator/aircraft GPS (when present)",
    ],
    status: "STAGED",
    statusNote:
      "Decode chain verified against DroneSecurity's own bundled real DJI Mini 2 / Mavic Air 2 captures (50 Msps, USRP) — output matched the upstream README exactly (10 frame candidates, 9 decoded, 7 CRC-OK). Never run against a live DJI transmission; HackRF's ~20 Msps ceiling vs. the 15.36 Msps floor the decoder requires is untested territory.",
    source: "field-bridge/droneid_decode_bridge.py",
    task: "#17",
    provenance: "DroneSecurity (RUB-SysSec, NDSS'23), AGPLv3 — reused under project's internal-defense licensing override",
  },
  {
    id: "dji-ocusync-rssi",
    name: "DJI OcuSync / Wi-Fi video (RSSI heuristic)",
    aka: "Energy-detection candidate flagging",
    band: "2.4 GHz + 5.8 GHz",
    modulation: "N/A — energy/RSSI sweep, no demod",
    decodes: [
      "Channel occupancy + hop-pattern fingerprint",
      "RSSI-based \"DJI Mini (candidate)\" detection",
    ],
    status: "LIVE",
    statusNote:
      "Deployed passive HackRF RX sweep, posts real detections to /api/detections/ingest. This is a signature/energy guess, not a protocol-level decode — DroneID (above) is the stronger, protocol-confirmed layer on top of it.",
    source: "field-bridge/hackrf_rx.py",
    task: "#17 / #39",
  },
  {
    id: "crsf",
    name: "CRSF (Crossfire / ExpressLRS)",
    aka: "TBS Crossfire serial protocol",
    band: "Wired UART, 420,000 baud (RF hop happens inside RX/TX-module hardware)",
    modulation: "N/A at this layer — serial byte stream after RF demod",
    decodes: [
      "GPS", "BATTERY_SENSOR", "HEARTBEAT", "LINK_STATISTICS",
      "RC_CHANNELS_PACKED", "ATTITUDE", "DEVICE_PING", "DEVICE_INFO",
      "Extended-header frames (0x28–0x96)",
    ],
    status: "STAGED",
    statusNote:
      "Frame parser + CRC-8/DVB-S2 (poly 0xD5) cross-verified against two independent upstream implementations (AlfredoCRSF, ExpressLRS). RX-only serial bridge exists but is untested against a real CRSF receiver's UART in this session.",
    source: "field-bridge/crsf_parser.py",
    provenance: "Frame layout/CRC read from AlfredoCRSF (GPLv3) + ExpressLRS (GPLv3)",
  },
  {
    id: "msp-v2",
    name: "MSP v2 (MultiWii Serial Protocol, native)",
    aka: "Betaflight/Cleanflight/iNAV FC-companion protocol",
    band: "Wired UART (no RF path — requires physical serial tap)",
    modulation: "N/A — serial state machine",
    decodes: [
      "MSP_IDENT and other function-coded request/response frames",
      "MSPv2 native preamble ('$','X',dir), flag/function/size/payload/CRC-8",
    ],
    status: "STAGED",
    statusNote:
      "Tested parsing infrastructure only, ported byte-for-byte from serialport-parser-msp-v2's MspParser.ts (MIT). No ingest bridge, no MSP-capable serial hardware available in this project.",
    source: "field-bridge/msp_parser.py",
    task: "backlog C8",
  },
  {
    id: "canopen",
    name: "CANopen (CiA 301)",
    aka: "NMT error-control / heartbeat listener",
    band: "Wired CAN bus (ISO 11898, 125k–1M bit/s) — no RF path",
    modulation: "N/A — fieldbus, requires electrical CAN connection",
    decodes: [
      "NMT error-control / heartbeat + bootup frames",
      "Node state (INITIALIZING / PRE-OPERATIONAL / OPERATIONAL / STOPPED)",
    ],
    status: "HARDWARE_BLOCKED",
    statusNote:
      "Real canopen library wrapper, decode logic verified against python-can's virtual CAN transport (the same one canopen's own upstream test suite uses). Cannot run against a real bus — this project owns no CAN adapter (PCAN-USB or CANable) today.",
    source: "field-bridge/canopen_parser.py",
    provenance: "christiansandberg/canopen, MIT",
  },
  {
    id: "dronecan",
    name: "DroneCAN (UAVCAN v0)",
    aka: "ArduPilot/PX4 CAN peripheral bus (GPS/compass/ESC/airspeed)",
    band: "Wired CAN bus (ISO 11898) — no RF path",
    modulation: "N/A — fieldbus, requires electrical CAN connection",
    decodes: [
      "uavcan.protocol.NodeStatus (heartbeat/health)",
      "uavcan.equipment.gnss.Fix2 (lat/lon/height/sats/status)",
    ],
    status: "HARDWARE_BLOCKED",
    statusNote:
      "Real dronecan/pydronecan library wrapper (not reimplemented DSDL parsing). Exercised against a real dronecan stack over python-can's virtual transport with genuine DSDL-typed messages. Same CAN-adapter hardware blocker as CANopen above.",
    source: "field-bridge/dronecan_parser.py",
    provenance: "DroneCAN/pydronecan, MIT",
  },
  {
    id: "sik-mavlink",
    name: "MAVLink over SiK telemetry radio",
    aka: "SiK 915 MHz ISM FHSS radio link",
    band: "915 MHz ISM, FHSS (~20–64 channels, firmware-dependent)",
    modulation: "FHSS (radio-internal demod; bridge taps the resulting UART byte stream)",
    decodes: [
      "MAVLink v1/v2 command + telemetry frames",
      "RTH / arm / disarm / mode-change command injection (paired test craft only)",
    ],
    status: "STAGED",
    statusNote:
      "Byte-accurate MAVLink codec reused from the console's own mavlink_codec.py. Transmission gated behind CEMA_AUTHORIZED_RANGE two-factor confirmation — only run at STEAG under Army Signals spectrum authorization, against a paired test craft.",
    source: "field-bridge/sik_mavlink_bridge.py",
  },
];

export const RF_KB_SOURCES = [
  {
    id: "rfuav",
    name: "RFUAV drone RF dataset",
    band: "Not locally captured (see status)",
    status: "STAGED",
    statusNote:
      "Code-only checkout (train/inference/spectrogram pipeline, 34 experiment configs). 0 of 35 labeled drone recordings present locally — raw IQ/spectrograms/weights are hosted on Hugging Face (kitofrank/RFUAV) and Roboflow, not pulled down. Confirmed via README: ~100 Msps USRP captures, 23–35 airframe-level classes (Phantom4Pro, Mini2, Mavic3, Matrice300, Inspire2, AVATA, FutabaT61Z, ...).",
    source: "field-bridge/drone_rf_kb/README.md",
    task: "#21 (staging pass, backlog B2)",
  },
  {
    id: "dronesecurity-samples",
    name: "DroneSecurity real IQ samples (mavic_air_2, mini2_sm)",
    band: "2.4/5.8 GHz OcuSync 2.0, 50 Msps raw complex64 IQ",
    status: "STAGED",
    statusNote:
      "Real captured DJI Mini 2 / Mavic Air 2 RF, already used by the DroneID decode bridge. A local conversion tool (convert_iq_to_spectrogram.py) reuses the classifier's exact spectrogram pipeline, verified against the real IQ loader on the Mac — but the full image-generation path (scipy/torch) has not been executed end-to-end; that must happen on the deploy VM.",
    source: "field-bridge/drone_rf_kb/README.md + convert_iq_to_spectrogram.py",
    task: "#21",
  },
];

export const RF_PROTOCOL_DB_META_NOTE =
  "Sub-GHz signature database (task #39) — 692 catalogued sub-GHz device signatures " +
  "(key fobs, TPMS, utility meters, gate/garage remotes, weather sensors, and more), " +
  "pulled verbatim from RF-Protocol-Database v4.0.0 (this project's reference repo at " +
  "~/Desktop/Zettawise/PMO Suraj/tool/RF-Protocol-Database), aggregated from urh_ng, " +
  "wmbusmeters, rtl_433, Flipper Zero (unleashed/roguemaster firmware), rc-switch, and " +
  "other open catalogues. This is a reference signature database for RF fingerprint " +
  "matching, not drone-specific — surfaced here as the richest real sub-GHz corpus this " +
  "project has already integrated, browsable/filterable below.";
