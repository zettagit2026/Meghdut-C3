// Tests for getMlClassProbNote() in detectionConfidence.js -- the honesty
// gate MlClassifierBadge.jsx uses so a demoted / non-target-grade contact's
// ML "%" is framed as a raw class probability ("ML class probability (no
// reject class): 96%"), never as a bare drone-likelihood ("ML: drone
// (96%)") next to an unidentified or protected contact. A target-grade
// contact keeps the normal reading (null note -> component falls through to
// its plain "ML: <label> (<pct>%)" branch).
//
// Runs under bare jest (react-scripts) -- the helper imports nothing.

const { getMlClassProbNote } = require("./detectionConfidence");

describe("getMlClassProbNote", () => {
  test("target-grade contact (protocol decode) gets no honesty override", () => {
    expect(getMlClassProbNote({
      protocol_confirmed: true, ml_label: "drone", ml_confidence: 0.96,
    })).toBeNull();
  });

  test("target-grade contact via backend target_grade stamp gets no override", () => {
    expect(getMlClassProbNote({
      target_grade: true, ml_label: "drone", ml_confidence: 0.9,
    })).toBeNull();
  });

  test("non-target-grade contact is framed as class-probability, using the backend's own note", () => {
    expect(getMlClassProbNote({
      target_grade: false,
      ml_label: "drone",
      ml_confidence: 0.96,
      ml_probability_note: "ML class probability (no reject class)",
    })).toBe("ML class probability (no reject class)");
  });

  test("non-target-grade contact with no backend note falls back to the identical honest wording", () => {
    // e.g. an older record, or the demoted-civilian/no-strike-protected
    // branch, which stamps target_grade:false but not ml_probability_note.
    expect(getMlClassProbNote({
      confidence_type: "heuristic_binary",
      bearing_available: false,
      ml_label: "drone",
      ml_confidence: 0.8,
    })).toBe("ML class probability (no reject class)");
  });

  test("demoted civilian/protected contact (NON_THREAT) is also framed honestly", () => {
    expect(getMlClassProbNote({
      threat_level: "NON_THREAT",
      no_strike: { matched: true, category: "CIVILIAN_INFRASTRUCTURE" },
      ml_label: "drone",
      ml_confidence: 0.91,
    })).toBe("ML class probability (no reject class)");
  });

  test("null-safe", () => {
    expect(getMlClassProbNote(null)).toBeNull();
  });
});
