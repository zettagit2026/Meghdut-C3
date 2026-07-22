import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { History } from "lucide-react";
import MlClassifierBadge from "@/components/MlClassifierBadge";

const THREAT_COLOR = {
  LOW: "var(--accent-success)",
  MEDIUM: "var(--accent-warning)",
  HIGH: "#FF8A00",
  CRITICAL: "var(--accent-critical)",
};

// Status badge colors — kept consistent with KillChain.jsx's AWAITING_ACK /
// TX_FAILED / TX_TIMEOUT convention (amber = pending ack, red-family =
// failure/defeat, green = live, gray = aged-out/inactive). This page is the
// one place operators can see every status a detection can be in, so the
// mapping must not drift from KillChain's per-status semantics.
const STATUS_STYLE = {
  ACTIVE: { color: "var(--accent-success)", label: "● ACTIVE" },
  LOST: { color: "var(--text-muted)", label: "○ LOST" },
  AWAITING_ACK: { color: "var(--accent-warning)", label: "◐ AWAITING ACK" },
  NEUTRALIZED: { color: "var(--accent-critical)", label: "● NEUTRALIZED" },
  TX_FAILED: { color: "var(--accent-critical)", label: "✕ TX FAILED" },
  TX_TIMEOUT: { color: "var(--accent-critical)", label: "✕ TX TIMEOUT" },
};

const STATUS_FILTERS = ["ALL", "ACTIVE", "LOST", "AWAITING_ACK", "NEUTRALIZED", "TX_FAILED", "TX_TIMEOUT"];

const ROW_CAP = 100;

// Small local relative-time formatter (no date lib in this project) so
// "LAST SEEN" reads as "3s ago" / "2m ago" / "1h ago" and keeps ticking live.
// Mirrors Dashboard.jsx's formatRelativeTime for consistency across pages.
function formatRelativeTime(isoString, now) {
  if (!isoString) return "—";
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return "—";
  const diffSec = Math.max(0, Math.floor((now - then) / 1000));
  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  return `${diffDay}d ago`;
}

export default function DetectionHistory() {
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [visibleCount, setVisibleCount] = useState(ROW_CAP);

  const load = async () => {
    try {
      const { data } = await api.get("/detections");
      setDetections(data);
    } catch (e) {
      toast.error("Failed to load detection history", { description: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const sorted = useMemo(
    () =>
      detections
        .slice()
        .sort((a, b) => new Date(b.last_seen || 0).getTime() - new Date(a.last_seen || 0).getTime()),
    [detections]
  );

  const filtered = useMemo(
    () => (statusFilter === "ALL" ? sorted : sorted.filter((d) => d.status === statusFilter)),
    [sorted, statusFilter]
  );

  const visible = filtered.slice(0, visibleCount);

  const counts = useMemo(() => {
    const c = { ALL: detections.length };
    for (const d of detections) c[d.status] = (c[d.status] || 0) + 1;
    return c;
  }, [detections]);

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <History size={12} className="inline mr-2" strokeWidth={1.5} /> Detection History
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          All Contacts
        </h1>
      </div>

      <div className="flex flex-wrap gap-2">
        {STATUS_FILTERS.map((s) => {
          const isActive = statusFilter === s;
          const style = s === "ALL" ? { color: "var(--accent-info)" } : STATUS_STYLE[s];
          return (
            <button
              key={s}
              data-testid={`filter-${s}`}
              onClick={() => { setStatusFilter(s); setVisibleCount(ROW_CAP); }}
              className={`px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest transition-colors ${
                isActive ? "bg-[#1A2235]" : "hover:bg-[#0F1626]"
              }`}
              style={{
                color: style?.color || "var(--text-muted)",
                borderColor: isActive ? (style?.color || "var(--text-muted)") : "var(--border-color, #1E2A3F)",
              }}
            >
              {s} ({counts[s] || 0})
            </button>
          );
        })}
      </div>

      <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest">Detection Log</span>
          <span className="font-mono text-[10px] text-slate-500">
            showing {visible.length} of {filtered.length}
          </span>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs" data-testid="detection-history-table">
            <thead>
              <tr className="tactical-border-b font-mono text-[10px] uppercase tracking-widest text-slate-500">
                <th className="text-left p-2">STATUS</th>
                <th className="text-left p-2">SRC</th>
                <th className="text-left p-2">CALLSIGN</th>
                <th className="text-left p-2">MODEL</th>
                <th className="text-left p-2">PROTOCOL</th>
                <th className="text-left p-2">THREAT</th>
                <th className="text-right p-2">FREQ (GHz)</th>
                <th className="text-right p-2">RSSI</th>
                <th className="text-right p-2">DIST (m)</th>
                <th className="text-left p-2">LAST SEEN</th>
                <th className="text-left p-2">CEMA</th>
                <th className="text-left p-2">KC</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {loading && (
                <tr><td colSpan={12} className="p-4 text-center text-slate-500">retrieving<span className="term-caret" /></td></tr>
              )}
              {!loading && visible.length === 0 && (
                <tr>
                  <td colSpan={12} className="p-4 text-center text-slate-500">
                    {detections.length === 0
                      ? "No detections recorded yet."
                      : "No detections match this filter."}
                  </td>
                </tr>
              )}
              {visible.map((d) => {
                const src = d.source || "UNKNOWN";
                const statusStyle = STATUS_STYLE[d.status] || { color: "var(--text-muted)", label: d.status || "—" };
                return (
                  <tr key={d.id} data-testid={`hist-row-${d.id}`}
                      className="tactical-border-b hover:bg-[#0F1626] transition-colors">
                    <td className="p-2">
                      <span data-testid={`hist-status-${d.id}`}
                            className="px-2 py-0.5 tactical-border font-bold text-[9px]"
                            style={{ color: statusStyle.color, borderColor: statusStyle.color }}>
                        {statusStyle.label}
                      </span>
                    </td>
                    <td className="p-2">
                      <span className="px-1.5 py-0.5 tactical-border font-bold text-[9px]"
                            style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}>
                        {src}
                      </span>
                    </td>
                    <td className="p-2 text-white">{d.callsign}</td>
                    <td className="p-2 text-slate-300">{d.model}</td>
                    <td className="p-2 text-slate-400">{d.protocol}</td>
                    <td className="p-2">
                      <span className="px-2 py-0.5 tactical-border font-bold text-[10px]"
                            style={{ color: THREAT_COLOR[d.threat_level], borderColor: THREAT_COLOR[d.threat_level] }}>
                        {d.threat_level}
                      </span>
                      <MlClassifierBadge detection={d} />
                    </td>
                    <td className="p-2 text-right text-slate-300">{d.center_freq_ghz}</td>
                    <td className="p-2 text-right text-slate-300">{d.rssi_dbm}</td>
                    <td className="p-2 text-right text-slate-300">
                      {d.distance_estimated === true ? (
                        <span title="RSSI-based estimate, not a precise measurement">~{d.distance_m}</span>
                      ) : (
                        d.distance_m
                      )}
                    </td>
                    <td className="p-2 text-slate-400" title={d.last_seen || ""}>
                      {formatRelativeTime(d.last_seen, now)}
                    </td>
                    <td className="p-2 text-[10px]" style={{ color: "var(--accent-info)" }}>{d.cema_stage}</td>
                    <td className="p-2 text-[10px]"
                        style={{ color: d.status === "NEUTRALIZED" ? "var(--accent-critical)" : "var(--accent-success)" }}>
                      {d.status === "NEUTRALIZED" ? "DEFEAT" : d.kill_chain_stage}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {filtered.length > visible.length && (
          <div className="tactical-border-t p-3 flex justify-center">
            <button
              data-testid="load-more-btn"
              onClick={() => setVisibleCount((c) => c + ROW_CAP)}
              className="px-4 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn"
              style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
            >
              LOAD MORE ({filtered.length - visible.length} remaining)
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
