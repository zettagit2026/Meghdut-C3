// Regression test for the "SafetyGate checklist ticks can't stay ticked"
// bug (checkboxes reset ~every second, blocking arming).
//
// ROOT CAUSE: the tick-reset useEffect keyed on `[open, checks]`, and every
// caller (Jamming.jsx, GnssSpoof.jsx, WifiDefeat.jsx, ...) builds `checks`
// as a NEW array literal on every render. Those host pages re-render often
// (e.g. the range-auth "expires in MM:SS" countdown ticks every second), so
// `checks` got a new reference every second even though its TEXT never
// changed -- the effect fired, wiping the operator's ticks back to all-false
// roughly once a second.
//
// FIX: the effect now keys on a stable content signature (`checksKey =
// checks.join("␞")`) instead of the raw array reference, so it only
// resets on gate-open or a genuine checklist-text change.
//
// Style note: like the rest of this suite (see Jamming.test.js), this is a
// pure-function/static-scan test -- no @testing-library/DOM rendering.

const fs = require("fs");
const path = require("path");

// Mirrors the exact signature construction added to SafetyGate.jsx so the
// "same content -> same key" / "different content -> different key"
// contract is verified independently of the source scan below.
const checksKey = (checks) => checks.join("␞");

describe("SafetyGate checklist content signature (checksKey)", () => {
  test("two arrays with identical content produce an EQUAL key even as different array instances", () => {
    const a = ["Step one.", "Step two.", "Step three."];
    const b = ["Step one.", "Step two.", "Step three."]; // new literal, same text
    expect(a).not.toBe(b); // different references...
    expect(checksKey(a)).toBe(checksKey(b)); // ...but the effect won't refire
  });

  test("changing any item's text produces a DIFFERENT key", () => {
    const a = ["Step one.", "Step two.", "Step three."];
    const b = ["Step one.", "Step TWO (changed).", "Step three."];
    expect(checksKey(a)).not.toBe(checksKey(b));
  });

  test("adding or removing an item produces a DIFFERENT key", () => {
    const a = ["Step one.", "Step two."];
    const b = ["Step one.", "Step two.", "Step three."];
    expect(checksKey(a)).not.toBe(checksKey(b));
  });

  test("reordering items produces a DIFFERENT key (order matters)", () => {
    const a = ["Step one.", "Step two."];
    const b = ["Step two.", "Step one."];
    expect(checksKey(a)).not.toBe(checksKey(b));
  });

  test("an empty checklist has a stable, deterministic key", () => {
    expect(checksKey([])).toBe(checksKey([]));
  });
});

describe("SafetyGate.jsx: reset effect depends on checklist CONTENT, not the array reference", () => {
  const src = fs.readFileSync(
    path.join(__dirname, "SafetyGate.jsx"),
    "utf8"
  );

  test("computes a checksKey content signature from `checks`", () => {
    expect(src).toMatch(/const\s+checksKey\s*=\s*checks\.join\(/);
  });

  test("the tick-reset useEffect's dependency array is [open, checksKey] -- NOT [open, checks]", () => {
    // Find the useEffect that resets ticks (it calls setTicks(checks.map(...)))
    const effectMatch = src.match(
      /useEffect\(\(\) => \{[\s\S]*?setTicks\(checks\.map\(\(\) => false\)\);[\s\S]*?\}, \[([^\]]*)\]\);/
    );
    expect(effectMatch).not.toBeNull();
    const depsRaw = effectMatch[1];
    // Split on commas, trim whitespace/comments
    const deps = depsRaw.split(",").map((d) => d.trim());
    expect(deps).toContain("open");
    expect(deps).toContain("checksKey");
    // The raw `checks` reference must NOT be a dependency of this effect --
    // that's exactly the regression this test guards against.
    expect(deps).not.toContain("checks");
  });

  test("the checkbox render itself is untouched -- still reads ticks[i] / toggles via setTicks", () => {
    expect(src).toContain("checked={ticks[i]}");
    expect(src).toMatch(/const nt = \[\.\.\.ticks\]; nt\[i\] = e\.target\.checked; setTicks\(nt\);/);
  });

  test("allTicked / canFire logic is untouched", () => {
    expect(src).toContain("const allTicked = ticks.every(Boolean);");
    expect(src).toContain("const canFire = allTicked && fratricideReady;");
  });
});
