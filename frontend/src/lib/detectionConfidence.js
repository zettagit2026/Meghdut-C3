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

// Render-time-only helper: whether the small "(unconfirmed)" tag should
// still be shown next to the model/protocol text.
//
// For heuristic_binary, the backend (see backend/DETECTION_DISPLAY_MODEL.md,
// `_heuristic_display()`) overrides the PRIMARY label to an honest generic
// category name -- e.g. "Unidentified 2.4GHz Emitter" -- instead of a
// specific manufacturer guess, but ONLY for the two exact raw model strings
// listed in `HEURISTIC_GENERIC_DISPLAY` (a plain dict.get with no fallback).
// For any OTHER heuristic_binary model string, the override never fires:
// `model`/`protocol` stay as the field-bridge's original specific-sounding
// guess, and `original_model` is never populated either (it's only set
// inside the branch where the override DID apply). So confidence_type alone
// cannot tell us whether the primary label is actually the honest generic
// text or still a confident-looking raw guess -- use `original_model`'s
// presence as the real signal for "did the override apply", falling back to
// showing the tag (the safe default) whenever it didn't.
//
// unclassified_signal's primary label is "Unclassified emitter (candidate)",
// where "candidate" alone reads ambiguous (candidate for what?) -- the
// explicit tag still earns its place there.
export function shouldShowUnconfirmedTag(d) {
  if (!isUnconfirmedDetection(d)) return false;
  if (d.confidence_type === "heuristic_binary") {
    // Tag only genuinely redundant when the generic-display override
    // actually replaced the primary label -- proxied by original_model
    // having been populated (only happens inside that same branch).
    return !d.original_model;
  }
  return true;
}

// Render-time-only helper: how to caption the muted secondary line showing
// `d.original_model`/`d.original_protocol` when it differs from the
// (possibly display-overridden) primary `d.model`/`d.protocol`.
//
// "was: X" (past tense) is accurate for the true reclassification cases --
// ml_probability's wifi-reclassification and unclassified_signal -- where
// the record's classification genuinely changed from the heuristic's guess
// to something else over time (see DETECTION_DISPLAY_MODEL.md).
//
// It is NOT accurate for heuristic_binary: there, `original_model` was
// NEVER a confirmed prior state that got corrected -- it's simply the raw
// RSSI-heuristic's pattern-matched guess, demoted straight to secondary at
// creation time. "was:" implies a correction that never happened. Use
// "possible match:" instead, which honestly frames it as an unconfirmed
// guess rather than a superseded fact.
export function getOriginalModelAnnotation(d) {
  if (!d || !d.original_model || d.original_model === d.model) return null;
  if (d.confidence_type === "heuristic_binary") {
    return {
      label: "possible match",
      title:
        "Unconfirmed RF pattern match only -- the RSSI/persistence heuristic guessed this specific model, but no ML classification or protocol decode confirms it. Could be any in-band emitter.",
    };
  }
  if (d.confidence_type === "wifi_attributed" || d.confidence_type === "multidomain_fused") {
    return {
      label: "was",
      title:
        "Original RSSI/ML drone-candidate guess, superseded by Kismet WiFi ground-truth fusion (a co-channel 802.11 device was observed in-band).",
    };
  }
  return {
    label: "was",
    title: "Original RSSI-heuristic guess, superseded by ML reclassification",
  };
}
