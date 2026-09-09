import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Radio, AlertTriangle, ShieldAlert, Infinity as InfinityIcon, Waves, Siren } from "lucide-react";
import SafetyGate, { JAM_CHECKS } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import EmergencyAbort from "@/components/EmergencyAbort";
import { useAuth } from "@/context/AuthContext";
import { handleEngageBlock } from "@/lib/engageFix";

// task #146: /jam/status polling previously swallowed failures in an empty
// catch block, so if polling died (backend restart, network partition, auth
// expiry) the jam session cards would freeze on their last-known status
// (e.g. "AWAITING ACK" / "TRANSMITTING") with zero indication that
// monitoring itself had stopped. Same tracking pattern as
// SystemHealth.jsx (task #144): consecutive-failure count + last-success
// timestamp, ~4x this page's own 2s poll interval for the staleness
// threshold.
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4; // ~8s

const BANDS = [
  { value: "433", label: "433 MHz (SiK ISM lower)" },
  { value: "915", label: "915 MHz (SiK ISM)" },
  { value: "2g4", label: "2.4 GHz (DJI video/control)" },
  // Same shared 2.4-2.4835GHz ISM band as "2g4" above — Bluetooth Classic
  // (79ch) and BLE (40ch) frequency-hop WITHIN that range, they do not
  // occupy separate spectrum. This is a broad-band noise burst, not a
  // hop-following jammer: it denies Bluetooth as a side effect of covering
  // the same ISM band DJI/Wi-Fi already shares, which is why it's listed
  // here as its own explicitly-labeled target rather than new RF logic.
  // Widen the Bandwidth (kHz) field below toward ~83500 kHz for the best
  // chance of covering Bluetooth's full hop range.
  { value: "bt_2g4", label: "Bluetooth (2.4GHz ISM, Classic+BLE — broad-band noise, not hop-following)" },
  { value: "5g8", label: "5.8 GHz (DJI video)" },
  { value: "gps_l1", label: "GPS L1 (1575.42 MHz, GNSS nav-denial)", gnss: true },
  { value: "galileo_e1", label: "Galileo E1 (1575.42 MHz, GNSS nav-denial)", gnss: true },
  { value: "beidou_b1", label: "BeiDou B1 (1561.098 MHz, GNSS nav-denial)", gnss: true },
  { value: "glonass_l1", label: "GLONASS L1 (1602 MHz base, GNSS nav-denial)", gnss: true },
];

// Mirrors field-bridge/hackrf_jam.py's GNSS_BANDS / backend's JAM_GNSS_BANDS —
// used ONLY to decide whether to show the extra GNSS warning copy below and
// add one extra SafetyGate checklist line. Carries no safety-gate weight of
// its own; it is additional copy inside the SAME confirm flow, not a new gate.
const GNSS_BANDS = new Set(["gps_l1", "galileo_e1", "beidou_b1", "glonass_l1"]);

// Commander directive (post spectrum-analyser field test): there is NO
// artificial auto-stop cap. A capped ~5s burst lets a drone's FHSS+FEC control
// link re-sync and recover. The operator sets a bounded window OR runs the jam
// CONTINUOUSLY until Stand Down / EMERGENCY ABORT. Either way the effect is
// ALWAYS instantly stoppable (see the prominent STOP control below).
const DEFAULT_DURATION_S = 5;

// Preset sweep spans for the MEGHDUT swept barrage. A single ~20MHz-
// instantaneous HackRF center only covers a slice of an ~80MHz hop band;
// sweeping the center across the band hits a frequency-hopping control link on
// every hop over the sweep's revisit interval.
const SWEEP_PRESETS = [
  { label: "2.4 GHz ISM (2400–2483.5) — DJI / Wi-Fi / BT / ELRS", start: 2400, stop: 2483.5 },
  { label: "5.8 GHz video (5725–5875)", start: 5725, stop: 5875 },
  { label: "915 MHz ISM (902–928) — SiK / LoRa", start: 902, stop: 928 },
];

// The four bands the OPERATOR'S own jammer covers (its per-band callers
// cema_433/915/24/58.py). Operator mode is band-fixed to these — mirrors the
// backend's OPERATOR_JAM_BANDS and operator_jam_wrapper.py's OPERATOR_BANDS.
const OPERATOR_BAND_VALUES = new Set(["433", "915", "2g4", "5g8"]);

// CUSTOM CENTER FREQUENCY (MHz) — lets the operator jam an exact channel
// (e.g. 2462 MHz = Wi-Fi channel 11) instead of picking a band preset. This
// is the SAME governed arm -> /jam/confirm -> /payloads/jam flow and the SAME
// backend field (JamRequestBody.freq_mhz, which already wins over `band` when
// both are present — see backend/server.py) — nothing here adds a new
// transmit path. HONEST LABEL: a channel-center barrage is AREA denial, not a
// per-BSSID strike — it denies the ENTIRE channel to every device in RF
// range on that frequency (the intended target AND its clients AND any other
// co-channel device, civilian or own), governed by the range-authorization
// lease above, not by BSSID. See the AREA-DENIAL caution + checklist item
// below whenever a custom frequency is set.
function hasCustomFreqMhz(value) {
  return value !== "" && value != null && Number.isFinite(Number(value)) && Number(value) > 0;
}

// Pure, exported for tests: builds the exact /payloads/jam request body.
// A set custom frequency takes precedence over `band` (mirrors the backend's
// own freq_mhz-wins-over-band precedence) -- band is omitted whenever a
// sweep or a custom frequency is in effect, same as the existing sweep
// omission below.
export function buildJamPayload({
  sweep, band, customFreqMhz, durationS, continuous, freqStartMhz, freqStopMhz,
  bandwidthKhz, txGain, jamMode, profile, armToken, jamConfirmToken,
}) {
  // Sweep always wins: if a stale custom frequency lingers in state from
  // before the operator toggled sweep on, it must never sneak a freq_mhz
  // into a sweep request alongside freq_start_mhz/freq_stop_mhz.
  const hasCustom = !sweep && hasCustomFreqMhz(customFreqMhz);
  return {
    band: (sweep || hasCustom) ? undefined : band,
    ...(hasCustom ? { freq_mhz: Number(customFreqMhz) } : {}),
    duration_s: durationS,
    continuous,
    sweep,
    ...(sweep ? { freq_start_mhz: Number(freqStartMhz), freq_stop_mhz: Number(freqStopMhz) } : {}),
    bandwidth_khz: bandwidthKhz,
    // tx_gain is only consumed by the MAX profile; FLAT/EXTERNAL_PA use a
    // fixed internal VGA, so it is sent but ignored server-side under those.
    tx_gain: txGain,
    jam_mode: jamMode,
    profile,
    arm_token: armToken,
    jam_confirm_token: jamConfirmToken,
  };
}

// Two jammers, one governed spine. "meghdut" = the built-in HackRF barrage
// jam; "operator" = the operator's OWN GNU Radio jammer, run pinned+bounded
// through the identical arm/confirm/range-auth/tx-halt gates (see
// field-bridge/operator_jam_bridge.py). Operator mode exists purely so the
// operator can A/B which jammer works — it is NOT a new authorization path.
const JAM_MODES = [
  { value: "meghdut", label: "MEGHDUT Barrage (built-in)",
    hint: "MEGHDUT's built-in HackRF band-limited noise barrage." },
  { value: "operator", label: "Operator Jam (your code)",
    hint: "Runs the operator's OWN unmodified GNU Radio jammer (fixed waveform: " +
          "GAUSSIAN noise ×12, 20 Msps; TX gain operator-adjustable up to the 47 dB " +
          "HackRF ceiling), pinned to the TX radio and continuous-until-stopped. " +
          "Band-fixed to 433 / 915 / 2.4 / 5.8 GHz." },
];

// Jam POWER profiles (anti-fade). HONEST labels — no overclaim. Only MAX
// consumes tx_gain; FLAT/EXTERNAL_PA use a fixed internal VGA (amp off), so
// the gain slider is hidden under those (see gain control below). Values
// mirror field-bridge/hackrf_jam.py JAM_PROFILE_* (max|flat|external_pa).
const JAM_PROFILES = [
  { value: "max", label: "MAX (highest power)",
    hint: "Highest power — fades on continuous TX (thermal droop). Uses TX gain." },
  { value: "flat", label: "FLAT (steady, zero-fade)",
    hint: "Steady, zero-fade — lower power (amp off, fixed internal VGA)." },
  { value: "external_pa", label: "EXTERNAL PA (exciter drive)",
    hint: "Cool exciter drive for an external PA (fixed internal VGA)." },
];

// Poll-and-render, same pattern as KillChain.jsx / Payloads.jsx — no
// bespoke WS consumer needed on the frontend.
const STATUS_STYLE = {
  AWAITING_ACK: { color: "var(--accent-warning)", label: "◐ AWAITING ACK", blink: true },
  JAM_ACTIVE:   { color: "var(--accent-critical)", label: "▮▮ TRANSMITTING", blink: true },
  JAM_COMPLETE: { color: "var(--accent-success)", label: "✓ JAM COMPLETE", blink: false },
  JAM_STOPPED:  { color: "var(--accent-warning)", label: "■ STOPPED (ABORT)", blink: false },
  TX_FAILED:    { color: "var(--accent-critical)", label: "✕ TX FAILED", blink: false },
  TX_TIMEOUT:   { color: "var(--accent-critical)", label: "✕ TX TIMEOUT", blink: false },
};

export default function Jamming() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const [searchParams] = useSearchParams();
  const [jamMode, setJamMode] = useState("meghdut");
  const [band, setBand] = useState("915");
  const [durationS, setDurationS] = useState(DEFAULT_DURATION_S);
  const [continuous, setContinuous] = useState(false);
  const [sweep, setSweep] = useState(false);
  const [freqStartMhz, setFreqStartMhz] = useState(SWEEP_PRESETS[0].start);
  const [freqStopMhz, setFreqStopMhz] = useState(SWEEP_PRESETS[0].stop);
  // CUSTOM CENTER FREQUENCY (MHz) — deep-linkable from WifiEnvironment.jsx's
  // "Jam Channel (Area)" affordance via ?freq=<MHz> (e.g. /jamming?freq=2462).
  // Prefilled once on mount; empty string means "no custom freq, use band".
  const [customFreqMhz, setCustomFreqMhz] = useState(() => {
    const freqParam = searchParams.get("freq");
    return freqParam && Number(freqParam) > 0 ? freqParam : "";
  });
  const [bandwidthKhz, setBandwidthKhz] = useState(500);
  const [txGain, setTxGain] = useState(20);
  // Jam POWER profile (anti-fade). "max" = highest power (fades on continuous TX);
  // "flat" = steady zero-fade (amp off, lower power); "external_pa" = cool exciter
  // drive for an external PA. Only MAX consumes tx_gain (see gain-slider gating).
  const [profile, setProfile] = useState("max");
  const [gateOpen, setGateOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const loadStatus = async () => {
    try {
      const { data } = await api.get("/jam/status");
      setSessions(data.sessions || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch (e) {
      setConsecutiveFailures((n) => n + 1);
    }
  };
  useEffect(() => { loadStatus(); const id = setInterval(loadStatus, POLL_INTERVAL_MS); return () => clearInterval(id); }, []);

  // Local clock tick so staleness (time-since-last-success) updates even
  // between poll ticks — same pattern as SystemHealth.jsx.
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  const active = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "JAM_ACTIVE");
  const isOperatorMode = jamMode === "operator";
  // Operator mode is band-fixed to the operator jammer's four presets; MEGHDUT
  // mode keeps the full band list (incl. Bluetooth + GNSS targets).
  const bandOptions = isOperatorMode ? BANDS.filter((b) => OPERATOR_BAND_VALUES.has(b.value)) : BANDS;

  // Switching into operator mode must snap the selected band into the
  // operator-supported set (otherwise a GNSS/BT band would be sent and 400).
  useEffect(() => {
    if (isOperatorMode && !OPERATOR_BAND_VALUES.has(band)) setBand("915");
  }, [isOperatorMode, band]);

  // Operator mode is band-fixed (single-center flowgraph) — it cannot sweep.
  // Force sweep off when operator mode is selected.
  useEffect(() => {
    if (isOperatorMode && sweep) setSweep(false);
  }, [isOperatorMode, sweep]);

  // Operator Jam is band-fixed server-side (the backend 400s on freq_mhz for
  // operator mode) — clear any custom frequency when switching into it.
  useEffect(() => {
    if (isOperatorMode && customFreqMhz) setCustomFreqMhz("");
  }, [isOperatorMode, customFreqMhz]);

  const isGnssTarget = !sweep && GNSS_BANDS.has(band);
  const hasCustomFreq = !sweep && !isOperatorMode && hasCustomFreqMhz(customFreqMhz);

  // Same JAM_CHECKS the SafetyGate has always used, PLUS one extra line each
  // when the operator has selected a GNSS target and/or a custom AREA-DENIAL
  // frequency — still the ONE SafetyGate component/flow, just with added
  // checklist items for the specific target (no second confirmation
  // mechanism).
  // Stabilize the reference so SafetyGate's checklist-reset effect doesn't
  // treat this page's frequent re-renders (e.g. the range-auth countdown
  // ticking every second) as a checklist change and wipe the operator's
  // ticks. Only recompute when the inputs that actually change the wording
  // change.
  const gateChecks = useMemo(() => [
    ...JAM_CHECKS,
    ...(isGnssTarget ? [
      "GNSS TARGET SELECTED: navigation-denial jamming reaches FAR beyond comms jamming " +
        "at the same TX power — GPS-band signals arrive at only ~-130dBm at the receiver, " +
        "so even modest transmit power can deny GNSS fixes well outside the intended " +
        "footprint. Effective denial radius and any risk to non-participating " +
        "receivers/aircraft/vehicles outside the range has been assessed.",
    ] : []),
    ...(hasCustomFreq ? [
      `AREA DENIAL — CUSTOM FREQUENCY ${customFreqMhz} MHz: this is a channel-wide barrage, ` +
        "NOT a per-BSSID / targeted strike — it denies the ENTIRE CHANNEL to ALL devices in RF " +
        "range, including co-channel civilian and own networks, along with the intended " +
        "target. Range authorization confirmed to cover area denial of this channel/frequency " +
        "(governed by the range-authorization lease, NOT by BSSID).",
    ] : []),
  ], [isGnssTarget, hasCustomFreq, customFreqMhz]);

  const fireJam = async () => {
    setSubmitting(true);
    try {
      // Step 1: a fresh arm token — jamming is unconditionally CRITICAL
      // severity, same second factor as FORCE_DISARM/FLIGHT_TERMINATION/
      // broadcast payload deploys.
      const { data: arm } = await api.post("/arm", { effect: "jam" });  // F3: bound to jam
      // Step 2: mint the jam confirmation token RIGHT NOW — this call only
      // happens because the SafetyGate checklist + two-click ARM & FIRE ->
      // CONFIRM FIRE sequence just completed (see onConfirm below). This is
      // what makes the token mean "a human just deliberately confirmed
      // this", the digital equivalent of typing 'TRANSMIT' at a terminal.
      const { data: confirm } = await api.post("/jam/confirm");
      // Step 3: the actual jam request, carrying both tokens. continuous / sweep
      // drive the field bridge; there is no artificial duration cap. A set
      // custom frequency (AREA DENIAL of an exact channel) takes precedence
      // over the band preset — see buildJamPayload.
      const { data } = await api.post("/payloads/jam", buildJamPayload({
        sweep, band, customFreqMhz, durationS, continuous, freqStartMhz, freqStopMhz,
        bandwidthKhz, txGain, jamMode, profile,
        armToken: arm.arm_token, jamConfirmToken: confirm.jam_confirm_token,
      }));
      const modeLabel = jamMode === "operator" ? "OPERATOR JAM" : "MEGHDUT BARRAGE";
      if (data.tx_bridge_subscribed === false) {
        // Honest false-green guard: request accepted (HTTP 200, AWAITING_ACK)
        // but NO cema-jam-bridge is subscribed, so nothing will radiate — it
        // will TX_TIMEOUT. Translate into the plain-language "TX subsystem
        // OFFLINE — Bring TX Online" fix (a button for commanders) instead of a
        // raw "start cema-jam-bridge" shell hint.
        if (!handleEngageBlock({ response: data }, { isCommander, onFixed: loadStatus })) {
          toast.error(`${modeLabel} NOT TRANSMITTED`, {
            description: `Nothing radiated. Request ${data.request_id?.slice(0, 8)} will TX_TIMEOUT.`,
          });
        }
      } else {
        const where = data.sweep
          ? `SWEEP ${data.freq_start_mhz}–${data.freq_stop_mhz} MHz`
          : `${data.freq_mhz} MHz`;
        const dur = data.continuous || data.duration_s == null ? "CONTINUOUS (until Stand Down)" : `${data.duration_s}s`;
        toast.info(`${modeLabel} REQUESTED — awaiting bridge ACK`, {
          description: `${where} · ${dur} · req ${data.request_id?.slice(0, 8)}`,
        });
      }
      loadStatus();
    } catch (e) {
      // Operator-friendly pre-condition translation (TX HALTED -> RESUME TX,
      // range-auth OFF -> arm it) before the generic error toast.
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: loadStatus })) return;
      toast.error("Jam request failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Radio size={12} className="inline mr-2" strokeWidth={1.5} /> RF Barrage Jam
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Communication Disruption
        </h1>
      </div>

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "var(--surface-critical)" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
          This transmits REAL RF via a HackRF (band-limited noise). It can run CONTINUOUSLY and it
          runs until YOU stop it — use STAND DOWN / EMERGENCY ABORT to cease TX instantly at any
          moment. Requires commander role, a fresh arm token, a jam confirmation token minted at the
          instant you complete the checklist below, AND a live Range Authorization lease for this
          effect (armed via the control below, re-checked by the bridge at the moment of
          transmission) — all four independently, every time.{" "}
          <span className="font-bold" style={{ color: "var(--accent-warning)" }}>
            Effectiveness at range depends on PA power, antenna and proximity (physical) and on
            jamming the CONTROL band (add GNSS denial to prevent return-to-home) — jamming does not
            guarantee a stop.
          </span>
        </div>
      </div>

      {/* Directive #3: PROMINENT, always-visible EMERGENCY ABORT — the very
          first control on the barrage screen, never buried. Halts ALL TX
          (continuous / swept jams included) instantly via POST /emergency/abort
          (sets tx_halt + kills the active jam). Works whether or not a jam is
          currently armed/firing — it is the operator's guaranteed off-switch. */}
      <div
        data-testid="jam-emergency-abort-panel"
        className="p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 pulse-crit"
        style={{ background: "var(--surface-critical)", border: "2px solid var(--accent-critical)" }}
      >
        <div className="font-mono leading-relaxed">
          <div className="text-sm font-black uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
            <Siren size={16} className="inline mr-2" strokeWidth={2} /> EMERGENCY ABORT / STAND DOWN
          </div>
          <div className="text-[11px] text-slate-300 mt-1">
            Halts every RF transmission immediately — continuous or swept jam in progress included.
            Always available; the jammer can ALWAYS be switched off.
          </div>
        </div>
        <EmergencyAbort />
      </div>

      <RangeAuthorizationControl effect="jam" label="RF JAMMING" />

      {isGnssTarget && (
        <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "var(--surface-critical)" }}>
          <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
          <div className="font-mono text-xs text-slate-300">
            <span className="font-bold" style={{ color: "var(--accent-critical)" }}>
              GNSS TARGET — ADDITIONAL CAUTION:
            </span>{" "}
            This is navigation-denial jamming (GPS/Galileo/BeiDou/GLONASS L1), not comms jamming.
            GNSS signals arrive at the receiver at only about -130 dBm — far weaker than a local
            WiFi/BT/telemetry link — so the effective denial radius for the SAME transmit power is
            proportionally much larger and harder to contain to the intended target/range. Confirm
            the STEAG range clearance and spectrum authorization specifically cover GNSS L1 denial
            before arming.
          </div>
        </div>
      )}

      {hasCustomFreq && (
        <div
          data-testid="jam-area-denial-caution"
          className="tactical-border p-4 flex items-start gap-3"
          style={{ background: "var(--surface-critical)" }}
        >
          <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
          <div className="font-mono text-xs text-slate-300">
            <span className="font-bold" style={{ color: "var(--accent-critical)" }}>
              AREA DENIAL — CUSTOM FREQUENCY, NOT A TARGETED STRIKE:
            </span>{" "}
            Jamming an exact center frequency ({customFreqMhz} MHz) denies THIS ENTIRE CHANNEL to
            ALL devices in range, including co-channel civilian and own networks — it is a
            channel-wide barrage, not a per-AP / per-BSSID engagement, and it cannot be scoped to
            one device. It hits the target AP, its clients, AND any other radio sharing that
            channel within RF range. This is governed by the range-authorization lease above, NOT
            by BSSID — confirm the range clearance covers area denial of this channel before
            arming.
          </div>
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="tactical-border p-4 space-y-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Burst Parameters</div>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Jammer</span>
            <select
              data-testid="jam-mode-select"
              value={jamMode}
              onChange={(e) => setJamMode(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
            >
              {JAM_MODES.map((m) => <option key={m.value} value={m.value}>{m.label}</option>)}
            </select>
            <span className="mt-1 block font-mono text-[10px] text-slate-500 leading-relaxed">
              {JAM_MODES.find((m) => m.value === jamMode)?.hint}
            </span>
          </label>

          {/* JAM POWER PROFILE (MEGHDUT built-in jam only — the operator's own
              jammer has a fixed waveform and ignores this). HONEST labels; the
              gain slider below is hidden under FLAT/EXTERNAL_PA (fixed VGA). */}
          {!isOperatorMode && (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Power Profile</span>
              <select
                data-testid="jam-profile-select"
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              >
                {JAM_PROFILES.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
              <span className="mt-1 block font-mono text-[10px] text-slate-500 leading-relaxed">
                {JAM_PROFILES.find((p) => p.value === profile)?.hint}
              </span>
            </label>
          )}

          {/* SWEPT BARRAGE (MEGHDUT only): step the center across a hop band so
              a frequency-hopping control link is hit on every hop. */}
          {!isOperatorMode && (
            <label className="flex items-start gap-2 cursor-pointer">
              <input
                data-testid="jam-sweep-toggle"
                type="checkbox"
                checked={sweep}
                onChange={(e) => setSweep(e.target.checked)}
                className="mt-1"
              />
              <span className="font-mono text-[10px] text-slate-400 leading-relaxed">
                <Waves size={11} className="inline mr-1" strokeWidth={1.5} />
                <span className="uppercase tracking-widest text-slate-300">Swept barrage (full-band)</span>
                {" — "}sweep the TX center across a band so a frequency-hopping (FHSS) control link is
                hit on every hop. A single ~20 MHz HackRF window covers only a slice of an ~80 MHz hop
                band; the sweep covers it over time (revisit ≈ steps × dwell), not instantaneously.
              </span>
            </label>
          )}

          {sweep ? (
            <div className="space-y-3">
              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Sweep preset</span>
                <select
                  data-testid="jam-sweep-preset"
                  value={`${freqStartMhz}-${freqStopMhz}`}
                  onChange={(e) => {
                    const p = SWEEP_PRESETS.find((x) => `${x.start}-${x.stop}` === e.target.value);
                    if (p) { setFreqStartMhz(p.start); setFreqStopMhz(p.stop); }
                  }}
                  className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                >
                  {SWEEP_PRESETS.map((p) => (
                    <option key={p.label} value={`${p.start}-${p.stop}`}>{p.label}</option>
                  ))}
                </select>
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Start (MHz)</span>
                  <input
                    data-testid="jam-sweep-start"
                    type="number" min={1} step={0.5}
                    value={freqStartMhz}
                    onChange={(e) => setFreqStartMhz(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Stop (MHz)</span>
                  <input
                    data-testid="jam-sweep-stop"
                    type="number" min={1} step={0.5}
                    value={freqStopMhz}
                    onChange={(e) => setFreqStopMhz(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
              </div>
            </div>
          ) : (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Band</span>
              <select
                data-testid="jam-band-select"
                value={band}
                onChange={(e) => setBand(e.target.value)}
                disabled={hasCustomFreq}
                className={`mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info ${hasCustomFreq ? "opacity-30 cursor-not-allowed" : ""}`}
              >
                {bandOptions.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
              </select>
            </label>
          )}

          {/* CUSTOM CENTER FREQUENCY (MHz) — jam an exact channel instead of a
              band preset (e.g. 2462 MHz = Wi-Fi channel 11). Not available in
              Operator mode (band-fixed server-side) or Swept mode (its own
              start/stop range applies instead). Overrides the Band select
              above when set — see buildJamPayload's freq_mhz-wins precedence. */}
          {!sweep && !isOperatorMode && (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Custom Center Frequency (MHz) — overrides Band, AREA DENIAL
              </span>
              <input
                data-testid="jam-custom-freq-input"
                type="number" min={1} step={0.5}
                value={customFreqMhz}
                onChange={(e) => setCustomFreqMhz(e.target.value)}
                placeholder="e.g. 2462 (Wi-Fi channel 11)"
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
              <span className="mt-1 block font-mono text-[10px] text-slate-500 leading-relaxed">
                Jam an exact center frequency — e.g. a specific Wi-Fi channel (2462 MHz = channel
                11) — instead of a band preset. AREA DENIAL: this denies the ENTIRE channel to ALL
                devices in RF range, not one AP; it is NOT a per-BSSID / targeted strike. Leave
                blank to use the Band preset above.
              </span>
            </label>
          )}

          {/* CONTINUOUS: no artificial auto-stop timer — runs until Stand Down /
              Abort. When off, an operator-set bounded window (NOT capped). */}
          <label className="flex items-start gap-2 cursor-pointer">
            <input
              data-testid="jam-continuous-toggle"
              type="checkbox"
              checked={continuous}
              onChange={(e) => setContinuous(e.target.checked)}
              className="mt-1"
            />
            <span className="font-mono text-[10px] text-slate-400 leading-relaxed">
              <InfinityIcon size={11} className="inline mr-1" strokeWidth={1.5} />
              <span className="uppercase tracking-widest text-slate-300">Continuous</span>
              {" — "}transmit until you STAND DOWN / EMERGENCY ABORT (no auto-stop timer). A capped
              short burst lets a FHSS+FEC link re-sync and recover; continuous denial does not.
            </span>
          </label>

          {!continuous && (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Duration (s) — bounded window, no cap
              </span>
              <input
                data-testid="jam-duration-input"
                type="number" min={0.5} step={0.5}
                value={durationS}
                onChange={(e) => setDurationS(Math.max(0.5, Number(e.target.value)))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
          )}

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Bandwidth (kHz){isOperatorMode ? " — fixed by operator code" : ""}
            </span>
            <input
              data-testid="jam-bandwidth-input"
              type="number" min={50} max={5000} step={50}
              value={bandwidthKhz}
              disabled={isOperatorMode}
              onChange={(e) => setBandwidthKhz(Number(e.target.value))}
              className={`mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info ${isOperatorMode ? "opacity-30 cursor-not-allowed" : ""}`}
            />
          </label>

          {/* TX GAIN — only the MAX profile consumes it; FLAT/EXTERNAL_PA use a
              fixed internal VGA (amp off), so the slider is hidden under those to
              avoid misleading the operator into thinking gain has any effect.
              Operator mode always shows it (its jammer is gain-adjustable). */}
          {(isOperatorMode || profile === "max") ? (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                TX Gain (0-47 dB — HackRF TX VGA hardware ceiling)
              </span>
              <input
                data-testid="jam-gain-input"
                type="number" min={0} max={47}
                value={txGain}
                onChange={(e) => setTxGain(Math.max(0, Math.min(47, Number(e.target.value))))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
              <span className="mt-1 block font-mono text-[10px] text-slate-500 leading-relaxed">
                {isOperatorMode
                  ? "Operator-adjustable — driven onto the operator jammer's osmosdr sink " +
                    "(overrides its baked-in gain). 47 dB is the HackRF TX VGA hardware maximum, " +
                    "not an artificial limit."
                  : "47 dB is the HackRF TX VGA hardware maximum (no artificial cap)."}
              </span>
            </label>
          ) : (
            <div
              data-testid="jam-gain-fixed-note"
              className="font-mono text-[10px] text-slate-500 leading-relaxed"
            >
              TX Gain is fixed under the{" "}
              <span className="uppercase">{profile.replace("_", " ")}</span>{" "}
              profile (internal VGA is set by the profile, amp off) — no gain control applies.
            </div>
          )}

          {isOperatorMode && (
            <div
              data-testid="jam-operator-note"
              className="tactical-border p-3 font-mono text-[10px] text-slate-400 leading-relaxed"
              style={{ background: "var(--surface-critical)" }}
            >
              OPERATOR JAM: runs the operator's OWN unmodified GNU Radio jammer, pinned to the TX
              radio (serial), continuous-until-stopped (no auto-stop timer). Waveform and bandwidth
              (GAUSSIAN ×12, 20 Msps) are fixed by the operator's code; TX GAIN is now
              operator-adjustable (0-47 dB, HackRF TX VGA hardware ceiling — no artificial cap) and
              driven onto its osmosdr sink, overriding the baked-in gain. Band, duration/continuous
              and TX gain apply; it is band-fixed (no sweep). Same arm / confirm / range-authorization
              / TX-halt gates as MEGHDUT — Stand Down stops it instantly.
            </div>
          )}

          <button
            data-testid="jam-arm-button"
            disabled={!!active || submitting}
            onClick={() => setGateOpen(true)}
            className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
              active || submitting
                ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                : "hover-accent-critical"
            }`}
            style={active || submitting ? undefined : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
          >
            ARM JAMMER
          </button>
        </div>

        <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-3">
            Jam Sessions
          </div>
          {statusUnconfirmed && (
            <div
              data-testid="jam-status-unconfirmed-banner"
              className="mb-3 flex items-center gap-2 px-3 py-2 pulse-crit"
              style={{ background: "#FF9500", color: "black" }}
            >
              <ShieldAlert size={14} strokeWidth={2} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                STATUS UNCONFIRMED — jam status feed stale, session states below may be out of date
              </span>
            </div>
          )}
          {sessions.length === 0 && (
            <div className="font-mono text-xs text-slate-600 text-center py-8">
              no jam sessions yet<span className="term-caret" />
            </div>
          )}
          <div className="space-y-2">
            {sessions.map((s) => {
              const st = STATUS_STYLE[s.status] || { color: "var(--text-muted)", label: s.status, blink: false };
              return (
                <div key={s.request_id} data-testid={`jam-session-${s.request_id}`}
                     className="flex items-center justify-between p-3 tactical-border">
                  <div className="font-mono text-[11px] text-slate-300">
                    {s.sweep
                      ? `SWEEP ${s.freq_start_mhz}–${s.freq_stop_mhz} MHz`
                      : `${s.freq_mhz} MHz`}{" · "}
                    {s.continuous || s.duration_s == null ? "CONTINUOUS" : `${s.duration_s}s`}
                    {" · "}gain={s.tx_gain}
                    <div className="text-slate-500 text-[10px] flex items-center gap-2">
                      <span>{s.request_id?.slice(0, 8)}</span>
                      <span
                        data-testid={`jam-mode-${s.request_id}`}
                        className="px-1.5 py-0.5 tactical-border uppercase tracking-widest"
                        style={{ color: s.jam_mode === "operator" ? "var(--accent-info)" : "var(--text-muted)" }}
                      >
                        {s.jam_mode === "operator" ? "OPERATOR" : "MEGHDUT"}
                      </span>
                    </div>
                  </div>
                  <span
                    data-testid={`jam-status-${s.request_id}`}
                    className={`px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest ${st.blink ? "blink" : ""}`}
                    style={{ color: st.color, borderColor: st.color }}
                  >
                    {st.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <SafetyGate
        open={gateOpen}
        payloadName={`${isOperatorMode ? "OPERATOR JAM" : sweep ? "MEGHDUT SWEPT BARRAGE" : hasCustomFreq ? "MEGHDUT AREA-DENIAL JAM (CUSTOM FREQ)" : "MEGHDUT RF BARRAGE JAM"} `
          + `(${sweep ? `${freqStartMhz}–${freqStopMhz} MHz sweep` : hasCustomFreq ? `${customFreqMhz} MHz — AREA DENIAL, whole channel` : (BANDS.find((b) => b.value === band)?.label || band)}`
          + `${continuous ? ", CONTINUOUS until Stand Down" : ""})`}
        severity="CRITICAL"
        checks={gateChecks}
        actionLabel="TRANSMIT"
        irreversibleNote="a real RF transmission — it cannot be recalled once sent"
        onClose={() => setGateOpen(false)}
        onConfirm={() => {
          setGateOpen(false);
          fireJam();
        }}
      />
    </div>
  );
}
