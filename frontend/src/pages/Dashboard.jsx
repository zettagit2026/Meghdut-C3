import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import ReactECharts from "echarts-for-react";
import { Radar, Skull, Activity, Signal, TrendingUp, AlertTriangle, Volume2, VolumeX } from "lucide-react";
import SystemHealth from "@/components/SystemHealth";
import MlClassifierBadge from "@/components/MlClassifierBadge";
import ConfidenceTypeBadge from "@/components/ConfidenceTypeBadge";
import UnconfirmedTag from "@/components/UnconfirmedTag";
import { isUnconfirmedDetection, shouldShowUnconfirmedTag, getOriginalModelAnnotation } from "@/lib/detectionConfidence";
import { isRecentCritical, isStaticCritical } from "@/lib/threatSalience";
import { announceNewCriticalContacts, isCriticalAlertMuted, setCriticalAlertMuted } from "@/lib/criticalAlertSound";
import { THREAT_COLOR } from "@/lib/threatLevels";
import { INFERNO_STOPS, SPECTRUM_FLOOR_DBM, SPECTRUM_CEIL_DBM } from "@/lib/spectrumColormap";

// Task #117: "Export IQ for RE analysis". Downloads the SigMF .sigmf-data/
// .sigmf-meta pair (see backend GET /detections/{id}/iq-export) as a zip so
// an analyst can hand it to URH (Universal Radio Hacker, external tool) for
// manual signal inspection. This does NOT do any demodulation/decoding
// itself -- it only fetches bytes the backend already assembled and saves
// them locally via a blob download (a plain <a href> can't carry the
// Bearer auth header, so this goes through the authenticated `api` client).
async function exportIqCapture(detection) {
  try {
    const res = await api.get(`/detections/${detection.id}/iq-export`, { responseType: "blob" });
    const cd = res.headers?.["content-disposition"] || "";
    const match = /filename="?([^"]+)"?/.exec(cd);
    const filename = match ? match[1] : `${detection.id}_iq_export.zip`;
    const url = window.URL.createObjectURL(new Blob([res.data]));
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
    toast.success(`IQ capture exported: ${filename}`);
  } catch (e) {
    // Honest failure surfacing -- most commonly "no capture attached yet"
    // (404 from the backend), not a fabricated success.
    let message = formatApiError(e);
    if (e?.response?.data instanceof Blob) {
      try {
        const text = await e.response.data.text();
        message = formatApiError({ response: { data: JSON.parse(text) } });
      } catch { /* keep generic message */ }
    }
    toast.error(`IQ export failed: ${message}`);
  }
}

// dBm -> waterfall color. Uses the shared, ABSOLUTE, calibrated colormap
// (perceptually-uniform "inferno" ramp; fixed floor/ceiling in
// lib/spectrumColormap.js) instead of the old per-frame blue->cyan->yellow->red
// rainbow, so a given color always means the same power level and there is no
// false banding at color transitions. ECharts interpolates across these stops.
const WATERFALL_COLOR_STOPS = INFERNO_STOPS;
const WF_MIN_DBM = SPECTRUM_FLOOR_DBM;
const WF_MAX_DBM = SPECTRUM_CEIL_DBM;

const WF_POLL_INTERVAL_MS = 2500;
const WF_MAX_CONSECUTIVE_FAILURES = 3;
const WF_STALE_THRESHOLD_MS = WF_POLL_INTERVAL_MS * 4;

function Waterfall() {
  const [rows, setRows] = useState([]);
  const [source, setSource] = useState("NONE");
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const chartRef = useRef(null);

  useEffect(() => {
    let id;
    let cancelled = false;
    const load = async () => {
      try {
        const { data } = await api.get("/spectrum/waterfall?bins=96&rows=24");
        if (cancelled) return;
        setRows(data.rows || []);
        setSource(data.source || "NONE");
        setLastSuccessAt(Date.now());
        setConsecutiveFailures(0);
      } catch {
        if (cancelled) return;
        setConsecutiveFailures((n) => n + 1);
      }
    };
    load();
    id = setInterval(load, WF_POLL_INTERVAL_MS);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const binCount = rows[0]?.length || 96;
  const rowCount = rows.length;

  // Imperative setOption on the existing chart instance on every poll tick,
  // instead of re-rendering <ReactECharts option=.../> with a brand new
  // option object -- avoids a full chart teardown/remount on each of the
  // 2.5s polls so the canvas keeps its zoom/size and only repaints data.
  useEffect(() => {
    const inst = chartRef.current?.getEchartsInstance?.();
    if (!inst) return;
    const heatData = [];
    rows.forEach((row, ri) => {
      row.forEach((v, ci) => {
        heatData.push([ci, ri, Math.round(v * 10) / 10]);
      });
    });
    inst.setOption({
      series: [{ data: heatData }],
      yAxis: { data: Array.from({ length: rowCount }, (_, i) => i) },
      xAxis: { data: Array.from({ length: binCount }, (_, i) => i) },
    });
  }, [rows, binCount, rowCount]);

  const baseOption = useMemo(() => ({
    backgroundColor: "transparent",
    animation: false,
    grid: { left: 36, right: 64, top: 8, bottom: 22, containLabel: false },
    tooltip: {
      trigger: "item",
      backgroundColor: "#0C111D",
      borderColor: "rgba(255,255,255,0.15)",
      textStyle: { color: "#F8FAFC", fontFamily: "JetBrains Mono, monospace", fontSize: 10 },
      formatter: (p) => {
        const freq = 2.400 + (p.value[0] / Math.max(1, binCount - 1)) * 0.1;
        return `${freq.toFixed(3)} GHz · row -${p.value[1]}<br/>${p.value[2]} dBm`;
      },
    },
    xAxis: {
      type: "category",
      data: Array.from({ length: binCount }, (_, i) => i),
      show: false,
      boundaryGap: false,
    },
    yAxis: {
      type: "category",
      data: Array.from({ length: rowCount || 1 }, (_, i) => i),
      show: false,
      inverse: true, // row 0 (most recent sample) rendered at top, matching prior div-stack order
    },
    // Continuous colorbar legend -- a waterfall without a dBm scale is not a
    // measurement. Fixed min/max (WF_MIN/MAX_DBM) so the scale is absolute.
    visualMap: {
      show: true,
      type: "continuous",
      min: WF_MIN_DBM,
      max: WF_MAX_DBM,
      calculable: false,
      itemHeight: 120,
      itemWidth: 10,
      right: 8,
      top: "center",
      text: [`${WF_MAX_DBM} dBm`, `${WF_MIN_DBM}`],
      textStyle: { color: "#94A3B8", fontFamily: "JetBrains Mono, monospace", fontSize: 9 },
      inRange: { color: WATERFALL_COLOR_STOPS },
    },
    series: [
      {
        type: "heatmap",
        data: [],
        progressive: 2000,
        itemStyle: { borderWidth: 0 },
      },
    ],
  }), [binCount, rowCount]);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > WF_STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= WF_MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const pollStale = staleByAge || staleByFailures || neverSucceeded;

  const isReal = source === "HACKRF" && !pollStale;

  return (
    <div data-testid="rf-waterfall" className="tactical-border" style={{ background: "var(--bg-terminal)" }}>
      <div className="flex items-center justify-between px-3 py-2 tactical-border-b">
        <div className="flex items-center gap-2">
          <Signal size={12} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-400">
            RF Spectrum Waterfall · 2.400–2.500 GHz · 25 MSPS
          </span>
          <span
            data-testid="waterfall-source"
            role="status"
            className="px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest tactical-border"
            style={{
              color: pollStale ? "var(--accent-critical)" : isReal ? "var(--accent-success)" : "var(--accent-warning)",
              borderColor: pollStale ? "var(--accent-critical)" : isReal ? "var(--accent-success)" : "var(--accent-warning)",
              background: pollStale
                ? "color-mix(in srgb, var(--accent-critical) 8%, transparent)"
                : isReal
                ? "color-mix(in srgb, var(--accent-success) 8%, transparent)"
                : "color-mix(in srgb, var(--accent-warning) 6%, transparent)",
            }}
          >
            {pollStale ? "◌ STALE" : isReal ? "● HACKRF LIVE" : "◌ NO SIGNAL"}
          </span>
        </div>
        <span className="font-mono text-[10px] text-slate-600 blink">● LIVE</span>
      </div>
      <div className="p-2 relative" style={{ height: 200 }}>
        {rows.length === 0 && (
          <div className="font-mono text-xs text-slate-600 p-6 text-center absolute inset-0 flex items-center justify-center">
            capturing IQ stream<span className="term-caret"></span>
          </div>
        )}
        <ReactECharts
          ref={chartRef}
          option={baseOption}
          notMerge={false}
          lazyUpdate={true}
          style={{ height: "100%", width: "100%", opacity: rows.length === 0 ? 0 : 1 }}
          opts={{ renderer: "canvas" }}
        />
      </div>
      <div className="tactical-border-t px-3 py-1 flex justify-between font-mono text-[10px] text-slate-600">
        <span>2.400</span><span>2.425</span><span>2.450</span><span>2.475</span><span>2.500 GHz</span>
      </div>
    </div>
  );
}

function StatTile({ label, value, sub, color = "var(--accent-info)", testid }) {
  return (
    <div data-testid={testid} className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
      <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">{label}</div>
      <div className="font-heading font-black text-4xl tracking-tighter mt-1 tnum" style={{ color }}>
        {value}
      </div>
      {sub && (
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">{sub}</div>
      )}
    </div>
  );
}

// Small local relative-time formatter (no date lib in this project) so
// "LAST SEEN" reads as "3s ago" / "2m ago" / "1h ago" and keeps ticking live.
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

export default function Dashboard() {
  const [detections, setDetections] = useState([]);
  const [loading, setLoading] = useState(true);
  const [now, setNow] = useState(() => Date.now());
  const [alertMuted, setAlertMuted] = useState(() => isCriticalAlertMuted());

  const load = async (cancelledRef) => {
    try {
      const { data } = await api.get("/detections");
      if (cancelledRef?.current) return;
      setDetections(data);
    } catch (e) {
      if (cancelledRef?.current) return;
      toast.error("Failed to load detections", { description: formatApiError(e) });
    } finally {
      if (!cancelledRef?.current) setLoading(false);
    }
  };

  useEffect(() => {
    const cancelledRef = { current: false };
    load(cancelledRef);
    const id = setInterval(() => load(cancelledRef), 2500);
    return () => { cancelledRef.current = true; clearInterval(id); };
  }, []);

  // Salience escalation (Task #38 / C5): fire the one-shot audio cue for any
  // CRITICAL contact that is brand-new on this data load. announceNewCriticalContacts
  // itself de-dupes per detection id forever, so this is safe to call on every
  // load/poll without re-announcing a contact the operator has already heard.
  useEffect(() => {
    const criticalIds = detections
      .filter((d) => d.status === "ACTIVE" && d.threat_level === "CRITICAL")
      .map((d) => d.id);
    announceNewCriticalContacts(criticalIds);
  }, [detections]);

  const toggleAlertMute = () => {
    const next = !alertMuted;
    setAlertMuted(next);
    setCriticalAlertMuted(next);
  };

  // Ticks once a second purely to keep the "LAST SEEN" relative-time column
  // live without needing to re-fetch detections.
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const active = detections.filter((d) => d.status === "ACTIVE");
  const swarmCount = new Set(active.filter((d) => d.swarm_id).map((d) => d.swarm_id)).size;
  const critical = active.filter((d) => d.threat_level === "CRITICAL").length;
  const neutralized = detections.filter((d) => d.status === "NEUTRALIZED").length;
  // All detections now originate exclusively from real hardware ingest
  // (HackRF / SiK radio bridges via POST /detections/ingest) — there is no
  // longer any code path that injects a simulated contact, so every source
  // present here is a live feed by construction.
  const liveSources = new Set(detections.map((d) => d.source).filter(Boolean));

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1 flex items-center gap-3">
            <Radar size={12} className="inline" strokeWidth={1.5} /> Command Center
            {liveSources.size > 0 && (
              <span
                data-testid="live-hw-indicator"
                className="px-2 py-0.5 tactical-border font-mono text-[10px] font-bold"
                style={{
                  color: "var(--accent-success)",
                  borderColor: "var(--accent-success)",
                  background: "color-mix(in srgb, var(--accent-success) 8%, transparent)",
                }}
              >
                ● {Array.from(liveSources).join(" + ")} LIVE
              </span>
            )}
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Tactical Overview
          </h1>
        </div>
        <button
          type="button"
          data-testid="critical-alert-mute-toggle"
          onClick={toggleAlertMute}
          title={alertMuted
            ? "Critical-contact audio cue is muted. Click to unmute."
            : "Audio cue plays once per newly-detected CRITICAL contact. Click to mute."}
          className="tactical-border px-2.5 py-1.5 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-slate-400 hover:text-white transition-colors"
        >
          {alertMuted ? <VolumeX size={13} strokeWidth={1.5} /> : <Volume2 size={13} strokeWidth={1.5} />}
          Critical Alert Tone {alertMuted ? "Muted" : "On"}
        </button>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-0 tactical-border">
        <div className="tactical-border-r">
          <StatTile testid="stat-active" label="Active Contacts" value={active.length} sub="Airspace targets" />
        </div>
        <div className="tactical-border-r">
          <StatTile testid="stat-critical" label="Critical Threats" value={critical}
                    color="var(--accent-critical)" sub="Immediate engagement" />
        </div>
        <div className="tactical-border-r">
          <StatTile testid="stat-swarms" label="Swarms Detected" value={swarmCount}
                    color="var(--accent-warning)" sub="Coordinated clusters" />
        </div>
        <StatTile testid="stat-neutralized" label="Neutralized" value={neutralized}
                  color="var(--accent-success)" sub="Kill-chain complete" />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div className="xl:col-span-2 space-y-6">
          <Waterfall />

          <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
            <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Activity size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
                <span className="font-mono text-xs uppercase tracking-widest">Active Contacts</span>
              </div>
              <div className="flex items-center gap-3">
                <span className="font-mono text-[10px] text-slate-500">{active.length} tracked</span>
              </div>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-xs" data-testid="detections-table">
                <thead>
                  <tr className="tactical-border-b font-mono text-[10px] uppercase tracking-widest text-slate-500">
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
                    <th className="text-left p-2">IQ</th>
                  </tr>
                </thead>
                <tbody className="font-mono">
                  {loading && (
                    <tr><td colSpan={11} className="p-4 text-center text-slate-500">acquiring<span className="term-caret" /></td></tr>
                  )}
                  {!loading && active.length === 0 && (
                    <tr>
                      <td colSpan={11} className="p-4 text-center text-slate-500">
                        {detections.length === 0
                          ? "No contacts. Start the RF bridge to begin detecting."
                          : "No active contacts in the last 10 minutes."}
                      </td>
                    </tr>
                  )}
                  {active.map((d) => {
                    const src = d.source || "UNKNOWN";
                    const unconfirmed = isUnconfirmedDetection(d);
                    const showUnconfirmedTag = shouldShowUnconfirmedTag(d);
                    const originalModelAnnotation = getOriginalModelAnnotation(d);
                    const recentCritical = isRecentCritical(d, now);
                    const staticCritical = isStaticCritical(d);
                    return (
                      <tr key={d.id} data-testid={`row-${d.id}`}
                          className={`tactical-border-b hover-surface transition-colors ${staticCritical ? "threat-critical-static" : ""} ${recentCritical ? "threat-critical-flash" : ""}`}>
                        <td className="p-2">
                          <span data-testid={`src-${d.id}`}
                                className="px-1.5 py-0.5 tactical-border font-bold text-[9px]"
                                style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}>
                            {src}
                          </span>
                        </td>
                        <td className="p-2 text-white font-bold" style={{ color: "var(--text-primary)" }}>{d.callsign}</td>
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
                            <span title="RSSI-based estimate, not a precise measurement">
                              ~{d.distance_m}
                            </span>
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
                            className="hover:underline underline-offset-2 decoration-dotted"
                            style={{ color: d.status === "NEUTRALIZED" ? "var(--accent-critical)" : "var(--accent-success)" }}
                          >
                            {d.status === "NEUTRALIZED" ? "DEFEAT" : d.kill_chain_stage}
                          </Link>
                        </td>
                        <td className="p-2 text-[10px]">
                          {d.confidence_type === "unclassified_signal" && (
                            <button
                              type="button"
                              data-testid={`iq-export-${d.id}`}
                              onClick={() => exportIqCapture(d)}
                              title="Export the associated SigMF IQ capture (.sigmf-data + .sigmf-meta) for manual RE analysis in URH -- fails honestly with a 404 if no capture has been attached to this contact yet"
                              className="px-1.5 py-0.5 tactical-border font-mono uppercase tracking-wide hover:bg-[#1A2436] transition-colors"
                              style={{ color: "#C77D3D", borderColor: "#8A5A2C" }}
                            >
                              Export IQ
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <div className="space-y-6">
          <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
            <div className="tactical-border-b px-4 py-3 flex items-center gap-2">
              <Skull size={14} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
              <span className="font-mono text-xs uppercase tracking-widest">Priority Targets</span>
            </div>
          <div className="p-4 space-y-3">
            {active
              .slice()
              .sort((a, b) => ({ CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 }[b.threat_level] - { CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0 }[a.threat_level]))
              .slice(0, 6)
              .map((d) => {
                const unconfirmed = isUnconfirmedDetection(d);
                const showUnconfirmedTag = shouldShowUnconfirmedTag(d);
                const originalModelAnnotation = getOriginalModelAnnotation(d);
                const recentCritical = isRecentCritical(d, now);
                const staticCritical = isStaticCritical(d);
                return (
                <div key={d.id}
                     className={`tactical-border p-3 ${staticCritical ? "threat-critical-static" : ""} ${recentCritical ? "threat-critical-flash" : ""}`}>
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-mono text-xs text-white font-bold" style={{ color: "var(--text-primary)" }}>{d.callsign}</span>
                    <span className="px-2 py-0.5 tactical-border font-mono font-bold text-[10px]"
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
                  </div>
                  <div className="font-mono text-[10px] text-slate-500 space-y-0.5">
                    <div>
                      MODEL: <span className="text-slate-300">{d.model}</span>
                      {showUnconfirmedTag && <UnconfirmedTag />}
                      {originalModelAnnotation && (
                        <span className="ml-1 text-slate-600" title={originalModelAnnotation.title}>
                          ({originalModelAnnotation.label}: {d.original_model})
                        </span>
                      )}
                    </div>
                    <div>BEARING: <span className="text-slate-300">{(d.bearing_deg === null || d.bearing_deg === undefined || d.bearing_available === false) ? "UNKNOWN (no DF array)" : `${d.bearing_deg}°${d.bearing_estimated ? ` ±${d.bearing_uncertainty_deg ?? "?"}° est.` : ""}`}</span> · ALT: <span className="text-slate-300">{d.altitude_m}m</span></div>
                    <div className="flex items-center gap-2">
                      <TrendingUp size={10} strokeWidth={1.5} /> {d.speed_ms} m/s
                      {d.swarm_id && (
                        <span className="ml-auto text-[10px]" style={{ color: "var(--accent-warning)" }}>{d.swarm_id}</span>
                      )}
                    </div>
                  </div>
                </div>
                );
              })}
            {active.length === 0 && (
              <div className="font-mono text-xs text-slate-600 text-center py-4">— NO ACTIVE TARGETS —</div>
            )}
          </div>
          </div>
          <SystemHealth />
        </div>
      </div>
    </div>
  );
}
