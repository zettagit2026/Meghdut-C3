import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { HeartPulse, ShieldAlert } from "lucide-react";
import { useAuth } from "@/context/AuthContext";
import ResumeTx from "./ResumeTx";

const DOT = (ok) => ({
  color: ok ? "var(--accent-success)" : "var(--accent-critical)",
});

const POLL_INTERVAL_MS = 3000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4; // ~12s

export default function SystemHealth() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const [h, setH] = useState(null);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let id;
    const load = async () => {
      try {
        const { data } = await api.get("/health");
        setH(data);
        setLastSuccessAt(Date.now());
        setConsecutiveFailures(0);
      } catch {
        setConsecutiveFailures((n) => n + 1);
      }
    };
    load();
    id = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  // Tick a local clock so staleness (time-since-last-success) updates even
  // when no new poll result has arrived — this is what lets us detect a
  // fully-dead polling loop (e.g. interval cleared, tab frozen) and not just
  // consecutive request failures.
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  // Never had a successful poll at all is also degraded, but only flag it
  // once we've given the first request(s) a chance to complete.
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const monitoringDegraded = staleByAge || staleByFailures || neverSucceeded;

  const rows = [
    ["Backend API", h?.backend],
    ["MongoDB", h?.mongo],
    ["HackRF RX", h?.hackrf],
    ["SiK Radio", h?.sik_radio],
  ];

  return (
    <div
      data-testid="system-health"
      className="tactical-border"
      style={{ background: "var(--bg-surface)" }}
    >
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <HeartPulse size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">System Health</span>
        </div>
        <span className="font-mono text-[10px] text-slate-500">poll 3s</span>
      </div>
      <div
        data-testid="monitoring-degraded-banner"
        role="alert"
        className="tactical-border-b px-4 py-2 items-center gap-2 pulse-crit"
        style={{
          display: monitoringDegraded ? "flex" : "none",
          background: "#FF9500",
          color: "black",
        }}
      >
        <ShieldAlert size={14} strokeWidth={2} />
        <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
          MONITORING DEGRADED — health feed stale, data below may be out of date
        </span>
      </div>
      {h?.tx_halted && (
        <div
          data-testid="tx-halted-banner"
          role="alert"
          className="tactical-border-b px-4 py-2 flex items-center justify-between gap-3 pulse-crit"
          style={{ background: "#FF3B30", color: "black" }}
        >
          <div className="flex items-center gap-2">
            <ShieldAlert size={14} strokeWidth={2} />
            <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
              {isCommander
                ? "TX HALTED — resume to re-arm the transmit path"
                : "TX HALTED — awaiting commander authorization to resume"}
            </span>
          </div>
          {isCommander && <ResumeTx />}
        </div>
      )}
      <div className="p-4 space-y-2 font-mono text-xs">
        {rows.map(([label, ok]) => (
          <div key={label} className="flex items-center justify-between">
            <span className="text-slate-400">{label}</span>
            <span data-testid={`hs-${label.toLowerCase().replace(/\s+/g, "-")}`}
                  style={DOT(!!ok)} className="font-bold uppercase tracking-widest text-[10px]">
              {ok ? "● UP" : "● DOWN"}
            </span>
          </div>
        ))}
        <div className="tactical-border-t pt-2 mt-2 flex items-center justify-between text-slate-500">
          <span>WS clients</span>
          <span className="text-slate-300">{h?.ws_clients ?? "?"}</span>
        </div>
        <div className="flex items-center justify-between text-slate-500">
          <span>Packets TX</span>
          <span className="text-slate-300">{h?.total_packets_tx ?? "?"}</span>
        </div>
        <div className="flex items-center justify-between text-slate-500">
          <span>Last good poll</span>
          <span
            data-testid="hs-last-success"
            className={monitoringDegraded ? "font-bold" : "text-slate-300"}
            style={monitoringDegraded ? { color: "var(--accent-critical)" } : undefined}
          >
            {lastSuccessAt ? `${Math.max(0, Math.round((now - lastSuccessAt) / 1000))}s ago` : "never"}
          </span>
        </div>
      </div>
    </div>
  );
}
