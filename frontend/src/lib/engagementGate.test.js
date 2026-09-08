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

const { deriveTxChips } = require("./engagementGate");

function sikChip(health) {
  return deriveTxChips(health).find((c) => c.key === "sik-link");
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
