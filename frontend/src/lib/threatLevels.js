// Shared threat-level color maps. The four severity tiers (LOW/MEDIUM/HIGH/
// CRITICAL) plus INFO stay defined in exactly one place.
//
// THEMING: these colors are safety-critical and were WCAG-AA validated against
// BOTH the dark and light console backgrounds (see index.css --threat-*).
// The dark set was re-tuned off maxed-saturation neon to calmer instrument-grade
// values, CVD-validated as a set (dataviz palette validator: worst adjacent
// Machado deutan ΔE 11.7), CRITICAL kept dominant, no AA regression.
//   dark  vs #0C111D surface (all >= 4.5:1):
//     low #10B981 7.43 · medium #EAB308 9.83 · high #F97316 6.73 ·
//     critical #EF4444 5.01 · info #38BDF8 8.80
//   light vs #E8EBF0 base (all >= 4.5:1):
//     low #047857 4.59 · medium #92600A 4.51 · high #9A3412 6.11 ·
//     critical #B91C1C 5.41 · info #155E75 6.08

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
  LOW: "#10B981",
  MEDIUM: "#EAB308",
  HIGH: "#F97316",
  CRITICAL: "#EF4444",
  INFO: "#38BDF8",
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
