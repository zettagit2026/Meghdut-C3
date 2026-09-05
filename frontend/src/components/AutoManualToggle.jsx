import { useEffect, useState } from "react";
import { ShieldCheck, X } from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { toast } from "sonner";

// C2 mode toggle (RFI 4.5.23) — MANUAL/AUTO bound to alert/cue only, never TX.
// Increment-1 bound, stated verbatim wherever the operator can see it (tooltip
// + confirm dialog) so it can never be read as an auto-engage switch.
const C2_BOUND_CAPTION =
  "AUTO = alert/annunciate/prioritize/cue only. It CANNOT arm, key TX, or " +
  "clear the TX halt — every engagement stays commander-gated.";

function modeColor(mode) {
  return mode === "AUTO" ? "var(--accent-warning)" : "var(--text-muted)";
}

export default function AutoManualToggle() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  const [mode, setMode] = useState("MANUAL");
  const [loaded, setLoaded] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .get("/c2/mode")
      .then(({ data }) => {
        if (!cancelled) setMode(data?.mode === "AUTO" ? "AUTO" : "MANUAL");
      })
      .catch(() => {
        // Leave the fail-safe default (MANUAL) rather than fabricate a mode.
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const targetMode = mode === "AUTO" ? "MANUAL" : "AUTO";
  const confirmPhrase = `SET MODE ${targetMode}`;
  const phraseOk = phrase.trim() === confirmPhrase;

  const openModal = () => {
    if (!isCommander) return;
    setPhrase("");
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setPhrase("");
  };

  const confirmToggle = async () => {
    if (!phraseOk || submitting) return;
    setSubmitting(true);
    try {
      const { data } = await api.post("/c2/mode", { mode: targetMode });
      const nextMode = data?.mode === "AUTO" ? "AUTO" : "MANUAL";
      setMode(nextMode);
      toast.success(`C2 MODE SET TO ${nextMode}`);
      closeModal();
    } catch (e) {
      toast.error("C2 mode change failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div data-testid="auto-manual-toggle" className="inline-flex items-center gap-1">
      <span
        data-testid="c2-mode-value"
        className="shrink-0 whitespace-nowrap px-2 py-0.5 tactical-border font-mono text-[10px] uppercase tracking-widest"
        style={{ color: modeColor(mode), borderColor: modeColor(mode) }}
        title={`C2 mode: ${loaded ? mode : "loading"}. ${C2_BOUND_CAPTION}`}
      >
        ● MODE: {loaded ? mode : "…"}
      </span>

      {isCommander ? (
        <button
          data-testid="c2-mode-toggle-btn"
          onClick={openModal}
          title={`Switch to ${targetMode}. ${C2_BOUND_CAPTION}`}
          className="shrink-0 px-1.5 py-0.5 tactical-border font-mono text-[9px] uppercase tracking-widest hover-surface"
          style={{ color: "var(--text-muted)" }}
        >
          SET {targetMode}
        </button>
      ) : null}

      {modalOpen && (
        <div
          data-testid="c2-mode-confirm"
          className="fixed inset-0 z-[60] flex items-center justify-center p-6"
          style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
        >
          <div
            className="max-w-md w-full"
            style={{ background: "var(--bg-surface)", border: "2px solid var(--accent-warning)" }}
          >
            <div
              className="px-4 py-3 flex items-center justify-between"
              style={{ background: "rgba(234,179,8,0.12)", borderBottom: "2px solid var(--accent-warning)" }}
            >
              <span className="font-heading font-black text-base uppercase tracking-tighter" style={{ color: "var(--text-primary)" }}>
                Set C2 Mode: {targetMode}
              </span>
              <button onClick={closeModal} className="text-slate-400 hover:text-[var(--text-primary)]">
                <X size={16} />
              </button>
            </div>
            <div className="p-4 space-y-3">
              <div className="font-mono text-xs text-slate-300 leading-relaxed">{C2_BOUND_CAPTION}</div>
              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                  Type the exact phrase: <span style={{ color: "var(--text-primary)" }}>{confirmPhrase}</span>
                </span>
                <input
                  type="text"
                  autoComplete="off"
                  value={phrase}
                  onChange={(e) => setPhrase(e.target.value)}
                  placeholder={confirmPhrase}
                  className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
                />
              </label>
              <div className="flex items-center justify-between pt-2" style={{ borderTop: "1px solid var(--border-col)" }}>
                <button
                  onClick={closeModal}
                  className="px-3 py-1.5 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
                >
                  CANCEL
                </button>
                <button
                  data-testid="c2-mode-confirm-submit"
                  onClick={confirmToggle}
                  disabled={!phraseOk || submitting}
                  className="flex items-center gap-2 px-3 py-1.5 font-mono text-xs font-bold uppercase tracking-widest border disabled:opacity-30 disabled:cursor-not-allowed"
                  style={phraseOk ? { color: "var(--accent-warning)", borderColor: "var(--accent-warning)" } : undefined}
                >
                  <ShieldCheck size={14} strokeWidth={1.5} />
                  CONFIRM {targetMode}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
