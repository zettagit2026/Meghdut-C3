// Tests for the WI-FI ENVIRONMENT panel logic + a static guarantee that the
// page is PROTECT-ONLY: no transmit/engage/deauth/arm call anywhere, and the
// one write it is allowed to make is POST /api/no-strike (adding a row to the
// existing P1 civilian-protection registry) -- a defensive action, never a
// targeting one.
//
// Runs under bare jest (react-scripts) -- the pure helpers import nothing, and
// the read-only/protect-only guarantee is a static source scan (no RTL
// dependency needed).

const fs = require("fs");
const path = require("path");
const {
  isPossibleUas, droneOuiVendor, ssidLooksLikeDrone, filterAps, sortAps,
  encryptionLabel, pmfLabel, lastSeenLabel, canDesignateUas, hasConcreteBssid,
} = require("./wifiEnvironment");

// A small fixture in the exact survey shape the backend returns.
const FIXTURE = [
  {
    bssid: "AC:DE:48:00:11:22", ssid: "OfficeNet", channel: "36",
    rssi_dbm: -47, vendor: "Cisco Systems, Inc", encryption: "WPA2-PSK-AES",
    pmf_required: false, pmf_supported: true, client_count: 3,
    last_seen: 1700000600, possible_uas: false,
  },
  {
    bssid: "00:11:22:33:44:55", ssid: "GuestWiFi", channel: "6",
    rssi_dbm: -70, vendor: "Netgear", encryption: "Open",
    pmf_required: null, pmf_supported: null, client_count: 0,
    last_seen: 1700000700, possible_uas: false,
  },
  {
    bssid: "60:60:1F:AA:BB:CC", ssid: null, channel: "149",
    rssi_dbm: -55, vendor: "Dji Innovations", encryption: null,
    client_count: null, last_seen: 1700000800,
    possible_uas: true, possible_uas_reason: "OUI 60:60:1F -> DJI",
  },
];

describe("possible-UAS tagging (non-actionable)", () => {
  test("drone OUI is detected", () => {
    expect(droneOuiVendor("60:60:1F:AA:BB:CC")).toBe("DJI");
    expect(droneOuiVendor("AC:DE:48:00:11:22")).toBeNull();
  });
  test("drone SSID pattern is detected", () => {
    expect(ssidLooksLikeDrone("Mavic-Air-1234")).toBe(true);
    expect(ssidLooksLikeDrone("OfficeNet")).toBe(false);
  });
  test("isPossibleUas honours both OUI and SSID and the backend flag", () => {
    expect(isPossibleUas(FIXTURE[2])).toBe(true); // DJI OUI
    expect(isPossibleUas(FIXTURE[0])).toBe(false);
    expect(isPossibleUas({ bssid: "x", ssid: "tello-abc" })).toBe(true);
  });
});

describe("labels", () => {
  test("encryptionLabel normalises unknown", () => {
    expect(encryptionLabel(FIXTURE[0])).toBe("WPA2-PSK-AES");
    expect(encryptionLabel(FIXTURE[2])).toBe("Unknown");
  });
  test("pmfLabel maps the flags", () => {
    expect(pmfLabel(FIXTURE[0])).toBe("Supported");
    expect(pmfLabel({ pmf_required: true })).toBe("Required");
    expect(pmfLabel(FIXTURE[1])).toBe("—");
  });
  test("lastSeenLabel is relative and deterministic", () => {
    expect(lastSeenLabel({ last_seen: 1000 }, 1030)).toBe("30s ago");
    expect(lastSeenLabel({ last_seen: 1000 }, 1000 + 120)).toBe("2m ago");
    expect(lastSeenLabel({}, 2000)).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// DESIGNATE gating predicate: the "Engage as suspected UAS" control is PRESENT
// only on a possible-UAS commander row with a concrete non-broadcast BSSID, and
// ABSENT on civilian / no-drone-indicator / protected / non-commander rows.
// ---------------------------------------------------------------------------
describe("designate gating predicate (canDesignateUas)", () => {
  const uasRow = FIXTURE[2]; // DJI OUI 60:60:1F, possible_uas true
  const civilianRow = FIXTURE[0]; // OfficeNet — not a possible UAS

  test("hasConcreteBssid rejects blank/absent/broadcast, accepts a real BSSID", () => {
    expect(hasConcreteBssid("60:60:1F:AA:BB:CC")).toBe(true);
    expect(hasConcreteBssid("")).toBe(false);
    expect(hasConcreteBssid(null)).toBe(false);
    expect(hasConcreteBssid("FF:FF:FF:FF:FF:FF")).toBe(false);
  });

  test("PRESENT only for a possible-UAS commander row with a concrete BSSID", () => {
    expect(canDesignateUas(uasRow, { isCommander: true, isProtected: false })).toBe(true);
  });

  test("ABSENT for a non-commander", () => {
    expect(canDesignateUas(uasRow, { isCommander: false, isProtected: false })).toBe(false);
  });

  test("ABSENT for a civilian-protected row", () => {
    expect(canDesignateUas(uasRow, { isCommander: true, isProtected: true })).toBe(false);
  });

  test("ABSENT for a civilian / no-drone-indicator row", () => {
    expect(canDesignateUas(civilianRow, { isCommander: true, isProtected: false })).toBe(false);
  });

  test("ABSENT without a concrete (non-broadcast) BSSID", () => {
    expect(canDesignateUas({ possible_uas: true, bssid: "" }, { isCommander: true })).toBe(false);
    expect(canDesignateUas({ possible_uas: true, bssid: "FF:FF:FF:FF:FF:FF" }, { isCommander: true })).toBe(false);
  });
});

describe("filter + sort (drives what the table renders)", () => {
  test("filter matches SSID/BSSID/vendor/channel", () => {
    expect(filterAps(FIXTURE, "office").map((a) => a.bssid)).toEqual(["AC:DE:48:00:11:22"]);
    expect(filterAps(FIXTURE, "dji").length).toBe(1);
    expect(filterAps(FIXTURE, "149")[0].bssid).toBe("60:60:1F:AA:BB:CC");
    expect(filterAps(FIXTURE, "").length).toBe(3);
  });
  test("sort by RSSI desc puts strongest first, nulls last", () => {
    const rows = sortAps(FIXTURE, "rssi_dbm", "desc");
    expect(rows.map((r) => r.rssi_dbm)).toEqual([-47, -55, -70]);
  });
  test("sort by ssid asc is alphabetical with nulls last", () => {
    const rows = sortAps(FIXTURE, "ssid", "asc");
    expect(rows[0].ssid).toBe("GuestWiFi");
    expect(rows[rows.length - 1].ssid).toBeNull();
  });

  // Regression: Kismet reports rssi_dbm = 0 for devices it is tracking but
  // has no signal measurement for (passive/BT-side or otherwise-unheard
  // devices -- the vast majority of a real survey). 0 is numerically greater
  // than every real dBm reading (-30 .. -90), so a naive descending sort
  // buries every real AP under a wall of 0-signal noise. Mirrors the
  // backend's kismet_survey.py `_sort_key` semantics.
  const MIXED_SIGNAL_FIXTURE = [
    { bssid: "AA:00:00:00:00:01", ssid: "NoSignal-1", rssi_dbm: 0, channel: "1" },
    { bssid: "AA:00:00:00:00:02", ssid: "WeakOffice", rssi_dbm: -82, channel: "6" },
    { bssid: "AA:00:00:00:00:03", ssid: "NoSignal-2", rssi_dbm: null, channel: "11" },
    { bssid: "AA:00:00:00:00:04", ssid: "Zetta-Office", rssi_dbm: -38, channel: "36" },
    { bssid: "AA:00:00:00:00:05", ssid: "NoSignal-3", rssi_dbm: 0, channel: "1" },
    { bssid: "AA:00:00:00:00:06", ssid: "BSNL", rssi_dbm: -52, channel: "44" },
    { bssid: "AA:00:00:00:00:07", ssid: "NoSignal-4", channel: "1" }, // rssi_dbm absent
  ];

  test("sort by RSSI desc: real APs surface above the 0/null no-signal flood", () => {
    const rows = sortAps(MIXED_SIGNAL_FIXTURE, "rssi_dbm", "desc");
    // Strongest real AP first, in strength order, before any no-signal row.
    expect(rows.slice(0, 3).map((r) => r.ssid)).toEqual([
      "Zetta-Office", "BSNL", "WeakOffice",
    ]);
    // All 0 / null / missing rows trail at the bottom, in any relative order.
    const tail = rows.slice(3).map((r) => r.rssi_dbm);
    expect(tail).toHaveLength(4);
    tail.forEach((v) => expect(v === 0 || v === null || v === undefined).toBe(true));
  });

  test("sort by RSSI asc: no-signal rows still rank last, not first", () => {
    const rows = sortAps(MIXED_SIGNAL_FIXTURE, "rssi_dbm", "asc");
    // Weakest real AP first, strongest real AP last among the real readings,
    // but the no-signal sentinel rows never get to masquerade as "weakest"
    // ahead of them, nor "strongest" -- they stay parked at the bottom.
    expect(rows.slice(0, 3).map((r) => r.ssid)).toEqual([
      "WeakOffice", "BSNL", "Zetta-Office",
    ]);
    const tail = rows.slice(3).map((r) => r.rssi_dbm);
    expect(tail).toHaveLength(4);
    tail.forEach((v) => expect(v === 0 || v === null || v === undefined).toBe(true));
  });
});

// ---------------------------------------------------------------------------
// GUARANTEE: the panel itself TRANSMITS NOTHING. It makes exactly two
// commander-gated non-GET calls — PROTECT (POST /no-strike) and DESIGNATE
// (POST /wifi-environment/designate). DESIGNATE only creates a governed contact
// and DEEP-LINKS (a client-side navigate, never an api call) into the existing
// /wifi-defeat SafetyGate flow where the deliberate arm/fire lives. The page
// must never call a fire/transmit endpoint directly. Scan its source.
// ---------------------------------------------------------------------------
describe("WifiEnvironment page: protect + designate, but transmits nothing itself", () => {
  const pageSrc = fs.readFileSync(
    path.join(__dirname, "..", "pages", "WifiEnvironment.jsx"), "utf8"
  );

  // Every api.<verb>("<path>"...) call in the page: method + endpoint path.
  const apiCalls = [...pageSrc.matchAll(
    /api\.(get|post|put|patch|delete)\s*\(\s*["'`]([^"'`]+)["'`]/gi
  )].map((m) => ({ method: m[1].toLowerCase(), path: m[2] }));

  test("renders honest situational-awareness copy incl. the commander-designate caveat", () => {
    const low = pageSrc.toLowerCase();
    expect(low).toContain("situational awareness");
    expect(low).toContain("not a blanket target list");
    // Honest spoofable-candidate framing (no stale "not engageable" absolute).
    expect(low).toContain("candidate, not identification");
    expect(low).not.toContain("not engageable");
  });

  test("reads the survey endpoint (api.get('/wifi-environment'))", () => {
    expect(pageSrc).toContain('api.get("/wifi-environment")');
  });

  test("makes no PUT/PATCH/DELETE call of any kind", () => {
    expect(pageSrc).not.toMatch(/api\.(put|patch|delete)\s*\(/);
  });

  // Exactly two non-GET writes, both POST, both on the allowed endpoints:
  // the PROTECT registry write and the DESIGNATE contact-promotion write.
  test("every api.* call is a GET, or one of the two allowed POSTs (protect + designate)", () => {
    expect(apiCalls.length).toBeGreaterThan(0);
    const nonGet = apiCalls.filter((c) => c.method !== "get");
    expect(nonGet.map((c) => `${c.method} ${c.path}`).sort()).toEqual([
      "post /no-strike",
      "post /wifi-environment/designate",
    ]);
  });

  test("the DESIGNATE call creates a governed contact only (POST /wifi-environment/designate)", () => {
    expect(pageSrc).toContain('api.post("/wifi-environment/designate"');
  });

  test("both writes are gated on the commander role client-side", () => {
    // PROTECT button disabled for non-commanders; DESIGNATE control only
    // rendered when canDesignateUas(...) — which requires isCommander.
    expect(pageSrc).toMatch(/disabled=\{!isCommander/);
    expect(pageSrc).toMatch(/canDesignateUas\(ap,\s*\{\s*isCommander/);
  });

  test("the panel issues NO api.* call to any fire/transmit endpoint", () => {
    const firePaths = [
      "/engage", "/arm", "/jam", "/deploy", "/broadcast", "/wifi-defeat",
      "/tx/", "/emergency", "/detections/", "/payloads",
    ];
    for (const { path: p } of apiCalls) {
      for (const f of firePaths) {
        expect(p.startsWith(f)).toBe(false);
      }
    }
  });

  test("routes to the governed wifi-defeat flow via a client navigate, never an api call", () => {
    // The deep-link is a react-router navigate to the /wifi-defeat ROUTE...
    expect(pageSrc).toMatch(/navigate\(`\/wifi-defeat\?contact=/);
    // ...and /wifi-defeat is NEVER invoked as an api.<verb>("/wifi-defeat"...).
    expect(pageSrc).not.toMatch(/api\.[a-z]+\s*\(\s*["'`]\/wifi-defeat/i);
  });
});
