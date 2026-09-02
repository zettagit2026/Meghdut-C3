import { useEffect, useState } from "react";
import { api, formatApiError, API_BASE } from "@/lib/api";
import { toast } from "sonner";
import { ScrollText, FileDown, Loader2, Link2 } from "lucide-react";

const KIND_COLOR = {
  AUTH: "var(--accent-info)",
  DETECTION: "var(--accent-warning)",
  UPLOAD: "var(--threat-high)",
  CEMA: "var(--accent-info)",
  KILLCHAIN: "var(--accent-warning)",
  MAVLINK: "var(--accent-success)",
  PAYLOAD: "var(--accent-critical)",
  SYSTEM: "var(--text-muted)",
};

const POLL_INTERVAL_MS = 4000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

export default function MissionLog() {
  const [logs, setLogs] = useState([]);
  const [exporting, setExporting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const load = async (cancelled) => {
    try {
      const { data } = await api.get("/logs?limit=300");
      if (cancelled?.current) return;
      setLogs(data);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch (e) {
      if (cancelled?.current) return;
      setConsecutiveFailures((n) => n + 1);
      toast.error("Load failed", { description: formatApiError(e) });
    }
  };
  useEffect(() => {
    const cancelled = { current: false };
    load(cancelled);
    const id = setInterval(() => load(cancelled), POLL_INTERVAL_MS);
    return () => { cancelled.current = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const tailingStale = staleByAge || staleByFailures || neverSucceeded;

  const downloadPdf = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      toast.info("Generating classified report…");
      const token = localStorage.getItem("cema_token");
      const res = await fetch(`${API_BASE}/report/mission.pdf`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const filename = `cema-mission-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}.pdf`;
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      const sizeKb = Math.max(1, Math.round(blob.size / 1024));
      toast.success("Mission report downloaded", {
        description: `${filename} · ${sizeKb} KB · ${new Date().toISOString().slice(0, 19)}Z`,
      });
    } catch (e) {
      toast.error("Report failed", { description: e.message });
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <ScrollText size={12} className="inline mr-2" strokeWidth={1.5} /> Mission Log
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Audit Trail
          </h1>
        </div>
        <button
          data-testid="report-pdf-btn"
          onClick={downloadPdf}
          disabled={exporting}
          aria-busy={exporting}
          className="flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest hover-accent-info transition-colors scanline-btn disabled:opacity-50 disabled:cursor-wait"
          style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
        >
          {exporting
            ? <><Loader2 size={14} strokeWidth={1.5} className="animate-spin" /> GENERATING…</>
            : <><FileDown size={14} strokeWidth={1.5} /> EXPORT MISSION REPORT (PDF)</>}
        </button>
      </div>

      <div className="tactical-border term-surface" style={{ background: "var(--bg-terminal)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest" style={{ color: "var(--text-term)" }}>
            /var/log/cema-cuas.jsonl
          </span>
          <span
            data-testid="mission-log-tailing"
            role="status"
            className={`font-mono text-[10px] ${tailingStale ? "" : "blink"}`}
            style={tailingStale ? { color: "var(--accent-critical)" } : { color: "var(--text-muted, #64748b)" }}
          >
            {tailingStale
              ? `● TAILING (stale, ${lastSuccessAt ? Math.max(0, Math.round((now - lastSuccessAt) / 1000)) : "?"}s ago)`
              : "● TAILING"}
          </span>
        </div>
        <div data-testid="mission-log-list" className="max-h-[70vh] overflow-y-auto font-mono text-xs">
          {logs.length === 0 && (
            <div className="p-4 text-slate-600">no events yet<span className="term-caret" /></div>
          )}
          {logs.map((l) => (
            <div key={l.id} data-testid={`log-${l.id}`}
                 className="px-4 py-2 tactical-border-b flex flex-col md:flex-row md:items-center gap-2 hover-surface">
              <span className="text-slate-500 shrink-0">{l.ts?.replace("T", " ").split(".")[0]}Z</span>
              <span className="uppercase tracking-widest text-[10px] shrink-0"
                    style={{ color: KIND_COLOR[l.kind] || "var(--text-primary)" }}>
                [{l.kind}]
              </span>
              <span className="text-slate-300 flex-1">{l.message}</span>
              <span className="text-slate-600 text-[10px]">{l.actor}</span>
              {l.entry_hash ? (
                <span
                  data-testid={`log-hash-${l.id}`}
                  className="shrink-0 inline-flex items-center gap-1 text-[10px] tracking-wider tabular-nums"
                  style={{ color: "var(--accent-info)" }}
                  title={`SHA-256 hash-chain link · seq ${l.seq}\nentry_hash: ${l.entry_hash}\nprev_hash:  ${l.prev_hash}`}
                >
                  <Link2 size={10} strokeWidth={2} />
                  {l.entry_hash.slice(0, 10)}
                </span>
              ) : (
                <span
                  className="shrink-0 text-[10px] tracking-wider text-slate-700"
                  title="Legacy entry written before the audit hash-chain existed (unchained)."
                >
                  unchained
                </span>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
