import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Radio, AlertTriangle, ShieldAlert } from "lucide-react";
import SafetyGate, { JAM_CHECKS } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";

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

const MAX_DURATION_S = 10; // mirrors backend JAM_MAX_DURATION_S / hackrf_jam.py MAX_DURATION_S

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
  const [band, setBand] = useState("915");
  const [durationS, setDurationS] = useState(5);
  const [bandwidthKhz, setBandwidthKhz] = useState(500);
  const [txGain, setTxGain] = useState(20);
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
  const isGnssTarget = GNSS_BANDS.has(band);

  // Same JAM_CHECKS the SafetyGate has always used, PLUS one extra line when
  // the operator has selected a GNSS target — still the ONE SafetyGate
  // component/flow, just with an added checklist item for this specific
  // target (no second confirmation mechanism).
  const gateChecks = isGnssTarget
    ? [
        ...JAM_CHECKS,
        "GNSS TARGET SELECTED: navigation-denial jamming reaches FAR beyond comms jamming " +
          "at the same TX power — GPS-band signals arrive at only ~-130dBm at the receiver, " +
          "so even modest transmit power can deny GNSS fixes well outside the intended " +
          "footprint. Effective denial radius and any risk to non-participating " +
          "receivers/aircraft/vehicles outside the range has been assessed.",
      ]
    : JAM_CHECKS;

  const fireJam = async () => {
    setSubmitting(true);
    try {
      // Step 1: a fresh arm token — jamming is unconditionally CRITICAL
      // severity, same second factor as FORCE_DISARM/FLIGHT_TERMINATION/
      // broadcast payload deploys.
      const { data: arm } = await api.post("/arm");
      // Step 2: mint the jam confirmation token RIGHT NOW — this call only
      // happens because the SafetyGate checklist + two-click ARM & FIRE ->
      // CONFIRM FIRE sequence just completed (see onConfirm below). This is
      // what makes the token mean "a human just deliberately confirmed
      // this", the digital equivalent of typing 'TRANSMIT' at a terminal.
      const { data: confirm } = await api.post("/jam/confirm");
      // Step 3: the actual jam request, carrying both tokens.
      const { data } = await api.post("/payloads/jam", {
        band,
        duration_s: durationS,
        bandwidth_khz: bandwidthKhz,
        tx_gain: txGain,
        arm_token: arm.arm_token,
        jam_confirm_token: confirm.jam_confirm_token,
      });
      toast.info(`JAM REQUESTED — awaiting bridge ACK`, {
        description: `${data.freq_mhz} MHz · ${data.duration_s}s · req ${data.request_id?.slice(0, 8)}`,
      });
      loadStatus();
    } catch (e) {
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

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "#1A0A08" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
          This transmits REAL RF via a HackRF, band-limited noise for a hard-capped, bounded burst
          (max {MAX_DURATION_S}s per request — never continuous/unattended). Requires commander role, a
          fresh arm token, a jam confirmation token minted at the instant you complete the checklist
          below, AND a live Range Authorization lease for this effect (armed via the control below,
          re-checked by the bridge at the moment of transmission) — all four independently, every time.
        </div>
      </div>

      <RangeAuthorizationControl effect="jam" label="RF JAMMING" />

      {isGnssTarget && (
        <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "#1A0A08" }}>
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

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="tactical-border p-5 space-y-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Burst Parameters</div>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Band</span>
            <select
              data-testid="jam-band-select"
              value={band}
              onChange={(e) => setBand(e.target.value)}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            >
              {BANDS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
            </select>
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Duration (s, max {MAX_DURATION_S})
            </span>
            <input
              data-testid="jam-duration-input"
              type="number" min={1} max={MAX_DURATION_S} step={0.5}
              value={durationS}
              onChange={(e) => setDurationS(Math.min(MAX_DURATION_S, Math.max(1, Number(e.target.value))))}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Bandwidth (kHz)</span>
            <input
              data-testid="jam-bandwidth-input"
              type="number" min={50} max={5000} step={50}
              value={bandwidthKhz}
              onChange={(e) => setBandwidthKhz(Number(e.target.value))}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">TX Gain (0-47)</span>
            <input
              data-testid="jam-gain-input"
              type="number" min={0} max={47}
              value={txGain}
              onChange={(e) => setTxGain(Number(e.target.value))}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <button
            data-testid="jam-arm-button"
            disabled={!!active || submitting}
            onClick={() => setGateOpen(true)}
            className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
              active || submitting
                ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                : "text-[#FF3B30] border-[#FF3B30] hover:bg-[#FF3B30] hover:text-black"
            }`}
          >
            ARM JAMMER
          </button>
        </div>

        <div className="tactical-border p-5" style={{ background: "var(--bg-surface)" }}>
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
                    {s.freq_mhz} MHz · {s.duration_s}s · gain={s.tx_gain}
                    <div className="text-slate-500 text-[10px]">{s.request_id?.slice(0, 8)}</div>
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
        payloadName={`RF BARRAGE JAM (${BANDS.find((b) => b.value === band)?.label || band})`}
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
