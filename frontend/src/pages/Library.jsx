import { useEffect, useMemo, useRef, useState } from "react";
import {
  Crosshair, Search, Upload, Download, ShieldAlert, Target, RadioTower, RefreshCw,
  BookOpen, Radio, Wrench,
} from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import {
  RF_KB_SOURCES,
  RF_PROTOCOL_DB_META_NOTE,
} from "@/data/protocolLibrary";
import rfProtocolDb from "@/data/rf_protocols_db.json";

// Merged "Threat & Protocol Library" page (Console IA Restructure, Phase P-A,
// Merge A — see .omc/plans/console-ia-restructure.md). This is a REBUILD, not
// a concatenation: ThreatLibrary.jsx and ProtocolLibrary.jsx are combined
// into one IDENTIFY surface with 3 tabs so the live decoder LIVE/READY/
// OFFLINE signal (guardrail 2) stays its own prominent tab rather than being
// drowned in reference bulk. No backend change — every tab consumes its
// existing endpoint unchanged (/threat-library, /threat-library/match,
// /protocols/status, the bundled sub-GHz signature DB). The originals
// (ThreatLibrary.jsx / ProtocolLibrary.jsx) are left in place until Phase P-D
// rewires routing/nav; their internals are lifted here verbatim.
//
// HONEST DOCTRINE (Tab 1 — surfaced in the UI, not hidden):
//   * A DECODED BROADCAST id (ASTM Remote ID serial / DJI DroneID make+model /
//     distinctive Wi-Fi SSID) gives an EXACT make/model/serial -> HIGH confidence.
//   * An RF-SIGNATURE-only observation (band + occupied BW + control-link family)
//     gives CLASS / FAMILY candidates only -> LOWER confidence, ranked, and NEVER
//     a fabricated exact model.
//   * Nothing usable -> honest "unknown", no guess.
// Library entries are compiled from PUBLIC specs; military entries are class-level
// placeholders only (no classified data).
//
// DOCTRINE (Tab 2 — operator, 2026-09-04): the library IDENTIFIES a drone over
// the air; the DEFEAT is jam / GNSS-spoof / SDR-MAVLink-injection, not the wire
// decoders. So the decoder board is split into two clearly-separated groups:
//   OPERATIONAL — over-the-air, fielded against airborne targets. Status is
//     LIVE / READY / OFFLINE, derived on the backend PURELY from observable
//     state via GET /protocols/status — never a hardcoded label.
//   FORENSIC — recovered / own airframe, bench, requires PHYSICAL access
//     (USB-UART / CAN). Useless against an airborne enemy; never shown next to
//     the operational board.

const LEVEL_STYLE = {
  CRITICAL: { color: "var(--accent-critical)" },
  HIGH: { color: "var(--accent-warning)" },
  MEDIUM: { color: "var(--accent-info)" },
  LOW: { color: "var(--accent-success)" },
  UNKNOWN: { color: "var(--text-muted)" },
};

const CONF_STYLE = {
  HIGH: { color: "var(--accent-success)", label: "HIGH — broadcast-confirmed" },
  MEDIUM: { color: "var(--accent-info)", label: "MEDIUM" },
  LOW: { color: "var(--accent-warning)", label: "LOW — signature only" },
};

const BASIS_LABEL = {
  broadcast_decode: "BROADCAST DECODE → exact make/model/serial",
  wifi_identity: "WI-FI BEACON → make/model (spoofable)",
  rf_signature: "RF SIGNATURE → class/family candidate(s)",
  none: "UNKNOWN → insufficient signature",
};

function Badge({ text, style, testid }) {
  return (
    <span
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: style?.color, borderColor: style?.color }}
      data-testid={testid}
    >
      {text}
    </span>
  );
}

function fmtList(v) {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
  return String(v);
}

function EntryCard({ e }) {
  const rf = e.rf_profile || {};
  const sig = e.signatures || {};
  const cm = e.countermeasures || {};
  const lvl = LEVEL_STYLE[e.threat_level] || LEVEL_STYLE.UNKNOWN;
  return (
    <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }} data-testid={`threat-entry-${e.id}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="font-heading font-bold text-sm">{e.make} · {e.model}</div>
          <div className="font-mono text-[10px] text-slate-500 mt-0.5">{e.class}</div>
        </div>
        <Badge text={e.threat_level} style={lvl} testid={`threat-level-${e.id}`} />
      </div>

      <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3 font-mono text-[11px]">
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Control link</div>
          <div className="text-slate-300">{rf.control_link_protocol || "—"}</div>
          <div className="text-slate-500 text-[10px] mt-0.5">family: {rf.control_link_family || "—"}</div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Bands</div>
          <div className="text-slate-300">{fmtList(rf.bands)}</div>
          <div className="text-slate-500 text-[10px] mt-0.5">
            video: {fmtList(rf.video_band)} {rf.video_type ? `(${rf.video_type})` : ""}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Occupied BW</div>
          <div className="text-slate-300">
            {sig.occupied_bw_mhz_range ? `${sig.occupied_bw_mhz_range[0]}–${sig.occupied_bw_mhz_range[1]} MHz` : "— (null)"}
          </div>
          <div className="text-slate-500 text-[10px] mt-0.5">
            FHSS: {sig.fhss == null ? "—" : String(sig.fhss)} · RemoteID: {rf.remoteid_capable == null ? "—" : String(rf.remoteid_capable)}
          </div>
        </div>
        <div>
          <div className="text-[9px] uppercase tracking-widest text-slate-500 mb-1">Countermeasures</div>
          <div className="text-slate-300">jam: {fmtList(cm.jam_bands)}</div>
          <div className="text-slate-500 text-[10px] mt-0.5">
            GNSS-deny: {cm.gnss_deny_applicable == null ? "—" : String(cm.gnss_deny_applicable)} ·
            takeover: {cm.cyber_takeover_applicable ? (cm.cyber_takeover_protocol || "yes") : "no"}
          </div>
        </div>
      </div>

      {cm.cyber_takeover_note && (
        <div className="mt-3 pt-2 tactical-border-t font-mono text-[10px] leading-relaxed text-slate-400">
          {cm.cyber_takeover_note}
        </div>
      )}

      <div className="mt-3 pt-2 tactical-border-t flex items-center justify-between font-mono text-[9px] uppercase tracking-widest text-slate-600">
        <span>{e.id}</span>
        <span>{e.data_source}</span>
      </div>
    </div>
  );
}

function MatchResult({ result }) {
  if (!result) return null;
  const basis = result.match_basis;
  const conf = result.confidence;
  const confStyle = CONF_STYLE[conf] || { color: "var(--text-muted)", label: conf || "—" };
  const lvl = LEVEL_STYLE[result.threat_level] || LEVEL_STYLE.UNKNOWN;

  return (
    <div className="tactical-border p-4" style={{ background: "var(--bg-elev)" }} data-testid="threat-match-result">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
          Match basis: <span style={{ color: "var(--text-primary)" }}>{BASIS_LABEL[basis] || basis}</span>
        </div>
        <div className="flex items-center gap-2">
          <Badge text={`THREAT ${result.threat_level}`} style={lvl} testid="threat-match-level" />
          {conf && <Badge text={confStyle.label} style={confStyle} testid="threat-match-confidence" />}
        </div>
      </div>

      <div className="mt-2 font-mono text-[10px] leading-relaxed text-slate-400" data-testid="threat-match-message">
        {result.message}
      </div>

      {result.best ? (
        <div className="mt-3">
          <div className="font-mono text-[9px] uppercase tracking-widest text-slate-500 mb-1">
            {result.exact_model_confirmed ? "Confirmed identification" : "Ranked candidates (class/family)"}
          </div>
          <div className="space-y-2">
            {result.candidates.map((c, i) => (
              <div key={`${c.id}-${i}`} className="tactical-border p-2 flex items-start justify-between gap-3" data-testid={`threat-candidate-${i}`}>
                <div className="min-w-0">
                  <div className="font-mono text-[11px] text-slate-200">
                    {c.make} · {c.model}
                    {!c.exact_model_confirmed && (
                      <span className="ml-2 text-[9px] uppercase tracking-widest text-slate-500">(candidate)</span>
                    )}
                  </div>
                  <div className="font-mono text-[9px] text-slate-500 mt-0.5">
                    {c.class} · {c.identification_level}
                    {c.serial ? ` · serial ${c.serial}` : ""}
                    {c.score != null ? ` · score ${c.score}` : ""}
                  </div>
                  {c.reasons?.length > 0 && (
                    <div className="font-mono text-[9px] text-slate-500 mt-0.5">{c.reasons.join(" · ")}</div>
                  )}
                  {c.countermeasures?.jam_bands && (
                    <div className="font-mono text-[9px] mt-1" style={{ color: "var(--accent-info)" }}>
                      Recommended: jam {fmtList(c.countermeasures.jam_bands)}
                      {c.countermeasures.cyber_takeover_applicable
                        ? ` · takeover (${c.countermeasures.cyber_takeover_protocol}) applicable`
                        : " · takeover NOT applicable (encrypted/FHSS)"}
                    </div>
                  )}
                </div>
                <Badge text={c.confidence} style={CONF_STYLE[c.confidence] || {}} />
              </div>
            ))}
          </div>
        </div>
      ) : (
        <div className="mt-3 font-mono text-[11px]" style={{ color: "var(--accent-warning)" }} data-testid="threat-match-unknown">
          No candidate — reported as UNKNOWN rather than guessed.
        </div>
      )}
    </div>
  );
}

// Quick, honest presets so an evaluator can see each confidence tier immediately.
const MATCH_PRESETS = [
  {
    label: "DJI DroneID (broadcast → exact)",
    body: { droneid: { make: "DJI", model: "Mavic 3" } },
  },
  {
    label: "Remote ID serial (broadcast → exact)",
    body: { remoteid: { uas_id: "1581F5FKD230100XXXXX", make: "DJI" } },
  },
  {
    label: "Wi-Fi SSID ANAFI- (beacon → make/model)",
    body: { wifi: { ssid: "ANAFI-654321" } },
  },
  {
    label: "OcuSync 2.4/5.8 (signature → class)",
    body: { bands: ["2.4GHz", "5.8GHz"], occupied_bw_mhz: 20, control_link_family: "DJI OcuSync", fhss: true },
  },
  {
    label: "Analog FPV 5.8 (signature → FPV class)",
    body: { bands: ["5.8GHz"], video_type: "analog", control_link_family: "Analog-FPV" },
  },
  {
    label: "Empty (→ honest UNKNOWN)",
    body: {},
  },
];

// ---- Tab 2: Decoder Status (live) ----

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

// ---- Tab 3: Signature DB (reference) ----

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
              className="bg-transparent outline-none font-mono text-xs w-full placeholder:text-slate-600"
              style={{ color: "var(--text-primary)" }}
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

// ---- Tab bar ----

const TABS = [
  { key: "identify", label: "Identify & Classify", icon: Target, testid: "library-tab-identify" },
  { key: "decoder", label: "Decoder Status", icon: Radio, testid: "library-tab-decoder" },
  { key: "signature", label: "Signature DB", icon: BookOpen, testid: "library-tab-signature" },
];

export default function Library() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const fileRef = useRef(null);
  const [activeTab, setActiveTab] = useState("identify");

  // --- Tab 1 state: threat library (identify & classify) ---
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const [query, setQuery] = useState("");
  const [classFilter, setClassFilter] = useState("ALL");

  const [match, setMatch] = useState(null);
  const [matchErr, setMatchErr] = useState(null);
  const [importMsg, setImportMsg] = useState(null);
  const [importErr, setImportErr] = useState(null);

  async function loadThreatLibrary() {
    try {
      const { data } = await api.get("/threat-library");
      setData(data);
      setError(null);
    } catch (e) {
      setError(formatApiError(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadThreatLibrary(); /* eslint-disable-next-line */ }, []);

  const entries = data?.entries || [];
  const summary = data?.summary || {};

  const classes = useMemo(() => {
    const set = new Set(entries.map((e) => e.class).filter(Boolean));
    return ["ALL", ...Array.from(set).sort()];
  }, [entries]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return entries.filter((e) => {
      if (classFilter !== "ALL" && e.class !== classFilter) return false;
      if (!q) return true;
      const hay = [
        e.make, e.model, e.class, e.threat_level,
        e.rf_profile?.control_link_family,
        ...(e.rf_profile?.bands || []),
        ...(e.aliases || []),
      ].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [entries, query, classFilter]);

  async function runMatch(body) {
    setMatchErr(null);
    try {
      const { data } = await api.post("/threat-library/match", body);
      setMatch(data);
    } catch (e) {
      setMatchErr(formatApiError(e));
      setMatch(null);
    }
  }

  async function onExport() {
    try {
      const { data } = await api.get("/threat-library/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `threat_library_v${data.version}_r${data.revision}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setImportErr(formatApiError(e));
    }
  }

  async function onImportFile(file) {
    setImportMsg(null);
    setImportErr(null);
    try {
      const text = await file.text();
      const library = JSON.parse(text);
      const { data } = await api.post("/threat-library/import", { library });
      setImportMsg(
        `Imported: rev ${data.audit.from_revision}→${data.audit.to_revision}, ` +
        `+${data.audit.added_count} added, ${data.audit.updated_count} updated. ` +
        `Now v${data.summary.version} (${data.summary.entry_count} entries).`
      );
      await loadThreatLibrary();
    } catch (e) {
      if (e?.name === "SyntaxError") setImportErr("File is not valid JSON.");
      else setImportErr(formatApiError(e));
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  // --- Tab 2 state: live decoder status board (poll runs regardless of the
  // active tab so the LIVE/READY/OFFLINE signal never goes stale by the time
  // the operator switches to it — guardrail 2). ---
  const [board, setBoard] = useState(null);
  const [statusError, setStatusError] = useState(null);
  const [statusLoading, setStatusLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    async function loadStatus() {
      try {
        const { data } = await api.get("/protocols/status");
        if (alive) { setBoard(data); setStatusError(null); }
      } catch (e) {
        if (alive) setStatusError(formatApiError(e));
      } finally {
        if (alive) setStatusLoading(false);
      }
    }
    loadStatus();
    const t = setInterval(loadStatus, 5000);
    return () => { alive = false; clearInterval(t); };
  }, []);

  const operational = board?.operational || [];
  const forensic = board?.forensic || [];

  return (
    <div className="space-y-6" data-testid="page-library">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> Inbuilt Drone Threat &amp; Protocol Library · RFI 4.2.12
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Threat &amp; Protocol Library
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          Identify &amp; classify a detection · live decoder status · sub-GHz signature reference — the defeat is
          jam / GNSS-spoof / SDR-injection, not the wire decoders
        </div>
      </div>

      {/* Tab bar */}
      <div className="tactical-border flex flex-wrap" style={{ background: "var(--bg-surface)" }} data-testid="library-tabs">
        {TABS.map(({ key, label, icon: Icon, testid }) => {
          const active = activeTab === key;
          return (
            <button
              key={key}
              onClick={() => setActiveTab(key)}
              data-testid={testid}
              aria-selected={active}
              className="flex items-center gap-2 px-4 py-3 font-mono text-[11px] uppercase tracking-widest transition-colors border-r hover-surface"
              style={{
                borderColor: "var(--border-col)",
                background: active ? "var(--active-surface)" : "transparent",
                color: active ? "var(--accent-info)" : "var(--text-secondary)",
                borderBottom: active ? "2px solid var(--accent-info)" : "2px solid transparent",
              }}
            >
              <Icon size={14} strokeWidth={1.5} />
              {label}
            </button>
          );
        })}
      </div>

      {/* ==== Tab 1: Identify & Classify ==== */}
      {activeTab === "identify" && (
        <div className="space-y-6" data-testid="library-tabpanel-identify">
          {/* Version + provenance strip */}
          <div className="tactical-border p-3 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2"
            style={{ background: "var(--bg-surface)" }}>
            <div className="font-mono text-[11px] text-slate-300">
              <span className="uppercase tracking-widest text-[9px] text-slate-500">Version </span>
              v{summary.version ?? "—"} · rev {summary.revision ?? "—"} ·
              <span className="uppercase tracking-widest text-[9px] text-slate-500"> updated </span>
              {summary.updated ? String(summary.updated).slice(0, 19).replace("T", " ") : "—"} ·
              <span className="uppercase tracking-widest text-[9px] text-slate-500"> entries </span>
              {summary.entry_count ?? "—"}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={onExport}
                data-testid="threat-export-btn"
                className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
              >
                <Download size={12} /> Export
              </button>
              <input
                ref={fileRef}
                type="file"
                accept="application/json,.json"
                className="hidden"
                data-testid="threat-import-file"
                onChange={(e) => { const f = e.target.files?.[0]; if (f) onImportFile(f); }}
              />
              <button
                onClick={() => fileRef.current?.click()}
                disabled={!isCommander}
                title={isCommander ? "Import + merge a threat-library JSON" : "Commander role required to update the fielded library"}
                data-testid="threat-import-btn"
                className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest transition-colors scanline-btn"
                style={{ opacity: isCommander ? 1 : 0.45, cursor: isCommander ? "pointer" : "not-allowed" }}
              >
                <Upload size={12} /> Import update{!isCommander && " (commander)"}
              </button>
            </div>
          </div>
          {summary.provenance_note && (
            <div className="font-mono text-[10px] leading-relaxed text-slate-500 -mt-3">
              {summary.provenance_note}
            </div>
          )}
          {importMsg && (
            <div className="tactical-border p-2 font-mono text-[10px]" style={{ color: "var(--accent-success)", background: "var(--bg-surface)" }} data-testid="threat-import-ok">
              {importMsg}
            </div>
          )}
          {importErr && (
            <div className="tactical-border p-2 font-mono text-[10px]" style={{ color: "var(--accent-critical)", background: "var(--bg-surface)" }} data-testid="threat-import-err">
              Import failed: {importErr}
            </div>
          )}

          {/* Matching / classification panel */}
          <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
            <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
              <span className="font-heading font-bold text-sm flex items-center gap-2">
                <Target size={15} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
                IDENTIFY &amp; CLASSIFY A DETECTION
              </span>
              <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600">honest confidence tiers</span>
            </div>
            <div className="p-4 space-y-3">
              <div className="font-mono text-[10px] leading-relaxed text-slate-500">
                <RadioTower size={12} className="inline mr-1" /> Broadcast decode (Remote ID / DroneID / SSID) →
                EXACT make/model/serial. RF signature (band + BW + family) → CLASS/FAMILY candidates only, never a
                fabricated exact model. Nothing usable → honest UNKNOWN.
              </div>
              <div className="flex flex-wrap gap-2">
                {MATCH_PRESETS.map((p) => (
                  <button
                    key={p.label}
                    onClick={() => runMatch(p.body)}
                    data-testid={`threat-preset-${p.label.replace(/[^a-z0-9]+/gi, "-").toLowerCase()}`}
                    className="px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              {matchErr && (
                <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }}>Match failed: {matchErr}</div>
              )}
              <MatchResult result={match} />
            </div>
          </div>

          {/* Browse / search */}
          <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
            <div className="tactical-border-b px-4 py-3 flex flex-col sm:flex-row gap-2 sm:items-center sm:justify-between">
              <span className="font-heading font-bold text-sm">THREAT LIBRARY ({filtered.length}/{entries.length})</span>
              <div className="flex flex-col sm:flex-row gap-2">
                <div className="flex items-center gap-2 tactical-border px-2 py-1.5">
                  <Search size={12} className="text-slate-500 shrink-0" />
                  <input
                    data-testid="threat-search"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="Search make, model, band, family..."
                    className="bg-transparent outline-none font-mono text-xs w-full text-slate-200 placeholder:text-slate-600"
                  />
                </div>
                <select
                  data-testid="threat-class-filter"
                  value={classFilter}
                  onChange={(e) => setClassFilter(e.target.value)}
                  className="tactical-border px-2 py-1.5 font-mono text-[11px] text-slate-300"
                  style={{ background: "var(--bg-surface)" }}
                >
                  {classes.map((c) => (
                    <option key={c} value={c} style={{ background: "var(--bg-surface)" }}>{c}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="p-4">
              {loading && (
                <div className="font-mono text-[11px] text-slate-500 flex items-center gap-2">
                  <RefreshCw size={12} className="animate-spin" /> loading threat library…
                </div>
              )}
              {error && (
                <div className="font-mono text-[11px]" style={{ color: "var(--accent-critical)" }} data-testid="threat-load-error">
                  Could not load threat library: {error}
                </div>
              )}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {filtered.map((e) => <EntryCard key={e.id} e={e} />)}
              </div>
              {!loading && !error && filtered.length === 0 && (
                <div className="font-mono text-[11px] text-slate-600 py-8 text-center">
                  No threat entries match this search/filter.
                </div>
              )}
            </div>
          </div>

          {/* Honest limits */}
          <div className="tactical-border p-3 font-mono text-[10px] leading-relaxed text-slate-500 flex gap-2"
            style={{ background: "var(--bg-surface)" }}>
            <ShieldAlert size={14} className="shrink-0 mt-0.5" style={{ color: "var(--accent-warning)" }} />
            <div>
              <span className="uppercase tracking-widest text-[9px] text-slate-400">Honest limits: </span>
              An RF signature alone (band/BW/family) yields a CLASS/FAMILY candidate, NOT a confirmed model or serial —
              only a decoded broadcast id can confirm identity. Cyber-takeover is applicable ONLY to unencrypted
              MAVLink-over-RF links; every encrypted/FHSS COTS link (OcuSync, SkyLink, ELRS, Crossfire) is marked
              not-injectable. Military entries are class-level placeholders with null emitter parameters — no classified
              or fabricated data.
            </div>
          </div>
        </div>
      )}

      {/* ==== Tab 2: Decoder Status (live) ==== */}
      {activeTab === "decoder" && (
        <div className="space-y-6" data-testid="library-tabpanel-decoder">
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
                {statusLoading ? <RefreshCw size={10} className="animate-spin" /> : null}
                {statusError ? <span style={{ color: "var(--accent-critical)" }}>status unavailable</span>
                       : `${operational.length} protocols`}
              </div>
            </div>
            {statusError && (
              <div className="tactical-border p-3 font-mono text-[10px] text-slate-400" style={{ background: "var(--bg-surface)" }} data-testid="protocol-status-error">
                Could not load live protocol status: {statusError}
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
        </div>
      )}

      {/* ==== Tab 3: Signature DB (reference) ==== */}
      {activeTab === "signature" && (
        <div className="space-y-6" data-testid="library-tabpanel-signature">
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
      )}
    </div>
  );
}
