import { useState } from "react";
import { Radiation, AlertOctagon, ShieldAlert, X } from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { useRangeAuthStatus, fmtRemaining } from "@/hooks/useRangeAuthStatus";

// Higher-level, additive gate per backend/RANGE_AUTHORIZATION_REDESIGN.md.
// This is NOT a replacement for SafetyGate's per-action ARM&FIRE->CONFIRM
// FIRE flow — that still runs unchanged for every individual jam/deploy
// action. This control only governs whether the field-bridge host will
// accept live TX AT ALL for a given effect ("jam" or "mavlink"), on a
// short-lived (~15 min) lease.
//
// Placement: mounted directly in Jamming.jsx (effect="jam") and Payloads.jsx
// (effect="mavlink") — near where an operator actually uses that capability
// — in addition to the always-visible top-of-page banner in Layout.jsx.

const CONFIRM_PHRASE = "AUTHORIZE LIVE RANGE";

export default function RangeAuthorizationControl({ effect, label }) {
  // Polling + staleness tracking (task #146) lives in the shared
  // hooks/useRangeAuthStatus.js hook, used by both this control and
  // RangeAuthorizationBanner.jsx.
  const { status, remaining, applyStatus, statusUnconfirmed } = useRangeAuthStatus(effect);
  const [modalOpen, setModalOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [disabling, setDisabling] = useState(false);

  const openModal = () => {
    // Always start from a fresh, empty state — never pre-fill/bind from any
    // stored/cached credential in app state (AuthContext, localStorage, etc).
    setPassword("");
    setPhrase("");
    setModalOpen(true);
  };

  const closeModal = () => {
    setModalOpen(false);
    setPassword("");
    setPhrase("");
  };

  const phraseMatches = phrase === CONFIRM_PHRASE; // client-side UX pre-check only; server re-validates

  const submitEnable = async (e) => {
    e.preventDefault();
    if (!password || !phraseMatches) return;
    setSubmitting(true);
    try {
      const { data } = await api.post("/range-authorization", {
        effect,
        enabled: true,
        password,
        confirm_phrase: phrase,
      });
      applyStatus(data);
      toast.success(`RANGE AUTHORIZATION ENABLED — ${label}`, {
        description: `Lease active — expires ${data?.expires_at || ""}`,
      });
      closeModal();
    } catch (err) {
      toast.error("Range authorization failed", { description: formatApiError(err) });
    } finally {
      setSubmitting(false);
    }
  };

  const disableNow = async () => {
    setDisabling(true);
    try {
      const { data } = await api.post("/range-authorization", { effect, enabled: false });
      applyStatus(data);
      toast.success(`RANGE AUTHORIZATION DISABLED — ${label}`);
    } catch (err) {
      toast.error("Disable failed", { description: formatApiError(err) });
    } finally {
      setDisabling(false);
    }
  };

  const enabled = !!status?.enabled;

  return (
    <div
      data-testid={`range-auth-control-${effect}`}
      className="tactical-border p-4 space-y-3"
      style={{ background: enabled ? "rgba(255,59,48,0.10)" : "var(--bg-surface)", borderColor: "var(--accent-critical)" }}
    >
      <div className="flex items-center gap-2">
        <Radiation size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <span className="font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
          Range Authorization — {label}
        </span>
      </div>

      <div className="font-mono text-[11px] text-slate-400 leading-relaxed">
        Separate, higher-level gate above the per-action SafetyGate checklist. Controls whether the
        field-bridge host will accept live transmission for this effect at all. Auto-expires ~15 min
        after arming; disabling is always one click.
      </div>

      {enabled ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between px-3 py-2 tactical-border" style={{ borderColor: "var(--accent-critical)" }}>
            <div className="font-mono text-xs">
              <span className="font-bold blink" style={{ color: "var(--accent-critical)" }}>● RANGE LIVE</span>
              <span className="text-slate-400 ml-3">
                expires in <span data-testid={`range-auth-control-countdown-${effect}`} className="text-white tnum">{fmtRemaining(remaining)}</span>
              </span>
            </div>
          </div>
          {statusUnconfirmed && (
            <div
              data-testid={`range-auth-control-unconfirmed-${effect}`}
              className="flex items-center gap-2 px-3 py-2 pulse-crit"
              style={{ background: "#FF9500", color: "black" }}
            >
              <ShieldAlert size={14} strokeWidth={2} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                STATUS UNCONFIRMED — status feed stale, real range state not verified — countdown may
                not reflect reality
              </span>
            </div>
          )}
          <div className="font-mono text-[10px] text-slate-500">
            armed by <span className="text-slate-300">{status?.enabled_by || "unknown"}</span>
            {status?.enabled_at ? <> at <span className="text-slate-300">{status.enabled_at}</span></> : null}
          </div>
          <button
            data-testid={`range-auth-disable-btn-${effect}`}
            onClick={disableNow}
            disabled={disabling}
            className="w-full px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors hover-accent-critical disabled:opacity-50"
            style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
          >
            DISABLE RANGE AUTHORIZATION
          </button>
        </div>
      ) : (
        <button
          data-testid={`range-auth-enable-btn-${effect}`}
          onClick={openModal}
          className="w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border-2 transition-colors"
          style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
        >
          <AlertOctagon size={14} strokeWidth={1.5} />
          AUTHORIZE LIVE RANGE ({label})
        </button>
      )}

      {modalOpen && (
        <div
          data-testid={`range-auth-modal-${effect}`}
          className="fixed inset-0 z-[60] flex items-center justify-center p-6"
          style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
        >
          <form
            onSubmit={submitEnable}
            className="max-w-xl w-full"
            style={{
              background: "var(--bg-surface)",
              border: "2px solid var(--accent-critical)",
              boxShadow: "0 0 40px rgba(255,59,48,0.35)",
            }}
          >
            <div
              className="px-5 py-4 flex items-center justify-between"
              style={{ background: "rgba(255,59,48,0.15)", borderBottom: "2px solid var(--accent-critical)" }}
            >
              <div className="flex items-center gap-3">
                <AlertOctagon size={22} strokeWidth={1.5} className="pulse-crit" style={{ color: "var(--accent-critical)" }} />
                <div>
                  <div className="font-heading font-black text-xl uppercase tracking-tighter" style={{ color: "var(--text-primary)" }}>
                    Authorize Live Range
                  </div>
                  <div className="font-mono text-[10px] uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
                    {label} — this arms REAL transmission capability
                  </div>
                </div>
              </div>
              <button type="button" data-testid={`range-auth-modal-close-${effect}`} onClick={closeModal} className="text-slate-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <div className="p-4 space-y-4">
              <div className="font-mono text-xs text-slate-300 leading-relaxed">
                This is <span className="font-bold" style={{ color: "var(--text-primary)" }}>not</span> the same as the per-action
                SafetyGate checklist you complete before every individual jam/deploy — that still runs
                separately, every time. This action arms the field-bridge host to accept live TX for{" "}
                <span className="font-bold" style={{ color: "var(--accent-critical)" }}>{label}</span>{" "}
                for up to 15 minutes. Everyone on every page will see a persistent RED banner while this
                is active.
              </div>

              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                  Re-enter your password
                </span>
                <input
                  data-testid={`range-auth-password-${effect}`}
                  type="password"
                  autoComplete="off"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus-accent-critical"
                  placeholder="Password"
                  required
                />
              </label>

              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                  Type the exact phrase: <span style={{ color: "var(--text-primary)" }}>{CONFIRM_PHRASE}</span>
                </span>
                <input
                  data-testid={`range-auth-phrase-${effect}`}
                  type="text"
                  autoComplete="off"
                  value={phrase}
                  onChange={(e) => setPhrase(e.target.value)}
                  className={`mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none ${
                    phrase && !phraseMatches ? "border-accent-critical" : "focus-accent-critical"
                  }`}
                  placeholder={CONFIRM_PHRASE}
                  required
                />
              </label>

              <div className="pt-2 flex items-center justify-between" style={{ borderTop: "1px solid var(--border-col)" }}>
                <button
                  type="button"
                  data-testid={`range-auth-modal-cancel-${effect}`}
                  onClick={closeModal}
                  className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
                >
                  CANCEL
                </button>
                <button
                  type="submit"
                  data-testid={`range-auth-modal-submit-${effect}`}
                  disabled={submitting || !password || !phraseMatches}
                  className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                    submitting || !password || !phraseMatches
                      ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                      : "text-white border-accent-critical"
                  }`}
                  style={!(submitting || !password || !phraseMatches) ? { background: "var(--accent-critical)" } : undefined}
                >
                  <AlertOctagon size={14} strokeWidth={1.5} />
                  AUTHORIZE LIVE RANGE
                </button>
              </div>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
