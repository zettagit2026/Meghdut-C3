// Render-only "(unconfirmed)" marker appended next to a detection's
// model/protocol text when the detection is heuristic_binary with nothing
// else (ml_label=="drone" or protocol_confirmed) actually confirming it.
// This NEVER changes the underlying `model`/`protocol` string -- only what's
// visually appended alongside it. Styling reuses the same muted palette
// ConfidenceTypeBadge.jsx already uses for "advisory_only" (#7A8699 /
// #3D4658) so uncertain signals stay visually consistent across the app.
export default function UnconfirmedTag() {
  return (
    <span
      className="ml-1 text-[9px] font-mono uppercase tracking-wide"
      style={{ color: "#7A8699" }}
      title="RSSI/persistence heuristic only -- no ML classification or protocol decode confirms this identity. Could be any in-band Wi-Fi/Bluetooth traffic."
    >
      (unconfirmed)
    </span>
  );
}
