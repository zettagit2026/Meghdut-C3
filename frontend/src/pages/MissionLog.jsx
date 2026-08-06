import { useEffect, useState } from "react";
import { api, formatApiError, API_BASE } from "@/lib/api";
import { toast } from "sonner";
import { ScrollText, FileDown } from "lucide-react";

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
    try {
      toast.info("Generating classified report…");
      const token = localStorage.getItem("cema_token");
      const res = await fetch(`${API_BASE}/report/mission.pdf`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `cema-mission-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.success("Mission report downloaded");
    } catch (e) {
      toast.error("Report failed", { description: e.message });
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
          className="flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn"
          style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
        >
          <FileDown size={14} strokeWidth={1.5} /> EXPORT MISSION REPORT (PDF)
        </button>
      </div>

      <div className="tactical-border" style={{ background: "var(--bg-terminal)" }}>
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
                 className="px-4 py-2 tactical-border-b flex flex-col md:flex-row md:items-center gap-2 hover:bg-[#0F1626]">
              <span className="text-slate-500 shrink-0">{l.ts?.replace("T", " ").split(".")[0]}Z</span>
              <span className="uppercase tracking-widest text-[10px] shrink-0"
                    style={{ color: KIND_COLOR[l.kind] || "var(--text-primary)" }}>
                [{l.kind}]
              </span>
              <span className="text-slate-300 flex-1">{l.message}</span>
              <span className="text-slate-600 text-[10px]">{l.actor}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
