import { useEffect, useMemo, useState } from "react";
import { BookOpen, Search, Radio, Wrench, RefreshCw } from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import {
  RF_KB_SOURCES,
  RF_PROTOCOL_DB_META_NOTE,
} from "@/data/protocolLibrary";
import rfProtocolDb from "@/data/rf_protocols_db.json";

// Drone Protocol Library — TRUTHFUL, LIVE per-protocol status board.
//
// DOCTRINE (operator, 2026-09-04): the Protocol Library IDENTIFIES a drone over
// the air; the DEFEAT is jam / GNSS-spoof / SDR-MAVLink-injection, not the wire
// decoders. So the board is split into two clearly-separated groups:
//
//   OPERATIONAL — over-the-air, fielded against airborne targets. Status is
//     LIVE / READY / OFFLINE, derived on the backend PURELY from observable
//     state (is the decoder service heartbeating? has it produced a real decode
//     recently?) via GET /api/protocols/status — never a hardcoded label.
//
//   FORENSIC — recovered / own airframe, bench, requires PHYSICAL access
//     (USB-UART / CAN). These wire decoders (CRSF/MSP/CANopen/DroneCAN/SiK-wire)
//     are USELESS against an airborne enemy and are NEVER shown next to the
//     operational board — they live in their own visually-distinct section.

const OP_STATUS_STYLE = {
  LIVE: {
    color: "var(--accent-success)",
    label: "● LIVE — decoding",
    blurb: "Service running and producing real decodes now.",
  },
  READY: {
    color: "var(--accent-info)",
    label: "◉ READY — awaiting signal",
    blurb: "Service running; no matching over-the-air signal decoded yet (not faked).",
  },
  OFFLINE: {
    color: "var(--accent-warning)",
    label: "○ OFFLINE — not running",
    blurb: "Decoder service is not running yet (awaiting deploy / start).",
  },
};

function fmtAge(s) {
  if (s == null) return "—";
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

function OperationalCard({ p }) {
  const s = OP_STATUS_STYLE[p.status] || { color: "var(--text-muted)", label: p.status, blurb: "" };
  return (
    <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }} data-testid={`protocol-op-${p.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-heading font-bold text-sm">{p.name}</div>
          {p.aka && <div className="font-mono text-[10px] text-slate-500 mt-0.5">{p.aka}</div>}
        </div>
        <span
          className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
          style={{ color: s.color, borderColor: s.color }}
          data-testid={`protocol-op-status-${p.id}`}
        >
          {s.label}
        </span>
      </div>

      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 font-mono text-[11px]">
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Over-the-air source</div>
          <div className="text-slate-300">{p.over_the_air}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Identifies</div>
          <div className="text-slate-300">{p.identifies}</div>
        </div>
      </div>

      <div className="mt-3 font-mono text-[10px] leading-relaxed" style={{ color: s.color }}>
        {s.blurb}
      </div>

      <div className="mt-3 pt-2 tactical-border-t grid grid-cols-2 gap-2 font-mono text-[10px] text-slate-400">
        <div>
          <span className="text-slate-600 uppercase tracking-widest text-[9px]">Last heartbeat</span>
          <div>{fmtAge(p.last_heartbeat_age_s)}</div>
        </div>
        <div>
          <span className="text-slate-600 uppercase tracking-widest text-[9px]">Last decode</span>
          <div>{p.decode_count > 0 ? `${fmtAge(p.last_decode_age_s)} (${p.decode_count})` : "none yet"}</div>
        </div>
      </div>

      {p.last_decode_summary && (
        <div className="mt-2 font-mono text-[10px] text-slate-300 break-words">
          <span className="text-slate-600 uppercase tracking-widest text-[9px]">Latest: </span>
          {p.last_decode_summary}
        </div>
      )}

      <div className="mt-3 pt-2 tactical-border-t flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-slate-600">
        <span>{p.service}</span>
        <span>OVER-THE-AIR</span>
      </div>
    </div>
  );
}

function ForensicCard({ p }) {
  // Visually distinct from the operational board: dashed border + muted ground,
  // so it never reads as "near-operational".
  return (
    <div
      className="p-4"
      style={{
        background: "var(--bg-elev)",
        border: "1px dashed var(--text-muted)",
        opacity: 0.92,
      }}
      data-testid={`protocol-forensic-${p.id}`}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="font-heading font-bold text-sm text-slate-300">{p.name}</div>
        <span
          className="px-2 py-0.5 font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
          style={{ color: "var(--text-muted)", border: "1px dashed var(--text-muted)" }}
        >
          ✕ FORENSIC — physical access
        </span>
      </div>
      <div className="mt-2 font-mono text-[11px] text-slate-400">
        <span className="text-[9px] uppercase tracking-widest text-slate-500">Requires: </span>
        {p.requires}
      </div>
      {p.ota_family && (
        <div className="mt-1 font-mono text-[10px] text-slate-500">
          OTA family coverage: {p.ota_family}
        </div>
      )}
      {p.source && (
        <div className="mt-2 font-mono text-[9px] uppercase tracking-widest text-slate-600">{p.source}</div>
      )}
    </div>
  );
}

// --- Reference cards (RF knowledge-base datasets — not operational claims) ---
const REF_STATUS_STYLE = {
  LIVE: { color: "var(--accent-success)", label: "● LIVE" },
  STAGED: { color: "var(--accent-warning)", label: "◐ STAGED (dataset)" },
  HARDWARE_BLOCKED: { color: "var(--accent-critical)", label: "✕ HARDWARE-BLOCKED" },
};

function RefCard({ p }) {
  const s = REF_STATUS_STYLE[p.status] || { color: "var(--text-muted)", label: p.status };
  return (
    <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }} data-testid={`protocol-ref-${p.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-heading font-bold text-sm">{p.name}</div>
          {p.aka && <div className="font-mono text-[10px] text-slate-500 mt-0.5">{p.aka}</div>}
        </div>
        <span
          className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
          style={{ color: s.color, borderColor: s.color }}
        >
          {s.label}
        </span>
      </div>
      {p.statusNote && (
        <div className="mt-3 font-mono text-[10px] leading-relaxed text-slate-500">{p.statusNote}</div>
      )}
      <div className="mt-3 pt-2 tactical-border-t flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-slate-600">
        <span>{p.source}</span>
        {p.task && <span>TASK {p.task}</span>}
      </div>
    </div>
  );
}

const PAGE_SIZE = 40;

function SignatureDbPanel() {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("ALL");
  const [page, setPage] = useState(0);

  const devices = rfProtocolDb.devices || [];
  const categories = useMemo(() => {
    const set = new Set(devices.map((d) => d.category).filter(Boolean));
    return ["ALL", ...Array.from(set).sort()];
  }, [devices]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return devices.filter((d) => {
      if (category !== "ALL" && d.category !== category) return false;
      if (!q) return true;
      return (
        (d.name || "").toLowerCase().includes(q) ||
        (d.manufacturer || "").toLowerCase().includes(q) ||
        (d.modulation || "").toLowerCase().includes(q) ||
        (d.category || "").toLowerCase().includes(q)
      );
    });
  }, [devices, query, category]);

  const shown = filtered.slice(0, (page + 1) * PAGE_SIZE);

  const fmtFreq = (hz) => {
    if (!hz && hz !== 0) return "—";
    return `${(hz / 1e6).toFixed(3)} MHz`;
  };

  return (
    <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
      <div className="tactical-border-b px-4 py-3 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <span className="font-heading font-bold text-sm">SUB-GHZ SIGNATURE DATABASE</span>
          <span className="font-mono text-[10px] text-slate-500">
            {filtered.length} / {devices.length} devices &middot; RF-Protocol-Database v{rfProtocolDb.version}
          </span>
        </div>
        <div className="font-mono text-[10px] leading-relaxed text-slate-500">
          {RF_PROTOCOL_DB_META_NOTE}
        </div>
        <div className="flex flex-col sm:flex-row gap-2">
          <div className="flex-1 flex items-center gap-2 tactical-border px-2 py-1.5">
            <Search size={12} className="text-slate-500 shrink-0" />
            <input
              data-testid="protocol-db-search"
              value={query}
              onChange={(e) => { setQuery(e.target.value); setPage(0); }}
              placeholder="Search name, manufacturer, modulation, category..."
              className="bg-transparent outline-none font-mono text-xs w-full text-slate-200 placeholder:text-slate-600"
            />
          </div>
          <select
            data-testid="protocol-db-category"
            value={category}
            onChange={(e) => { setCategory(e.target.value); setPage(0); }}
            className="tactical-border px-2 py-1.5 font-mono text-[11px] bg-transparent text-slate-300"
            style={{ background: "var(--bg-surface)" }}
          >
            {categories.map((c) => (
              <option key={c} value={c} style={{ background: "var(--bg-surface)" }}>
                {c}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-xs" data-testid="protocol-db-table">
          <thead>
            <tr className="tactical-border-b font-mono text-[10px] uppercase tracking-widest text-slate-500">
              <th className="text-left px-4 py-2">Name</th>
              <th className="text-left px-4 py-2">Manufacturer</th>
              <th className="text-left px-4 py-2">Category</th>
              <th className="text-left px-4 py-2">Modulation</th>
              <th className="text-left px-4 py-2">Frequency</th>
              <th className="text-left px-4 py-2">Source</th>
            </tr>
          </thead>
          <tbody className="font-mono">
            {shown.map((d) => (
              <tr key={d.device_id} className="tactical-border-b hover-surface transition-colors">
                <td className="px-4 py-2 text-slate-300">{d.name}</td>
                <td className="px-4 py-2 text-slate-400">{d.manufacturer || "—"}</td>
                <td className="px-4 py-2 text-slate-400">{d.category || "—"}</td>
                <td className="px-4 py-2 text-slate-400">{d.modulation || "—"}</td>
                <td className="px-4 py-2 text-slate-400">{fmtFreq(d.frequency)}</td>
                <td className="px-4 py-2 text-slate-600 text-[10px]">{d.source || "—"}</td>
              </tr>
            ))}
            {shown.length === 0 && (
              <tr>
                <td colSpan={6} className="px-4 py-8 text-center text-slate-600">
                  No devices match this search/filter.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {shown.length < filtered.length && (
        <div className="tactical-border-t p-3 flex justify-center">
          <button
            onClick={() => setPage((p) => p + 1)}
            className="px-4 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
          >
            Load more ({filtered.length - shown.length} remaining)
          </button>
        </div>
      )}
    </div>
  );
}

export default function ProtocolLibrary() {
  const [board, setBoard] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function load() {
      try {
        const { data } = await api.get("/protocols/status");
        if (alive) { setBoard(data); setError(null); }
      } catch (e) {
        if (alive) setError(formatApiError(e));
      } finally {
        if (alive) setLoading(false);
      }
    }
    load();
    const t = setInterval(load, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const operational = board?.operational || [];
  const forensic = board?.forensic || [];

  return (
    <div className="space-y-6" data-testid="page-protocol-library">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <BookOpen size={12} className="inline mr-2" strokeWidth={1.5} /> Drone Protocol Library
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Protocol Library
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          Live per-protocol status &middot; identify a drone over the air — the defeat is jam / GNSS-spoof / SDR-injection
        </div>
      </div>

      {board?.doctrine && (
        <div
          className="tactical-border p-3 font-mono text-[10px] leading-relaxed text-slate-400"
          style={{ background: "var(--bg-surface)" }}
        >
          {board.doctrine}
        </div>
      )}

      {/* ---- OPERATIONAL: over-the-air ---- */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <div className="font-mono text-[11px] uppercase tracking-widest" style={{ color: "var(--accent-info)" }}>
            <Radio size={13} className="inline mr-2" strokeWidth={1.5} />
            Operational — over-the-air (fielded against airborne targets)
          </div>
          <div className="font-mono text-[9px] uppercase tracking-widest text-slate-600 flex items-center gap-1">
            {loading ? <RefreshCw size={10} className="animate-spin" /> : null}
            {error ? <span style={{ color: "var(--accent-critical)" }}>status unavailable</span>
                   : `${operational.length} protocols`}
          </div>
        </div>
        {error && (
          <div className="tactical-border p-3 font-mono text-[10px] text-slate-400" style={{ background: "var(--bg-surface)" }} data-testid="protocol-status-error">
            Could not load live protocol status: {error}
          </div>
        )}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {operational.map((p) => (
            <OperationalCard key={p.id} p={p} />
          ))}
        </div>
      </div>

      {/* ---- FORENSIC: bench / physical access — visually separated ---- */}
      <div>
        <div className="font-mono text-[11px] uppercase tracking-widest text-slate-500 mb-1">
          <Wrench size={13} className="inline mr-2" strokeWidth={1.5} />
          Forensic — recovered / own airframe (bench, physical access)
        </div>
        <div className="font-mono text-[10px] text-slate-600 mb-2 leading-relaxed">
          Wire / bus decoders that require PHYSICAL contact with a recovered or own airframe
          (USB-UART / CAN). Useless against an airborne enemy — NOT a fielded counter-UAS
          capability, listed here only for post-recovery bench analysis.
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {forensic.map((p) => (
            <ForensicCard key={p.id} p={p} />
          ))}
        </div>
      </div>

      {/* ---- Reference datasets ---- */}
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-2">
          Drone RF Knowledge-Base Sources ({RF_KB_SOURCES.length})
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {RF_KB_SOURCES.map((p) => (
            <RefCard key={p.id} p={p} />
          ))}
        </div>
      </div>

      <SignatureDbPanel />
    </div>
  );
}
