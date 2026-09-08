// Pure, dependency-free helpers for the WI-FI ENVIRONMENT panel
// (RF situational awareness). No axios / no React imports here on purpose so
// this logic is unit-testable under bare jest and reused by the page.
//
// SITUATIONAL AWARENESS ONLY. Nothing in this module targets or engages an
// AP. isPossibleUas() produces a NON-ACTIONABLE visual hint; it never gates
// or enables any action. Real engagement stays entirely on the drone-contact
// target flow, never here.

// Small, best-effort drone-vendor OUI hints. Mirrors the backend
// (backend/kismet_survey.py DRONE_MANUFACTURER_OUIS, itself a copy of
// field-bridge/kismet_bridge.py's list). SSID + OUI are SPOOFABLE, so a hit is
// a manufacturer CANDIDATE for display, never an identification.
export const DRONE_OUIS = {
  "60:60:1F": "DJI",
  "34:D2:62": "DJI",
  "A0:14:3D": "DJI",
  "48:1C:B9": "DJI",
  "90:3A:E6": "Parrot",
  "00:12:1C": "Parrot",
  "00:26:7E": "Parrot",
  "90:03:B7": "Parrot",
  "EC:5B:CD": "Autel",
};

export const DRONE_SSID_PATTERNS = [
  "dji-", "mavic", "phantom", "spark", "tello", "parrot", "bebop",
  "anafi", "autel", "evo-", "skydio", "fimi", "hubsan",
];

export function macOui(mac) {
  if (!mac) return "";
  return String(mac).toUpperCase().split(":").slice(0, 3).join(":");
}

export function droneOuiVendor(mac) {
  return DRONE_OUIS[macOui(mac)] || null;
}

export function ssidLooksLikeDrone(ssid) {
  if (!ssid) return false;
  const low = String(ssid).toLowerCase();
  return DRONE_SSID_PATTERNS.some((p) => low.includes(p));
}

// A row's possible-UAS hint. The backend already computes `possible_uas`;
// this recomputes defensively so the tag is correct even for an older/partial
// payload. NON-ACTIONABLE.
export function isPossibleUas(ap) {
  if (!ap) return false;
  if (ap.possible_uas === true) return true;
  return droneOuiVendor(ap.bssid) !== null || ssidLooksLikeDrone(ap.ssid);
}

// Human-readable encryption label. Kismet's crypt_string is already printable
// (e.g. "WPA2-PSK-AES", "Open"); we only normalise the empty/unknown case.
export function encryptionLabel(ap) {
  const enc = ap && ap.encryption;
  if (enc === null || enc === undefined || enc === "") return "Unknown";
  return String(enc);
}

// PMF (802.11w) short label from the two boolean flags.
export function pmfLabel(ap) {
  if (!ap) return "—";
  if (ap.pmf_required === true) return "Required";
  if (ap.pmf_supported === true) return "Supported";
  if (ap.pmf_required === false || ap.pmf_supported === false) return "No";
  return "—";
}

// Filter rows by a free-text query across SSID / BSSID / vendor / channel.
export function filterAps(aps, query) {
  const list = Array.isArray(aps) ? aps : [];
  const q = (query || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter((ap) => {
    const hay = [
      ap.ssid, ap.bssid, ap.vendor, ap.channel, ap.encryption,
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  });
}

const NUMERIC_KEYS = new Set([
  "rssi_dbm", "client_count", "last_seen", "first_seen", "frequency_khz",
]);

// A meaningful 802.11 RSSI reading is in dBm and therefore strictly negative
// (typ. -30 strong .. -90 weak). Kismet reports 0 for a device it is tracking
// but has no signal measurement for (passive/BT-side or otherwise-unheard
// devices -- the vast majority of the list), and null/undefined when the
// field is absent entirely. Mirrors backend/kismet_survey.py's `_sort_key`:
// both 0 and null mean "no real signal" and must never outrank a real AP.
function hasRealRssi(v) {
  return typeof v === "number" && Number.isFinite(v) && v < 0;
}

// Sort rows by a column key. Stable-ish: nulls always sort last regardless of
// direction, so empty cells never crowd the top. For rssi_dbm specifically,
// "no signal" (0 / null / non-negative) is treated as the null case too, so
// it always ranks last -- it can never be mistaken for the strongest AP just
// because 0 > -38 numerically.
export function sortAps(aps, key, dir = "desc") {
  const list = Array.isArray(aps) ? aps.slice() : [];
  const sign = dir === "asc" ? 1 : -1;
  const numeric = NUMERIC_KEYS.has(key);
  const isRssi = key === "rssi_dbm";
  list.sort((a, b) => {
    let av = a ? a[key] : null;
    let bv = b ? b[key] : null;
    const aNull = isRssi
      ? !hasRealRssi(av)
      : (av === null || av === undefined || av === "");
    const bNull = isRssi
      ? !hasRealRssi(bv)
      : (bv === null || bv === undefined || bv === "");
    if (aNull && bNull) return 0;
    if (aNull) return 1; // nulls (and no-signal rssi) last
    if (bNull) return -1;
    if (numeric) {
      av = Number(av); bv = Number(bv);
      return (av - bv) * sign;
    }
    return String(av).localeCompare(String(bv)) * sign;
  });
  return list;
}

// Seconds-since-last-seen -> compact relative label. `nowSec` injectable for
// deterministic tests.
export function lastSeenLabel(ap, nowSec) {
  const t = ap && ap.last_seen;
  if (!t) return "—";
  const now = nowSec != null ? nowSec : Math.floor(Date.now() / 1000);
  const d = Math.max(0, now - t);
  if (d < 60) return `${d}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return `${Math.floor(d / 86400)}d ago`;
}
