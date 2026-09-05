import { useEffect, useRef, useState } from "react";
import { AlertTriangle } from "lucide-react";
import { api, formatApiError, wsUrl } from "@/lib/api";
import { announceNewCriticalContacts } from "@/lib/criticalAlertSound";
import { toast } from "sonner";

// Docked SOP-rule-firing annunciation surface (RFI 4.5.8 / 4.5.13).
// Loads the recent-firings backlog once (GET /sop/alerts), then subscribes to
// the live SOP push over the SAME ws channel every other module already uses
// (`/api/ws/mavlink`, see MavlinkConsole.jsx) — the backend broadcasts every
// message type (packet/jam_status/sop_alert/...) on this one connection, it
// is never mavlink-specific. Reconnect/backoff mirrors MavlinkConsole.jsx.
//
// Every entry here is, at most, a PROPOSED recommendation the commander still
// clears through the existing gated engagement flow — there is no fire
// control on this panel (see .omc/plans/zone-sop-engine.md §invariant 1).
const SEVERITY_COLOR = {
  INFO: "var(--accent-info)",
  CAUTION: "var(--accent-warning)",
  WARNING: "var(--threat-high)",
  CRITICAL: "var(--accent-critical)",
};

function severityColor(severity) {
  return SEVERITY_COLOR[severity] || "var(--text-muted)";
}

export default function AnnunciationPanel() {
  const [alerts, setAlerts] = useState([]);
  const [ackingIds, setAckingIds] = useState(() => new Set());
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  useEffect(() => {
    unmountedRef.current = false;

    // Backlog first. A failed load leaves the panel honestly empty rather
    // than showing stale/fabricated entries.
    api
      .get("/sop/alerts")
      .then(({ data }) => {
        if (unmountedRef.current) return;
        setAlerts(Array.isArray(data?.alerts) ? data.alerts : []);
      })
      .catch(() => {});

    const MAX_BACKOFF_MS = 15000;
    const NO_AUTH_RETRY_MS = 1500;

    const scheduleReconnect = () => {
      if (unmountedRef.current) return;
      const attempt = attemptRef.current;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attemptRef.current = attempt + 1;
      reconnectTimerRef.current = setTimeout(connect, delay);
    };

    const connect = () => {
      if (unmountedRef.current) return;

      // Re-read the token fresh on every attempt so a rotated/late token is
      // picked up without requiring a page reload.
      const token = localStorage.getItem("cema_token");
      if (!token) {
        reconnectTimerRef.current = setTimeout(connect, NO_AUTH_RETRY_MS);
        return;
      }

      const ws = new WebSocket(`${wsUrl("/api/ws/mavlink")}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg?.type !== "sop_alert" || !msg.alert) return;
          const incoming = msg.alert;
          setAlerts((prev) => (prev.some((a) => a.id === incoming.id) ? prev : [incoming, ...prev]));
          // Audible cue on a NEW CRITICAL firing only — never on backlog load
          // (this branch only runs for live ws pushes) and never on
          // lower severities (criticalAlertSound already CRITICAL-only, but
          // gate here too so this call site stays honest on its own).
          if (incoming.severity === "CRITICAL") {
            announceNewCriticalContacts([incoming.id]);
          }
        } catch {
          /* malformed frame — ignore, keep the connection alive */
        }
      };

      ws.onerror = () => {
        // onclose fires right after and handles reconnect scheduling.
      };

      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (unmountedRef.current) return;
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      unmountedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current) {
        const ws = wsRef.current;
        wsRef.current = null;
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
      }
    };
  }, []);

  const ack = async (id) => {
    if (ackingIds.has(id)) return;
    setAckingIds((prev) => new Set(prev).add(id));
    try {
      const { data } = await api.post(`/sop/alerts/${id}/ack`);
      setAlerts((prev) => prev.map((a) => (a.id === id ? { ...a, ...data } : a)));
    } catch (e) {
      toast.error("Acknowledge failed", { description: formatApiError(e) });
    } finally {
      setAckingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  return (
    <div className="tactical-border p-3" style={{ background: "var(--bg-surface)" }} data-testid="annunciation-panel">
      <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-2 flex items-center gap-2">
        <AlertTriangle size={12} strokeWidth={1.5} />
        Annunciations
        {alerts.length > 0 && <span style={{ color: "var(--text-muted)" }}>({alerts.length})</span>}
      </div>

      {alerts.length === 0 ? (
        <div className="font-mono text-[10px] text-slate-600" data-testid="annunciation-empty">
          no active annunciations
        </div>
      ) : (
        <div className="space-y-2 max-h-72 overflow-y-auto">
          {alerts.map((a) => {
            const color = severityColor(a.severity);
            const acked = !!a.acknowledged_by;
            return (
              <div
                key={a.id}
                data-testid="annunciation-item"
                className="flex items-start gap-3 p-2 tactical-border"
                style={{ borderColor: color, opacity: acked ? 0.55 : 1 }}
              >
                <span
                  className="shrink-0 px-1.5 py-0.5 font-mono text-[9px] font-bold uppercase tracking-widest"
                  style={{ color, border: `1px solid ${color}` }}
                >
                  {a.severity || "INFO"}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-mono text-xs" style={{ color: "var(--text-primary)" }}>
                    {a.rule_name || "SOP Rule"}
                  </div>
                  {a.message && <div className="font-mono text-[11px] text-slate-400">{a.message}</div>}
                  <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[9px] uppercase tracking-widest text-slate-500">
                    {a.zone_id && <span>ZONE {String(a.zone_id).slice(0, 8)}</span>}
                    <span>{a.ts ? new Date(a.ts).toLocaleTimeString() : "—"}</span>
                    {acked && (
                      <span style={{ color: "var(--accent-success)" }}>
                        ACKED{a.acknowledged_by ? ` — ${a.acknowledged_by}` : ""}
                      </span>
                    )}
                  </div>
                  {a.cue && (
                    <div
                      data-testid="annunciation-cue-tag"
                      className="mt-1.5 px-2 py-1 font-mono text-[9px] uppercase tracking-widest"
                      style={{ border: "1px dashed var(--accent-warning)", color: "var(--accent-warning)" }}
                    >
                      COMMANDER REVIEW REQUIRED — {a.cue.status || "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"}
                      {a.cue.recommended_effect && <> · recommended: {a.cue.recommended_effect}</>}
                      <div className="mt-0.5 normal-case tracking-normal text-slate-400">
                        This is a recommendation only — nothing has been fired or armed.
                      </div>
                    </div>
                  )}
                </div>
                {!acked && (
                  <button
                    data-testid={`annunciation-ack-${a.id}`}
                    onClick={() => ack(a.id)}
                    disabled={ackingIds.has(a.id)}
                    className="shrink-0 px-2 py-1 tactical-border font-mono text-[9px] uppercase tracking-widest hover-surface disabled:opacity-40"
                  >
                    ACK
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
