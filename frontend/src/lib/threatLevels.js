// Shared threat-level color maps. The four severity tiers (LOW/MEDIUM/HIGH/
// CRITICAL) plus INFO stay defined in exactly one place.
//
// THEMING: these colors are safety-critical and were WCAG-AA validated against
// BOTH the dark and light console backgrounds (see index.css --threat-*).
//   dark  vs #0C111D surface  ·  light vs #E8EBF0 base (all >= 4.5:1):
//   low #047857 4.59 · medium #92600A 4.51 · high #9A3412 6.11 ·
//   critical #B91C1C 5.41 · info #155E75 6.08

// DOM badges (Dashboard.jsx, DetectionHistory.jsx) use inline style
// color/borderColor, where CSS custom properties resolve fine -- so these
// point at the --threat-* tokens and adapt automatically on theme flip.
export const THREAT_COLOR = {
  LOW: "var(--threat-low)",
  MEDIUM: "var(--threat-medium)",
  HIGH: "var(--threat-high)",
  CRITICAL: "var(--threat-critical)",
};

// maplibre-gl paint properties need literal color strings (they cannot read
// CSS custom properties), so the hex is duplicated per theme here. These MUST
// stay 1:1 with the --threat-* values in index.css.
export const THREAT_COLOR_HEX_DARK = {
  LOW: "#39FF14",
  MEDIUM: "#FFD60A",
  HIGH: "#FF8A00",
  CRITICAL: "#FF3B30",
  INFO: "#00F0FF",
};

export const THREAT_COLOR_HEX_LIGHT = {
  LOW: "#047857",
  MEDIUM: "#92600A",
  HIGH: "#9A3412",
  CRITICAL: "#B91C1C",
  INFO: "#155E75",
};

// Back-compat default export name kept for any dark-only consumer.
export const THREAT_COLOR_HEX = THREAT_COLOR_HEX_DARK;

// Resolve the correct literal-hex set for the currently active theme. Pass a
// theme string, or omit to read <html data-theme> at call time.
export function getThreatHex(theme) {
  const t =
    theme ||
    (typeof document !== "undefined" && document.documentElement.getAttribute("data-theme")) ||
    "dark";
  return t === "light" ? THREAT_COLOR_HEX_LIGHT : THREAT_COLOR_HEX_DARK;
}
