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

export const BROADCAST_BSSID = "FF:FF:FF:FF:FF:FF";

// A concrete, non-broadcast softAP BSSID (mirrors the backend
// _wifi_bssid_missing_or_broadcast fail-closed guard).
export function hasConcreteBssid(bssid) {
  if (!bssid || typeof bssid !== "string") return false;
  const b = bssid.trim().toUpperCase();
  return b !== "" && b !== BROADCAST_BSSID;
}

// Is this survey row DESIGNATE-eligible? A commander MAY promote it to a
// suspected-UAS contact that routes to the governed per-BSSID Wi-Fi-defeat
// flow ONLY when it is a possible-UAS row, NOT already civilian-protected, the
// viewer is a commander, and it carries a concrete non-broadcast BSSID.
// Civilian / no-drone-indicator / protected / non-commander rows are
// NON-selectable (no engage control). This is a client-side convenience gate
// ONLY — the backend /wifi-environment/designate re-derives every check and is
// the source of truth (the client possible_uas flag is never trusted there).
export function canDesignateUas(ap, { isCommander, isProtected } = {}) {
  if (!ap) return false;
  if (!isCommander) return false;
  if (isProtected) return false;
  if (!isPossibleUas(ap)) return false;
  return hasConcreteBssid(ap.bssid);
}

// Return the first ENABLED no-strike entry that matches this AP (by BSSID or
// OUI), or null. Mirrors WifiEnvironment.jsx's entryMatchesAp but lives here so
// the row-action gate is unit-testable. Disabled entries (enabled === false) are
// skipped. Client-side display convenience ONLY — the backend is the source of
// truth for any real consult/fire decision.
export function matchedNoStrikeEntry(ap, entries) {
  if (!ap || !Array.isArray(entries)) return null;
  const bssid = (ap.bssid || "").toUpperCase();
  const oui = macOui(ap.bssid);
  for (const entry of entries) {
    if (!entry || entry.enabled === false) continue;
    const m = entry.match || {};
    if (m.bssid && bssid && String(m.bssid).toUpperCase() === bssid) return entry;
    if (m.oui && oui && String(m.oui).toUpperCase() === oui) return entry;
  }
  return null;
}

// COMMANDER NO-STRIKE OVERRIDE — the Wi-Fi survey row's Designate/Engage gate.
// Returns one of:
//   "protected"       -> a CIVILIAN_INFRASTRUCTURE match: the HARD FLOOR. NO
//                        engage/override control — reclassify the registry entry
//                        first (commander declassify). This is the buttonless badge.
//   "engage"          -> a drone-OUI/SSID-tagged, non-civilian row: normal designate.
//   "engage-override" -> a commander + non-civilian row that is NOT drone-tagged:
//                        designate with commander_override (honest non-drone
//                        candidate). Fire still needs the fire-time no-strike
//                        override token for a NEUTRAL/FRIENDLY match.
//   "none"            -> nothing actionable (non-commander, or a broadcast/absent
//                        BSSID).
// The CIVILIAN hard floor is the ONLY thing that removes the engage control; a
// NEUTRAL/FRIENDLY match is engageable (the override token is the fire-time gate,
// not a designate-time block). Kept in lock-step with canDesignateUas.
export function noStrikeRowAction(ap, entries, isCommander) {
  const matched = matchedNoStrikeEntry(ap, entries);
  if (matched && matched.category === "CIVILIAN_INFRASTRUCTURE") return "protected";
  if (!isCommander) return "none";
  if (!hasConcreteBssid(ap && ap.bssid)) return "none";
  if (canDesignateUas(ap, { isCommander, isProtected: false })) return "engage";
  return "engage-override";
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

// COMMANDER-OVERRIDE (non-drone) DESIGNATE justification. The backend gate is
// GATED on COMMANDER_OVERRIDE_JUSTIFICATION_REQUIRED, surfaced to the frontend
// on the /wifi-environment survey payload as
// `commander_override_justification_required` (DEFAULT FALSE per operator
// directive — the field is not needed until near field deployment). When the
// flag is ON, a commander-override designate of a NON-DRONE (possibly-civilian)
// AP requires a real, actively-typed justification (>=20 chars, floored
// server-side by _looks_like_real_attestation); when OFF, no justification is
// required and the field is not shown. These pure predicates let the modal
// render/require the field with NO rebuild when the flag flips — the backend
// remains the source of truth for every gate.
export const DESIGNATE_JUSTIFICATION_MIN_LEN = 20;

// Is a justification REQUIRED for this designate? Only for a commander-override
// (non-drone) designation AND only when the backend flag is on.
export function designateJustificationRequired(commanderOverride, flagOn) {
  return commanderOverride === true && flagOn === true;
}

// Is the typed justification minimally valid (long enough, not blank)? Mirrors
// the backend length floor; the backend re-checks and also rejects placeholders.
export function designateJustificationValid(text) {
  return typeof text === "string" && text.trim().length >= DESIGNATE_JUSTIFICATION_MIN_LEN;
}

// AREA-DENIAL "Jam Channel" deep-link helpers. A Wi-Fi channel jam is a
// PHYSICAL area-denial effect -- a barrage at a channel's center frequency
// denies that ENTIRE channel to every device in RF range (the target AP,
// its clients, and any other co-channel device, civilian or own), never a
// single BSSID. These helpers only compute the center MHz to prefill on
// Jamming.jsx; they carry no targeting/gating weight of their own.

// Standard Wi-Fi channel -> 2.4GHz center-frequency (MHz) mapping (channels
// 1-13 are linear 5MHz spacing from 2412 MHz; channel 14 is the Japan-only
// special case at 2484 MHz). For channel numbers above the 2.4GHz range this
// falls back to the standard 5MHz-per-channel-number UNII spacing from a
// 5000 MHz base (covers the common 36-165 5GHz range). Used only as a
// FALLBACK when a survey row has no measured `frequency_ghz` -- see
// apChannelToMhz, which prefers the real Kismet measurement when present.
export function wifiChannelToMhz(channel) {
  const ch = Number(channel);
  if (!Number.isFinite(ch) || ch < 1) return null;
  if (ch === 14) return 2484;
  if (ch <= 13) return 2412 + (ch - 1) * 5;
  return 5000 + ch * 5;
}

// Best available AREA-DENIAL jam center-frequency (MHz) for a survey row.
// Prefers Kismet's own measured `frequency_ghz` (a real reading, and correct
// for 5GHz channels too); falls back to the standard channel->MHz table
// (wifiChannelToMhz) when only `channel` is known. Returns null when neither
// is available (the row is not jam-deep-link-eligible).
export function apChannelToMhz(ap) {
  if (!ap) return null;
  const ghz = Number(ap.frequency_ghz);
  if (Number.isFinite(ghz) && ghz > 0) return Math.round(ghz * 1000);
  return wifiChannelToMhz(ap.channel);
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
