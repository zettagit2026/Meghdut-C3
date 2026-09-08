import { Radio, Satellite, RadioTower, Wifi } from "lucide-react";

// Pure data, extracted out of DecisionSupport.jsx (RFI 4.5.3/4.5.4/4.5.6/4.5.7)
// so the honest GNSS effector split (Item C: gnss_deny_jam vs gnss_spoof are
// two DISTINCT effectors, each deep-linking to its own gated engagement page)
// can be locked with a focused unit test without rendering the live authed
// /decision page (weapons console).
//
// The ONLY places a commander can actually act on a recommendation — the
// pre-existing gated engagement pages. Route paths must match App.js.
// `to` is normally a static path; wifi_deauth/arsdk_inject use a function so
// the deep-link carries ?contact=<id> straight to the pre-filled target chip
// on WifiDefeat.jsx (mirrors WifiDefeat.jsx's own ?contact= reader).
export const EFFECTOR_ROUTE = {
  jam: { to: "/jamming", label: "RF BARRAGE JAM", icon: Radio },
  gnss_deny_jam: { to: "/jamming", label: "GNSS-BAND JAM (AREA DENIAL)", icon: Satellite },
  gnss_spoof: { to: "/gnss-spoof", label: "GNSS SPOOF", icon: Satellite },
  mavlink_takeover: { to: "/takeover", label: "MAVLINK TAKEOVER", icon: RadioTower },
  wifi_deauth: { to: (id) => `/wifi-defeat?contact=${id}`, label: "WI-FI DEFEAT", icon: Wifi },
  arsdk_inject: { to: (id) => `/wifi-defeat?contact=${id}`, label: "WI-FI DEFEAT", icon: Wifi },
};

// FeasibilityCell's per-effector grid-cell label (distinct from EFFECTOR_ROUTE's
// engage-button label above — e.g. gnss_spoof shows "GNSS SPOOF (V1)" here to
// flag the V1 placeholder scoring, while the engage button just says "GNSS SPOOF").
export const FEASIBILITY_LABELS = {
  jam: "JAM",
  gnss_deny_jam: "GNSS DENY (JAM)",
  gnss_spoof: "GNSS SPOOF (V1)",
  mavlink_takeover: "TAKEOVER",
  wifi_deauth: "WIFI-DEAUTH",
  arsdk_inject: "WIFI-INJECT",
};
