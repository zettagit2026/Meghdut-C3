// COMMANDER NO-STRIKE OVERRIDE (frontend) — pure predicate tests + a static
// guarantee that WifiDefeat.jsx's override modal REQUIRES a justification, mints
// the token via the override endpoint, and carries no_strike_override into the
// deploy body exactly as iff_friendly_fire_ack is carried.
//
// Runs under bare jest — the predicates import nothing, and the modal wiring
// guarantee is a static source scan (no RTL dependency needed).

const fs = require("fs");
const path = require("path");
const {
  noStrikeOverrideNeeded, noStrikeOverrideCategory, noStrikeOverrideJustificationValid,
  MIN_NO_STRIKE_OVERRIDE_JUSTIFICATION_LEN, OVERRIDABLE_NO_STRIKE_CATEGORIES,
} = require("./noStrikeOverride");

describe("noStrikeOverrideNeeded — only OVERRIDABLE categories open the modal", () => {
  test("NEUTRAL and FRIENDLY_OWN_FORCE need an override", () => {
    expect(noStrikeOverrideNeeded({ no_strike: { matched: true, category: "NEUTRAL" } })).toBe(true);
    expect(noStrikeOverrideNeeded({ no_strike: { matched: true, category: "FRIENDLY_OWN_FORCE" } })).toBe(true);
  });

  test("CIVILIAN_INFRASTRUCTURE is the HARD FLOOR — never offered an override", () => {
    expect(noStrikeOverrideNeeded(
      { no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" } })).toBe(false);
    expect(OVERRIDABLE_NO_STRIKE_CATEGORIES).not.toContain("CIVILIAN_INFRASTRUCTURE");
  });

  test("an unmatched / stampless contact needs no override", () => {
    expect(noStrikeOverrideNeeded({ no_strike: { matched: false, category: "NEUTRAL" } })).toBe(false);
    expect(noStrikeOverrideNeeded({})).toBe(false);
    expect(noStrikeOverrideNeeded(null)).toBe(false);
  });

  test("noStrikeOverrideCategory returns the overridable category or null", () => {
    expect(noStrikeOverrideCategory({ no_strike: { matched: true, category: "NEUTRAL" } })).toBe("NEUTRAL");
    expect(noStrikeOverrideCategory(
      { no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" } })).toBeNull();
  });
});

describe("noStrikeOverrideJustificationValid — a real, long-enough justification", () => {
  test("rejects blank / too-short", () => {
    expect(noStrikeOverrideJustificationValid("")).toBe(false);
    expect(noStrikeOverrideJustificationValid("too short")).toBe(false);
    expect(noStrikeOverrideJustificationValid("   ")).toBe(false);
    expect(noStrikeOverrideJustificationValid(null)).toBe(false);
  });

  test("accepts a justification at/above the minimum length", () => {
    const ok = "x".repeat(MIN_NO_STRIKE_OVERRIDE_JUSTIFICATION_LEN);
    expect(noStrikeOverrideJustificationValid(ok)).toBe(true);
    expect(noStrikeOverrideJustificationValid(
      "Positive visual ID of a hostile relay.")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Static guarantee on WifiDefeat.jsx: the override modal mints via the override
// endpoint, gates its confirm on a valid justification, and carries the token.
// ---------------------------------------------------------------------------
describe("WifiDefeat.jsx override modal wiring (static scan)", () => {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "pages", "WifiDefeat.jsx"), "utf8");

  test("mints the override token via the no-strike-override endpoint", () => {
    expect(src).toMatch(/api\.post\(`\/detections\/\$\{target\}\/no-strike-override`/);
  });

  test("the confirm button is disabled until the justification is valid", () => {
    expect(src).toMatch(/disabled=\{[^}]*noStrikeOverrideJustificationValid\(overrideJustification\)/);
  });

  test("carries no_strike_override into the wifi-defeat deploy body", () => {
    expect(src).toMatch(/no_strike_override:\s*noStrikeOverride/);
  });

  test("opens the override modal only when noStrikeOverrideNeeded is true", () => {
    expect(src).toMatch(/noStrikeOverrideNeeded\(selectedDet\)/);
  });
});
