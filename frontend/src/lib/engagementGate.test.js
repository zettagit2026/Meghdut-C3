// Tests for deriveTxChips' owner-aware, fail-safe SiK LINK chip (S1). The chip
// label must be HONEST about WHICH liveness mode is up and must NEVER paint UP
// without genuine evidence surfaced by the backend tx_subsystem block.
//
// Runs under bare jest (react-scripts / craco test): deriveTxChips is a pure
// function over a plain health object. The module imports `@/lib/api` at the top
// (used only by the unrelated classifyEngageBlock helper); the `@` webpack alias
// isn't mapped under bare jest, so virtual-mock it — deriveTxChips itself never
// touches it.
jest.mock("@/lib/api", () => ({ formatApiError: (e) => String(e) }), { virtual: true });

const fs = require("fs");
const path = require("path");
const { deriveTxChips, deriveReadiness, CHIP_GROUP, FIX } = require("./engagementGate");

function sikChip(health) {
  return deriveTxChips(health).find((c) => c.key === "sik-link");
}

function chipsByKey(health) {
  const byKey = {};
  for (const c of deriveTxChips(health)) byKey[c.key] = c;
  return byKey;
}

describe("deriveTxChips SiK LINK chip (owner-aware, fail-safe)", () => {
  test("TX mode (rf-bridge owns SiK, TX consumer connected) -> UP (TX)", () => {
    const chip = sikChip({
      tx_subsystem: { sik_link_up: true, sik_link_mode: "tx", sik_owner: "rf-bridge" },
    });
    expect(chip.label).toBe("SiK LINK UP (TX)");
    expect(chip.tone).toBe("ok");
  });

  test("RX mode (recent sniffer RX) -> UP (RX)", () => {
    const chip = sikChip({
      tx_subsystem: { sik_link_up: true, sik_link_mode: "rx", sik_owner: "sniffer" },
    });
    expect(chip.label).toBe("SiK LINK UP (RX)");
    expect(chip.tone).toBe("ok");
  });

  test("no evidence -> DOWN (no false-UP)", () => {
    const chip = sikChip({
      tx_subsystem: { sik_link_up: false, sik_link_mode: null, sik_owner: null },
    });
    expect(chip.label).toBe("SiK LINK DOWN");
    expect(chip.tone).toBe("warn");
  });

  test("missing tx_subsystem defaults to DOWN (fail-closed)", () => {
    const chip = sikChip({});
    expect(chip.label).toBe("SiK LINK DOWN");
  });
});

// ---------------------------------------------------------------------------
// S2/S3: header-strip redesign — unambiguous chip labels + grouping, and the
// single fire-readiness verdict. See project brief: the old flat strip put
// "TX OFFLINE" (bridges) next to "TX LIVE" (master halt) -- two DIFFERENT
// subsystems wearing the word "TX" -- which read as self-contradictory.
// ---------------------------------------------------------------------------
describe("deriveTxChips: unambiguous, non-contradictory transmit labels (S2)", () => {
  test("bridges-online chip is labeled 'TX BRIDGES: …', never bare 'TX ONLINE/OFFLINE'", () => {
    const onChip = chipsByKey({ tx_subsystem: { bridges_online: true, tx_halted: true } })["tx-online"];
    const offChip = chipsByKey({ tx_subsystem: { bridges_online: false, tx_halted: true } })["tx-online"];
    expect(onChip.label).toBe("TX BRIDGES: ONLINE");
    expect(offChip.label).toBe("TX BRIDGES: OFFLINE");
    // Guard the exact regression: bare "TX ONLINE" / "TX OFFLINE" must be gone.
    expect(onChip.label).not.toBe("TX ONLINE");
    expect(offChip.label).not.toBe("TX OFFLINE");
  });

  test("master-halt chip is labeled 'MASTER-HALT: …', never bare 'TX HALTED/LIVE'", () => {
    const haltedChip = chipsByKey({ tx_subsystem: { tx_halted: true } })["tx-halt"];
    const clearedChip = chipsByKey({ tx_subsystem: { tx_halted: false } })["tx-halt"];
    expect(haltedChip.label).toBe("MASTER-HALT: ENGAGED");
    expect(clearedChip.label).toBe("MASTER-HALT: CLEARED");
    expect(haltedChip.label).not.toBe("TX HALTED");
    expect(clearedChip.label).not.toBe("TX LIVE");
  });

  test("bridges-online and master-halt chips never share a word that could read as opposites", () => {
    // A state where bridges are online AND halt is cleared: the two chips'
    // labels must not both start with the bare token "TX " in a way that
    // makes them look like opposing pairs of the same subsystem.
    const chips = chipsByKey({ tx_subsystem: { bridges_online: true, tx_halted: false } });
    expect(chips["tx-online"].label).toBe("TX BRIDGES: ONLINE");
    expect(chips["tx-halt"].label).toBe("MASTER-HALT: CLEARED");
    // Each label names ITS OWN subsystem before the state word.
    expect(chips["tx-online"].label.split(":")[0]).not.toBe(chips["tx-halt"].label.split(":")[0]);
  });

  test("chips carry a `group` for structural clustering (TRANSMIT / AUTHORIZATION / LINK)", () => {
    const chips = chipsByKey({ tx_subsystem: {} });
    expect(chips["tx-online"].group).toBe(CHIP_GROUP.transmit);
    expect(chips["tx-halt"].group).toBe(CHIP_GROUP.transmit);
    expect(chips["tx-radio"].group).toBe(CHIP_GROUP.transmit);
    expect(chips["range-auth"].group).toBe(CHIP_GROUP.authorization);
    expect(chips["sik-link"].group).toBe(CHIP_GROUP.link);
  });
});

describe("deriveReadiness: single fire-readiness verdict, worst-blocker-first (S3)", () => {
  test("TX halted -> NOT READY, action = RESUME TX (checked first, regardless of other gates)", () => {
    const r = deriveReadiness({
      tx_subsystem: {
        tx_halted: true,
        bridges_online: false, // even with other gates also unmet...
        range_auth: {},
      },
    });
    expect(r.ready).toBe(false);
    expect(r.action).toBe("RESUME TX");
    expect(r.fix).toBe(FIX.resume);
  });

  test("halt cleared but bridges offline -> NOT READY, action = BRING TX ONLINE", () => {
    const r = deriveReadiness({
      tx_subsystem: { tx_halted: false, bridges_online: false, range_auth: {} },
    });
    expect(r.ready).toBe(false);
    expect(r.action).toBe("BRING TX ONLINE");
    expect(r.fix).toBe(FIX.online);
  });

  test("halt cleared, bridges online, no range-auth armed -> NOT READY, action = ARM RANGE-AUTH", () => {
    const r = deriveReadiness({
      tx_subsystem: {
        tx_halted: false,
        bridges_online: true,
        range_auth: { rf_jam: { enabled: false } },
      },
    });
    expect(r.ready).toBe(false);
    expect(r.action).toBe("ARM RANGE-AUTH");
    expect(r.fix).toBe(FIX.rangeAuth);
  });

  test("all preconditions met -> READY TO FIRE (no action, no fix)", () => {
    const r = deriveReadiness({
      tx_subsystem: {
        tx_halted: false,
        bridges_online: true,
        range_auth: { rf_jam: { enabled: true } },
      },
    });
    expect(r.ready).toBe(true);
    expect(r.action).toBeNull();
    expect(r.fix).toBeNull();
  });

  test("missing tx_subsystem fails closed to NOT READY (never a false READY)", () => {
    const r = deriveReadiness({});
    expect(r.ready).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Reuse guard: the header strip and the Engagement Control panel must both
// call engagementGate's derivation rather than forking their own copy of the
// gate-priority logic. Static-scan the consuming source files (same style as
// SafetyGate.test.js / Jamming.test.js in this suite).
// ---------------------------------------------------------------------------
describe("Reuse guard: consumers call engagementGate, they do not reinvent gate logic", () => {
  const componentsDir = path.join(__dirname, "..", "components");
  const headerSrc = fs.readFileSync(path.join(componentsDir, "HeaderStatusStrip.jsx"), "utf8");
  const panelSrc = fs.readFileSync(path.join(componentsDir, "EngagementControl.jsx"), "utf8");
  const layoutSrc = fs.readFileSync(path.join(componentsDir, "Layout.jsx"), "utf8");

  test("HeaderStatusStrip.jsx derives its verdict from engagementGate.deriveReadiness", () => {
    expect(headerSrc).toMatch(/from\s+"@\/lib\/engagementGate"/);
    expect(headerSrc).toContain("deriveReadiness");
    expect(headerSrc).toContain("deriveTxChips");
  });

  test("HeaderStatusStrip.jsx never hardcodes the retired contradictory labels", () => {
    expect(headerSrc).not.toMatch(/"TX ONLINE"|'TX ONLINE'/);
    expect(headerSrc).not.toMatch(/"TX OFFLINE"|'TX OFFLINE'/);
    expect(headerSrc).not.toMatch(/"TX HALTED"|'TX HALTED'/);
    expect(headerSrc).not.toMatch(/"TX LIVE"|'TX LIVE'/);
  });

  test("EngagementControl.jsx computes readiness via engagementGate.deriveReadiness, not an inline copy", () => {
    expect(panelSrc).toContain("deriveReadiness");
    // The retired inline if/else priority chain must be gone.
    expect(panelSrc).not.toMatch(/if\s*\(tx\.tx_halted\)\s*\{[\s\S]*?readiness\s*=/);
  });

  test("Layout.jsx renders the shared HeaderStatusStrip on the persistent header (every page)", () => {
    expect(layoutSrc).toContain("HeaderStatusStrip");
    expect(layoutSrc).toMatch(/<HeaderStatusStrip\s+health=\{health\}\s*\/>/);
  });
});
