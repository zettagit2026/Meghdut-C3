// Renders the honest confidence representation for a detection's
// `confidence_type` (see backend/CONFIDENCE_MODEL.md). Deliberately does NOT
// render a single universal "confidence meter" -- a CRC-verified decode, a
// softmax probability, a persistence heuristic, and a presence-only advisory
// have different epistemic status and must not be visually conflated.
//
// - protocol_verified -> hard verified badge, no percentage (pass/fail, and it passed)
// - ml_probability    -> muted percentage/probability chip (never "confirmed" styling)
// - heuristic_binary  -> plain "flagged" tag, no number
// - advisory_only     -> plain neutral "advisory" tag, no number
// - absent/unknown    -> render nothing (falls back to existing threat_level display)
export default function ConfidenceTypeBadge({ detection }) {
  const type = detection?.confidence_type;
  if (!type) return null;

  const common = "mt-1 inline-block w-fit px-1.5 py-0.5 tactical-border font-mono text-[11px] font-semibold uppercase tracking-wide";

  switch (type) {
    case "protocol_verified":
      return (
        <span
          className={common}
          style={{ color: "#0F1626", borderColor: "var(--accent-success)", background: "var(--accent-success)" }}
          title="Protocol-level CRC-verified decode -- pass/fail, not a probability. This detection genuinely decoded."
        >
          ✓ VERIFIED
        </span>
      );
    case "ml_probability": {
      const pct = Number.isFinite(detection.ml_confidence)
        ? Math.round(detection.ml_confidence * 100)
        : null;
      return (
        <span
          className={common}
          style={{ color: "#94A8C7", borderColor: "#3D5273", background: "rgba(61,82,115,0.22)" }}
          title="Softmax probability from a closed-world classifier -- informational, not a confirmed verdict."
        >
          ML PROB{pct !== null ? ` ${pct}%` : ""}
        </span>
      );
    }
    case "heuristic_binary":
      return (
        <span
          className={common}
          style={{ color: "var(--accent-warning, #D4A017)", borderColor: "var(--accent-warning, #D4A017)" }}
          title="Confirmed by RSSI/persistence heuristic -- no real-valued probability exists for this signal."
        >
          FLAGGED
        </span>
      );
    case "advisory_only":
      return (
        <span
          className={common}
          style={{ color: "#7A8699", borderColor: "#3D4658" }}
          title="Presence-only advisory -- explicitly not a device-identity or threat claim."
        >
          ADVISORY
        </span>
      );
    default:
      return null;
  }
}
