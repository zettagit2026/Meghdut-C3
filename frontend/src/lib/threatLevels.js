// Shared threat-level color maps. Previously duplicated verbatim across
// Dashboard.jsx, DetectionHistory.jsx, and Map.jsx -- extracted here so the
// four severity tiers (LOW/MEDIUM/HIGH/CRITICAL) stay defined in exactly one
// place. Pure extraction: values are unchanged from what each file had.

// Used by Dashboard.jsx and DetectionHistory.jsx for inline style
// color/borderColor on DOM badges, where CSS custom properties resolve fine.
export const THREAT_COLOR = {
  LOW: "var(--accent-success)",
  MEDIUM: "var(--accent-warning)",
  HIGH: "#FF8A00",
  CRITICAL: "var(--accent-critical)",
};

// Used by Map.jsx for maplibre-gl paint properties, which need literal color
// strings rather than CSS custom properties. Values match the CSS variables
// above 1:1 (see index.css: --accent-success/#39FF14, --accent-warning/#FFD60A,
// --accent-critical/#FF3B30) plus the same raw HIGH hex used everywhere.
export const THREAT_COLOR_HEX = {
  LOW: "#39FF14",
  MEDIUM: "#FFD60A",
  HIGH: "#FF8A00",
  CRITICAL: "#FF3B30",
};
