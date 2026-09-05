import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { handleEngageBlock } from "@/lib/engageFix";
import {
  Wifi, AlertTriangle, ShieldAlert, ShieldCheck, ShieldOff, Siren,
  Target as TargetIcon, Crosshair, Infinity as InfinityIcon,
} from "lucide-react";
import SafetyGate from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import EmergencyAbort from "@/components/EmergencyAbort";
import { useAuth } from "@/context/AuthContext";

// =============================================================================
// WI-FI DEFEAT (Parrot/Tello) — Phase P5 governed frontend effector surface.
// Clone of the Jamming.jsx / Takeover.jsx governed-effector pattern: SAME
// arm -> confirm -> SafetyGate(ARM->CONFIRM) -> deploy spine, SAME
// RangeAuthorizationControl + EmergencyAbort + IFF/fratricide interlock. No
// new transmit path is invented here — this page only wires the EXISTING
// gate components onto the backend's POST /api/payloads/wifi-defeat.
//
// TWO mechanisms, kept HONEST per .omc/plans/wifi-defeat-active-cuas-plan.md:
//   deauth       -> effect=wifi_deauth   -> LINK-DROP (forces the drone's OWN
//                    link-loss failsafe; NOT command takeover; no-op vs PMF;
//                    defeated by MAC-randomization).
//   arsdk_*/tello_* -> effect=arsdk_inject -> a targeted UNAUTHENTICATED UDP
//                    land/emergency command against a cooperative UNENCRYPTED
//                    Parrot ARSDK3 / Ryze-DJI Tello airframe ONLY.
// Never sold as "takeover" — see the honesty banner + checklist below.
// =============================================================================

const MODE_META = {
  deauth: {
    label: "802.11 DEAUTH",
    effect: "wifi_deauth",
    honest:
      "802.11 DEAUTH — link-drop, forces the drone's link-loss failsafe (RTH/hover/land, " +
      "not operator-controlled). NO-OP vs 802.11w/PMF; MAC-randomization defeats targeting. " +
      "NOT a takeover.",
  },
  arsdk_land: {
    label: "PARROT ARSDK — LAND",
    effect: "arsdk_inject",
    honest:
      "PARROT ARSDK land/emergency — unauthenticated UDP command; UNENCRYPTED Parrot only; " +
      "model/firmware-dependent.",
  },
  arsdk_emergency: {
    label: "PARROT ARSDK — EMERGENCY",
    effect: "arsdk_inject",
    honest:
      "PARROT ARSDK land/emergency — unauthenticated UDP command; UNENCRYPTED Parrot only; " +
      "model/firmware-dependent.",
  },
  tello_land: {
    label: "TELLO — LAND",
    effect: "arsdk_inject",
    honest: "TELLO land/emergency — unauthenticated UDP command; Ryze/DJI Tello only.",
  },
  tello_emergency: {
    label: "TELLO — EMERGENCY",
    effect: "arsdk_inject",
    honest: "TELLO land/emergency — unauthenticated UDP command; Ryze/DJI Tello only.",
  },
};

const MODE_ORDER = ["deauth", "arsdk_land", "arsdk_emergency", "tello_land", "tello_emergency"];

// Honest SafetyGate checklist — mirrors JAM_CHECKS / MAVLINK_SDR_INJECT_CHECKS
// in components/SafetyGate.jsx, kept local here since this effect's checklist
// is specific to the two Wi-Fi-defeat mechanisms and their honesty gates.
const WIFI_DEFEAT_CHECKS = [
  "Range Authorization is armed for this effect via the GUI toggle (see control below) for " +
    "this session — arming a different effect does NOT arm this.",
  "Target softAP BSSID confirmed correct and concrete (not a broadcast BSSID) — the backend " +
    "fail-closed refuses a targeted defeat without one.",
  "DEAUTH ONLY: target is not known to run 802.11w/PMF (a no-op there) — reviewed the honesty banner.",
  "ARSDK/TELLO INJECT ONLY: target is confirmed an unencrypted Parrot ARSDK3 or Ryze/DJI Tello " +
    "airframe — this does NOT work against any other or encrypted link.",
  "Physical safety perimeter established; personnel clear of the TX antenna.",
  "This is a REAL RF/UDP transmission (not a preview) and cannot be recalled once sent.",
];

// Loose, UX-only candidate filter for the fallback dropdown (the real gate is
// the backend's fail-closed BSSID/identity checks at fire time) — a Wi-Fi
// drone candidate carries a resolvable softAP BSSID/SSID or a Wi-Fi-family hint.
const WIFI_MARKERS = ["wifi", "parrot", "anafi", "bebop", "arsdk", "ardrone", "tello", "ryze"];
const isWifiDroneCandidate = (d) => {
  if (!d) return false;
  if (d.bssid || d.softap_bssid || d.target_bssid || d.ssid) return true;
  const hay = [d.control_link_family, d.model, d.callsign, d.protocol, d.threat_library_id]
    .filter(Boolean).join(" ").toLowerCase();
  return WIFI_MARKERS.some((m) => hay.includes(m));
};

// Same IFF-friendly test used by Takeover.jsx.
const isFriendly = (d) =>
  !!d && (d.iff_verified === true || d.threat_level === "FRIENDLY (IFF verified)");

// Recognise the backend's fratricide-interlock 403 (identical regex to Takeover.jsx).
const isFratricideRefusal = (e) =>
  e?.response?.status === 403 &&
  /FRATRICIDE|CONFIRMED-FRIENDLY|friendly-fire ack/i.test(formatApiError(e) || "");

// Status feed staleness tracking — same pattern as Jamming.jsx / Takeover.jsx.
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

const STATUS_STYLE = {
  AWAITING_ACK:         { color: "var(--accent-warning)", label: "◐ AWAITING ACK", blink: true },
  WIFI_DEFEAT_ACTIVE:   { color: "var(--accent-critical)", label: "▮▮ TRANSMITTING", blink: true },
  WIFI_DEFEAT_COMPLETE: { color: "var(--accent-success)", label: "✓ COMPLETE", blink: false },
  WIFI_DEFEAT_STOPPED:  { color: "var(--accent-warning)", label: "■ STOPPED (ABORT)", blink: false },
  TX_FAILED:            { color: "var(--accent-critical)", label: "✕ TX FAILED", blink: false },
  TX_TIMEOUT:           { color: "var(--accent-critical)", label: "✕ TX TIMEOUT", blink: false },
};

export default function WifiDefeat() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  // ---- Deep-link target (same pattern as Takeover.jsx) -----------------------
  const [searchParams] = useSearchParams();
  const deepLinkedId = searchParams.get("contact");
  const appliedDeepLinkIdRef = useRef(null);

  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState("deauth");
  const [continuous, setContinuous] = useState(true);
  const [count, setCount] = useState(20);
  const [clientMac, setClientMac] = useState("");
  const [gateOpen, setGateOpen] = useState(false);
  const [fratricide, setFratricide] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const load = async () => {
    try {
      const { data } = await api.get("/detections");
      const all = data || [];
      setDets(all);
      const active = all.filter((x) => x.status === "ACTIVE" && isWifiDroneCandidate(x));
      setTarget((prev) => {
        if (deepLinkedId && appliedDeepLinkIdRef.current !== deepLinkedId &&
            all.some((x) => x.id === deepLinkedId)) {
          appliedDeepLinkIdRef.current = deepLinkedId;
          return deepLinkedId;
        }
        if (prev && all.some((x) => x.id === prev)) return prev;
        return active.length ? active[0].id : "";
      });
    } catch (e) { toast.error("Load failed", { description: formatApiError(e) }); }
  };
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []); // eslint-disable-line
  useEffect(() => { if (deepLinkedId) load(); }, [deepLinkedId]); // eslint-disable-line

  const loadStatus = async () => {
    try {
      const { data } = await api.get("/wifi-defeat/status");
      setSessions(data.sessions || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch {
      setConsecutiveFailures((n) => n + 1);
    }
  };
  useEffect(() => { loadStatus(); const id = setInterval(loadStatus, POLL_INTERVAL_MS); return () => clearInterval(id); }, []);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  const activeSession = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "WIFI_DEFEAT_ACTIVE");

  const selectedDet = dets.find((d) => d.id === target);
  const friendlySelected = isFriendly(selectedDet);
  const isDeepLinked = !!deepLinkedId && dets.some((d) => d.id === deepLinkedId);
  const wifiActiveDets = dets.filter((x) => x.status === "ACTIVE" && isWifiDroneCandidate(x));

  const meta = MODE_META[mode];
  const effect = meta.effect;
  const canArm = !activeSession && !submitting && !!target;

  const toggleAuthorize = async () => {
    if (!selectedDet) return;
    if (friendlySelected) {
      toast.error("Confirmed FRIENDLY — cannot authorize as target", {
        description: "Use the deliberate commander fratricide override to engage a confirmed friendly.",
      });
      return;
    }
    setAuthorizing(true);
    try {
      const nextAuthorized = !selectedDet.authorized_target;
      await api.post(`/detections/${selectedDet.id}/authorize-target`, { authorized: nextAuthorized });
      toast[nextAuthorized ? "success" : "info"](
        `${selectedDet.callsign} ${nextAuthorized ? "AUTHORIZED" : "DE-AUTHORIZED"} as target`
      );
      await load();
    } catch (e) {
      toast.error("Authorize-target failed", { description: formatApiError(e) });
    } finally {
      setAuthorizing(false);
    }
  };

  const openGate = () => {
    // Same posture as Takeover.jsx's openSdrGate: a confirmed friendly never
    // takes the routine path — it opens the gate already in fratricide mode.
    setFratricide(friendlySelected);
    setGateOpen(true);
  };

  const fireWifiDefeat = async () => {
    if (!target) { toast.error("No target selected"); return; }
    setSubmitting(true);
    try {
      // Step 1: arm token bound to this effect (wifi_deauth | arsdk_inject) AND
      // this exact target (F3 — backend rejects a token spent on a different
      // effect/target).
      const { data: arm } = await api.post("/arm", { effect, target_detection_id: target });
      // Step 2: mint the wifi-defeat confirmation token RIGHT NOW — this call
      // only happens because the SafetyGate ARM->CONFIRM two-step just
      // completed. NOT interchangeable with jam/gnss/deploy/sdr-inject tokens.
      const { data: confirm } = await api.post("/wifi-defeat/confirm", {});
      // Step 3 (fratricide only): the DELIBERATE, single-use, target-bound
      // commander friendly-fire ack — the ONLY thing that can license a
      // deauth/inject against a confirmed friendly. Minted here, consumed
      // once by the backend. Reuses the exact endpoint Takeover/Payloads use.
      let iffAck;
      if (friendlySelected) {
        const { data: ack } = await api.post(`/detections/${target}/friendly-fire-ack`);
        iffAck = ack.iff_friendly_fire_ack;
      }
      // Step 4: the actual wifi-defeat request.
      const { data } = await api.post("/payloads/wifi-defeat", {
        target_detection_id: target,
        mode,
        ...(mode === "deauth" && !continuous ? { count: Number(count) } : {}),
        ...(mode === "deauth" && clientMac.trim() ? { client_mac: clientMac.trim() } : {}),
        arm_token: arm.arm_token,
        wifi_defeat_confirm_token: confirm.wifi_defeat_confirm_token,
        ...(iffAck ? { iff_friendly_fire_ack: iffAck } : {}),
      });
      if (data.tx_bridge_subscribed === false) {
        if (!handleEngageBlock({ response: data }, { isCommander, onFixed: loadStatus })) {
          toast.error(`${meta.label}: NOT TRANSMITTED`, {
            description: `Nothing radiated. Request ${data.request_id?.slice(0, 8)} will TX_TIMEOUT.`,
          });
        }
      } else {
        toast.info(`${meta.label} REQUESTED — awaiting bridge ACK`, {
          description: `${data.target_bssid || "?"}${data.channel != null ? ` ch ${data.channel}` : ""} · ` +
            `${data.continuous ? "CONTINUOUS" : "bounded"} · req ${data.request_id?.slice(0, 8)}`,
        });
      }
      loadStatus();
    } catch (e) {
      if (!fratricide && isFratricideRefusal(e)) {
        toast.error("FRATRICIDE INTERLOCK — routine request refused", {
          description: "Target is IFF-CONFIRMED FRIENDLY. Engage only via the deliberate commander friendly-fire override.",
        });
        setFratricide(true);
        setGateOpen(true);
        return;
      }
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: loadStatus })) return;
      toast.error("Wi-Fi defeat request failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  const iffLabel = (d) => {
    if (!d) return "—";
    if (isFriendly(d)) return "IFF-CONFIRMED FRIENDLY";
    if (d.authorized_target) return "AUTHORIZED TARGET";
    return d.threat_level || "UNVERIFIED";
  };
  const bssidOf = (d) => (d && (d.bssid || d.softap_bssid || d.target_bssid)) || "—";

  return (
    <div data-testid="page-wifi-defeat" className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Wifi size={12} className="inline mr-2" strokeWidth={1.5} /> Active Wi-Fi Defeat
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            WI-FI DEFEAT
          </h1>
        </div>
      </div>

      {/* Always-visible EMERGENCY ABORT — never buried. */}
      <div
        data-testid="wifi-defeat-emergency-abort-panel"
        className="p-4 flex flex-col sm:flex-row sm:items-center justify-between gap-3 pulse-crit"
        style={{ background: "var(--surface-critical)", border: "2px solid var(--accent-critical)" }}
      >
        <div className="font-mono leading-relaxed">
          <div className="text-sm font-black uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
            <Siren size={16} className="inline mr-2" strokeWidth={2} /> EMERGENCY ABORT / STAND DOWN
          </div>
          <div className="text-[11px] text-slate-300 mt-1">
            Halts every deauth/inject transmission immediately — continuous deauth in progress included.
          </div>
        </div>
        <EmergencyAbort />
      </div>

      {/* Honesty banner — reflects the CURRENTLY SELECTED mode. Never overclaims
          either mechanism as "takeover". */}
      <div
        data-testid="wifi-defeat-honesty-banner"
        className="tactical-border p-4 flex items-start gap-3"
        style={{ background: "var(--surface-critical)" }}
      >
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>HONEST CAPABILITY:</span>{" "}
          {meta.honest} Requires commander role, a fresh arm token, a wifi-defeat confirmation token
          minted at the instant you complete the checklist below, the IFF fratricide interlock, AND a
          live Range Authorization lease for effect={effect} (armed via the control below) — all
          independently, every time.
        </div>
      </div>

      <RangeAuthorizationControl effect={effect} label={`WI-FI DEFEAT (${meta.label})`} />

      {/* ---- TARGET ---------------------------------------------------------- */}
      <section className="space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 flex items-center gap-2">
          <TargetIcon size={12} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} /> Target
        </div>

        {isDeepLinked && selectedDet ? (
          <div
            data-testid="wifi-defeat-target-chip"
            className="tactical-border p-4 flex flex-wrap items-center gap-x-6 gap-y-2"
            style={{ background: "var(--bg-surface)", borderColor: friendlySelected ? "var(--accent-critical)" : "var(--border-col)" }}
          >
            <div className="flex items-center gap-2">
              <Crosshair size={16} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
              <span className="font-heading font-black text-xl uppercase tracking-tight">{selectedDet.callsign}</span>
            </div>
            <div className="font-mono text-[11px] text-slate-400">MODEL <span className="text-slate-200">{selectedDet.model || "?"}</span></div>
            <div className="font-mono text-[11px] text-slate-400">SSID <span className="text-slate-200">{selectedDet.ssid || "?"}</span></div>
            <div className="font-mono text-[11px] text-slate-400">BSSID <span className="text-slate-200">{bssidOf(selectedDet)}</span></div>
            <div className="font-mono text-[11px]">
              IFF{" "}
              <span className="font-bold" style={{ color: friendlySelected ? "var(--accent-critical)" : "var(--text-primary)" }}>
                {iffLabel(selectedDet)}
              </span>
            </div>
            <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600 ml-auto">read-only · from cue</span>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <select
              data-testid="wifi-defeat-target-select"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className="tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
            >
              {wifiActiveDets.length === 0 && <option value="">— NO ACTIVE WI-FI DRONE TARGETS —</option>}
              {wifiActiveDets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.callsign} · {d.model || "?"} · {bssidOf(d)}
                  {isFriendly(d) ? " ⚠ IFF-CONFIRMED FRIENDLY" : d.authorized_target ? "" : " (NOT AUTHORIZED)"}
                </option>
              ))}
            </select>
          </div>
        )}

        {selectedDet && !friendlySelected && (
          <button
            data-testid="wifi-defeat-authorize-target-toggle"
            onClick={toggleAuthorize}
            disabled={authorizing}
            className={`inline-flex items-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest transition-colors disabled:opacity-40 ${
              selectedDet.authorized_target
                ? "text-[var(--accent-success)] border-[var(--accent-success)] hover:bg-[var(--accent-success)] hover:text-black"
                : "text-[var(--accent-critical)] border-[var(--accent-critical)] hover:bg-[var(--accent-critical)] hover:text-black"
            }`}
          >
            {selectedDet.authorized_target ? <ShieldCheck size={14} strokeWidth={1.5} /> : <ShieldOff size={14} strokeWidth={1.5} />}
            {selectedDet.authorized_target ? "TARGET AUTHORIZED" : "AUTHORIZE TARGET"}
          </button>
        )}
        {selectedDet && friendlySelected && (
          <span
            data-testid="wifi-defeat-friendly-target-indicator"
            className="inline-flex items-center gap-2 px-3 py-2 border-2 font-mono text-[10px] font-bold uppercase tracking-widest"
            style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
            title="IFF-confirmed friendly — engaging is fratricide and requires the deliberate commander override."
          >
            <ShieldAlert size={14} strokeWidth={1.75} />
            IFF-CONFIRMED FRIENDLY
          </span>
        )}

        {friendlySelected && (
          <div
            data-testid="wifi-defeat-fratricide-banner"
            className="p-4 flex items-start gap-3 border-2"
            style={{ borderColor: "var(--accent-critical)", background: "color-mix(in srgb, var(--accent-critical) 16%, var(--bg-surface))" }}
          >
            <ShieldAlert size={22} strokeWidth={1.75} style={{ color: "var(--accent-critical)", flexShrink: 0 }} />
            <div className="font-mono text-xs" style={{ color: "var(--text-primary)" }}>
              <div className="font-heading font-black text-base uppercase tracking-tight" style={{ color: "var(--accent-critical)" }}>
                ⚠ TARGET IFF-CONFIRMED FRIENDLY — ENGAGING WILL BE FRATRICIDE
              </div>
              <div className="mt-1 text-slate-300">
                <span className="font-bold" style={{ color: "var(--text-primary)" }}>{selectedDet?.callsign}</span>{" "}
                has replied to IFF interrogation. A softAP BSSID belonging to a registered/friendly
                asset is NEVER deauthed/injected without the deliberate commander friendly-fire ack.{" "}
                {isCommander
                  ? "Arming below opens the deliberate, single-use commander friendly-fire override."
                  : "Only a commander may deliberately override this; your role cannot engage a confirmed friendly."}
              </div>
            </div>
          </div>
        )}
      </section>

      {/* ---- MODE -------------------------------------------------------------- */}
      <section className="space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Mode</div>
        <div data-testid="wifi-defeat-mode-group" className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {MODE_ORDER.map((m) => (
            <label
              key={m}
              data-testid={`wifi-defeat-mode-option-${m}`}
              className="flex items-start gap-3 p-3 tactical-border cursor-pointer hover-surface"
              style={mode === m ? { borderColor: "var(--accent-critical)", background: "color-mix(in srgb, var(--accent-critical) 8%, var(--bg-surface))" } : undefined}
            >
              <input
                type="radio"
                name="wifi-defeat-mode"
                value={m}
                checked={mode === m}
                onChange={() => setMode(m)}
                className="mt-1"
                style={{ accentColor: "var(--accent-critical)" }}
              />
              <div>
                <div className="font-mono text-xs font-bold uppercase tracking-widest">{MODE_META[m].label}</div>
                <div className="font-mono text-[10px] text-slate-400 mt-1 leading-relaxed">{MODE_META[m].honest}</div>
              </div>
            </label>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="tactical-border p-4 space-y-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Burst Parameters</div>

          {mode === "deauth" ? (
            <>
              <label className="flex items-start gap-2 cursor-pointer">
                <input
                  data-testid="wifi-defeat-continuous-toggle"
                  type="checkbox"
                  checked={continuous}
                  onChange={(e) => setContinuous(e.target.checked)}
                  className="mt-1"
                />
                <span className="font-mono text-[10px] text-slate-400 leading-relaxed">
                  <InfinityIcon size={11} className="inline mr-1" strokeWidth={1.5} />
                  <span className="uppercase tracking-widest text-slate-300">Continuous</span>
                  {" — "}deauth until you STAND DOWN / EMERGENCY ABORT or the lease expires (no burst cap).
                </span>
              </label>

              {!continuous && (
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                    Frame count (bounded burst)
                  </span>
                  <input
                    data-testid="wifi-defeat-count-input"
                    type="number" min={1} max={100000} step={1}
                    value={count}
                    onChange={(e) => setCount(Math.max(1, Math.min(100000, Number(e.target.value) || 1)))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
              )}

              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                  Client MAC (optional — defaults to broadcast client at the bridge)
                </span>
                <input
                  data-testid="wifi-defeat-client-mac-input"
                  type="text"
                  placeholder="AA:BB:CC:DD:EE:FF"
                  value={clientMac}
                  onChange={(e) => setClientMac(e.target.value)}
                  className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                />
              </label>
            </>
          ) : (
            <div
              data-testid="wifi-defeat-inject-note"
              className="tactical-border p-3 font-mono text-[10px] text-slate-400 leading-relaxed"
            >
              Sends a single unauthenticated UDP {mode.endsWith("land") ? "LAND" : "EMERGENCY"} command to
              the target's open softAP — no burst/continuous parameters apply to this mode.
            </div>
          )}

          <button
            data-testid="wifi-defeat-arm-button"
            disabled={!canArm}
            onClick={openGate}
            className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
              !canArm ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed" : "hover-accent-critical"
            }`}
            style={!canArm ? undefined : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
          >
            {friendlySelected ? "ARM WI-FI DEFEAT (FRIENDLY — OVERRIDE)" : "ARM WI-FI DEFEAT"}
          </button>
        </div>

        <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-3">
            Wi-Fi Defeat Sessions
          </div>
          {statusUnconfirmed && (
            <div
              data-testid="wifi-defeat-status-unconfirmed-banner"
              className="mb-3 flex items-center gap-2 px-3 py-2 pulse-crit"
              style={{ background: "#FF9500", color: "black" }}
            >
              <ShieldAlert size={14} strokeWidth={2} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                STATUS UNCONFIRMED — wifi-defeat status feed stale, session states below may be out of date
              </span>
            </div>
          )}
          {sessions.length === 0 && (
            <div className="font-mono text-xs text-slate-600 text-center py-8">
              no wifi-defeat sessions yet<span className="term-caret" />
            </div>
          )}
          <div className="space-y-2">
            {sessions.map((s) => {
              const st = STATUS_STYLE[s.status] || { color: "var(--text-muted)", label: s.status, blink: false };
              return (
                <div key={s.request_id} data-testid={`wifi-defeat-session-${s.request_id}`}
                     className="flex items-center justify-between p-3 tactical-border">
                  <div className="font-mono text-[11px] text-slate-300">
                    {s.mode?.toUpperCase()} · {s.target_bssid || "?"}
                    {s.channel != null ? ` · ch ${s.channel}` : ""}
                    {" · "}{s.continuous ? "CONTINUOUS" : s.count != null ? `×${s.count}` : "single-shot"}
                    <div className="text-slate-500 text-[10px]">{s.request_id?.slice(0, 8)}</div>
                  </div>
                  <span
                    data-testid={`wifi-defeat-status-${s.request_id}`}
                    className={`px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest ${st.blink ? "blink" : ""}`}
                    style={{ color: st.color, borderColor: st.color }}
                  >
                    {st.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <SafetyGate
        open={gateOpen}
        payloadName={`WI-FI DEFEAT — ${meta.label}`}
        severity="CRITICAL"
        checks={WIFI_DEFEAT_CHECKS}
        actionLabel="TRANSMIT"
        irreversibleNote="a real RF/UDP transmission — it cannot be recalled once sent"
        fratricide={fratricide}
        isCommander={isCommander}
        friendlyCallsign={fratricide ? selectedDet?.callsign : undefined}
        onClose={() => { setGateOpen(false); setFratricide(false); }}
        onConfirm={() => {
          setGateOpen(false);
          setFratricide(false);
          fireWifiDefeat();
        }}
      />
    </div>
  );
}
