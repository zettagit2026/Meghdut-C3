// Tests for the P2 no-strike classification render-helpers
// (isProtectedContact / isTargetGrade) in detectionConfidence.js. These decide
// which lane a contact renders in on the Dashboard priority board:
//   * PROTECTED  -> off priority, into the "Protected / Civilian" lane
//   * NOT target-grade (and not protected/conflict) -> "Low-confidence RF" lane
//   * a no_strike_conflict decode -> STAYS on priority (never protected)
//
// Runs under bare jest (react-scripts) -- the helpers import nothing.

const { isProtectedContact, isTargetGrade } = require("./detectionConfidence");

describe("isProtectedContact", () => {
  test("civilian no_strike match is protected", () => {
    expect(isProtectedContact({ no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" } })).toBe(true);
  });
  test("NON_THREAT sentinel is protected", () => {
    expect(isProtectedContact({ threat_level: "NON_THREAT" })).toBe(true);
  });
  test("friendly registry is protected", () => {
    expect(isProtectedContact({ threat_level: "FRIENDLY (registry)" })).toBe(true);
  });
  test("a decoded-drone CONFLICT is NOT protected (stays on priority)", () => {
    expect(isProtectedContact({
      no_strike_conflict: { registry_entry_id: "civ-oui" },
      protocol_confirmed: true, threat_level: "HIGH",
    })).toBe(false);
  });
  test("a plain hostile contact is not protected", () => {
    expect(isProtectedContact({ threat_level: "HIGH", protocol_confirmed: true })).toBe(false);
  });
  test("null-safe", () => {
    expect(isProtectedContact(null)).toBe(false);
  });
});

describe("isTargetGrade", () => {
  test("prefers the backend target_grade stamp", () => {
    expect(isTargetGrade({ target_grade: true })).toBe(true);
    expect(isTargetGrade({ target_grade: false, protocol_confirmed: true })).toBe(false);
  });
  test("T1 protocol decode", () => {
    expect(isTargetGrade({ protocol_confirmed: true })).toBe(true);
    expect(isTargetGrade({ confidence_type: "protocol_verified" })).toBe(true);
  });
  test("T2 real DF bearing only (not estimated)", () => {
    expect(isTargetGrade({ bearing_available: true, bearing_estimated: false })).toBe(true);
    expect(isTargetGrade({ bearing_available: true, bearing_estimated: true })).toBe(false);
  });
  test("T4 multidomain fusion", () => {
    expect(isTargetGrade({ confidence_type: "multidomain_fused" })).toBe(true);
  });
  test("T3 wifi drone softAP non-civilian", () => {
    expect(isTargetGrade({ match_protocol: "wifi", make_candidate: "DJI (candidate)" })).toBe(true);
    expect(isTargetGrade({
      match_protocol: "wifi", make_candidate: "x",
      no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" },
    })).toBe(false);
  });
  test("bare heuristic no-DF is not target-grade", () => {
    expect(isTargetGrade({ confidence_type: "heuristic_binary", bearing_available: false })).toBe(false);
    expect(isTargetGrade({})).toBe(false);
    expect(isTargetGrade(null)).toBe(false);
  });
});

// The priority-board partition the Dashboard applies, verified end-to-end on a
// small mixed fixture: protected + low-confidence pulled off, conflict kept on.
describe("priority board partition", () => {
  const active = [
    { id: "conf", protocol_confirmed: true, threat_level: "HIGH", no_strike_conflict: { registry_entry_id: "civ" } },
    { id: "df", bearing_available: true, bearing_estimated: false, threat_level: "HIGH" },
    { id: "civ", no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" }, threat_level: "NON_THREAT" },
    { id: "friend", threat_level: "FRIENDLY (registry)", no_strike: { matched: true, category: "FRIENDLY_OWN_FORCE" } },
    { id: "lowrf", confidence_type: "heuristic_binary", bearing_available: false, threat_level: "LOW" },
  ];
  const isProtectedOrNonThreat = (d) => isProtectedContact(d) || d.threat_level === "NON_THREAT";
  const priority = active.filter((d) => !isProtectedOrNonThreat(d) && (isTargetGrade(d) || d.no_strike_conflict));
  const protectedLane = active.filter((d) => isProtectedOrNonThreat(d));
  const lowConf = active.filter((d) => !isProtectedOrNonThreat(d) && !isTargetGrade(d) && !d.no_strike_conflict);

  test("priority keeps only target-grade + the conflict decode", () => {
    expect(priority.map((d) => d.id).sort()).toEqual(["conf", "df"]);
  });
  test("protected lane holds civilian + friendly", () => {
    expect(protectedLane.map((d) => d.id).sort()).toEqual(["civ", "friend"]);
  });
  test("low-confidence lane holds the bare heuristic emitter", () => {
    expect(lowConf.map((d) => d.id)).toEqual(["lowrf"]);
  });
  test("partition is exhaustive and disjoint", () => {
    const all = [...priority, ...protectedLane, ...lowConf].map((d) => d.id).sort();
    expect(all).toEqual(["civ", "conf", "df", "friend", "lowrf"]);
  });
});
