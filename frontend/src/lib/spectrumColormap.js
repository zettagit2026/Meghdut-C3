// Calibrated spectrum-waterfall colormap.
//
// WHY THIS EXISTS: an RF waterfall is a *measurement instrument*, so a given
// color must always mean the same power level. Both waterfall surfaces (the
// canvas SpectrumWaterfall.jsx and the ECharts one in Dashboard.jsx) therefore
// share ONE absolute dBm -> color mapping defined here, instead of each
// auto-scaling its own colors per frame.
//
// CALIBRATION (fixed floor/ceiling, operator-meaningful, documented):
//   floor  -95 dBm  = receiver noise floor / "no signal" -> near-black
//   ceiling -30 dBm = strong/saturating emitter          -> hot yellow-white
// Anything below the floor clamps to the darkest ramp step; anything above the
// ceiling clamps to the brightest. These are the same limits both surfaces use.
export const SPECTRUM_FLOOR_DBM = -95;
export const SPECTRUM_CEIL_DBM = -30;

// PERCEPTUALLY-UNIFORM ramp (matplotlib "inferno"). Chosen over the old
// blue->cyan->yellow->red rainbow because a rainbow has uneven perceptual
// steps that create FALSE BANDING at the cyan/yellow transitions -- artifacts
// that look like real spectral features on an instrument. Inferno rises
// monotonically in luminance (black noise floor -> bright hot signal), so equal
// dBm steps read as equal brightness steps and there is no false banding.
//
// This ramp is DELIBERATELY absolute and theme-independent: both waterfalls
// render on the dark "terminal" surface (--bg-terminal) in light AND dark
// themes, so the calibration must not shift with the theme. It is exported as
// data (not hardcoded inline) so both surfaces and the colorbar legend stay in
// lock-step.
export const INFERNO_STOPS = [
  "#000004", // -95 dBm  noise floor
  "#1b0c41",
  "#4a0c6b",
  "#781c6d",
  "#a52c60",
  "#cf4446",
  "#ed6925",
  "#fb9b06",
  "#f7d13d",
  "#fcffa4", // -30 dBm  saturating signal
];

const _rgb = INFERNO_STOPS.map((h) => [
  parseInt(h.slice(1, 3), 16),
  parseInt(h.slice(3, 5), 16),
  parseInt(h.slice(5, 7), 16),
]);

// Absolute dBm -> [r,g,b] using the fixed floor/ceiling above. The same dBm
// always yields the same color, on every frame and both surfaces.
export function dbmToRGB(dbm) {
  const t = Math.max(0, Math.min(1, (dbm - SPECTRUM_FLOOR_DBM) / (SPECTRUM_CEIL_DBM - SPECTRUM_FLOOR_DBM)));
  const seg = t * (_rgb.length - 1);
  const i = Math.min(_rgb.length - 2, Math.floor(seg));
  const f = seg - i;
  const a = _rgb[i];
  const b = _rgb[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f),
  ];
}
