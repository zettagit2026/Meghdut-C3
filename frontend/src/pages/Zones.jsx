import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  MapPin, Pencil, Trash2, ToggleLeft, ToggleRight, X, RefreshCw, ShieldAlert, Info,
} from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

// Zone management page (RFI 4.5.2.1 / 4.5.2.3).
// Table over db.zones (GET/PUT/DELETE /zones per .omc/plans/zone-sop-engine.md
// §Data models/Zone, §Endpoints). Polygon geometry itself is drawn/edited on
// the Map page (server.py POST /zones origin) — this page edits metadata
// (name/zone_type/priority/notes), toggles enabled, and deletes, all
// commander-gated (require_commander on every write). Honest empty/loading/
// error states throughout — never a fabricated row.

const ZONE_TYPES = ["DETECTION", "TRACKING", "ALERT", "MITIGATION", "CLUTTER"];

// One color per zone_type, reusing the app's existing accent vars (mirrors
// ThreatLibrary.jsx's LEVEL_STYLE / DetectionHistory.jsx's STATUS_STYLE
// convention) rather than inventing a new palette.
const ZONE_TYPE_STYLE = {
  DETECTION: { color: "var(--accent-info)" },
  TRACKING: { color: "var(--accent-success)" },
  ALERT: { color: "var(--accent-warning)" },
  MITIGATION: { color: "var(--accent-critical)" },
  CLUTTER: { color: "var(--text-muted)" },
};

// Typed-phrase confirm idiom reused (in spirit) from
// RangeAuthorizationControl.jsx's CONFIRM_PHRASE — a deliberate,
// un-fat-fingerable acknowledgment before an irreversible action, scaled down
// for a metadata-only delete (no TX/password involved here).
const DELETE_CONFIRM_PHRASE = "DELETE ZONE";

function TypeBadge({ type }) {
  const style = ZONE_TYPE_STYLE[type] || { color: "var(--text-muted)" };
  return (
    <span
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: style.color, borderColor: style.color }}
    >
      {type || "—"}
    </span>
  );
}

function vertexCount(zone) {
  const ring = zone?.polygon?.coordinates?.[0];
  return Array.isArray(ring) ? ring.length : 0;
}

function ZoneEditModal({ zone, onClose, onSaved }) {
  const [name, setName] = useState(zone.name || "");
  const [zoneType, setZoneType] = useState(zone.zone_type || ZONE_TYPES[0]);
  const [priority, setPriority] = useState(zone.priority ?? 0);
  const [notes, setNotes] = useState(zone.notes || "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      const { data } = await api.put(`/zones/${zone.id}`, {
        name,
        zone_type: zoneType,
        priority: Number(priority),
        notes,
      });
      onSaved(data);
    } catch (e2) {
      setErr(formatApiError(e2));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-6"
      style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
      data-testid="zone-edit-modal"
    >
      <form onSubmit={submit} className="max-w-xl w-full tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="px-5 py-3 tactical-border-b flex items-center justify-between">
          <span className="font-heading font-black text-lg uppercase tracking-tighter">Edit Zone</span>
          <button
            type="button"
            data-testid="zone-edit-close"
            onClick={onClose}
            className="text-slate-400 hover:text-[var(--text-primary)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Name</span>
            <input
              data-testid="zone-edit-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
              required
            />
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Zone type</span>
            <select
              data-testid="zone-edit-type"
              value={zoneType}
              onChange={(e) => setZoneType(e.target.value)}
              className="mt-1 w-full tactical-border px-3 py-2 font-mono text-xs"
              style={{ background: "var(--bg-surface)" }}
            >
              {ZONE_TYPES.map((t) => (
                <option key={t} value={t} style={{ background: "var(--bg-surface)" }}>{t}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Priority</span>
            <input
              data-testid="zone-edit-priority"
              type="number"
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
            />
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Notes</span>
            <textarea
              data-testid="zone-edit-notes"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              rows={3}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
            />
          </label>
          <div className="font-mono text-[10px] leading-relaxed text-slate-500 flex gap-2">
            <Info size={12} className="shrink-0 mt-0.5" />
            <span>Polygon geometry is drawn/edited on the Map page — this form edits metadata only.</span>
          </div>
          {err && (
            <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="zone-edit-error">
              {err}
            </div>
          )}
          <div className="tactical-border-t pt-3 flex items-center justify-between">
            <button
              type="button"
              data-testid="zone-edit-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              type="submit"
              data-testid="zone-edit-save"
              disabled={saving}
              className="px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest hover-accent-info scanline-btn disabled:opacity-50"
              style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
            >
              {saving ? "SAVING…" : "SAVE"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

function ZoneDeleteGate({ zone, onClose, onDeleted }) {
  const [phrase, setPhrase] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState(null);
  const matches = phrase.trim() === DELETE_CONFIRM_PHRASE; // client-side UX pre-check only; server re-validates the write

  async function confirmDelete() {
    if (!matches) return;
    setDeleting(true);
    setErr(null);
    try {
      await api.delete(`/zones/${zone.id}`);
      onDeleted(zone.id);
    } catch (e) {
      setErr(formatApiError(e));
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-6"
      style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
      data-testid="zone-delete-modal"
    >
      <div className="max-w-xl w-full" style={{ background: "var(--bg-surface)", border: "2px solid var(--accent-critical)" }}>
        <div
          className="px-5 py-4 flex items-center justify-between"
          style={{ background: "rgba(255,59,48,0.15)", borderBottom: "2px solid var(--accent-critical)" }}
        >
          <div className="flex items-center gap-3">
            <ShieldAlert size={20} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            <div className="font-heading font-black text-lg uppercase tracking-tighter">Delete Zone</div>
          </div>
          <button data-testid="zone-delete-close" onClick={onClose} className="text-slate-400 hover:text-[var(--text-primary)]">
            <X size={18} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div className="font-mono text-xs text-slate-300 leading-relaxed">
            Permanently delete zone{" "}
            <span className="font-bold" style={{ color: "var(--text-primary)" }}>{zone.name}</span>. This cannot be
            undone and removes it from every SOP rule scoped to it.
          </div>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Type the exact phrase: <span style={{ color: "var(--text-primary)" }}>{DELETE_CONFIRM_PHRASE}</span>
            </span>
            <input
              data-testid="zone-delete-phrase"
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder={DELETE_CONFIRM_PHRASE}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
            />
          </label>
          {err && (
            <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="zone-delete-error">
              {err}
            </div>
          )}
          <div className="pt-2 flex items-center justify-between" style={{ borderTop: "1px solid var(--border-col)" }}>
            <button
              data-testid="zone-delete-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              data-testid="zone-delete-confirm"
              onClick={confirmDelete}
              disabled={!matches || deleting}
              className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                !matches || deleting
                  ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                  : "text-white border-accent-critical"
              }`}
              style={matches && !deleting ? { background: "var(--accent-critical)" } : undefined}
            >
              <Trash2 size={14} strokeWidth={1.5} /> {deleting ? "DELETING…" : "DELETE ZONE"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Zones() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  const [zones, setZones] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notLive, setNotLive] = useState(false);

  const [editingZone, setEditingZone] = useState(null);
  const [deletingZone, setDeletingZone] = useState(null);
  const [togglingId, setTogglingId] = useState(null);
  const [toggleErr, setToggleErr] = useState(null);

  async function load() {
    setLoading(true);
    setError(null);
    setNotLive(false);
    try {
      const { data } = await api.get("/zones");
      setZones(Array.isArray(data) ? data : (data?.zones || []));
    } catch (e) {
      if (e?.response?.status === 404) {
        // Zone service not live yet (Phase A still landing) — honest empty
        // state, never a fabricated zone list.
        setNotLive(true);
        setZones([]);
      } else {
        setError(formatApiError(e));
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function toggleEnabled(zone) {
    setToggleErr(null);
    setTogglingId(zone.id);
    try {
      const { data } = await api.put(`/zones/${zone.id}`, { enabled: !zone.enabled });
      setZones((zs) => zs.map((z) => (z.id === zone.id ? { ...z, ...data } : z)));
    } catch (e) {
      setToggleErr(formatApiError(e));
    } finally {
      setTogglingId(null);
    }
  }

  const sorted = useMemo(
    () =>
      zones.slice().sort(
        (a, b) => (b.priority ?? 0) - (a.priority ?? 0) || String(a.name || "").localeCompare(String(b.name || ""))
      ),
    [zones]
  );

  return (
    <div className="space-y-6" data-testid="page-zones">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <MapPin size={12} className="inline mr-2" strokeWidth={1.5} /> Zone Management · RFI 4.5.2.1
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          Zone Management
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          Detection / tracking / alert / mitigation / clutter zones · point-in-polygon containment
        </div>
      </div>

      {!isCommander && (
        <div
          className="tactical-border p-3 font-mono text-[10px] leading-relaxed flex gap-2"
          style={{ background: "var(--bg-surface)", color: "var(--text-muted)" }}
          data-testid="zones-readonly-note"
        >
          <Info size={14} className="shrink-0 mt-0.5" />
          <div>Read-only — commander-gated. Enable/disable, edit, and delete require the commander role.</div>
        </div>
      )}

      <div className="font-mono text-[10px] leading-relaxed text-slate-500" data-testid="zones-geometry-note">
        Polygon geometry (draw / redraw) is edited on the{" "}
        <Link to="/map" className="hover:underline underline-offset-2 decoration-dotted" style={{ color: "var(--accent-info)" }}>
          Map
        </Link>{" "}
        page — this table edits zone metadata only.
      </div>

      {toggleErr && (
        <div
          className="tactical-border p-2 font-mono text-[10px]"
          style={{ color: "var(--accent-critical)", background: "var(--bg-surface)" }}
          data-testid="zones-toggle-error"
        >
          Update failed: {toggleErr}
        </div>
      )}

      <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest">Zones ({sorted.length})</span>
          <button
            onClick={load}
            data-testid="zones-refresh-btn"
            className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
          >
            <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs" data-testid="zones-table">
            <thead>
              <tr className="tactical-border-b font-mono text-[10px] uppercase tracking-widest text-slate-500">
                <th className="text-left p-2">NAME</th>
                <th className="text-left p-2">TYPE</th>
                <th className="text-right p-2">VERTICES</th>
                <th className="text-left p-2">STATUS</th>
                <th className="text-right p-2">PRIORITY</th>
                <th className="text-left p-2">CREATED BY</th>
                <th className="text-right p-2">ACTIONS</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {loading && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-slate-500">
                    retrieving<span className="term-caret" />
                  </td>
                </tr>
              )}
              {!loading && error && (
                <tr>
                  <td colSpan={7} className="p-4 text-center" style={{ color: "var(--accent-critical)" }} data-testid="zones-load-error">
                    Could not load zones: {error}
                  </td>
                </tr>
              )}
              {!loading && !error && notLive && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-slate-500" data-testid="zones-not-live">
                    Zone service not online yet (GET /zones 404). No zones fabricated — check back once the backend
                    zone endpoints land.
                  </td>
                </tr>
              )}
              {!loading && !error && !notLive && sorted.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-slate-500" data-testid="zones-empty">
                    No zones defined yet. Draw one on the Map page.
                  </td>
                </tr>
              )}
              {!loading && !error && sorted.map((zone) => {
                const busy = togglingId === zone.id;
                return (
                  <tr key={zone.id} className="tactical-border-b hover-surface transition-colors" data-testid={`zone-row-${zone.id}`}>
                    <td className="p-2" style={{ color: "var(--text-primary)" }}>{zone.name}</td>
                    <td className="p-2"><TypeBadge type={zone.zone_type} /></td>
                    <td className="p-2 text-right text-slate-300">{vertexCount(zone)}</td>
                    <td className="p-2">
                      <span
                        className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest"
                        style={{
                          color: zone.enabled ? "var(--accent-success)" : "var(--text-muted)",
                          borderColor: zone.enabled ? "var(--accent-success)" : "var(--text-muted)",
                        }}
                      >
                        {zone.enabled ? "● ENABLED" : "○ DISABLED"}
                      </span>
                    </td>
                    <td className="p-2 text-right text-slate-300">{zone.priority ?? "—"}</td>
                    <td className="p-2 text-slate-400">{zone.created_by || "—"}</td>
                    <td className="p-2">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          data-testid={`zone-enable-toggle-${zone.id}`}
                          onClick={() => toggleEnabled(zone)}
                          disabled={!isCommander || busy}
                          title={isCommander ? (zone.enabled ? "Disable zone" : "Enable zone") : "Commander role required"}
                          className="p-1.5 tactical-border hover-accent-info transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          {zone.enabled
                            ? <ToggleRight size={14} style={{ color: "var(--accent-success)" }} />
                            : <ToggleLeft size={14} style={{ color: "var(--text-muted)" }} />}
                        </button>
                        <button
                          data-testid={`zone-edit-btn-${zone.id}`}
                          onClick={() => setEditingZone(zone)}
                          disabled={!isCommander}
                          title={isCommander ? "Edit zone metadata" : "Commander role required"}
                          className="p-1.5 tactical-border hover-accent-info transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          data-testid={`zone-delete-btn-${zone.id}`}
                          onClick={() => setDeletingZone(zone)}
                          disabled={!isCommander}
                          title={isCommander ? "Delete zone" : "Commander role required"}
                          className="p-1.5 tactical-border hover-accent-critical transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                          style={{ color: "var(--accent-critical)" }}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {editingZone && (
        <ZoneEditModal
          zone={editingZone}
          onClose={() => setEditingZone(null)}
          onSaved={(updated) => {
            setZones((zs) => zs.map((z) => (z.id === editingZone.id ? { ...z, ...updated } : z)));
            setEditingZone(null);
          }}
        />
      )}

      {deletingZone && (
        <ZoneDeleteGate
          zone={deletingZone}
          onClose={() => setDeletingZone(null)}
          onDeleted={(id) => {
            setZones((zs) => zs.filter((z) => z.id !== id));
            setDeletingZone(null);
          }}
        />
      )}
    </div>
  );
}
