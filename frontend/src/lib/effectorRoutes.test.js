// Locks the honest GNSS effector split (DecisionSupport.jsx Item C):
// gnss_deny_jam (GNSS-band area-denial JAM) and gnss_spoof (V1 spoof) are two
// DISTINCT effectors with distinct feasibility-grid labels and distinct
// deep-links into their own gated engagement pages. A regression that merges
// them back into one cell/route, or crosses their routes, must fail this test.
//
// Runs under bare jest (react-scripts / craco test). effectorRoutes.js is a
// pure data module (no api/context/router dependency), so no mocking is
// needed beyond letting jest resolve the real lucide-react icon imports.
const { EFFECTOR_ROUTE, FEASIBILITY_LABELS } = require("./effectorRoutes");

describe("GNSS effector split stays honest and distinct (Item C)", () => {
  test("gnss_deny_jam routes to /jamming as an area-denial JAM engage label", () => {
    expect(EFFECTOR_ROUTE.gnss_deny_jam).toMatchObject({
      to: "/jamming",
      label: "GNSS-BAND JAM (AREA DENIAL)",
    });
  });

  test("gnss_spoof routes to /gnss-spoof as its own engage label", () => {
    expect(EFFECTOR_ROUTE.gnss_spoof).toMatchObject({
      to: "/gnss-spoof",
      label: "GNSS SPOOF",
    });
  });

  test("the two GNSS routes are distinct effectors, never merged", () => {
    expect(EFFECTOR_ROUTE.gnss_deny_jam.to).not.toBe(EFFECTOR_ROUTE.gnss_spoof.to);
    expect(EFFECTOR_ROUTE.gnss_deny_jam).not.toBe(EFFECTOR_ROUTE.gnss_spoof);
  });

  test("feasibility grid shows GNSS DENY (JAM) for gnss_deny_jam", () => {
    expect(FEASIBILITY_LABELS.gnss_deny_jam).toBe("GNSS DENY (JAM)");
  });

  test("feasibility grid shows GNSS SPOOF (V1) for gnss_spoof (flags V1 placeholder scoring)", () => {
    expect(FEASIBILITY_LABELS.gnss_spoof).toBe("GNSS SPOOF (V1)");
  });

  test("both GNSS cells have their own key -- neither is silently dropped", () => {
    expect(Object.keys(FEASIBILITY_LABELS)).toEqual(
      expect.arrayContaining(["gnss_deny_jam", "gnss_spoof"])
    );
    expect(Object.keys(EFFECTOR_ROUTE)).toEqual(
      expect.arrayContaining(["gnss_deny_jam", "gnss_spoof"])
    );
  });
});
