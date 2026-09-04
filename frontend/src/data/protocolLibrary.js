// RF knowledge-base reference datasets for the "Drone Protocol Library" module
// (task #82). The live protocol status board itself (operational + forensic
// entries, per-protocol status derivation) is NOT defined here — the single
// source of truth for that list is backend/protocol_status.py, served live
// via GET /api/protocols/status and rendered generically by
// ProtocolLibrary.jsx. This file only holds the reference RF knowledge-base
// sources and the sub-GHz signature database meta note below.

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
