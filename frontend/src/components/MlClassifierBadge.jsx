// Small secondary/supplementary chip surfacing the ML classify bridge's
// output (see field-bridge/ml_classify_bridge.py). Deliberately styled as a
// muted, secondary signal — NOT a primary verdict — because the underlying
// model is a closed-world 3-class classifier (drone / wifi_2_4 / wifi_5)
// with no idle/noise/reject class, and is known to hallucinate "drone" at
// very high confidence on pure noise when not energy-gated. Never render
// this with bright/"confirmed" styling (e.g. accent-success green +
// checkmark), even at high confidence, and never render anything when the
// label is absent or the gate suppressed this cycle's inference (stale).
const ML_LABEL_TEXT = {
  drone: "drone",
  wifi_2_4: "wifi 2.4",
  wifi_5: "wifi 5",
};

export default function MlClassifierBadge({ detection }) {
  const label = detection?.ml_label;
  if (!label || detection?.ml_gated) return null;

  const pct = Number.isFinite(detection.ml_confidence)
    ? Math.round(detection.ml_confidence * 100)
    : null;
  const text = ML_LABEL_TEXT[label] || label;

  return (
    <span
      className="mt-1 inline-block w-fit px-1.5 py-0.5 tactical-border font-mono text-[11px] font-semibold uppercase tracking-wide"
      style={{ color: "#94A8C7", borderColor: "#3D5273", background: "rgba(61,82,115,0.22)" }}
      title="Supplementary classifier signal only — closed-world 3-class model (drone / wifi 2.4 / wifi 5), no idle/noise class. Known to hallucinate &quot;drone&quot; on ungated noise. Not a confirmed verdict."
    >
      ML: {text}{pct !== null ? ` (${pct}%)` : ""}
    </span>
  );
}
