import { useEffect, useMemo, useRef, useState } from "react";
import {
  Crosshair, Search, Upload, Download, ShieldAlert, Target, RadioTower, RefreshCw,
} from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

// Drone Threat Library (RFI Northern Command Sec 4.2.12).
//
// HONEST DOCTRINE (surfaced in the UI, not hidden):
//   * A DECODED BROADCAST id (ASTM Remote ID serial / DJI DroneID make+model /
//     distinctive Wi-Fi SSID) gives an EXACT make/model/serial -> HIGH confidence.
//   * An RF-SIGNATURE-only observation (band + occupied BW + control-link family)
//     gives CLASS / FAMILY candidates only -> LOWER confidence, ranked, and NEVER
//     a fabricated exact model.
//   * Nothing usable -> honest "unknown", no guess.
// Library entries are compiled from PUBLIC specs; military entries are class-level
// placeholders only (no classified data).

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

export default function ThreatLibrary() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const fileRef = useRef(null);

  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const [query, setQuery] = useState("");
  const [classFilter, setClassFilter] = useState("ALL");

  const [match, setMatch] = useState(null);
  const [matchErr, setMatchErr] = useState(null);
  const [importMsg, setImportMsg] = useState(null);
  const [importErr, setImportErr] = useState(null);

  async function load() {
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

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

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
      await load();
    } catch (e) {
      if (e?.name === "SyntaxError") setImportErr("File is not valid JSON.");
      else setImportErr(formatApiError(e));
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="space-y-6" data-testid="page-threat-library">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> Inbuilt Drone Threat Library · RFI 4.2.12
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Threat Library
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          COTS + class-level military threat drones · identify &amp; classify · offline/online update without OEM
        </div>
      </div>

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
  );
}
