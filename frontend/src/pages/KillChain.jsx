import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import CytoscapeComponent from "react-cytoscapejs";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Crosshair, ChevronRight, CheckCircle2, Circle, Loader2, Skull, ShieldAlert } from "lucide-react";

const CHAIN = ["DETECT", "TRACK", "IDENTIFY", "DECIDE", "DEFEAT"];

const POLL_INTERVAL_MS = 5000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

// Layout geometry for the preset stage-column layout: one column per
// kill-chain stage, contacts stacked vertically within their stage's
// column. A preset layout (rather than a force-directed one) is used
// deliberately -- operators read left-to-right stage progression, and a
// physics-based layout would shuffle node positions on every poll tick,
// which is disorienting in a live-updating tactical display.
const COL_SPACING = 180;
const ROW_SPACING = 90;

// WCAG 1.4.1 (Use of Color): every graph-node state below is encoded by
// shape AND color, mirroring the icon+color convention from the flat-list
// view (task #37) so colorblind operators aren't relying on hue alone.
function stageVisual(d) {
  const idx = d.kill_chain_index;
  const defeated = d.status === "NEUTRALIZED";
  const awaitingAck = d.status === "AWAITING_ACK";
  const txFailed = d.status === "TX_FAILED";
  const txTimeout = d.status === "TX_TIMEOUT";
  if (defeated) return { color: "#FB3A5D", shape: "diamond", label: "NEUTRALIZED" };
  if (awaitingAck) return { color: "#F5A623", shape: "hexagon", label: "AWAITING ACK" };
  if (txFailed || txTimeout) return { color: "#FB3A5D", shape: "octagon", label: txFailed ? "TX FAILED" : "TX TIMEOUT" };
  // In-progress stage node
  return { color: "#00F0FF", shape: "ellipse", label: CHAIN[idx] || "DETECT" };
}

export default function KillChain() {
  const [dets, setDets] = useState([]);
  // Deep-link support: Dashboard/DetectionHistory link here as
  // /killchain?contact=<id> so an operator clicking a KC-stage cell on the
  // main tactical views is scrolled straight to that contact's row instead
  // of having to scan the flat list for it.
  const [searchParams] = useSearchParams();
  const deepLinkedId = searchParams.get("contact");
  const scrolledRef = useRef(false);
  const [selectedId, setSelectedId] = useState(null);
  const cyRef = useRef(null);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const load = async (cancelled) => {
    try {
      const { data } = await api.get("/detections");
      if (cancelled?.current) return;
      setDets(data);
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
  const monitoringDegraded = staleByAge || staleByFailures || neverSucceeded;

  useEffect(() => {
    if (!deepLinkedId || scrolledRef.current || dets.length === 0) return;
    const el = document.getElementById(`kc-row-${deepLinkedId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      scrolledRef.current = true;
    }
    setSelectedId(deepLinkedId);
  }, [dets, deepLinkedId]);

  const advance = async (id) => {
    try { await api.post(`/detections/${id}/killchain-advance`); load(); }
    catch (e) { toast.error("Advance failed", { description: formatApiError(e) }); }
  };

  // Build cytoscape elements from the live detection list. This is pure
  // derived state -- recomputed on every `dets` poll tick, no separate
  // graph state to keep in sync, so the graph updates in place on the
  // existing 5s poll without a remount (CytoscapeComponent diffs `elements`
  // by id and only touches what changed).
  const elements = useMemo(() => {
    const stageCounts = {};
    const nodes = dets.map((d) => {
      const idx = d.kill_chain_index ?? 0;
      const col = d.status === "NEUTRALIZED" ? CHAIN.length - 1 : idx;
      const row = stageCounts[col] || 0;
      stageCounts[col] = row + 1;
      const v = stageVisual(d);
      return {
        data: {
          id: d.id,
          label: `${d.callsign}\n${v.label}`,
          color: v.color,
          shape: v.shape,
          swarmId: d.swarm_id || null,
        },
        position: { x: col * COL_SPACING + 90, y: row * ROW_SPACING + 60 },
      };
    });

    // Swarm-cluster membership edges: contacts sharing a swarm_id (set by
    // backend/swarm_classifier.py's union-find clustering) are connected so
    // the graph visually groups coordinated swarm members regardless of
    // which kill-chain column they currently sit in.
    const bySwarm = {};
    dets.forEach((d) => { if (d.swarm_id) (bySwarm[d.swarm_id] ||= []).push(d.id); });
    const edges = [];
    Object.entries(bySwarm).forEach(([swarmId, ids]) => {
      for (let i = 1; i < ids.length; i++) {
        edges.push({ data: { id: `${swarmId}-${ids[0]}-${ids[i]}`, source: ids[0], target: ids[i], swarmId } });
      }
    });

    return [...nodes, ...edges];
  }, [dets]);

  const stylesheet = useMemo(() => [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        shape: "data(shape)",
        label: "data(label)",
        color: "#E2E8F0",
        "font-family": "monospace",
        "font-size": 9,
        "text-wrap": "wrap",
        "text-valign": "bottom",
        "text-margin-y": 6,
        width: 34,
        height: 34,
        "border-width": 2,
        "border-color": "#1E293B",
      },
    },
    {
      selector: "node:selected",
      style: { "border-color": "#00F0FF", "border-width": 3 },
    },
    {
      selector: "edge",
      style: {
        width: 2,
        "line-color": "var(--accent-warning, #F5A623)",
        "line-style": "dashed",
        "curve-style": "bezier",
        "target-arrow-shape": "none",
      },
    },
  ], []);

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> Kill Chain
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Detect → Track → Identify → Decide → Defeat
        </h1>
      </div>

      <div
        data-testid="killchain-monitoring-degraded-banner"
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
          MONITORING DEGRADED — kill-chain feed stale, statuses below (including AWAITING ACK /
          NEUTRALIZED) may be out of date
        </span>
      </div>

      {/* Node-link graph: one node per active detection, columns keyed to
          kill-chain stage, dashed edges linking swarm-cluster members. This
          is a companion visualization to the detail list below, not a
          replacement -- clicking a node highlights (and scrolls to) that
          contact's full detail row, which retains all WCAG icon+color
          encoding, status badges, and the ADVANCE control. */}
      <div
        className="tactical-border"
        style={{ background: "var(--bg-surface)" }}
        role="group"
        aria-label="Kill-chain contact graph — visual summary; see the detail list below for the same information in an accessible format"
      >
        <div className="px-4 pt-3 font-mono text-[10px] uppercase tracking-widest text-slate-500">
          contact graph · {dets.length} tracked
        </div>
        {dets.length === 0 ? (
          <div className="p-8 font-mono text-xs text-slate-600 text-center">
            no contacts under tracking<span className="term-caret" />
          </div>
        ) : (
          <CytoscapeComponent
            elements={CytoscapeComponent.normalizeElements(elements)}
            stylesheet={stylesheet}
            layout={{ name: "preset" }}
            style={{ width: "100%", height: 320 }}
            cy={(cy) => {
              cyRef.current = cy;
              cy.off("tap", "node");
              cy.on("tap", "node", (evt) => {
                const id = evt.target.id();
                setSelectedId(id);
                const el = document.getElementById(`kc-row-${id}`);
                if (el) el.scrollIntoView({ behavior: "smooth", block: "center" });
              });
            }}
          />
        )}
        <div className="px-4 pb-3 pt-1 font-mono text-[9px] uppercase tracking-widest text-slate-600 flex gap-4 flex-wrap">
          <span><span style={{ color: "#00F0FF" }}>●</span> in-progress</span>
          <span><span style={{ color: "#FB3A5D" }}>◆</span> neutralized</span>
          <span><span style={{ color: "#F5A623" }}>⬡</span> awaiting ack</span>
          <span><span style={{ color: "#FB3A5D" }}>⬣</span> tx failed/timeout</span>
          <span>┄ swarm-cluster link</span>
        </div>
      </div>

      <div className="space-y-0 tactical-border">
        {dets.length === 0 && (
          <div className="p-8 font-mono text-xs text-slate-600 text-center">
            no contacts under tracking<span className="term-caret" />
          </div>
        )}
        {dets.map((d) => {
          const idx = d.kill_chain_index;
          const defeated = d.status === "NEUTRALIZED";
          // Distinct bridge-ack states — see backend/server.py's
          // AWAITING_ACK → NEUTRALIZED / TX_FAILED / TX_TIMEOUT state
          // machine. "we requested this" (amber, pending) must never be
          // rendered the same as "this is confirmed" (green) or "this
          // failed" (red) — that conflation is exactly what caused the
          // earlier live-demo incident.
          const awaitingAck = d.status === "AWAITING_ACK";
          const txFailed = d.status === "TX_FAILED";
          const txTimeout = d.status === "TX_TIMEOUT";
          const terminalOrPending = defeated || awaitingAck || txFailed || txTimeout;
          const isDeepLinked = deepLinkedId === d.id || selectedId === d.id;
          return (
            <div key={d.id} data-testid={`kc-${d.id}`} id={`kc-row-${d.id}`}
                 className="p-5 tactical-border-b last:border-b-0"
                 style={{
                   background: "var(--bg-surface)",
                   outline: isDeepLinked ? "1px solid var(--accent-info)" : "none",
                   outlineOffset: -1,
                 }}>
              <div className="flex items-center justify-between mb-3">
                <div>
                  <div className="font-heading font-black text-xl tracking-tighter">
                    {d.callsign} · <span className="text-slate-500">{d.model}</span>
                  </div>
                  <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-0.5">
                    sys={d.system_id} · protocol={d.protocol} · threat={d.threat_level}
                    {d.swarm_id && <> · <span style={{color:"var(--accent-warning)"}}>{d.swarm_id}</span></>}
                    {d.last_payload && <> · last-payload={d.last_payload}</>}
                  </div>
                </div>
                {!terminalOrPending && (
                  <button
                    data-testid={`kc-advance-${d.id}`}
                    onClick={() => advance(d.id)}
                    className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn"
                    style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
                  >
                    ADVANCE <ChevronRight size={12} strokeWidth={1.5} />
                  </button>
                )}
                {awaitingAck && (
                  <span data-testid={`kc-status-${d.id}`}
                        className="px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest blink"
                        style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}>
                    ◐ AWAITING ACK
                  </span>
                )}
                {defeated && (
                  <span data-testid={`kc-status-${d.id}`}
                        className="px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest pulse-crit"
                        style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
                    ● NEUTRALIZED
                  </span>
                )}
                {(txFailed || txTimeout) && (
                  <span data-testid={`kc-status-${d.id}`}
                        className="px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest pulse-crit"
                        style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
                    ✕ {txFailed ? "TX FAILED" : "TX TIMEOUT"}
                  </span>
                )}
              </div>

              <div className="grid grid-cols-5 gap-0 tactical-border">
                {CHAIN.map((step, i) => {
                  const done = defeated ? true : i < idx;
                  const active = !defeated && i === idx;
                  const isDefeat = i === 4 && defeated;
                  const pending = !isDefeat && !done && !active;
                  // WCAG 1.4.1 (Use of Color): each kill-chain node state is
                  // conveyed by icon shape + text label in addition to color,
                  // so colorblind operators aren't relying on hue alone to
                  // read stage progression. "pending" uses a lighter muted
                  // tone (#94A3B8-equivalent) than --text-muted, which fails
                  // the 3:1 UI-component contrast minimum against this bg.
                  const nodeColor = isDefeat ? "var(--accent-critical)"
                    : done ? "var(--accent-success)"
                    : active ? "var(--accent-info)" : "#94A3B8";
                  const NodeIcon = isDefeat ? Skull : done ? CheckCircle2 : active ? Loader2 : Circle;
                  const stateLabel = isDefeat ? "NEUTRALIZED" : done ? "COMPLETE" : active ? "IN PROGRESS" : "PENDING";
                  return (
                    <div
                      key={step}
                      className={`p-4 kc-node text-center tactical-border-r last:border-r-0 ${
                        isDefeat ? "defeat" : done ? "done" : active ? "active" : ""
                      }`}
                      style={{ color: nodeColor }}
                    >
                      <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
                        STAGE {i + 1}
                      </div>
                      <div className="flex items-center justify-center gap-1.5 mb-1">
                        <NodeIcon
                          size={14}
                          strokeWidth={2}
                          className={active ? "animate-spin" : ""}
                          aria-hidden="true"
                        />
                        <div className="font-heading font-black text-lg tracking-tighter uppercase">{step}</div>
                      </div>
                      <div className={`font-mono text-[9px] uppercase tracking-widest ${active ? "blink" : ""}`}>
                        {stateLabel}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
