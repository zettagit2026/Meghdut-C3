// Tests for the CUSTOM CENTER-FREQUENCY / AREA-DENIAL addition to the RF
// barrage Jam page: a pure payload-builder test (freq_mhz carried through
// the exact same governed arm -> /jam/confirm -> /payloads/jam flow) plus a
// static source scan confirming the AREA-DENIAL caution copy is present and
// honest (a Wi-Fi channel jam denies the WHOLE channel to every device in
// range, never a single AP/BSSID).
//
// Runs under bare jest (react-scripts / craco test). buildJamPayload is a
// pure, exported module-level function -- requiring Jamming.jsx only
// DEFINES the page component, it never RENDERS it (the component function
// body only runs when React calls it), so no RTL/DOM rendering is needed.
// This project's jest config has no "@/..." alias resolution and this repo's
// installed react-router-dom (v7, exports-only) isn't resolvable under this
// jest's CJS resolver either -- both are pre-existing environment gaps, not
// something introduced by this change (every other page-level test in this
// suite only static-scans its page's source for the same reason; see
// lib/wifiEnvironment.test.js). Virtual-mocking just those unresolvable
// specifiers (a standard, contained jest technique, scoped to this test file
// only) lets buildJamPayload be imported and unit-tested for real, exactly
// like a lib/pure-function test, instead of only regex-scanned.

const fs = require("fs");
const path = require("path");

jest.mock("react-router-dom", () => ({
  useSearchParams: () => [new URLSearchParams()],
}), { virtual: true });
jest.mock("@/lib/api", () => ({
  api: { get: jest.fn(), post: jest.fn() },
  formatApiError: () => "",
}), { virtual: true });
jest.mock("@/components/SafetyGate", () => ({
  __esModule: true,
  default: () => null,
  JAM_CHECKS: ["stub check"],
}), { virtual: true });
jest.mock("@/components/RangeAuthorizationControl", () => ({
  __esModule: true,
  default: () => null,
}), { virtual: true });
jest.mock("@/components/EmergencyAbort", () => ({
  __esModule: true,
  default: () => null,
}), { virtual: true });
jest.mock("@/context/AuthContext", () => ({
  useAuth: () => ({ user: { role: "commander" } }),
}), { virtual: true });
jest.mock("@/lib/engageFix", () => ({
  handleEngageBlock: () => false,
}), { virtual: true });

const { buildJamPayload } = require("./Jamming");

const BASE_ARGS = {
  sweep: false,
  band: "915",
  customFreqMhz: "",
  durationS: 5,
  continuous: false,
  freqStartMhz: 2400,
  freqStopMhz: 2483.5,
  bandwidthKhz: 500,
  txGain: 20,
  jamMode: "meghdut",
  profile: "max",
  armToken: "ARM123",
  jamConfirmToken: "CONFIRM456",
};

describe("buildJamPayload (pure) — custom-frequency AREA-DENIAL jam", () => {
  test("with no custom frequency: sends the band preset, no freq_mhz key at all", () => {
    const body = buildJamPayload(BASE_ARGS);
    expect(body.band).toBe("915");
    expect(body).not.toHaveProperty("freq_mhz");
    expect(body.arm_token).toBe("ARM123");
    expect(body.jam_confirm_token).toBe("CONFIRM456");
  });

  test("with a custom frequency set: carries freq_mhz and omits band (freq_mhz wins)", () => {
    const body = buildJamPayload({ ...BASE_ARGS, customFreqMhz: "2462" });
    expect(body.freq_mhz).toBe(2462);
    expect(body.band).toBeUndefined();
  });

  test("custom frequency is coerced to a Number", () => {
    const body = buildJamPayload({ ...BASE_ARGS, customFreqMhz: "5745.5" });
    expect(body.freq_mhz).toBe(5745.5);
    expect(typeof body.freq_mhz).toBe("number");
  });

  test("blank / zero / negative custom frequency is treated as unset — falls back to band", () => {
    for (const v of ["", "0", "-100", null, undefined]) {
      const body = buildJamPayload({ ...BASE_ARGS, customFreqMhz: v });
      expect(body).not.toHaveProperty("freq_mhz");
      expect(body.band).toBe("915");
    }
  });

  test("sweep still wins over a stray custom frequency (band and freq_mhz both omitted, sweep range sent)", () => {
    const body = buildJamPayload({
      ...BASE_ARGS, sweep: true, customFreqMhz: "2462",
      freqStartMhz: 2400, freqStopMhz: 2483.5,
    });
    expect(body.band).toBeUndefined();
    expect(body.sweep).toBe(true);
    expect(body.freq_start_mhz).toBe(2400);
    expect(body.freq_stop_mhz).toBe(2483.5);
    // Same precedent as the pre-existing sweep-omits-band behavior: sweeping
    // sends its own start/stop range, not a single freq_mhz.
    expect(body).not.toHaveProperty("freq_mhz");
  });

  test("every other field passes through unchanged (continuous / duration / gain / mode / profile / tokens)", () => {
    const body = buildJamPayload({
      ...BASE_ARGS, customFreqMhz: "2437", continuous: true, durationS: 12,
      bandwidthKhz: 750, txGain: 30, jamMode: "operator", profile: "flat",
    });
    expect(body.continuous).toBe(true);
    expect(body.duration_s).toBe(12);
    expect(body.bandwidth_khz).toBe(750);
    expect(body.tx_gain).toBe(30);
    expect(body.jam_mode).toBe("operator");
    expect(body.profile).toBe("flat");
  });
});

describe("Jamming page: custom-frequency AREA-DENIAL affordance is honestly labeled", () => {
  const pageSrc = fs.readFileSync(path.join(__dirname, "Jamming.jsx"), "utf8");
  const low = pageSrc.toLowerCase();

  test("has a custom center-frequency input alongside the Band dropdown", () => {
    expect(pageSrc).toContain('data-testid="jam-custom-freq-input"');
    expect(pageSrc).toContain('data-testid="jam-band-select"');
  });

  test("the request still flows through the existing arm -> /jam/confirm -> /payloads/jam calls, unchanged", () => {
    expect(pageSrc).toContain('api.post("/arm", { effect: "jam" })');
    expect(pageSrc).toContain('api.post("/jam/confirm")');
    expect(pageSrc).toContain('api.post("/payloads/jam"');
  });

  test("copy honestly frames a custom frequency as AREA DENIAL of the whole channel, never per-BSSID/targeted", () => {
    expect(low).toContain("area denial");
    expect(low).toContain("entire channel");
    expect(low).toMatch(/all devices in (rf )?range/);
    expect(low).toContain("civilian");
    expect(low).toContain("own networks");
    expect(low).toMatch(/not a per-bssid|never a per-bssid|not.{0,20}targeted strike/);
    expect(low).toMatch(/range[- ]authorization/);
  });

  test("the AREA-DENIAL caution banner renders only when a custom frequency is set (mirrors the GNSS caution pattern)", () => {
    expect(pageSrc).toMatch(/hasCustomFreq\s*&&\s*\(/);
    expect(pageSrc).toContain('data-testid="jam-area-denial-caution"');
  });

  test("the SafetyGate checklist gets an extra AREA-DENIAL item when a custom frequency is set", () => {
    expect(pageSrc).toMatch(/\.\.\.\(hasCustomFreq \? \[/);
    expect(pageSrc).toMatch(/AREA DENIAL — CUSTOM FREQUENCY/);
  });

  test("Operator Jam mode (band-fixed server-side) hides/clears the custom-frequency input", () => {
    expect(pageSrc).toMatch(/isOperatorMode && customFreqMhz/);
    expect(pageSrc).toMatch(/!sweep && !isOperatorMode/);
  });

  test("the custom-frequency prefill reads the ?freq= query param (WifiEnvironment.jsx deep-link)", () => {
    expect(pageSrc).toContain("useSearchParams");
    expect(pageSrc).toContain('searchParams.get("freq")');
  });
});
