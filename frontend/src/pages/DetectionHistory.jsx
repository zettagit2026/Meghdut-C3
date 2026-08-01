import { Fragment, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { History, ShieldAlert } from "lucide-react";
import MlClassifierBadge from "@/components/MlClassifierBadge";
import ConfidenceTypeBadge from "@/components/ConfidenceTypeBadge";
import UnconfirmedTag from "@/components/UnconfirmedTag";
import { isUnconfirmedDetection, shouldShowUnconfirmedTag, getOriginalModelAnnotation } from "@/lib/detectionConfidence";
import { isStaticCritical } from "@/lib/threatSalience";
import { AlertTriangle } from "lucide-react";
import { THREAT_COLOR } from "@/lib/threatLevels";

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

const POLL_INTERVAL_MS = 60000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

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

// Cadence panel: fetches /detections/{id}/cadence on demand and renders it
// plainly, labeled with the real sample size backing each number so it never
// reads as a definitive classification. All numbers here come straight from
// the backend's statistics on real re-confirmation/session timestamps -- no
// value here is fabricated or defaulted to a guess.
function CadencePanel({ detectionId }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get(`/detections/${detectionId}/cadence`)
      .then(({ data }) => { if (!cancelled) setData(data); })
      .catch((e) => { if (!cancelled) setError(formatApiError(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [detectionId]);

  if (loading) return <div className="p-3 text-slate-500">loading cadence<span className="term-caret" /></div>;
  if (error) return <div className="p-3 text-[var(--accent-critical)]">{error}</div>;
  if (!data) return null;

  const { session, cross_session } = data;
  const sStats = session.interval_stats;
  const gStats = cross_session.gap_stats;

  return (
    <div className="p-4 grid grid-cols-1 md:grid-cols-2 gap-4 bg-[#0B111D]">
      <div>
        <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          Re-confirmation cadence (this contact)
        </div>
        <div className="text-slate-300">
          {session.reconfirm_count} re-confirmation{session.reconfirm_count === 1 ? "" : "s"}
          {session.observation_span_s != null && (
            <> over {(session.observation_span_s / 60).toFixed(1)}m observation window</>
          )}
        </div>
        {sStats ? (
          <div className="mt-1 text-slate-400">
            <div>mean interval: {sStats.mean_interval_s}s (n={sStats.sample_count} interval{sStats.sample_count === 1 ? "" : "s"})</div>
            <div>range: {sStats.min_interval_s}s – {sStats.max_interval_s}s</div>
            {sStats.stddev_interval_s != null ? (
              <div>stddev: {sStats.stddev_interval_s}s (CV {sStats.coefficient_of_variation})</div>
            ) : (
              <div className="text-slate-600">stddev not shown — fewer than 3 events, not enough to say</div>
            )}
          </div>
        ) : (
          <div className="mt-1 text-slate-600">Not enough re-confirmations yet to compute an interval.</div>
        )}
        <div className="mt-2 text-[10px] text-slate-600 italic">{session.note}</div>
      </div>
      <div>
        <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          Reappearance regularity (same source/model/protocol, across sessions)
        </div>
        <div className="text-slate-300">
          {cross_session.session_count} session{cross_session.session_count === 1 ? "" : "s"} seen,{" "}
          {cross_session.gap_count} silence gap{cross_session.gap_count === 1 ? "" : "s"} measured
        </div>
        {gStats ? (
          <div className="mt-1 text-slate-400">
            <div>mean gap: {gStats.mean_gap_s}s (n={gStats.sample_count})</div>
            <div>range: {gStats.min_gap_s}s – {gStats.max_gap_s}s</div>
            {gStats.stddev_gap_s != null ? (
              <div>stddev: {gStats.stddev_gap_s}s (CV {gStats.coefficient_of_variation})</div>
            ) : (
              <div className="text-slate-600">stddev not shown — fewer than 2 gaps, not enough to say</div>
            )}
          </div>
        ) : (
          <div className="mt-1 text-slate-600">Not enough distinct sessions yet to measure a reappearance gap.</div>
        )}
        <div className="mt-2 text-[10px] text-slate-600 italic">{cross_session.note}</div>
      </div>
    </div>
  );
}

export default function DetectionHistory() {
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [statusFilter, setStatusFilter] = useState("ALL");
  const [visibleCount, setVisibleCount] = useState(ROW_CAP);
  const [expandedId, setExpandedId] = useState(null);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);

  const load = async (cancelled) => {
    try {
      const { data } = await api.get("/detections");
      if (cancelled?.current) return;
      setDetections(data);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch (e) {
      if (cancelled?.current) return;
      setConsecutiveFailures((n) => n + 1);
      toast.error("Failed to load detection history", { description: formatApiError(e) });
    } finally {
      if (!cancelled?.current) setLoading(false);
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

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const monitoringDegraded = staleByAge || staleByFailures || neverSucceeded;

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

      <div
        data-testid="history-monitoring-degraded-banner"
        role="alert"
        className="tactical-border px-4 py-2 items-center gap-2 pulse-crit"
        style={{
          display: monitoringDegraded ? "flex" : "none",
          background: "#FF9500",
          color: "black",
        }}
      >
        <ShieldAlert size={14} strokeWidth={2} />
        <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
          MONITORING DEGRADED — detection feed stale, history below may be out of date
        </span>
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
                const isExpanded = expandedId === d.id;
                const unconfirmed = isUnconfirmedDetection(d);
                const showUnconfirmedTag = shouldShowUnconfirmedTag(d);
                const originalModelAnnotation = getOriginalModelAnnotation(d);
                const staticCritical = isStaticCritical(d);
                return (
                  <Fragment key={d.id}>
                  <tr data-testid={`hist-row-${d.id}`}
                      onClick={() => setExpandedId(isExpanded ? null : d.id)}
                      className={`tactical-border-b hover:bg-[#0F1626] transition-colors cursor-pointer ${staticCritical ? "threat-critical-static" : ""}`}>
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
                    <td className="p-2 text-slate-300">
                      {d.model}
                      {showUnconfirmedTag && <UnconfirmedTag />}
                      {originalModelAnnotation && (
                        <div className="text-[9px] text-slate-600" title={originalModelAnnotation.title}>
                          {originalModelAnnotation.label}: {d.original_model}
                        </div>
                      )}
                    </td>
                    <td className="p-2 text-slate-400">
                      {d.protocol}
                      {showUnconfirmedTag && <UnconfirmedTag />}
                    </td>
                    <td className="p-2">
                      <div className="flex flex-col items-start gap-0.5">
                        <span className="px-2 py-0.5 tactical-border font-bold text-[10px]"
                              style={{
                                color: THREAT_COLOR[d.threat_level],
                                borderColor: THREAT_COLOR[d.threat_level],
                                opacity: unconfirmed && d.threat_level === "MEDIUM" ? 0.6 : 1,
                              }}
                              title={unconfirmed && d.threat_level === "MEDIUM"
                                ? "Softened: threat level based on an unconfirmed RSSI/persistence heuristic only, no ML classification or protocol decode."
                                : undefined}>
                          {staticCritical && (
                            <AlertTriangle size={10} strokeWidth={2} className="inline mr-1 -mt-0.5" />
                          )}
                          {d.threat_level}
                        </span>
                        <MlClassifierBadge detection={d} />
                        <ConfidenceTypeBadge detection={d} />
                      </div>
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
                    <td className="p-2 text-[10px]">
                      <Link
                        to={`/signals?contact=${d.id}`}
                        data-testid={`cema-link-${d.id}`}
                        title="View in CEMA pipeline →"
                        onClick={(e) => e.stopPropagation()}
                        className="hover:underline underline-offset-2 decoration-dotted"
                        style={{ color: "var(--accent-info)" }}
                      >
                        {d.cema_stage}
                      </Link>
                    </td>
                    <td className="p-2 text-[10px]">
                      <Link
                        to={`/killchain?contact=${d.id}`}
                        data-testid={`kc-link-${d.id}`}
                        title="View in Kill Chain →"
                        onClick={(e) => e.stopPropagation()}
                        className="hover:underline underline-offset-2 decoration-dotted"
                        style={{ color: d.status === "NEUTRALIZED" ? "var(--accent-critical)" : "var(--accent-success)" }}
                      >
                        {d.status === "NEUTRALIZED" ? "DEFEAT" : d.kill_chain_stage}
                      </Link>
                    </td>
                  </tr>
                  {isExpanded && (
                    <tr className="tactical-border-b">
                      <td colSpan={12} className="p-0">
                        <CadencePanel detectionId={d.id} />
                      </td>
                    </tr>
                  )}
                  </Fragment>
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
