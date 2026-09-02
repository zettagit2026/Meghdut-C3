import { useEffect, useState } from "react";
import * as AlertDialogPrimitive from "@radix-ui/react-alert-dialog";
import { AlertTriangle, X, ShieldCheck, ShieldAlert, Skull } from "lucide-react";

// The EXACT phrase a commander must type, verbatim, to license engaging a
// target that IFF has CONFIRMED FRIENDLY. Kept as an exported constant so the
// input's placeholder, the on-screen instruction, and the match check can
// never drift apart. This is the deliberate, un-fat-fingerable acknowledgment
// that (together with commander role + the normal safety checklist) is the
// ONLY way the fratricide-override confirm button unlocks — it must be
// impossible to engage a confirmed friendly by just clicking through.
export const FRIENDLY_FIRE_ACK_PHRASE =
  "I am knowingly engaging a target confirmed FRIENDLY";

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
  "Range Authorization is armed for this effect via the GUI toggle (see banner) for this session.",
];

// GNSS L1 civil-signal spoofing ("soft-kill", Task #103) — a DECEPTION
// effect (fabricated position), not a denial effect like jamming, so the
// checklist wording differs materially. Per
// field-bridge/GNSS_SPOOF_ARCHITECTURE.md §5b, the single most important
// item here is NOT static — frontend/src/pages/GnssSpoof.jsx builds the
// final `checks` array passed to SafetyGate by taking this BASE list and
// APPENDING one more item whose text is built LIVE from
// POST /gnss-spoof/preview's response (the exact fabricated lat/lon/offset/
// bearing), so the operator ticks a box containing the real numbers, not a
// generic "I confirm" button. This base list intentionally does NOT include
// that dynamic item — see GnssSpoof.jsx's gateChecks construction.
export const GNSS_SPOOF_CHECKS = [
  "Range Authorization is armed for effect=gnss_spoof via the GUI toggle (see banner) for this session — " +
    "arming effect=jam does NOT arm this.",
  "This is DECEPTION (a fabricated position), not denial — GPS-dependent friendly assets that acquire this " +
    "signal may enter a flyaway/self-preservation failsafe or an unexpected geofence-breach RTH.",
  "Physical safety perimeter established; personnel clear of the TX antenna.",
  "Burst duration (max 3.0s) and frequency (1575.42 MHz, GPS L1) reviewed — this is a REAL RF " +
    "transmission, not a preview.",
];

export default function SafetyGate({
  open, onClose, onConfirm, payloadName, severity,
  checks = CHECKS,
  actionLabel = "FIRE",
  irreversibleNote = "irreversible",
  // FRATRICIDE-OVERRIDE mode. When `fratricide` is true the selected target has
  // been IFF-CONFIRMED FRIENDLY, and this gate becomes the ONE deliberate,
  // commander-only path that can license the engagement (backend refuses the
  // routine authorize/deploy path with 403). It layers, on top of the normal
  // checklist: (a) a conspicuous fratricide warning, (b) a hard commander-role
  // wall for anyone else, and (c) an explicit typed acknowledgment + checkbox.
  // Non-fratricide callers (Jamming.jsx, GnssSpoof.jsx, non-friendly payloads)
  // pass nothing and get exactly the prior behavior.
  fratricide = false,
  isCommander = false,
  friendlyCallsign,
}) {
  const [ticks, setTicks] = useState(() => checks.map(() => false));
  const [confirming, setConfirming] = useState(false);
  const [ackChecked, setAckChecked] = useState(false);
  const [typedAck, setTypedAck] = useState("");

  useEffect(() => {
    if (open) {
      setTicks(checks.map(() => false));
      setConfirming(false);
      setAckChecked(false);
      setTypedAck("");
    }
  }, [open, checks]);

  const allTicked = ticks.every(Boolean);
  // In fratricide mode the confirm is gated behind commander role AND both the
  // explicit checkbox and the verbatim typed phrase — never just the checklist.
  const fratricideReady =
    !fratricide ||
    (isCommander && ackChecked && typedAck.trim() === FRIENDLY_FIRE_ACK_PHRASE);
  const canFire = allTicked && fratricideReady;

  const handleFire = () => {
    if (!canFire) return;
    if (!confirming) { setConfirming(true); return; }
    onConfirm();
  };

  return (
    <AlertDialogPrimitive.Root open={open} onOpenChange={(o) => { if (!o) onClose(); }}>
      <AlertDialogPrimitive.Portal>
        <AlertDialogPrimitive.Overlay
          className="fixed inset-0 z-50"
          style={{ background: "rgba(5, 8, 16, 0.85)", backdropFilter: "blur(4px)" }}
        />
        <AlertDialogPrimitive.Content
          data-testid="safety-gate"
          className="fixed left-1/2 top-1/2 z-50 -translate-x-1/2 -translate-y-1/2 max-w-2xl w-full tactical-border"
          style={{ background: "var(--bg-surface)" }}
        >
          <div
            className="px-5 py-3 tactical-border-b flex items-center justify-between"
            style={{ background: fratricide ? "color-mix(in srgb, var(--accent-critical) 22%, var(--bg-surface))" : "rgba(255,59,48,0.08)" }}
          >
            <div className="flex items-center gap-2">
              {fratricide
                ? <Skull size={16} strokeWidth={1.75} style={{ color: "var(--accent-critical)" }} />
                : <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />}
              <AlertDialogPrimitive.Title asChild>
                <span className="font-heading font-black text-lg uppercase tracking-tighter"
                      style={{ color: fratricide ? "var(--accent-critical)" : "var(--text-primary)" }}>
                  {fratricide ? "Fratricide Override — Confirmed Friendly" : "Pre-Flight Safety Gate"}
                </span>
              </AlertDialogPrimitive.Title>
            </div>
            <button data-testid="safety-close" onClick={onClose}
                    className="text-slate-400 hover:text-[var(--text-primary)]">
              <X size={16} />
            </button>
          </div>
          <div className="p-4 space-y-4">
            {fratricide && (
              <div
                data-testid="fratricide-warning"
                className="p-3 flex items-start gap-3 border-2"
                style={{
                  borderColor: "var(--accent-critical)",
                  background: "color-mix(in srgb, var(--accent-critical) 16%, var(--bg-surface))",
                }}
              >
                <ShieldAlert size={20} strokeWidth={1.75} style={{ color: "var(--accent-critical)", flexShrink: 0 }} />
                <div className="font-mono text-xs" style={{ color: "var(--text-primary)" }}>
                  <div className="font-heading font-black text-sm uppercase tracking-tight" style={{ color: "var(--accent-critical)" }}>
                    ⚠ TARGET IFF-CONFIRMED FRIENDLY — ENGAGING WILL BE FRATRICIDE
                  </div>
                  <div className="mt-1 text-slate-300">
                    {friendlyCallsign
                      ? <><span className="font-bold" style={{ color: "var(--text-primary)" }}>{friendlyCallsign}</span> has replied to IFF interrogation and is a confirmed friendly asset. </>
                      : "This contact has replied to IFF interrogation and is a confirmed friendly asset. "}
                    There is <span className="font-bold" style={{ color: "var(--accent-critical)" }}>no standing override</span>.
                    Proceeding mints a single-use, one-engagement fratricide ack and is loudly audited.
                  </div>
                </div>
              </div>
            )}
            <AlertDialogPrimitive.Description asChild>
              <div className="font-mono text-xs">
                You are about to arm <span className="font-bold" style={{ color: "var(--text-primary)" }}>{payloadName}</span>{" "}
                <span className="px-2 py-0.5 tactical-border font-bold text-[10px]"
                      style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
                  {severity}
                </span>{" "}
                — this action is <span className="font-bold" style={{ color: "var(--accent-critical)" }}>{irreversibleNote}</span>.
              </div>
            </AlertDialogPrimitive.Description>
            <div className="space-y-2">
              {checks.map((c, i) => (
                <label key={i} data-testid={`safety-check-${i}`}
                       className="flex items-start gap-3 p-2 tactical-border cursor-pointer hover-surface">
                  <input
                    type="checkbox"
                    checked={ticks[i]}
                    onChange={(e) => {
                      const nt = [...ticks]; nt[i] = e.target.checked; setTicks(nt);
                    }}
                    className="mt-0.5"
                    style={{ accentColor: "var(--accent-success)" }}
                  />
                  <span className="font-mono text-xs text-slate-300">{c}</span>
                </label>
              ))}
            </div>
            {fratricide && !isCommander && (
              <div
                data-testid="fratricide-commander-required"
                className="p-3 border-2 font-mono text-xs"
                style={{
                  borderColor: "var(--accent-critical)",
                  background: "color-mix(in srgb, var(--accent-critical) 10%, var(--bg-surface))",
                  color: "var(--accent-critical)",
                }}
              >
                <span className="font-bold uppercase tracking-widest">Commander role required.</span>{" "}
                <span className="text-slate-300">
                  Only a commander may authorize engaging a target confirmed FRIENDLY. This account
                  cannot proceed — no fratricide override is available to your role.
                </span>
              </div>
            )}
            {fratricide && isCommander && (
              <div className="space-y-3">
                <label
                  data-testid="fratricide-ack-checkbox"
                  className="flex items-start gap-3 p-2 border-2 cursor-pointer"
                  style={{ borderColor: "var(--accent-critical)", background: "color-mix(in srgb, var(--accent-critical) 8%, var(--bg-surface))" }}
                >
                  <input
                    type="checkbox"
                    checked={ackChecked}
                    onChange={(e) => setAckChecked(e.target.checked)}
                    className="mt-0.5"
                    style={{ accentColor: "var(--accent-critical)" }}
                  />
                  <span className="font-mono text-xs font-bold" style={{ color: "var(--text-primary)" }}>
                    {FRIENDLY_FIRE_ACK_PHRASE}.
                  </span>
                </label>
                <div className="space-y-1">
                  <div className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
                    Type the acknowledgment verbatim to unlock:
                  </div>
                  <input
                    data-testid="fratricide-ack-input"
                    type="text"
                    value={typedAck}
                    onChange={(e) => setTypedAck(e.target.value)}
                    placeholder={FRIENDLY_FIRE_ACK_PHRASE}
                    autoComplete="off"
                    spellCheck={false}
                    className="tactical-input tactical-border w-full px-3 py-2 font-mono text-xs focus:outline-none"
                    style={{
                      borderColor:
                        typedAck.length === 0
                          ? undefined
                          : typedAck.trim() === FRIENDLY_FIRE_ACK_PHRASE
                            ? "var(--accent-success)"
                            : "var(--accent-critical)",
                    }}
                  />
                </div>
              </div>
            )}
            <div className="tactical-border-t pt-3 flex items-center justify-between">
              <AlertDialogPrimitive.Cancel asChild>
                <button
                  data-testid="safety-cancel"
                  onClick={onClose}
                  className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
                >
                  CANCEL
                </button>
              </AlertDialogPrimitive.Cancel>
              <button
                data-testid="safety-fire"
                disabled={!canFire}
                onClick={handleFire}
                className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                  !canFire
                    ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                    : confirming
                      ? "text-white pulse-crit"
                      : ""
                }`}
                style={
                  !canFire
                    ? undefined
                    : confirming
                      ? { background: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                      : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                }
              >
                {fratricide ? <Skull size={14} strokeWidth={1.75} /> : <ShieldCheck size={14} strokeWidth={1.5} />}
                {fratricide
                  ? (confirming ? "CONFIRM FRATRICIDE" : "MINT ACK & ENGAGE FRIENDLY")
                  : (confirming ? `CONFIRM ${actionLabel}` : `ARM & ${actionLabel}`)}
              </button>
            </div>
          </div>
        </AlertDialogPrimitive.Content>
      </AlertDialogPrimitive.Portal>
    </AlertDialogPrimitive.Root>
  );
}
