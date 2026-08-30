// Renders the honest confidence representation for a detection's
// `confidence_type` (see backend/CONFIDENCE_MODEL.md). Deliberately does NOT
// render a single universal "confidence meter" -- a CRC-verified decode, a
// softmax probability, a persistence heuristic, and a presence-only advisory
// have different epistemic status and must not be visually conflated.
//
// - protocol_verified -> hard verified badge, no percentage (pass/fail, and it passed)
// - ml_probability    -> muted percentage/probability chip (never "confirmed" styling)
// - heuristic_binary  -> plain "flagged" tag, no number
// - unclassified_signal -> real energy-gated RF present, but the 3-class ML
//   model's winning softmax probability was too weak to trust any of its
//   known classes -- distinct from ml_probability (a trusted class guess)
// - advisory_only     -> plain neutral "advisory" tag, no number
// - wifi_attributed   -> neutral "Wi-Fi (identified)" tag: an RF 2.4GHz drone-
//   candidate re-attributed to ordinary WiFi because the Kismet WiFi monitor
//   saw a co-channel IEEE802.11 device (ground-truth fusion). Not a drone.
// - multidomain_fused -> "FUSED / CORROBORATED": RF candidate corroborated by a
//   co-channel drone-OUI (DJI/Parrot/Autel) WiFi device (ground-truth fusion).
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
          Classification Confidence{pct !== null ? ` ${pct}%` : ""}
        </span>
      );
    }
    case "unclassified_signal": {
      const pct = Number.isFinite(detection.ml_confidence)
        ? Math.round(detection.ml_confidence * 100)
        : null;
      return (
        <span
          className={common}
          style={{ color: "#C77D3D", borderColor: "#8A5A2C", background: "rgba(138,90,44,0.18)" }}
          title="Real energy-gated RF detected, but the classifier's top-class confidence was too low to trust any of its 3 known classes (drone/wifi_2_4/wifi_5). Genuine unknown emitter, not a fabricated label."
        >
          UNCLASSIFIED{pct !== null ? ` (top guess ${pct}%)` : ""}
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
    case "wifi_attributed": {
      // RF drone-candidate re-attributed to ordinary WiFi because a co-channel
      // IEEE802.11 device was seen by the Kismet WiFi monitor (ground truth).
      // Neutral, non-threat styling -- this is not a drone.
      const wf = detection.wifi_fusion || {};
      const manuf = wf.matched_manuf || "Wi-Fi device";
      const ssid = wf.matched_ssid ? ` (${wf.matched_ssid})` : "";
      return (
        <span
          className={common}
          style={{ color: "#5FA8D3", borderColor: "#2F5A73", background: "rgba(47,90,115,0.18)" }}
          title={`Cross-referenced against the Kismet WiFi monitor: a co-channel IEEE802.11 device (${manuf}${ssid}) is present in-band, so this 2.4GHz contact is attributed to ordinary WiFi, not a drone. Reference data, downgraded to LOW/advisory -- not deleted.`}
        >
          Wi-Fi (identified)
        </span>
      );
    }
    case "multidomain_fused": {
      // RF drone-candidate CORROBORATED by a co-channel drone-OUI
      // (DJI/Parrot/Autel) IEEE802.11 device seen by the Kismet WiFi monitor.
      const wf = detection.wifi_fusion || {};
      const manuf = wf.matched_manuf ? ` (${wf.matched_manuf})` : "";
      return (
        <span
          className={common}
          style={{ color: "#0F1626", borderColor: "var(--accent-danger, #E5484D)", background: "var(--accent-danger, #E5484D)" }}
          title={`Multi-domain fusion: the RF detection is corroborated by a co-channel drone-manufacturer WiFi device${manuf} seen by the Kismet WiFi monitor. Two independent sensors agree -- confidence raised.`}
        >
          ✓ FUSED / CORROBORATED
        </span>
      );
    }
    default:
      return null;
  }
}
