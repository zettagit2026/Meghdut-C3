import { useEffect, useState } from "react";
import { AlertTriangle, X, ShieldCheck } from "lucide-react";

// Payloads that require the safety gate before firing (irreversible / kinetic).
export const SAFETY_GATED = new Set([
  "PL-003", "PL-004", "PL-005", "PL-006", "PL-007", "PL-010",
]);

const CHECKS = [
  "Test range confirmed screened (Faraday cage / range clearance).",
  "Target drone is OWNED by the operating team.",
  "Physical safety perimeter established; personnel behind cover.",
  "For kinetic payloads: propellers PHYSICALLY REMOVED.",
  "Legal authorisation for MAVLink emission on this frequency.",
];

// Same rigor, adapted wording for a real RF barrage-jam TX burst instead of
// a MAVLink kinetic/logical command — reused by frontend/src/pages/
// Jamming.jsx via the `checks`/`actionLabel`/`irreversibleNote` props below,
// rather than building a separate, weaker confirmation UI from scratch.
export const JAM_CHECKS = [
  "STEAG range clearance confirmed; Army Signals spectrum authorization current for this band.",
  "No friendly/non-participating RF equipment operating in-band within range.",
  "Physical safety perimeter established; personnel clear of the TX antenna.",
  "Burst duration and frequency reviewed — this is a REAL RF transmission, not a preview.",
  "This bridge host's CEMA_AUTHORIZED_RANGE has been deliberately set for this session.",
];

export default function SafetyGate({
  open, onClose, onConfirm, payloadName, severity,
  checks = CHECKS,
  actionLabel = "FIRE",
  irreversibleNote = "irreversible",
}) {
  const [ticks, setTicks] = useState(() => checks.map(() => false));
  const [confirming, setConfirming] = useState(false);

  useEffect(() => {
    if (open) { setTicks(checks.map(() => false)); setConfirming(false); }
  }, [open, checks]);

  if (!open) return null;

  const allTicked = ticks.every(Boolean);

  const handleFire = () => {
    if (!allTicked) return;
    if (!confirming) { setConfirming(true); return; }
    onConfirm();
  };

  return (
    <div
      data-testid="safety-gate"
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ background: "rgba(5, 8, 16, 0.85)", backdropFilter: "blur(4px)" }}
    >
      <div className="max-w-2xl w-full tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div
          className="px-5 py-3 tactical-border-b flex items-center justify-between"
          style={{ background: "rgba(255,59,48,0.08)" }}
        >
          <div className="flex items-center gap-2">
            <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            <span className="font-heading font-black text-lg uppercase tracking-tighter text-white">
              Pre-Flight Safety Gate
            </span>
          </div>
          <button data-testid="safety-close" onClick={onClose}
                  className="text-slate-400 hover:text-white">
            <X size={16} />
          </button>
        </div>
        <div className="p-5 space-y-4">
          <div className="font-mono text-xs">
            You are about to arm <span className="text-white font-bold">{payloadName}</span>{" "}
            <span className="px-2 py-0.5 tactical-border font-bold text-[10px]"
                  style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
              {severity}
            </span>{" "}
            — this action is <span className="text-[#FF3B30] font-bold">{irreversibleNote}</span>.
          </div>
          <div className="space-y-2">
            {checks.map((c, i) => (
              <label key={i} data-testid={`safety-check-${i}`}
                     className="flex items-start gap-3 p-2 tactical-border cursor-pointer hover:bg-[#0F1626]">
                <input
                  type="checkbox"
                  checked={ticks[i]}
                  onChange={(e) => {
                    const nt = [...ticks]; nt[i] = e.target.checked; setTicks(nt);
                  }}
                  className="mt-0.5 accent-[#39FF14]"
                />
                <span className="font-mono text-xs text-slate-300">{c}</span>
              </label>
            ))}
          </div>
          <div className="tactical-border-t pt-3 flex items-center justify-between">
            <button
              data-testid="safety-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover:text-white hover:bg-[#0F1626]"
            >
              CANCEL
            </button>
            <button
              data-testid="safety-fire"
              disabled={!allTicked}
              onClick={handleFire}
              className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                !allTicked
                  ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                  : confirming
                    ? "text-white border-[#FF3B30] pulse-crit"
                    : "text-[#FF3B30] border-[#FF3B30] hover:bg-[#FF3B30] hover:text-black"
              }`}
              style={confirming ? { background: "#FF3B30" } : undefined}
            >
              <ShieldCheck size={14} strokeWidth={1.5} />
              {confirming ? `CONFIRM ${actionLabel}` : `ARM & ${actionLabel}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
