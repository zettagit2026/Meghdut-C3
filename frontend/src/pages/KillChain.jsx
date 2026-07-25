import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Crosshair, ChevronRight, CheckCircle2, Circle, Loader2, Skull } from "lucide-react";

const CHAIN = ["DETECT", "TRACK", "IDENTIFY", "DECIDE", "DEFEAT"];

export default function KillChain() {
  const [dets, setDets] = useState([]);
  // Deep-link support: Dashboard/DetectionHistory link here as
  // /killchain?contact=<id> so an operator clicking a KC-stage cell on the
  // main tactical views is scrolled straight to that contact's row instead
  // of having to scan the flat list for it.
  const [searchParams] = useSearchParams();
  const deepLinkedId = searchParams.get("contact");
  const scrolledRef = useRef(false);

  const load = async () => {
    try { const { data } = await api.get("/detections"); setDets(data); }
    catch (e) { toast.error("Load failed", { description: formatApiError(e) }); }
  };
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []);

  useEffect(() => {
    if (!deepLinkedId || scrolledRef.current || dets.length === 0) return;
    const el = document.getElementById(`kc-row-${deepLinkedId}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      scrolledRef.current = true;
    }
  }, [dets, deepLinkedId]);

  const advance = async (id) => {
    try { await api.post(`/detections/${id}/killchain-advance`); load(); }
    catch (e) { toast.error("Advance failed", { description: formatApiError(e) }); }
  };

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
          const isDeepLinked = deepLinkedId === d.id;
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
