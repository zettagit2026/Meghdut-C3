import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Radio, AlertTriangle } from "lucide-react";
import SafetyGate, { JAM_CHECKS } from "@/components/SafetyGate";

const BANDS = [
  { value: "433", label: "433 MHz (SiK ISM lower)" },
  { value: "915", label: "915 MHz (SiK ISM)" },
  { value: "2g4", label: "2.4 GHz (DJI video/control)" },
  { value: "5g8", label: "5.8 GHz (DJI video)" },
];

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

  const loadStatus = async () => {
    try {
      const { data } = await api.get("/jam/status");
      setSessions(data.sessions || []);
    } catch (e) { /* non-fatal — status polling failure shouldn't spam toasts every 2s */ }
  };
  useEffect(() => { loadStatus(); const id = setInterval(loadStatus, 2000); return () => clearInterval(id); }, []);

  const active = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "JAM_ACTIVE");

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
          below, AND the physical bridge host's own CEMA_AUTHORIZED_RANGE=1 environment gate — all four
          independently, every time.
        </div>
      </div>

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
        checks={JAM_CHECKS}
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
