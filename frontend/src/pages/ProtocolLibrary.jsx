import { useMemo, useState } from "react";
import { BookOpen, Search } from "lucide-react";
import {
  PROTOCOL_PARSERS,
  RF_KB_SOURCES,
  RF_PROTOCOL_DB_META_NOTE,
} from "@/data/protocolLibrary";
import rfProtocolDb from "@/data/rf_protocols_db.json";

// Drone Protocol Library (task #82) — a browsable reference surface for the
// protocol/decoder/signature knowledge this project has already built, not a
// generic placeholder. Three real sources, each pulled from the actual repo
// content (see frontend/src/data/protocolLibrary.js for citations):
//
//   1. Real, tested field-bridge parsers/decoders (CRSF, MSP, CANopen,
//      DroneCAN, DJI DroneID, MAVLink/SiK) — status distinguishes LIVE vs.
//      STAGED (tested, not wired to hardware) vs. HARDWARE_BLOCKED.
//   2. Drone RF knowledge-base staging notes (RFUAV, DroneSecurity samples).
//   3. RF-Protocol-Database's 692-device sub-GHz signature catalogue (task
//      #39), bundled as a static JSON file and rendered as a searchable/
//      filterable table (too large for a static list).

const STATUS_STYLE = {
  LIVE: { color: "var(--accent-success)", label: "● LIVE / DEPLOYED" },
  STAGED: { color: "var(--accent-warning)", label: "◐ STAGED — TESTED, NOT LIVE" },
  HARDWARE_BLOCKED: { color: "var(--accent-critical)", label: "✕ HARDWARE-BLOCKED" },
};

function StatusBadge({ status }) {
  const s = STATUS_STYLE[status] || { color: "var(--text-muted)", label: status };
  return (
    <span
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: s.color, borderColor: s.color }}
    >
      {s.label}
    </span>
  );
}

function ParserCard({ p }) {
  return (
    <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }} data-testid={`protocol-card-${p.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-heading font-bold text-sm">{p.name}</div>
          {p.aka && <div className="font-mono text-[10px] text-slate-500 mt-0.5">{p.aka}</div>}
        </div>
        <StatusBadge status={p.status} />
      </div>

      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 font-mono text-[11px]">
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Band / Link</div>
          <div className="text-slate-300">{p.band}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Modulation</div>
          <div className="text-slate-300">{p.modulation}</div>
        </div>
      </div>

      {p.decodes && (
        <div className="mt-3">
          <div className="font-mono text-[9px] uppercase tracking-widest text-slate-500 mb-1">Decodes</div>
          <div className="flex flex-wrap gap-1.5">
            {p.decodes.map((d) => (
              <span key={d} className="px-1.5 py-0.5 tactical-border font-mono text-[10px] text-slate-300">
                {d}
              </span>
            ))}
          </div>
        </div>
      )}

      {p.statusNote && (
        <div className="mt-3 font-mono text-[10px] leading-relaxed text-slate-500">
          {p.statusNote}
        </div>
      )}

      <div className="mt-3 pt-2 tactical-border-t flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-slate-600">
        <span>{p.source}</span>
        {p.task && <span>TASK {p.task}</span>}
      </div>
      {p.provenance && (
        <div className="mt-1 font-mono text-[9px] text-slate-600">{p.provenance}</div>
      )}
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
          Real parser/decoder inventory &middot; not a generic reference — every entry traces to a file in this repo
        </div>
      </div>

      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-2">
          Protocol Parsers &amp; Decoders ({PROTOCOL_PARSERS.length})
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {PROTOCOL_PARSERS.map((p) => (
            <ParserCard key={p.id} p={p} />
          ))}
        </div>
      </div>

      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-2">
          Drone RF Knowledge-Base Sources ({RF_KB_SOURCES.length})
        </div>
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {RF_KB_SOURCES.map((p) => (
            <ParserCard key={p.id} p={p} />
          ))}
        </div>
      </div>

      <SignatureDbPanel />
    </div>
  );
}
