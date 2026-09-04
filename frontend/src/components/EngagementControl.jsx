import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Crosshair, ShieldCheck, ShieldAlert, Lock } from "lucide-react";
import TxStatusChips from "@/components/TxStatusChips";
import TxOnlineControl from "@/components/TxOnlineControl";
import ResumeTx from "@/components/ResumeTx";
import EmergencyAbort from "@/components/EmergencyAbort";
import { readTxSubsystem, anyRangeAuthArmed, toneColor, TONE } from "@/lib/engagementGate";

const POLL_INTERVAL_MS = 3000;

// Engagement Control — the coherent, GUI-only surface for the transmit/engage
// pre-conditions. It shows live plain-language status chips and, for a
// commander, the buttons that make each blocked pre-condition fixable WITHOUT a
// terminal: Bring TX Online / Stand Down, Resume TX / Halt. Non-commanders see
// the same status (read-only) plus the always-available Emergency Abort safety
// stop. This never weakens a safety gate — it only makes the pre-conditions the
// SafetyGate/arm/IFF/range-auth flow already enforces visible and fixable.
export default function EngagementControl() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const [health, setHealth] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const { data } = await api.get("/health");
      setHealth(data);
    } catch {
      // Leave last-known state; the chips fail-closed (TX HALTED/OFFLINE) on
      // missing data rather than painting a false green.
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const tx = readTxSubsystem(health);
  const raArmed = anyRangeAuthArmed(tx);

  // Single plain-language readiness verdict, worst-blocker first. This mirrors
  // the order the backend gates actually fire in, so the operator always sees
  // the NEXT thing to fix.
  let readiness;
  if (tx.tx_halted) {
    readiness = {
      tone: TONE.crit,
      icon: ShieldAlert,
      text: "Fire path BLOCKED — transmit is HALTED. A commander must RESUME TX.",
    };
  } else if (!tx.bridges_online) {
    readiness = {
      tone: TONE.warn,
      icon: ShieldAlert,
      text: "Not ready — TX bridges are OFFLINE. A commander must Bring TX Online.",
    };
  } else if (!raArmed) {
    readiness = {
      tone: TONE.warn,
      icon: ShieldAlert,
      text: "TX online — arm RANGE-AUTH for the intended effect before firing.",
    };
  } else {
    readiness = {
      tone: TONE.ok,
      icon: ShieldCheck,
      text: "Engagement path READY — TX online, halt cleared, range-auth armed.",
    };
  }
  const RIcon = readiness.icon;

  return (
    <div
      data-testid="engagement-control"
      className="tactical-border"
      style={{ background: "var(--bg-surface)" }}
    >
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Crosshair size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">Engagement Control</span>
        </div>
        <span className="font-mono text-[10px] text-slate-500">poll 3s</span>
      </div>

      <div className="p-4 space-y-4">
        <TxStatusChips health={health} />

        <div
          data-testid="engagement-readiness"
          role="status"
          aria-live="polite"
          className="flex items-start gap-2 px-3 py-2 tactical-border"
          style={{
            color: toneColor(readiness.tone),
            borderColor: toneColor(readiness.tone),
            background: `color-mix(in srgb, ${toneColor(readiness.tone)} 8%, transparent)`,
          }}
        >
          <RIcon size={14} strokeWidth={2} className="mt-0.5 shrink-0" />
          <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
            {readiness.text}
          </span>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          {isCommander ? (
            <>
              <TxOnlineControl bridgesOnline={tx.bridges_online} onChanged={refresh} />
              {tx.tx_halted ? (
                <ResumeTx />
              ) : (
                <span
                  data-testid="tx-live-indicator"
                  className="flex items-center gap-2 px-3 py-2 font-mono text-[10px] uppercase tracking-widest"
                  style={{ color: "var(--accent-success)" }}
                >
                  <ShieldCheck size={13} strokeWidth={1.5} /> TX HALT CLEARED
                </span>
              )}
              <EmergencyAbort />
            </>
          ) : (
            <>
              <span
                data-testid="engagement-control-locked"
                className="flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-slate-500"
              >
                <Lock size={12} strokeWidth={1.5} />
                Commander role required to bring TX online / resume — status is read-only
              </span>
              {/* Emergency Abort stays available to any operator (safety stop). */}
              <EmergencyAbort />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
