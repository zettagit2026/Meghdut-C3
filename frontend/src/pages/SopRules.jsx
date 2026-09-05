import { useEffect, useMemo, useState } from "react";
import {
  ScrollText, Pencil, Trash2, ToggleLeft, ToggleRight, X, RefreshCw, Info, ShieldAlert,
  Plus, FlaskConical,
} from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";

// No-code SOP rule builder (RFI 4.5.2.3 / 4.5.23).
//
// Pure form 1:1 to the db.sop_rules schema (.omc/plans/zone-sop-engine.md
// §Data models/SOP rule). Action control offers ONLY
// ALERT/ANNUNCIATE/PRIORITIZE/CUE_RECOMMENDATION -- there is deliberately no
// engage/fire/deploy option anywhere in this UI; the engine's strongest
// output is a proposal a commander still clears through the existing gated
// endpoints (SafetyGate / RangeAuthorizationControl), never a transmit call
// from here. Create/edit/delete are commander-only server-side
// (require_commander); this page hides/disables those controls for
// non-commanders. The validate preview (POST /sop/rules/validate) is
// get_current_user, so every operator can dry-run a rule against the
// CURRENT live contact set with no side effects.

const ACTION_TYPES = ["ALERT", "ANNUNCIATE", "PRIORITIZE", "CUE_RECOMMENDATION"];
const SEVERITIES = ["INFO", "CAUTION", "WARNING", "CRITICAL"];
const CONFIDENCE_TYPES = [
  "heuristic_binary", "ml_probability", "protocol_verified", "advisory_only", "unclassified_signal",
];
const THREAT_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"];
const RECOMMENDED_EFFECTS = ["jam", "gnss_spoof", "mavlink"];

const ACTION_TYPE_STYLE = {
  ALERT: { color: "var(--accent-warning)" },
  ANNUNCIATE: { color: "var(--accent-info)" },
  PRIORITIZE: { color: "var(--accent-success)" },
  CUE_RECOMMENDATION: { color: "var(--accent-critical)" },
};

const SEVERITY_STYLE = {
  INFO: { color: "var(--accent-info)" },
  CAUTION: { color: "var(--accent-warning)" },
  WARNING: { color: "var(--accent-warning)" },
  CRITICAL: { color: "var(--accent-critical)" },
};

// Typed-phrase confirm idiom reused (in spirit) from
// RangeAuthorizationControl.jsx's CONFIRM_PHRASE / Zones.jsx's
// ZoneDeleteGate -- a deliberate, un-fat-fingerable acknowledgment before an
// irreversible delete.
const DELETE_CONFIRM_PHRASE = "DELETE RULE";

function emptyRuleForm() {
  return {
    name: "",
    enabled: true,
    priority: 0,
    zone_id: "",
    conditions: {
      zone_membership: "any",
      require_position: false,
      protocol_in: [],
      class_in: [],
      family_in: [],
      band_in: [],
      min_confidence: "",
      confidence_type_in: [],
      threat_level_in: [],
    },
    action: {
      type: "ALERT",
      severity: "INFO",
      message_template: "",
      rank_boost: "",
      recommended_effect: "",
    },
  };
}

function ruleToForm(rule) {
  const c = rule.conditions || {};
  const a = rule.action || {};
  return {
    name: rule.name || "",
    enabled: rule.enabled !== false,
    priority: rule.priority ?? 0,
    zone_id: rule.zone_id || "",
    conditions: {
      zone_membership: c.zone_membership || "any",
      require_position: !!c.require_position,
      protocol_in: c.protocol_in || [],
      class_in: c.class_in || [],
      family_in: c.family_in || [],
      band_in: c.band_in || [],
      min_confidence: c.min_confidence ?? "",
      confidence_type_in: c.confidence_type_in || [],
      threat_level_in: c.threat_level_in || [],
    },
    action: {
      type: a.type || "ALERT",
      severity: a.severity || "INFO",
      message_template: a.message_template || "",
      rank_boost: a.rank_boost ?? "",
      recommended_effect: a.recommended_effect || "",
    },
  };
}

function formToBody(form) {
  return {
    name: form.name.trim(),
    enabled: !!form.enabled,
    priority: Number(form.priority) || 0,
    zone_id: form.zone_id ? form.zone_id : null,
    conditions: {
      zone_membership: form.conditions.zone_membership,
      require_position: !!form.conditions.require_position,
      protocol_in: form.conditions.protocol_in,
      class_in: form.conditions.class_in,
      family_in: form.conditions.family_in,
      band_in: form.conditions.band_in,
      min_confidence:
        form.conditions.min_confidence === "" || form.conditions.min_confidence === null
          ? null
          : Number(form.conditions.min_confidence),
      confidence_type_in: form.conditions.confidence_type_in,
      threat_level_in: form.conditions.threat_level_in,
    },
    action: {
      type: form.action.type,
      severity: form.action.severity,
      message_template: form.action.message_template,
      rank_boost:
        form.action.type === "PRIORITIZE" && form.action.rank_boost !== ""
          ? Number(form.action.rank_boost)
          : null,
      recommended_effect:
        form.action.type === "CUE_RECOMMENDATION" && form.action.recommended_effect
          ? form.action.recommended_effect
          : null,
    },
  };
}

function ActionTypeBadge({ type }) {
  const style = ACTION_TYPE_STYLE[type] || { color: "var(--text-muted)" };
  return (
    <span
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: style.color, borderColor: style.color }}
    >
      {type || "—"}
    </span>
  );
}

function SeverityBadge({ severity }) {
  const style = SEVERITY_STYLE[severity] || { color: "var(--text-muted)" };
  return (
    <span
      className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest whitespace-nowrap"
      style={{ color: style.color, borderColor: style.color }}
    >
      {severity || "—"}
    </span>
  );
}

// Comma-chip multiselect for open-vocabulary fields (protocol/class/family/
// band are threat-library-derived free text, not a fixed enum, so a fixed
// dropdown would either fabricate options or silently exclude real ones).
function ChipInput({ testid, values, onChange, placeholder }) {
  const [draft, setDraft] = useState("");

  function commit() {
    const v = draft.trim();
    if (v && !values.includes(v)) onChange([...values, v]);
    setDraft("");
  }

  function remove(v) {
    onChange(values.filter((x) => x !== v));
  }

  return (
    <div className="tactical-border px-2 py-1.5 flex flex-wrap gap-1.5 items-center" data-testid={testid}>
      {values.map((v) => (
        <span
          key={v}
          className="px-2 py-0.5 font-mono text-[10px] flex items-center gap-1"
          style={{ background: "var(--bg-elev)", color: "var(--text-primary)" }}
        >
          {v}
          <button
            type="button"
            data-testid={`${testid}-remove-${v}`}
            onClick={() => remove(v)}
            className="text-slate-500 hover:text-[var(--accent-critical)]"
          >
            <X size={10} />
          </button>
        </span>
      ))}
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
          if (e.key === "Backspace" && !draft && values.length) remove(values[values.length - 1]);
        }}
        onBlur={commit}
        placeholder={placeholder}
        className="bg-transparent outline-none font-mono text-[11px] text-slate-200 placeholder:text-slate-600 flex-1 min-w-[8ch]"
      />
    </div>
  );
}

function CheckboxGroup({ testidPrefix, options, values, onChange }) {
  function toggle(opt) {
    onChange(values.includes(opt) ? values.filter((v) => v !== opt) : [...values, opt]);
  }
  return (
    <div className="flex flex-wrap gap-3">
      {options.map((opt) => (
        <label key={opt} className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-slate-400 cursor-pointer">
          <input
            type="checkbox"
            data-testid={`${testidPrefix}-${opt}`}
            checked={values.includes(opt)}
            onChange={() => toggle(opt)}
          />
          {opt}
        </label>
      ))}
    </div>
  );
}

function MatchPreview({ preview, err, running }) {
  return (
    <div className="tactical-border p-3" style={{ background: "var(--bg-elev)" }} data-testid="sop-match-preview">
      {running && (
        <div className="font-mono text-[10px] text-slate-500">validating<span className="term-caret" /></div>
      )}
      {!running && err && (
        <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }}>Validate failed: {err}</div>
      )}
      {!running && !err && !preview && (
        <div className="font-mono text-[10px] text-slate-600">Run VALIDATE to preview live matches — no side effects.</div>
      )}
      {!running && !err && preview && (
        <div className="space-y-2">
          <div className="font-mono text-[11px]" style={{ color: "var(--text-primary)" }}>
            Would match <span className="font-bold">{preview.would_match_count}</span> of{" "}
            {preview.contacts_evaluated} current contact{preview.contacts_evaluated === 1 ? "" : "s"}.
          </div>
          {preview.matches?.length > 0 && (
            <div className="space-y-1 max-h-40 overflow-y-auto">
              {preview.matches.map((m, i) => (
                <div key={i} className="font-mono text-[10px] text-slate-400 flex flex-wrap gap-2 items-center">
                  <span style={{ color: "var(--text-primary)" }}>
                    {m.contact_ref?.callsign || m.contact_ref?.detection_id || m.contact_ref?.uas_id || m.contact_ref?.icao24 || m.contact_ref?.kind || "contact"}
                  </span>
                  <ActionTypeBadge type={m.action_type} />
                  <SeverityBadge severity={m.severity} />
                  {m.message && <span className="text-slate-500">{m.message}</span>}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function RuleForm({ initial, zones, isCommander, onCancel, onSaved }) {
  const [form, setForm] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState(null);
  const [preview, setPreview] = useState(null);
  const [previewErr, setPreviewErr] = useState(null);
  const [validating, setValidating] = useState(false);

  function setCond(patch) {
    setForm((f) => ({ ...f, conditions: { ...f.conditions, ...patch } }));
  }
  function setAction(patch) {
    setForm((f) => ({ ...f, action: { ...f.action, ...patch } }));
  }

  async function runValidate() {
    setValidating(true);
    setPreviewErr(null);
    try {
      const { data } = await api.post("/sop/rules/validate", formToBody(form));
      setPreview(data);
    } catch (e) {
      setPreviewErr(formatApiError(e));
      setPreview(null);
    } finally {
      setValidating(false);
    }
  }

  async function submit(e) {
    e.preventDefault();
    if (!isCommander) return;
    setSaving(true);
    setSaveErr(null);
    try {
      const body = formToBody(form);
      const { data } = initial.__id
        ? await api.put(`/sop/rules/${initial.__id}`, body)
        : await api.post("/sop/rules", body);
      onSaved(data);
    } catch (e2) {
      setSaveErr(formatApiError(e2));
    } finally {
      setSaving(false);
    }
  }

  const isEditing = !!initial.__id;

  return (
    <form onSubmit={submit} className="tactical-border p-4 space-y-5" style={{ background: "var(--bg-surface)" }} data-testid="sop-rule-form">
      <div className="flex items-center justify-between">
        <span className="font-heading font-black text-lg uppercase tracking-tighter">
          {isEditing ? "Edit Rule" : "New Rule"}
        </span>
        <button type="button" data-testid="sop-cancel-btn" onClick={onCancel} className="text-slate-400 hover:text-[var(--text-primary)]">
          <X size={16} />
        </button>
      </div>

      {!isCommander && (
        <div className="font-mono text-[10px] leading-relaxed flex gap-2" style={{ color: "var(--text-muted)" }} data-testid="sop-form-readonly-note">
          <Info size={12} className="shrink-0 mt-0.5" /> Commander-gated — you can preview matches but cannot save.
        </div>
      )}

      {/* Top */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <label className="block sm:col-span-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Name *</span>
          <input
            data-testid="sop-rule-name"
            value={form.name}
            onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            required
            disabled={!isCommander}
            className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none disabled:opacity-50"
          />
        </label>
        <label className="flex items-center gap-2 mt-1">
          <input
            type="checkbox"
            data-testid="sop-rule-enabled"
            checked={form.enabled}
            onChange={(e) => setForm((f) => ({ ...f, enabled: e.target.checked }))}
            disabled={!isCommander}
          />
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Enabled</span>
        </label>
        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Priority</span>
          <input
            data-testid="sop-rule-priority"
            type="number"
            value={form.priority}
            onChange={(e) => setForm((f) => ({ ...f, priority: e.target.value }))}
            disabled={!isCommander}
            className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none disabled:opacity-50"
          />
        </label>
        <label className="block sm:col-span-2">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Zone</span>
          <select
            data-testid="sop-zone-select"
            value={form.zone_id}
            onChange={(e) => setForm((f) => ({ ...f, zone_id: e.target.value }))}
            disabled={!isCommander}
            className="mt-1 w-full tactical-border px-3 py-2 font-mono text-xs disabled:opacity-50"
            style={{ background: "var(--bg-surface)" }}
          >
            <option value="" style={{ background: "var(--bg-surface)" }}>— global / non-spatial —</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id} style={{ background: "var(--bg-surface)" }}>{z.name}</option>
            ))}
          </select>
        </label>
      </div>

      {/* Conditions */}
      <div className="tactical-border-t pt-4 space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Conditions</div>

        <div className="flex flex-wrap items-center gap-4">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Zone membership</span>
          {["inside", "outside", "any"].map((m) => (
            <label key={m} className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-slate-300 cursor-pointer">
              <input
                type="radio"
                name="zone_membership"
                data-testid={`sop-membership-${m}`}
                checked={form.conditions.zone_membership === m}
                onChange={() => setCond({ zone_membership: m })}
                disabled={!isCommander}
              />
              {m}
            </label>
          ))}
        </div>

        <label className="flex items-start gap-2">
          <input
            type="checkbox"
            data-testid="sop-require-position"
            checked={form.conditions.require_position}
            onChange={(e) => setCond({ require_position: e.target.checked })}
            disabled={!isCommander}
            className="mt-0.5"
          />
          <span className="font-mono text-[10px] text-slate-400 leading-relaxed">
            Require position
            <span className="block text-slate-600">
              Honesty note: spatial conditions (this, or inside/outside) only match positioned RemoteID / ADS-B /
              DroneID contacts — a position-less detection is an honest miss, never a fabricated pin.
            </span>
          </span>
        </label>

        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Protocol in</span>
            <div className="mt-1">
              <ChipInput
                testid="sop-protocol-in"
                values={form.conditions.protocol_in}
                onChange={(v) => setCond({ protocol_in: v })}
                placeholder="e.g. remoteid, hackrf…"
              />
            </div>
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Class in</span>
            <div className="mt-1">
              <ChipInput
                testid="sop-class-in"
                values={form.conditions.class_in}
                onChange={(v) => setCond({ class_in: v })}
                placeholder="e.g. quadcopter…"
              />
            </div>
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Family in</span>
            <div className="mt-1">
              <ChipInput
                testid="sop-family-in"
                values={form.conditions.family_in}
                onChange={(v) => setCond({ family_in: v })}
                placeholder="e.g. DJI OcuSync…"
              />
            </div>
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Band in</span>
            <div className="mt-1">
              <ChipInput
                testid="sop-band-in"
                values={form.conditions.band_in}
                onChange={(v) => setCond({ band_in: v })}
                placeholder="e.g. 2.4GHz, 5.8GHz…"
              />
            </div>
          </label>
        </div>

        <label className="block max-w-xs">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Min confidence (0–1, optional)</span>
          <input
            data-testid="sop-min-confidence"
            type="number"
            min="0"
            max="1"
            step="0.05"
            value={form.conditions.min_confidence}
            onChange={(e) => setCond({ min_confidence: e.target.value })}
            disabled={!isCommander}
            placeholder="—"
            className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none disabled:opacity-50"
          />
        </label>

        <div>
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500 block mb-1.5">Confidence type in</span>
          <CheckboxGroup
            testidPrefix="sop-confidence-type"
            options={CONFIDENCE_TYPES}
            values={form.conditions.confidence_type_in}
            onChange={(v) => setCond({ confidence_type_in: v })}
          />
        </div>

        <div>
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500 block mb-1.5">Threat level in</span>
          <CheckboxGroup
            testidPrefix="sop-threat-level"
            options={THREAT_LEVELS}
            values={form.conditions.threat_level_in}
            onChange={(v) => setCond({ threat_level_in: v })}
          />
        </div>
      </div>

      {/* Action */}
      <div className="tactical-border-t pt-4 space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Action</div>

        <div className="flex flex-wrap items-center gap-4" data-testid="sop-action-type">
          {ACTION_TYPES.map((t) => (
            <label key={t} className="flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-widest text-slate-300 cursor-pointer">
              <input
                type="radio"
                name="action_type"
                data-testid={`sop-action-type-${t}`}
                checked={form.action.type === t}
                onChange={() => setAction({ type: t })}
                disabled={!isCommander}
              />
              {t}
            </label>
          ))}
        </div>
        <div className="font-mono text-[9px] text-slate-600">
          No engage / fire / deploy option exists — the strongest action is a commander-cleared proposal.
        </div>

        <label className="block max-w-xs">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Severity</span>
          <select
            data-testid="sop-severity"
            value={form.action.severity}
            onChange={(e) => setAction({ severity: e.target.value })}
            disabled={!isCommander}
            className="mt-1 w-full tactical-border px-3 py-2 font-mono text-xs disabled:opacity-50"
            style={{ background: "var(--bg-surface)" }}
          >
            {SEVERITIES.map((s) => (
              <option key={s} value={s} style={{ background: "var(--bg-surface)" }}>{s}</option>
            ))}
          </select>
        </label>

        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Message template</span>
          <textarea
            data-testid="sop-message-template"
            value={form.action.message_template}
            onChange={(e) => setAction({ message_template: e.target.value })}
            disabled={!isCommander}
            rows={2}
            className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none disabled:opacity-50"
          />
          <span className="font-mono text-[9px] text-slate-600 block mt-1">
            Placeholders: {"{model}"} {"{class}"} {"{family}"} {"{protocol}"} {"{band}"} {"{zone_name}"} {"{threat_level}"} — missing
            fields render blank, never a crash.
          </span>
        </label>

        {form.action.type === "PRIORITIZE" && (
          <label className="block max-w-xs">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Rank boost</span>
            <input
              data-testid="sop-rank-boost"
              type="number"
              value={form.action.rank_boost}
              onChange={(e) => setAction({ rank_boost: e.target.value })}
              disabled={!isCommander}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none disabled:opacity-50"
            />
          </label>
        )}

        {form.action.type === "CUE_RECOMMENDATION" && (
          <label className="block max-w-xs">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Recommended effect</span>
            <select
              data-testid="sop-recommended-effect"
              value={form.action.recommended_effect}
              onChange={(e) => setAction({ recommended_effect: e.target.value })}
              disabled={!isCommander}
              className="mt-1 w-full tactical-border px-3 py-2 font-mono text-xs disabled:opacity-50"
              style={{ background: "var(--bg-surface)" }}
            >
              <option value="" style={{ background: "var(--bg-surface)" }}>— none —</option>
              {RECOMMENDED_EFFECTS.map((e) => (
                <option key={e} value={e} style={{ background: "var(--bg-surface)" }}>{e}</option>
              ))}
            </select>
            <span className="font-mono text-[9px] leading-relaxed block mt-1" style={{ color: "var(--accent-warning)" }}>
              COMMANDER RECOMMENDATION only — a display label stamped PROPOSED_REQUIRES_HUMAN_AUTHORIZATION. It never
              auto-fires; the commander still clears the effect through the existing gated arm/confirm/range-auth
              chain.
            </span>
          </label>
        )}
      </div>

      {/* Validate preview */}
      <div className="tactical-border-t pt-4 space-y-2">
        <div className="flex items-center justify-between">
          <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Live preview</span>
          <button
            type="button"
            data-testid="sop-validate-btn"
            onClick={runValidate}
            disabled={validating || !form.name.trim()}
            className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn disabled:opacity-50"
          >
            <FlaskConical size={12} /> {validating ? "VALIDATING…" : "VALIDATE"}
          </button>
        </div>
        <MatchPreview preview={preview} err={previewErr} running={validating} />
      </div>

      {saveErr && (
        <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="sop-save-error">
          {saveErr}
        </div>
      )}

      <div className="tactical-border-t pt-3 flex items-center justify-between">
        <button
          type="button"
          data-testid="sop-cancel-btn-2"
          onClick={onCancel}
          className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
        >
          CANCEL
        </button>
        <button
          type="submit"
          data-testid="sop-save-btn"
          disabled={!isCommander || saving || !form.name.trim()}
          title={isCommander ? undefined : "Commander role required"}
          className="px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest hover-accent-info scanline-btn disabled:opacity-50"
          style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
        >
          {saving ? "SAVING…" : "SAVE RULE"}
        </button>
      </div>
    </form>
  );
}

function RuleDeleteGate({ rule, onClose, onDeleted }) {
  const [phrase, setPhrase] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [err, setErr] = useState(null);
  const matches = phrase.trim() === DELETE_CONFIRM_PHRASE; // client-side UX pre-check only; server re-validates

  async function confirmDelete() {
    if (!matches) return;
    setDeleting(true);
    setErr(null);
    try {
      await api.delete(`/sop/rules/${rule.id}`);
      onDeleted(rule.id);
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
      data-testid="sop-rule-delete-modal"
    >
      <div className="max-w-xl w-full" style={{ background: "var(--bg-surface)", border: "2px solid var(--accent-critical)" }}>
        <div
          className="px-5 py-4 flex items-center justify-between"
          style={{ background: "rgba(255,59,48,0.15)", borderBottom: "2px solid var(--accent-critical)" }}
        >
          <div className="flex items-center gap-3">
            <ShieldAlert size={20} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            <div className="font-heading font-black text-lg uppercase tracking-tighter">Delete Rule</div>
          </div>
          <button data-testid="sop-rule-delete-close" onClick={onClose} className="text-slate-400 hover:text-[var(--text-primary)]">
            <X size={18} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div className="font-mono text-xs text-slate-300 leading-relaxed">
            Permanently delete rule{" "}
            <span className="font-bold" style={{ color: "var(--text-primary)" }}>{rule.name}</span>. This cannot be
            undone; it is hot-removed from the next evaluation tick.
          </div>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Type the exact phrase: <span style={{ color: "var(--text-primary)" }}>{DELETE_CONFIRM_PHRASE}</span>
            </span>
            <input
              data-testid="sop-rule-delete-phrase"
              value={phrase}
              onChange={(e) => setPhrase(e.target.value)}
              autoComplete="off"
              spellCheck={false}
              placeholder={DELETE_CONFIRM_PHRASE}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
            />
          </label>
          {err && (
            <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="sop-rule-delete-error">
              {err}
            </div>
          )}
          <div className="pt-2 flex items-center justify-between" style={{ borderTop: "1px solid var(--border-col)" }}>
            <button
              data-testid="sop-rule-delete-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              data-testid="sop-rule-delete-confirm"
              onClick={confirmDelete}
              disabled={!matches || deleting}
              className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                !matches || deleting
                  ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                  : "text-white border-accent-critical"
              }`}
              style={matches && !deleting ? { background: "var(--accent-critical)" } : undefined}
            >
              <Trash2 size={14} strokeWidth={1.5} /> {deleting ? "DELETING…" : "DELETE RULE"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function SopRules() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  const [rules, setRules] = useState([]);
  const [zones, setZones] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notLive, setNotLive] = useState(false);

  const [editing, setEditing] = useState(null); // null | "new" | ruleObj
  const [deletingRule, setDeletingRule] = useState(null);
  const [togglingId, setTogglingId] = useState(null);
  const [toggleErr, setToggleErr] = useState(null);

  async function load() {
    setLoading(true);
    setError(null);
    setNotLive(false);
    try {
      const [rulesRes, zonesRes] = await Promise.all([
        api.get("/sop/rules"),
        api.get("/zones").catch(() => ({ data: { zones: [] } })),
      ]);
      const data = rulesRes.data;
      setRules(Array.isArray(data) ? data : (data?.rules || []));
      const zdata = zonesRes.data;
      setZones(Array.isArray(zdata) ? zdata : (zdata?.zones || []));
    } catch (e) {
      if (e?.response?.status === 404) {
        // SOP rule service not live yet (Phase B still landing) — honest
        // empty state, never a fabricated rule list.
        setNotLive(true);
        setRules([]);
      } else {
        setError(formatApiError(e));
      }
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function toggleEnabled(rule) {
    setToggleErr(null);
    setTogglingId(rule.id);
    try {
      const { data } = await api.put(`/sop/rules/${rule.id}`, { enabled: !rule.enabled });
      setRules((rs) => rs.map((r) => (r.id === rule.id ? { ...r, ...data } : r)));
    } catch (e) {
      setToggleErr(formatApiError(e));
    } finally {
      setTogglingId(null);
    }
  }

  const zoneName = useMemo(() => {
    const byId = Object.fromEntries(zones.map((z) => [z.id, z.name]));
    return (id) => (id ? (byId[id] || id) : "— global —");
  }, [zones]);

  const sorted = useMemo(
    () =>
      rules.slice().sort(
        (a, b) => (b.priority ?? 0) - (a.priority ?? 0) || String(a.name || "").localeCompare(String(b.name || ""))
      ),
    [rules]
  );

  function onSaved(saved) {
    setRules((rs) => {
      const exists = rs.some((r) => r.id === saved.id);
      return exists ? rs.map((r) => (r.id === saved.id ? saved : r)) : [saved, ...rs];
    });
    setEditing(null);
  }

  return (
    <div className="space-y-6" data-testid="page-sop-rules">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <ScrollText size={12} className="inline mr-2" strokeWidth={1.5} /> No-Code SOP Engine · RFI 4.5.2.3
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          SOP Rules
        </h1>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mt-1">
          Alert / annunciate / prioritize / cue rules · hot-applied, no redeploy
        </div>
      </div>

      {!isCommander && (
        <div
          className="tactical-border p-3 font-mono text-[10px] leading-relaxed flex gap-2"
          style={{ background: "var(--bg-surface)", color: "var(--text-muted)" }}
          data-testid="sop-readonly-note"
        >
          <Info size={14} className="shrink-0 mt-0.5" />
          <div>
            Read-only — commander-gated. Create, edit, and delete require the commander role. You may still run the
            live validate preview on any draft.
          </div>
        </div>
      )}

      {toggleErr && (
        <div
          className="tactical-border p-2 font-mono text-[10px]"
          style={{ color: "var(--accent-critical)", background: "var(--bg-surface)" }}
          data-testid="sop-toggle-error"
        >
          Update failed: {toggleErr}
        </div>
      )}

      {editing && (
        <RuleForm
          key={editing === "new" ? "new" : editing.id}
          initial={editing === "new" ? emptyRuleForm() : { ...ruleToForm(editing), __id: editing.id }}
          zones={zones}
          isCommander={isCommander}
          onCancel={() => setEditing(null)}
          onSaved={onSaved}
        />
      )}

      <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <span className="font-mono text-xs uppercase tracking-widest">Rules ({sorted.length})</span>
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              data-testid="sop-refresh-btn"
              className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn"
            >
              <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> Refresh
            </button>
            <button
              onClick={() => setEditing("new")}
              disabled={!isCommander}
              title={isCommander ? "New SOP rule" : "Commander role required"}
              data-testid="sop-new-rule-btn"
              className="flex items-center gap-2 px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn disabled:opacity-30 disabled:cursor-not-allowed"
            >
              <Plus size={12} /> New rule
            </button>
          </div>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-xs" data-testid="sop-rules-table">
            <thead>
              <tr className="tactical-border-b font-mono text-[10px] uppercase tracking-widest text-slate-500">
                <th className="text-left p-2">NAME</th>
                <th className="text-left p-2">ZONE</th>
                <th className="text-left p-2">ACTION</th>
                <th className="text-left p-2">SEVERITY</th>
                <th className="text-left p-2">STATUS</th>
                <th className="text-right p-2">PRIORITY</th>
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
                  <td colSpan={7} className="p-4 text-center" style={{ color: "var(--accent-critical)" }} data-testid="sop-load-error">
                    Could not load SOP rules: {error}
                  </td>
                </tr>
              )}
              {!loading && !error && notLive && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-slate-500" data-testid="sop-not-live">
                    SOP rule service not online yet (GET /sop/rules 404). No rules fabricated — check back once the
                    backend rule endpoints land.
                  </td>
                </tr>
              )}
              {!loading && !error && !notLive && sorted.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-slate-500" data-testid="sop-rules-empty">
                    No SOP rules defined yet.
                  </td>
                </tr>
              )}
              {!loading && !error && sorted.map((rule) => {
                const busy = togglingId === rule.id;
                return (
                  <tr key={rule.id} className="tactical-border-b hover-surface transition-colors" data-testid={`sop-rule-row-${rule.id}`}>
                    <td className="p-2" style={{ color: "var(--text-primary)" }}>{rule.name}</td>
                    <td className="p-2 text-slate-400">{zoneName(rule.zone_id)}</td>
                    <td className="p-2"><ActionTypeBadge type={rule.action?.type} /></td>
                    <td className="p-2"><SeverityBadge severity={rule.action?.severity} /></td>
                    <td className="p-2">
                      <span
                        className="px-2 py-0.5 tactical-border font-bold text-[9px] uppercase tracking-widest"
                        style={{
                          color: rule.enabled ? "var(--accent-success)" : "var(--text-muted)",
                          borderColor: rule.enabled ? "var(--accent-success)" : "var(--text-muted)",
                        }}
                      >
                        {rule.enabled ? "● ENABLED" : "○ DISABLED"}
                      </span>
                    </td>
                    <td className="p-2 text-right text-slate-300">{rule.priority ?? "—"}</td>
                    <td className="p-2">
                      <div className="flex items-center justify-end gap-2">
                        <button
                          onClick={() => toggleEnabled(rule)}
                          disabled={!isCommander || busy}
                          title={isCommander ? (rule.enabled ? "Disable rule" : "Enable rule") : "Commander role required"}
                          className="p-1.5 tactical-border hover-accent-info transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          {rule.enabled
                            ? <ToggleRight size={14} style={{ color: "var(--accent-success)" }} />
                            : <ToggleLeft size={14} style={{ color: "var(--text-muted)" }} />}
                        </button>
                        <button
                          data-testid={`sop-rule-edit-${rule.id}`}
                          onClick={() => setEditing(rule)}
                          disabled={!isCommander}
                          title={isCommander ? "Edit rule" : "Commander role required"}
                          className="p-1.5 tactical-border hover-accent-info transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          <Pencil size={14} />
                        </button>
                        <button
                          data-testid={`sop-rule-delete-${rule.id}`}
                          onClick={() => setDeletingRule(rule)}
                          disabled={!isCommander}
                          title={isCommander ? "Delete rule" : "Commander role required"}
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

      {deletingRule && (
        <RuleDeleteGate
          rule={deletingRule}
          onClose={() => setDeletingRule(null)}
          onDeleted={(id) => {
            setRules((rs) => rs.filter((r) => r.id !== id));
            setDeletingRule(null);
          }}
        />
      )}
    </div>
  );
}
