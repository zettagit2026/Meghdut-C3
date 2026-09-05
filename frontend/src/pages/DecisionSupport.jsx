import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Crosshair, RefreshCw, ShieldAlert, ChevronDown, ChevronRight, Lock, ExternalLink,
  Radio, Satellite, RadioTower, AlertTriangle,
} from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

// Decision Support panel (RFI Northern Command 4.5.3 / 4.5.4 / 4.5.6 / 4.5.7,
// feeds 4.2.2 manual C2). Commander-cued recommendations ONLY — NO auto-fire.
//
// This panel calls exactly two endpoints, both commander-only and both
// side-effect-free with respect to transmission:
//   GET  /api/effector/recommendations            (poll, read-only)
//   POST /api/effector/recommendations/recompute   (recompute + audit only)
// There is deliberately NO execute/engage/fire endpoint here. The ONLY way to
// act on a recommendation is to follow the deep-link into the EXISTING
// gated engagement pages (Jamming / GNSS Spoof / SDR MAVLink Inject), where
// the commander still has to clear SafetyGate + RangeAuthorizationControl +
// arm/confirm/tx-halt exactly as today. Never add a fire button here.

const POLL_MS = 5000;

const VERDICT_STYLE = {
  FEASIBLE: { color: "var(--accent-success)", label: "FEASIBLE" },
  FEASIBLE_UNVERIFIED_RANGE: { color: "var(--accent-warning)", label: "FEASIBLE · RANGE UNVERIFIED" },
  FEASIBLE_PLACEHOLDER_V1: { color: "var(--accent-warning)", label: "FEASIBLE · V1 PLACEHOLDER" },
  NOT_FEASIBLE: { color: "var(--accent-critical)", label: "NOT FEASIBLE" },
  UNKNOWN: { color: "var(--text-muted)", label: "UNKNOWN" },
};

const THREAT_STYLE = {
  CRITICAL: { color: "var(--accent-critical)" },
  HIGH: { color: "var(--accent-warning)" },
  MEDIUM: { color: "var(--accent-info)" },
  LOW: { color: "var(--accent-success)" },
  UNKNOWN: { color: "var(--text-muted)" },
};

// The ONLY places a commander can actually act on a recommendation — the
// pre-existing gated engagement pages. Route paths must match App.js.
const EFFECTOR_ROUTE = {
  jam: { to: "/jamming", label: "RF BARRAGE JAM", icon: Radio },
  gnss_deny: { to: "/gnss-spoof", label: "GNSS SPOOF", icon: Satellite },
  mavlink_takeover: { to: "/sdr-mavlink-inject", label: "SDR MAVLINK INJECT", icon: RadioTower },
};

function VerdictBadge({ verdict, testid }) {
  const s = VERDICT_STYLE[verdict] || VERDICT_STYLE.UNKNOWN;
  return (
    <span
      data-testid={testid}
      title={s.label}
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: s.color, borderColor: s.color }}
    >
      {s.label}
    </span>
  );
}

function FeasibilityCell({ kind, testidPrefix, id, feasibility }) {
  const f = feasibility || {};
  const labelMap = { jam: "JAM", gnss_deny: "GNSS-DENY", mavlink_takeover: "TAKEOVER" };
  return (
    <div className="flex flex-col gap-1">
      <div className="font-mono text-[9px] uppercase tracking-widest text-slate-500">{labelMap[kind]}</div>
      <VerdictBadge verdict={f.verdict} testid={`decision-feasibility-${testidPrefix}-${id}`} />
      {f.rationale && (
        <div className="font-mono text-[9px] leading-relaxed text-slate-500 max-w-[220px]">{f.rationale}</div>
      )}
      {f.link_class && (
        <div className="font-mono text-[9px] text-slate-600">link: {f.link_class}</div>
      )}
    </div>
  );
}

function ScoreBreakdown({ breakdown }) {
  const b = breakdown || {};
  const entries = Object.entries(b);
  if (entries.length === 0) {
    return <div className="font-mono text-[10px] text-slate-600">No score breakdown reported.</div>;
  }
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 font-mono text-[10px]">
      {entries.map(([k, v]) => (
        <div key={k} className="tactical-border p-2" style={{ background: "var(--bg-elev)" }}>
          <div className="text-[9px] uppercase tracking-widest text-slate-500">{k}</div>
          <div className="text-slate-200 mt-0.5">{v === null || v === undefined ? "—" : String(v)}</div>
        </div>
      ))}
    </div>
  );
}

function FailoverList({ id, failoverOrder }) {
  const list = failoverOrder || [];
  if (list.length === 0) {
    return <div className="font-mono text-[10px] text-slate-600">No failover effectors reported.</div>;
  }
  return (
    <div className="space-y-1" data-testid={`decision-failover-${id}`}>
      {list.map((f, i) => (
        <div
          key={`${f.effector}-${i}`}
          className="flex items-center justify-between gap-3 font-mono text-[10px] tactical-border px-2 py-1"
          style={{ background: "var(--bg-elev)" }}
        >
          <span className="text-slate-300 uppercase tracking-widest">{i + 1}. {f.effector}</span>
          <span
            className="uppercase tracking-widest"
            style={{ color: f.available ? "var(--accent-success)" : "var(--accent-critical)" }}
          >
            {f.available ? "AVAILABLE" : "UNAVAILABLE"}
          </span>
          {f.reason && <span className="text-slate-500 flex-1 text-right">{f.reason}</span>}
        </div>
      ))}
    </div>
  );
}

function RecommendationRow({ rec }) {
  const [expanded, setExpanded] = useState(false);
  const id = rec.detection_id;
  const threatStyle = THREAT_STYLE[rec.threat_level] || THREAT_STYLE.UNKNOWN;
  const route = rec.recommended_effector ? EFFECTOR_ROUTE[rec.recommended_effector] : null;

  return (
    <div
      className="tactical-border"
      style={{ background: "var(--bg-surface)" }}
      data-testid={`decision-row-${id}`}
    >
      <div className="p-4 flex flex-col gap-3">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <div className="font-heading font-bold text-sm">{rec.callsign || id}</div>
            <div className="font-mono text-[10px] text-slate-500 mt-0.5">{id}</div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <span
              className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest"
              style={{ color: threatStyle.color, borderColor: threatStyle.color }}
            >
              THREAT {rec.threat_level || "UNKNOWN"}
            </span>
            <span className="font-mono text-[10px] text-slate-400">score {rec.threat_score ?? "—"}</span>
            <span
              className="font-mono text-[9px] uppercase tracking-widest"
              style={{ color: rec.position_known ? "var(--accent-success)" : "var(--text-muted)" }}
            >
              {rec.position_known ? "● POSITION KNOWN" : "○ POSITION UNKNOWN"}
            </span>
            {rec.dedup_status?.already_engaged && (
              <span
                className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest"
                style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
                title={rec.dedup_status?.reason}
              >
                ALREADY ENGAGED
              </span>
            )}
          </div>
        </div>

        <button
          onClick={() => setExpanded((e) => !e)}
          data-testid={`decision-score-toggle-${id}`}
          className="self-start flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-slate-500 hover-accent-info transition-colors"
        >
          {expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
          why this score
        </button>
        {expanded && <ScoreBreakdown breakdown={rec.score_breakdown} />}

        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-2 tactical-border-t">
          <FeasibilityCell kind="jam" testidPrefix="jam" id={id} feasibility={rec.feasibility?.jam} />
          <FeasibilityCell kind="gnss_deny" testidPrefix="gnss" id={id} feasibility={rec.feasibility?.gnss_deny} />
          <FeasibilityCell kind="mavlink_takeover" testidPrefix="takeover" id={id} feasibility={rec.feasibility?.mavlink_takeover} />
        </div>

        <div
          className="tactical-border-t pt-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3"
          data-testid={`decision-recommended-${id}`}
        >
          <div>
            <div className="font-mono text-[9px] uppercase tracking-widest text-slate-500">Recommended effector</div>
            <div className="font-heading font-bold text-sm mt-0.5" style={{ color: rec.recommended_effector ? "var(--accent-info)" : "var(--text-muted)" }}>
              {rec.recommended_effector ? rec.recommended_effector.toUpperCase() : "NONE — no feasible+available effector"}
            </div>
            {rec.recommended_rationale && (
              <div className="font-mono text-[10px] leading-relaxed text-slate-500 mt-1 max-w-xl">
                {rec.recommended_rationale}
              </div>
            )}
          </div>
          {route ? (
            <Link
              to={route.to}
              data-testid={`decision-engage-link-${id}`}
              className="flex items-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn shrink-0"
              title="Opens the existing gated engagement page — arm/confirm/range-auth/tx-halt still apply there"
            >
              <route.icon size={13} strokeWidth={1.5} />
              PROCEED TO {route.label}
              <ExternalLink size={11} />
            </Link>
          ) : (
            <div className="font-mono text-[9px] uppercase tracking-widest text-slate-600 shrink-0">
              no gated engagement page to deep-link
            </div>
          )}
        </div>

        <div>
          <div className="font-mono text-[9px] uppercase tracking-widest text-slate-500 mb-1">Failover order</div>
          <FailoverList id={id} failoverOrder={rec.failover_order} />
        </div>

        <div className="font-mono text-[9px] uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
          {rec.status || "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"}
        </div>
      </div>
    </div>
  );
}

function CommanderRequired() {
  return (
    <div
      className="tactical-border p-6 flex items-start gap-3"
      style={{ background: "var(--bg-surface)", borderColor: "var(--accent-critical)" }}
      data-testid="decision-commander-required"
    >
      <Lock size={20} className="shrink-0 mt-0.5" style={{ color: "var(--accent-critical)" }} strokeWidth={1.5} />
      <div>
        <div className="font-heading font-bold text-sm uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
          COMMANDER ACCESS REQUIRED
        </div>
        <div className="font-mono text-[11px] leading-relaxed text-slate-400 mt-2 max-w-xl">
          Effector recommendations reveal targeting priority and are commander-gated server-side
          (require_commander) on both the recommendation feed and the recompute action. This panel does
          not call those endpoints as a non-commander operator.
        </div>
      </div>
    </div>
  );
}

export default function DecisionSupport() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(isCommander);
  const [recomputing, setRecomputing] = useState(false);
  const [recomputeErr, setRecomputeErr] = useState(null);

  const load = useCallback(async () => {
    if (!isCommander) return;
    try {
      const { data } = await api.get("/effector/recommendations");
      setData(data);
      setError(null);
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setLoading(false);
    }
  }, [isCommander]);

  useEffect(() => {
    if (!isCommander) return;
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [isCommander, load]);

  async function onRecompute() {
    setRecomputeErr(null);
    setRecomputing(true);
    try {
      const { data } = await api.post("/effector/recommendations/recompute");
      setData(data);
    } catch (e) {
      setRecomputeErr(formatApiError(e));
    } finally {
      setRecomputing(false);
    }
  }

  if (!isCommander) {
    return (
      <div className="space-y-6" data-testid="page-decision-support">
        <PageHeader />
        <CommanderRequired />
      </div>
    );
  }

  const recommendations = data?.recommendations || [];
  const excluded = data?.excluded || [];
  const summary = data?.summary || {};

  return (
    <div className="space-y-6" data-testid="page-decision-support">
      <PageHeader
        right={
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              data-testid="decision-refresh"
              className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
            >
              <RefreshCw size={12} strokeWidth={1.5} /> Refresh
            </button>
            <button
              onClick={onRecompute}
              disabled={recomputing}
              data-testid="decision-recompute"
              className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-critical transition-colors scanline-btn"
              style={{ opacity: recomputing ? 0.6 : 1 }}
            >
              <RefreshCw size={12} strokeWidth={1.5} className={recomputing ? "animate-spin" : ""} />
              {recomputing ? "RECOMPUTING…" : "RECOMPUTE + AUDIT"}
            </button>
          </div>
        }
      />

      {data?.disclaimer && (
        <div
          className="tactical-border p-3 font-mono text-[10px] leading-relaxed flex gap-2"
          style={{ background: "var(--bg-surface)", borderColor: "var(--accent-critical)" }}
        >
          <ShieldAlert size={14} className="shrink-0 mt-0.5" style={{ color: "var(--accent-critical)" }} />
          <div>
            <div className="uppercase tracking-widest text-[9px]" style={{ color: "var(--accent-critical)" }}>
              {data.disclaimer}
            </div>
            {data.doctrine && <div className="text-slate-500 mt-1">{data.doctrine}</div>}
          </div>
        </div>
      )}

      {recomputeErr && (
        <div className="tactical-border p-2 font-mono text-[10px]" style={{ color: "var(--accent-critical)", background: "var(--bg-surface)" }}>
          Recompute failed: {recomputeErr}
        </div>
      )}

      <div className="tactical-border p-3 flex flex-wrap gap-x-6 gap-y-1 font-mono text-[10px] text-slate-400" style={{ background: "var(--bg-surface)" }}>
        <span><span className="uppercase tracking-widest text-[9px] text-slate-500">generated </span>{data?.generated_at ? String(data.generated_at).slice(0, 19).replace("T", " ") : "—"}</span>
        <span><span className="uppercase tracking-widest text-[9px] text-slate-500">recommendations </span>{recommendations.length}</span>
        <span><span className="uppercase tracking-widest text-[9px] text-slate-500">excluded </span>{excluded.length}</span>
        {Object.entries(summary).map(([k, v]) => (
          <span key={k}><span className="uppercase tracking-widest text-[9px] text-slate-500">{k} </span>{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        ))}
      </div>

      {loading && (
        <div className="font-mono text-[11px] text-slate-500 flex items-center gap-2">
          <RefreshCw size={12} className="animate-spin" /> loading effector recommendations…
        </div>
      )}
      {error && (
        <div className="tactical-border p-3 font-mono text-[11px]" style={{ color: "var(--accent-critical)", background: "var(--bg-surface)" }} data-testid="decision-load-error">
          Could not load effector recommendations: {error}
        </div>
      )}

      {!loading && !error && recommendations.length === 0 && (
        <div className="tactical-border p-8 text-center font-mono text-[11px] text-slate-600" style={{ background: "var(--bg-surface)" }}>
          No recommendations — no contact currently scored for effector engagement.
        </div>
      )}

      <div className="space-y-4">
        {recommendations.map((rec) => (
          <RecommendationRow key={rec.detection_id} rec={rec} />
        ))}
      </div>

      {excluded.length > 0 && (
        <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-heading font-bold text-sm mb-2 flex items-center gap-2">
            <AlertTriangle size={14} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} />
            EXCLUDED
          </div>
          <pre className="font-mono text-[10px] leading-relaxed text-slate-500 whitespace-pre-wrap overflow-x-auto">
            {JSON.stringify(excluded, null, 2)}
          </pre>
        </div>
      )}

      <div className="font-mono text-[10px] leading-relaxed text-slate-500 tactical-border p-3" style={{ background: "var(--bg-surface)" }}>
        The ONLY way to act on a recommendation is the button above, which navigates to the existing gated
        engagement page (RF Barrage Jam / GNSS Spoof / SDR MAVLink Inject). This panel issues no fire/transmit
        call of its own — arm, confirm, range-authorization, and TX-halt checks still apply exactly as before.
      </div>
    </div>
  );
}

function PageHeader({ right }) {
  return (
    <div className="flex items-start justify-between gap-4 flex-wrap">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> Decision / Effector-Selection Engine · RFI 4.5.3/4.5.4/4.5.6/4.5.7
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Decision Support
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          Ranked, proposed effector recommendations · commander-authorized engagement only
        </div>
      </div>
      {right}
    </div>
  );
}
