import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { api, formatApiError } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { Rss, ShieldAlert, ShieldPlus, ShieldCheck, Search, ArrowUp, ArrowDown, X, Crosshair, Radio } from "lucide-react";
import {
  filterAps, sortAps, isPossibleUas, canDesignateUas, encryptionLabel, pmfLabel, lastSeenLabel, macOui,
  hasConcreteBssid, designateJustificationRequired, designateJustificationValid, DESIGNATE_JUSTIFICATION_MIN_LEN,
  apChannelToMhz,
} from "@/lib/wifiEnvironment";

// WI-FI ENVIRONMENT — RF situational awareness.
//
// A READ-ONLY window onto the ambient Wi-Fi picture from Kismet (via the
// backend's GET /api/wifi-environment proxy). It lets the operator SEE every
// AP/device in range. It is NOT a blanket target list, and the panel itself
// TRANSMITS NOTHING. It makes exactly two commander-gated, non-GET calls:
//   (1) PROTECT — POST /api/no-strike adds a row to the P1 no-strike
//       civilian-protection registry (a defensive action, never an engagement).
//   (2) DESIGNATE — POST /api/wifi-environment/designate promotes ONE
//       drone-OUI/SSID, non-civilian, non-broadcast row into a governed
//       db.detections contact and then DEEP-LINKS into the existing,
//       byte-unchanged /wifi-defeat SafetyGate flow. This is a fire-ADJACENT
//       targeting affordance, NOT a fire control: it creates a governed contact
//       and routes — the deliberate arm/confirm/fire happens on the WifiDefeat
//       screen (its SafetyGate), never here. Commander-gated; the backend
//       re-derives every gate server-side (the client hint is never trusted).
// A "possible UAS" tag is an advisory candidate hint (SSID/OUI spoofable —
// candidate, not identification); only such a row is designate-eligible.

const COLUMNS = [
  { key: "ssid", label: "SSID", sortable: true },
  { key: "bssid", label: "BSSID", sortable: true },
  { key: "channel", label: "CH", sortable: true },
  { key: "rssi_dbm", label: "RSSI", sortable: true },
  { key: "vendor", label: "VENDOR", sortable: true },
  { key: "encryption", label: "ENCRYPTION / PMF", sortable: true },
  { key: "client_count", label: "CLIENTS", sortable: true },
  { key: "last_seen", label: "LAST SEEN", sortable: true },
];

const NO_STRIKE_CATEGORIES = ["CIVILIAN_INFRASTRUCTURE", "FRIENDLY_OWN_FORCE", "NEUTRAL"];

// Does an already-loaded no-strike registry entry match this AP row (by
// BSSID or OUI)? Client-side, best-effort -- purely a display convenience so
// a commander can see what is already protected; the backend remains the
// source of truth for any real consult.
function entryMatchesAp(entry, ap) {
  const match = entry?.match || {};
  const bssid = (ap?.bssid || "").toUpperCase();
  const oui = macOui(ap?.bssid);
  if (match.bssid && bssid && match.bssid.toUpperCase() === bssid) return true;
  if (match.oui && oui && match.oui.toUpperCase() === oui) return true;
  return false;
}

// Commander-only PROTECT action: pre-fills a no-strike registry entry from
// one AP row and submits it via the existing P1 CRUD endpoint. This is
// deliberately the ONLY mutating call this page makes -- there is no
// engage, deauth, or arming affordance here or anywhere else on this page.
function AddNoStrikeModal({ ap, onClose, onAdded }) {
  const oui = macOui(ap.bssid);
  const [category, setCategory] = useState("CIVILIAN_INFRASTRUCTURE");
  const [label, setLabel] = useState(ap.vendor || ap.ssid || ap.bssid || "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit(e) {
    e.preventDefault();
    setSaving(true);
    setErr(null);
    try {
      const match = {};
      if (ap.bssid) match.bssid = ap.bssid;
      if (ap.ssid) match.ssid_exact = ap.ssid;
      if (oui) match.oui = oui;
      const { data } = await api.post("/no-strike", { category, match, label: label.trim() });
      toast.success("ADDED TO NO-STRIKE", { description: `${label.trim()} — ${category}` });
      onAdded(data);
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
      data-testid="wifi-env-no-strike-modal"
    >
      <form onSubmit={submit} className="max-w-xl w-full tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="px-5 py-3 tactical-border-b flex items-center justify-between">
          <span className="font-heading font-black text-lg uppercase tracking-tighter flex items-center gap-2">
            <ShieldPlus size={16} strokeWidth={1.5} style={{ color: "var(--accent-success)" }} />
            Add To No-Strike
          </span>
          <button
            type="button"
            data-testid="wifi-env-no-strike-close"
            onClick={onClose}
            className="text-slate-400 hover:text-[var(--text-primary)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div className="font-mono text-[10px] leading-relaxed text-slate-500">
            A civilian-protection floor entry — this only ever PROTECTS a match, it can never engage or
            authorise an effect. Read-only fields below come straight from the Wi-Fi survey row.
          </div>
          <dl className="grid grid-cols-2 gap-2 font-mono text-[11px]">
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">BSSID</dt>
              <dd className="text-slate-300">{ap.bssid || "—"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">SSID</dt>
              <dd className="text-slate-300">{ap.ssid || "(hidden)"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">OUI</dt>
              <dd className="text-slate-300">{oui || "—"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">Vendor</dt>
              <dd className="text-slate-300">{ap.vendor || "unknown"}</dd>
            </div>
          </dl>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Category</span>
            <select
              data-testid="wifi-env-no-strike-category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="mt-1 w-full tactical-border px-3 py-2 font-mono text-xs"
              style={{ background: "var(--bg-surface)" }}
            >
              {NO_STRIKE_CATEGORIES.map((c) => (
                <option key={c} value={c} style={{ background: "var(--bg-surface)" }}>{c}</option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Label</span>
            <input
              data-testid="wifi-env-no-strike-label"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
              required
            />
          </label>
          {err && (
            <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="wifi-env-no-strike-error">
              {err}
            </div>
          )}
          <div className="tactical-border-t pt-3 flex items-center justify-between">
            <button
              type="button"
              data-testid="wifi-env-no-strike-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              type="submit"
              data-testid="wifi-env-no-strike-save"
              disabled={saving || !label.trim()}
              className="px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest hover-accent-info scanline-btn disabled:opacity-50"
              style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}
            >
              {saving ? "SAVING…" : "ADD PROTECTION"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

// Commander-only DESIGNATE handoff. This is a fire-ADJACENT targeting
// affordance, NOT a fire control: confirming here promotes ONE possible-UAS
// survey row into a governed contact and then DEEP-LINKS into the existing,
// byte-unchanged /wifi-defeat SafetyGate flow. The actual arm/confirm/fire
// happens on that next screen — never here. Deliberate (a confirm modal),
// never a bare one-click deauth, with HONEST spoofable-candidate copy.
// `justificationRequired` is TRUE only for a commander-override (non-drone)
// designation AND only when the backend COMMANDER_OVERRIDE_JUSTIFICATION_REQUIRED
// flag is on (surfaced on the survey payload). When TRUE, a required ≥20-char
// justification textarea is shown and the confirm stays disabled until it is
// valid; the justification is sent in the designate POST. When FALSE (default,
// pre-field), no field is shown, the confirm is enabled immediately, and the
// POST omits the justification entirely.
function DesignateUasModal({ ap, justificationRequired, onConfirm, onClose }) {
  const oui = macOui(ap.bssid);
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);
  const [justification, setJustification] = useState("");

  const justificationOk = !justificationRequired || designateJustificationValid(justification);

  async function submit(e) {
    e.preventDefault();
    if (!justificationOk) return;
    setSaving(true);
    setErr(null);
    try {
      await onConfirm(justificationRequired ? justification.trim() : undefined);
      // On success the parent navigates away; leave `saving` true so the
      // button stays disabled through the unmount (no flicker).
    } catch (e2) {
      setErr(formatApiError(e2));
      setSaving(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-6"
      style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
      data-testid="wifi-env-designate-modal"
    >
      <form onSubmit={submit} className="max-w-xl w-full tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="px-5 py-3 tactical-border-b flex items-center justify-between">
          <span className="font-heading font-black text-lg uppercase tracking-tighter flex items-center gap-2">
            <Crosshair size={16} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} />
            Engage As Suspected UAS
          </span>
          <button
            type="button"
            data-testid="wifi-env-designate-close"
            onClick={onClose}
            className="text-slate-400 hover:text-[var(--text-primary)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div className="font-mono text-[10px] leading-relaxed text-slate-400 space-y-2">
            <p>
              This designates a <span className="font-bold">SUSPECTED UAS</span>. The SSID and OUI are
              <span className="font-bold"> spoofable</span> — this is a candidate,
              <span className="font-bold"> NOT an identification</span>.
            </p>
            <p>
              It will deauth <span className="font-bold">ONLY this one BSSID</span> ({ap.bssid}) — never a
              broadcast or band-wide deauth.
            </p>
            <p>
              Confirming here only creates a governed contact and routes you to the Wi-Fi-defeat screen. The
              actual <span className="font-bold">arm and fire happen on that next screen</span> (its SafetyGate
              is the deliberate confirm) — nothing is armed, transmitted, or fired here.
            </p>
          </div>
          <dl className="grid grid-cols-2 gap-2 font-mono text-[11px]">
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">BSSID</dt>
              <dd className="text-slate-300">{ap.bssid || "—"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">SSID</dt>
              <dd className="text-slate-300">{ap.ssid || "(hidden)"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">OUI</dt>
              <dd className="text-slate-300">{oui || "—"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">Vendor</dt>
              <dd className="text-slate-300">{ap.vendor || "unknown"}</dd>
            </div>
          </dl>
          {justificationRequired && (
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Justification — required. Why is this NON-DRONE, possibly-civilian AP a target?
                (min {DESIGNATE_JUSTIFICATION_MIN_LEN} chars, specific — not a placeholder)
              </span>
              <textarea
                data-testid="wifi-env-designate-justification"
                value={justification}
                onChange={(e) => setJustification(e.target.value)}
                rows={3}
                required
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none"
                style={{ background: "var(--bg-surface)" }}
                placeholder="e.g. Observed co-located with the reported UAS launch point, RSSI tracking the visual contact…"
              />
              <span
                data-testid="wifi-env-designate-justification-count"
                className="mt-1 block font-mono text-[9px] uppercase tracking-widest"
                style={{ color: justificationOk ? "var(--accent-success)" : "var(--text-muted)" }}
              >
                {justification.trim().length}/{DESIGNATE_JUSTIFICATION_MIN_LEN} min
              </span>
            </label>
          )}
          {err && (
            <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }} data-testid="wifi-env-designate-error">
              {err}
            </div>
          )}
          <div className="tactical-border-t pt-3 flex items-center justify-between">
            <button
              type="button"
              data-testid="wifi-env-designate-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              type="submit"
              data-testid="wifi-env-designate-confirm"
              disabled={saving || !justificationOk}
              className="px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest hover-surface scanline-btn disabled:opacity-50"
              style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
            >
              {saving ? "DESIGNATING…" : "DESIGNATE & PROCEED"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

// Commander-only JAM CHANNEL (AREA) caution. This is a SEPARATE, distinct
// affordance from the ENGAGE (per-BSSID deauth) controls above -- it never
// deauths and it is NOT gated by the no-strike registry (a channel-wide
// barrage cannot be scoped to spare one BSSID, so no-strike protection can't
// meaningfully apply the way it does to a per-BSSID deauth). Confirming here
// makes NO api call and TRANSMITS NOTHING -- it only client-side
// navigate()s to the Jamming page with this channel's center frequency
// prefilled; the actual arm/confirm/fire (and its own AREA-DENIAL caution +
// SafetyGate checklist item) live entirely on that next screen.
function JamChannelAreaModal({ ap, freqMhz, civilianProtected, onClose, onConfirm }) {
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-6"
      style={{ background: "rgba(5, 8, 16, 0.9)", backdropFilter: "blur(4px)" }}
      data-testid="wifi-env-jam-area-modal"
    >
      <div className="max-w-xl w-full tactical-border" style={{ background: "var(--bg-surface)" }}>
        <div className="px-5 py-3 tactical-border-b flex items-center justify-between">
          <span className="font-heading font-black text-lg uppercase tracking-tighter flex items-center gap-2">
            <Radio size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            Jam Channel — Area Denial
          </span>
          <button
            type="button"
            data-testid="wifi-env-jam-area-close"
            onClick={onClose}
            className="text-slate-400 hover:text-[var(--text-primary)]"
          >
            <X size={16} />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <div className="font-mono text-[10px] leading-relaxed text-slate-300 space-y-2">
            <p>
              <span className="font-bold" style={{ color: "var(--accent-critical)" }}>
                AREA DENIAL — NOT a per-BSSID / targeted strike.
              </span>{" "}
              Jamming channel {ap.channel || "—"} ({freqMhz != null ? `${freqMhz} MHz` : "frequency unknown"}) denies
              THIS ENTIRE CHANNEL to <span className="font-bold">ALL devices within RF range</span> — the target
              AP ({ap.bssid}) AND its clients AND every other co-channel device, including{" "}
              <span className="font-bold">civilian and your own networks</span>. This is a channel-wide barrage,
              not a targeted deauth; it cannot be scoped to one device.
            </p>
            <p>
              Governed by the RANGE AUTHORIZATION lease on the Jamming page, NOT by BSSID — it is not gated by
              the no-strike registry the way per-BSSID engagement is, because area jam cannot exempt one AP from
              a channel-wide barrage.
            </p>
            {civilianProtected && (
              <p
                data-testid="wifi-env-jam-area-civilian-warning"
                style={{ color: "var(--accent-critical)" }}
                className="font-bold"
              >
                WARNING — REGISTERED CIVILIAN INFRASTRUCTURE: this AP is on the no-strike registry as
                CIVILIAN_INFRASTRUCTURE. Jamming its channel still denies THAT civilian network and its
                neighbours too — the no-strike protection cannot exempt it from an area-wide channel jam.
              </p>
            )}
            <p>
              Confirming here only takes you to the Jamming page with this frequency prefilled — nothing is
              armed or transmitted here. The arm/confirm/fire sequence and its SafetyGate live on that page.
            </p>
          </div>
          <dl className="grid grid-cols-2 gap-2 font-mono text-[11px]">
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">BSSID</dt>
              <dd className="text-slate-300">{ap.bssid || "—"}</dd>
            </div>
            <div>
              <dt className="text-slate-500 uppercase tracking-widest text-[9px]">Channel</dt>
              <dd className="text-slate-300">{ap.channel || "—"}</dd>
            </div>
          </dl>
          <div className="tactical-border-t pt-3 flex items-center justify-between">
            <button
              type="button"
              data-testid="wifi-env-jam-area-cancel"
              onClick={onClose}
              className="px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest text-slate-400 hover-surface"
            >
              CANCEL
            </button>
            <button
              type="button"
              data-testid="wifi-env-jam-area-confirm"
              onClick={onConfirm}
              disabled={freqMhz == null}
              className="px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest hover-surface scanline-btn disabled:opacity-50"
              style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
            >
              PROCEED TO JAMMING (AREA DENIAL)
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default function WifiEnvironment() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const navigate = useNavigate();

  const [survey, setSurvey] = useState(null);
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState("rssi_dbm");
  const [sortDir, setSortDir] = useState("desc");
  const [loadError, setLoadError] = useState(null);
  const [noStrikeEntries, setNoStrikeEntries] = useState([]);
  const [addingAp, setAddingAp] = useState(null);
  const [designatingAp, setDesignatingAp] = useState(null);
  const [jammingAp, setJammingAp] = useState(null);

  const cancelledRef = useRef(false);
  useEffect(() => () => { cancelledRef.current = true; }, []);

  const load = async () => {
    try {
      const { data } = await api.get("/wifi-environment");
      if (cancelledRef.current) return;
      setSurvey(data);
      setLoadError(null);
    } catch (e) {
      if (cancelledRef.current) return;
      setLoadError(formatApiError(e));
    }
  };

  // Live view: poll every 4s like the other live surfaces (Signals/FPV).
  useEffect(() => {
    load();
    const id = setInterval(load, 4000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // "Protected" badge (nicety): load the no-strike registry ONCE so already-
  // protected rows can be tagged client-side. Commander-only endpoint -- a
  // non-commander simply won't see the badge, which is fine (they also can't
  // add one). Never retried/polled; a fresh add is merged in locally instead.
  useEffect(() => {
    if (!isCommander) return;
    let cancelled = false;
    api.get("/no-strike")
      .then(({ data }) => { if (!cancelled) setNoStrikeEntries(data?.entries || []); })
      .catch(() => { /* best-effort nicety only -- never surface an error for this */ });
    return () => { cancelled = true; };
  }, [isCommander]);

  const aps = useMemo(() => survey?.aps || [], [survey]);
  const rows = useMemo(
    () => sortAps(filterAps(aps, query), sortKey, sortDir),
    [aps, query, sortKey, sortDir]
  );
  const possibleUasCount = useMemo(
    () => aps.filter((ap) => isPossibleUas(ap)).length, [aps]
  );
  const enabledNoStrikeEntries = useMemo(
    () => noStrikeEntries.filter((e) => e.enabled !== false), [noStrikeEntries]
  );
  const isApProtected = (ap) => enabledNoStrikeEntries.some((entry) => entryMatchesAp(entry, ap));

  // Commander DESIGNATE: create the governed contact, then deep-link into the
  // existing /wifi-defeat SafetyGate flow. This call ONLY creates the contact +
  // routes — it does NOT arm or fire (WifiDefeat's SafetyGate is the deliberate
  // confirm). The backend re-derives every gate server-side.
  const doDesignate = async (ap, justification) => {
    const ch = ap.channel != null && ap.channel !== "" ? Number(ap.channel) : null;
    const { data } = await api.post("/wifi-environment/designate", {
      bssid: ap.bssid,
      ssid: ap.ssid,
      oui: ap.oui || macOui(ap.bssid),
      channel: Number.isFinite(ch) ? ch : null,
      vendor: ap.vendor,
      pmf_required: ap.pmf_required,
      pmf_supported: ap.pmf_supported,
      // COMMANDER OVERRIDE: only set for a non-drone-tagged row the commander
      // deliberately designated (the backend forces the honest non-drone label).
      commander_override: ap.__commanderOverride === true,
      // JUSTIFICATION: sent ONLY when the modal collected one (commander-override
      // AND the backend flag is on). Omitted entirely otherwise, so the payload
      // shape for a normal drone-tagged designate is unchanged.
      ...(justification ? { justification } : {}),
    });
    toast.success("DESIGNATED — SUSPECTED UAS", {
      description: `${ap.ssid || ap.bssid} → Wi-Fi defeat (candidate, not identification)`,
    });
    navigate(`/wifi-defeat?contact=${data.detection_id}`);
  };

  const toggleSort = (key) => {
    if (key === sortKey) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "ssid" || key === "vendor" || key === "bssid" ? "asc" : "desc");
    }
  };

  const configured = survey ? survey.configured : true;
  const available = survey ? survey.available : false;

  return (
    <div className="space-y-5" data-testid="wifi-environment-page">
      {/* Header */}
      <div className="flex items-end justify-between flex-wrap gap-3">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Rss size={12} className="inline mr-2" strokeWidth={1.5} /> RF Situational Awareness
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Wi-Fi Environment
          </h1>
        </div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 text-right">
          <div>
            SOURCE: <span style={{ color: "var(--text-primary)" }}>KISMET (RX-only)</span>
          </div>
          {survey && (
            <div className="mt-0.5">
              <span style={{ color: available ? "var(--accent-success)" : "var(--accent-warning)" }}>
                ● {available ? "LIVE" : "NO DATA"}
              </span>
              {available && (
                <> · {survey.ap_count}/{survey.total_seen} shown{survey.polled_at ? ` · ${new Date(survey.polled_at).toLocaleTimeString()}` : ""}</>
              )}
            </div>
          )}
        </div>
      </div>

      {/* SITUATIONAL-AWARENESS / NOT-A-TARGET-LIST banner (prominent, always on) */}
      <div
        data-testid="wifi-env-sa-banner"
        className="tactical-border p-3 flex items-start gap-3"
        style={{
          borderColor: "var(--accent-warning)",
          background: "color-mix(in srgb, var(--accent-warning) 10%, var(--bg-surface))",
        }}
      >
        <ShieldAlert size={18} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} className="mt-0.5 shrink-0" />
        <div className="font-mono text-[11px] leading-relaxed" style={{ color: "var(--text-primary)" }}>
          <span className="font-bold uppercase tracking-widest" style={{ color: "var(--accent-warning)" }}>
            Situational awareness — not a blanket target list.
          </span>{" "}
          This is a passive, read-only view of the ambient Wi-Fi picture from Kismet. A “possible UAS”
          tag is an advisory <span className="font-bold">candidate</span> hint — SSID/OUI are spoofable, so
          it is a <span className="font-bold">candidate, not identification</span>. A commander MAY designate
          a possible-UAS row as a suspected-UAS contact, which routes into the governed per-BSSID
          Wi-Fi-defeat flow — the deliberate arm/confirm happens there (its SafetyGate), never on this
          panel. No AP is armed, transmitted to, or fired on from here.
        </div>
      </div>

      {/* Controls */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 tactical-border px-3 py-2" style={{ background: "var(--bg-surface)" }}>
          <Search size={13} strokeWidth={1.5} style={{ color: "var(--text-muted)" }} />
          <input
            data-testid="wifi-env-filter"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter SSID / BSSID / vendor / channel…"
            className="bg-transparent outline-none font-mono text-xs w-72 max-w-[70vw]"
            style={{ color: "var(--text-primary)" }}
          />
        </div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
          {possibleUasCount > 0 && (
            <span style={{ color: "var(--accent-warning)" }}>
              ⚑ {possibleUasCount} possible-UAS tagged (advisory)
            </span>
          )}
        </div>
      </div>

      {loadError && (
        <div className="font-mono text-[11px] p-2 tactical-border"
             style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
          Load failed: {loadError}
        </div>
      )}

      {/* Honest empty/unconfigured states */}
      {survey && !configured && (
        <div data-testid="wifi-env-unconfigured" className="font-mono text-xs p-4 tactical-border" style={{ color: "var(--text-muted)" }}>
          Kismet is not configured on the backend (KISMET_URL unset). Set KISMET_URL / KISMET_APIKEY
          in the backend environment to enable the Wi-Fi environment survey.
        </div>
      )}
      {survey && configured && !available && (
        <div data-testid="wifi-env-unavailable" className="font-mono text-xs p-4 tactical-border" style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}>
          Kismet reachable status: {survey.status || "no data available"}. No AP survey to show yet.
        </div>
      )}

      {/* Survey table */}
      {available && (
        <div className="tactical-border overflow-x-auto" style={{ background: "var(--bg-surface)" }}>
          <table className="w-full border-collapse font-mono text-xs" data-testid="wifi-env-table">
            <thead>
              <tr className="tactical-border-b" style={{ background: "var(--bg-base)" }}>
                {COLUMNS.map((c) => {
                  const active = c.key === sortKey;
                  return (
                    <th
                      key={c.key}
                      onClick={c.sortable ? () => toggleSort(c.key) : undefined}
                      className={`px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap ${c.sortable ? "cursor-pointer select-none" : ""}`}
                      style={{ color: active ? "var(--accent-info)" : "var(--text-muted)" }}
                    >
                      <span className="inline-flex items-center gap-1">
                        {c.label}
                        {active && (sortDir === "asc"
                          ? <ArrowUp size={11} strokeWidth={2} />
                          : <ArrowDown size={11} strokeWidth={2} />)}
                      </span>
                    </th>
                  );
                })}
                <th className="px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
                  TAG
                </th>
                <th className="px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
                  PROTECT
                </th>
                <th className="px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
                  ENGAGE
                </th>
                <th className="px-3 py-2 text-left uppercase tracking-widest text-[10px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>
                  JAM CHANNEL (AREA)
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((ap, i) => {
                const uas = isPossibleUas(ap);
                const protectedAp = isApProtected(ap);
                // COMMANDER NO-STRIKE OVERRIDE (row gate): a CIVILIAN_INFRASTRUCTURE
                // match is a HARD FLOOR — no engage/override control, just a badge
                // (reclassify the registry entry first). A NEUTRAL/FRIENDLY (or
                // unmatched) row is engageable for a commander: a drone-OUI/SSID row
                // via the normal designate, a non-drone row via commander_override
                // (honest non-drone candidate; fire still needs the fire-time
                // no-strike override token).
                const matchedEntry = enabledNoStrikeEntries.find((e) => entryMatchesAp(e, ap)) || null;
                const civilianProtected = matchedEntry?.category === "CIVILIAN_INFRASTRUCTURE";
                const droneDesignable = canDesignateUas(ap, { isCommander, isProtected: false });
                const overrideDesignable =
                  isCommander && !civilianProtected && !droneDesignable && hasConcreteBssid(ap.bssid);
                // JAM CHANNEL (AREA): a SEPARATE, distinct affordance from the
                // per-BSSID ENGAGE controls above -- never a deauth, and NOT
                // gated by the no-strike registry (an area barrage can't spare
                // one BSSID). Same commander role gate as ENGAGE; only needs a
                // derivable center frequency.
                const jamFreqMhz = apChannelToMhz(ap);
                return (
                  <tr key={ap.bssid || i} className="tactical-border-b" data-testid={`wifi-env-row-${ap.bssid}`}>
                    <td className="px-3 py-2" style={{ color: "var(--text-primary)" }}>
                      {ap.ssid || <span className="text-slate-600 italic">(hidden)</span>}
                    </td>
                    <td className="px-3 py-2 text-slate-400">{ap.bssid}</td>
                    <td className="px-3 py-2 text-slate-300">{ap.channel || "—"}</td>
                    <td className="px-3 py-2" style={{ color: rssiColor(ap.rssi_dbm) }}>
                      {ap.rssi_dbm != null ? `${ap.rssi_dbm} dBm` : "—"}
                    </td>
                    <td className="px-3 py-2 text-slate-300">{ap.vendor || "unknown"}</td>
                    <td className="px-3 py-2 text-slate-300">
                      {encryptionLabel(ap)}
                      <span className="text-slate-500"> · PMF {pmfLabel(ap)}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-300">{ap.client_count != null ? ap.client_count : "—"}</td>
                    <td className="px-3 py-2 text-slate-400">{lastSeenLabel(ap)}</td>
                    <td className="px-3 py-2">
                      {uas ? (
                        <span
                          className="px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap"
                          title={ap.possible_uas_reason || "matches a drone OUI/SSID pattern — advisory only, non-actionable"}
                          style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
                        >
                          ⚑ Possible UAS
                        </span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {protectedAp ? (
                        <span
                          data-testid={`wifi-env-protected-${ap.bssid}`}
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap"
                          title="Already on the no-strike civilian-protection registry (matched by BSSID/OUI)."
                          style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}
                        >
                          <ShieldCheck size={10} strokeWidth={2} /> Protected
                        </span>
                      ) : (
                        <button
                          type="button"
                          data-testid={`wifi-env-add-no-strike-${ap.bssid}`}
                          onClick={() => setAddingAp(ap)}
                          disabled={!isCommander}
                          title={isCommander
                            ? "Add this AP to the no-strike civilian-protection registry (PROTECT only — never an engage/targeting action)."
                            : "Commander role required"}
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap hover-surface transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                          style={{ color: "var(--text-muted)" }}
                        >
                          <ShieldPlus size={10} strokeWidth={2} /> Add to no-strike
                        </button>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {civilianProtected ? (
                        <span
                          data-testid={`wifi-env-protected-badge-${ap.bssid}`}
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap"
                          title="CIVILIAN_INFRASTRUCTURE no-strike match — hard floor. No override exists: reclassify the registry entry (commander declassify) before this can ever be engaged."
                          style={{ color: "var(--accent-success)", borderColor: "var(--accent-success)" }}
                        >
                          <ShieldCheck size={10} strokeWidth={2} /> Protected — reclassify to engage
                        </span>
                      ) : droneDesignable ? (
                        <button
                          type="button"
                          data-testid={`wifi-env-designate-${ap.bssid}`}
                          onClick={() => setDesignatingAp(ap)}
                          title="Designate this possible-UAS AP as a SUSPECTED-UAS contact and route to the governed per-BSSID Wi-Fi-defeat flow (candidate, not identification; arm/fire happens on the next screen)."
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap hover-surface transition-colors"
                          style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
                        >
                          <Crosshair size={10} strokeWidth={2} /> Engage as suspected UAS
                        </button>
                      ) : overrideDesignable ? (
                        <button
                          type="button"
                          data-testid={`wifi-env-designate-override-${ap.bssid}`}
                          onClick={() => setDesignatingAp({ ...ap, __commanderOverride: true })}
                          title="COMMANDER OVERRIDE: this AP is not drone-OUI/SSID tagged. Designate it as a candidate (NOT an identification) and route to the governed per-BSSID Wi-Fi-defeat flow. Fire still requires the SafetyGate and, for a NEUTRAL/FRIENDLY match, a fire-time no-strike override token."
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap hover-surface transition-colors"
                          style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
                        >
                          <Crosshair size={10} strokeWidth={2} /> Engage (commander override)
                        </button>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {isCommander && jamFreqMhz != null ? (
                        <button
                          type="button"
                          data-testid={`wifi-env-jam-area-${ap.bssid}`}
                          onClick={() => setJammingAp(ap)}
                          title={`AREA DENIAL: jam channel ${ap.channel || "—"} (${jamFreqMhz} MHz) entirely — denies ALL devices in RF range on this channel, not just this AP (never a per-BSSID deauth). Deep-links to the Jamming page; the arm/confirm/fire happens there.`}
                          className="inline-flex items-center gap-1 px-2 py-0.5 tactical-border text-[9px] font-bold uppercase tracking-widest whitespace-nowrap hover-surface transition-colors"
                          style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
                        >
                          <Radio size={10} strokeWidth={2} /> Jam Channel (Area)
                        </button>
                      ) : (
                        <span
                          className="text-slate-600"
                          title={!isCommander ? "Commander role required" : "No channel/frequency data for this row"}
                        >
                          —
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={COLUMNS.length + 4} className="px-3 py-6 text-center text-slate-600">
                    {query ? "No APs match the filter." : "No access points in range."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Footer honesty note */}
      <div className="font-mono text-[10px] p-3 tactical-border" style={{ color: "var(--text-muted)" }}>
        Passive RX-only survey via Kismet (monitor-mode NIC). Vendor is Kismet's OUI-derived guess;
        SSID / OUI are spoofable, so a “possible UAS” tag is a candidate hint, never an identification.
        This panel transmits nothing itself. A commander may (a) add a row to the no-strike
        civilian-protection registry (PROTECT), (b) designate a possible-UAS row as a suspected-UAS
        contact that deep-links into the governed, per-BSSID Wi-Fi-defeat flow — where the SafetyGate
        arm/confirm and every fire-time gate still apply, or (c) deep-link a row's channel into the
        Jamming page (JAM CHANNEL (AREA)) — a channel-wide AREA-DENIAL barrage, NOT a per-BSSID strike,
        that denies the whole channel to every device in RF range including civilian/own co-channel
        traffic; it is not scoped by the no-strike registry, and the arm/confirm/fire (with its own
        AREA-DENIAL caution) happens entirely on that next page. Designating creates a governed contact
        only; it never arms, transmits, or fires, and only a drone-OUI/SSID, non-civilian, non-broadcast
        row is eligible.
      </div>

      {addingAp && (
        <AddNoStrikeModal
          ap={addingAp}
          onClose={() => setAddingAp(null)}
          onAdded={(entry) => {
            setNoStrikeEntries((es) => [entry, ...es]);
            setAddingAp(null);
          }}
        />
      )}

      {designatingAp && (
        <DesignateUasModal
          ap={designatingAp}
          justificationRequired={designateJustificationRequired(
            designatingAp?.__commanderOverride === true,
            survey?.commander_override_justification_required === true,
          )}
          onClose={() => setDesignatingAp(null)}
          onConfirm={(justification) => doDesignate(designatingAp, justification)}
        />
      )}

      {jammingAp && (
        <JamChannelAreaModal
          ap={jammingAp}
          freqMhz={apChannelToMhz(jammingAp)}
          civilianProtected={enabledNoStrikeEntries.some(
            (e) => entryMatchesAp(e, jammingAp) && e.category === "CIVILIAN_INFRASTRUCTURE"
          )}
          onClose={() => setJammingAp(null)}
          onConfirm={() => {
            const freqMhz = apChannelToMhz(jammingAp);
            setJammingAp(null);
            navigate(`/jamming?freq=${freqMhz}`);
          }}
        />
      )}
    </div>
  );
}

function rssiColor(rssi) {
  if (rssi == null) return "var(--text-muted)";
  if (rssi >= -55) return "var(--accent-success)";
  if (rssi >= -70) return "var(--accent-info)";
  if (rssi >= -80) return "var(--accent-warning)";
  return "var(--text-muted)";
}
