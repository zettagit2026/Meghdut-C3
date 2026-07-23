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
//
// unclassified_signal (2026-07-23) is ALSO always unconfirmed -- by
// construction ml_classify_bridge.py only ever emits this confidence_type
// when its own top-class softmax probability fell below
// UNCLASSIFIED_MAX_CONFIDENCE, i.e. the classifier is explicitly saying "I
// don't know what this is", which is a strictly more explicit statement of
// uncertainty than heuristic_binary (no ML opinion at all). Gating this
// case on ml_label/protocol_confirmed the same way heuristic_binary is
// would be wrong: unclassified_signal's own ml_label is exactly the weak
// guess this confidence_type exists to say "don't trust" -- so unlike the
// heuristic_binary path, an ml_label of "drone" here must NOT be treated
// as confirmation. This is additive to ConfidenceTypeBadge's distinct
// "UNCLASSIFIED" badge, not redundant with it: the badge communicates WHAT
// the record's confidence type is, this tag communicates that the
// detection has not been confirmed as any specific threat.
export function isUnconfirmedDetection(d) {
  if (!d) return false;
  if (d.confidence_type === "unclassified_signal") return true;
  if (d.confidence_type !== "heuristic_binary") return false;
  const mlConfirmsDrone = d.ml_label === "drone";
  const protocolConfirmed = d.protocol_confirmed === true;
  return !mlConfirmsDrone && !protocolConfirmed;
}
