// Render-time-only helper: decides whether a detection's displayed
// model/protocol should be visually flagged as UNCONFIRMED.
//
// This is purely a display decision -- it never touches the underlying
// `model`/`protocol` string values, which must stay byte-identical to what
// hackrf_rx.py / field-bridge/ml_classify_bridge.py post (those exact
// strings are used for server-side merge-matching between the RSSI
// heuristic and the ML classify bridge; changing them would break that
// merge and reintroduce a worse version of an already-fixed bug).
//
// A detection is "unconfirmed" when its confidence_type is heuristic_binary
// (RSSI/persistence only -- no ML classification, no protocol decode) AND
// nothing else in the record actually confirms a drone-consistent signal:
//   - no ml_label at all, or ml_label isn't "drone" (wifi_2_4/wifi_5 are
//     explicitly NOT drone-consistent -- see MlClassifierBadge.jsx)
//   - protocol_confirmed is not true (no CRC-verified decode)
//
// The 2.4GHz band is saturated with ordinary Wi-Fi/Bluetooth traffic that
// routinely crosses the RSSI persistence threshold, so heuristic_binary
// alone must never read as "probable drone" -- it's an unconfirmed RF
// contact that could be anything in-band.
export function isUnconfirmedDetection(d) {
  if (!d || d.confidence_type !== "heuristic_binary") return false;
  const mlConfirmsDrone = d.ml_label === "drone";
  const protocolConfirmed = d.protocol_confirmed === true;
  return !mlConfirmsDrone && !protocolConfirmed;
}
