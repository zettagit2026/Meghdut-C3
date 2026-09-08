// COMMANDER NO-STRIKE OVERRIDE — client-side predicates for the deliberate
// fire-time override modal (WifiDefeat.jsx). Pure + testable. The backend is the
// source of truth for every gate; these only decide whether to OPEN the modal
// and whether its inputs are minimally well-formed before POSTing.

// Categories whose fire-time floor is OVERRIDABLE (mirrors the backend
// _NO_STRIKE_OVERRIDABLE_CATEGORIES). CIVILIAN_INFRASTRUCTURE is deliberately
// absent: it is the HARD FLOOR — no override exists, and the UI never offers one.
export const OVERRIDABLE_NO_STRIKE_CATEGORIES = ["NEUTRAL", "FRIENDLY_OWN_FORCE"];

// Minimum justification length — mirrors the backend
// MIN_FRIENDLY_ASSET_ATTESTATION_LEN so the client refuses a too-short/trivial
// justification before the round-trip (the backend enforces it again).
export const MIN_NO_STRIKE_OVERRIDE_JUSTIFICATION_LEN = 20;

// Does this detection carry an OVERRIDABLE (NEUTRAL / FRIENDLY_OWN_FORCE)
// no-strike stamp that must be overridden before it can be engaged? A
// CIVILIAN_INFRASTRUCTURE stamp returns false here on purpose — it is never
// overridable, so the modal is never offered for it.
export function noStrikeOverrideNeeded(det) {
  const ns = det && det.no_strike;
  if (!ns || !ns.matched) return false;
  return OVERRIDABLE_NO_STRIKE_CATEGORIES.includes(ns.category);
}

// The overridable category on the detection (or null) — for the modal copy.
export function noStrikeOverrideCategory(det) {
  return noStrikeOverrideNeeded(det) ? det.no_strike.category : null;
}

// Is the typed justification minimally valid (long enough, not blank)? The
// backend applies the same floor plus a trivial-placeholder reject.
export function noStrikeOverrideJustificationValid(text) {
  if (typeof text !== "string") return false;
  return text.trim().length >= MIN_NO_STRIKE_OVERRIDE_JUSTIFICATION_LEN;
}
