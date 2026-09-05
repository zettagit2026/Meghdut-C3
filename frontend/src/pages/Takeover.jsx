import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError, wsUrl } from "@/lib/api";
import { toast } from "sonner";
import { handleEngageBlock } from "@/lib/engageFix";
import {
  Bomb, AlertTriangle, Target as TargetIcon, ShieldCheck, ShieldOff, ShieldAlert,
  RadioTower, Radio, Zap, Copy, Crosshair, Siren, ChevronRight,
} from "lucide-react";
import SafetyGate, { SAFETY_GATED, MAVLINK_SDR_INJECT_CHECKS } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import EmergencyAbort from "@/components/EmergencyAbort";
import { THREAT_COLOR } from "@/lib/threatLevels";
import { useAuth } from "@/context/AuthContext";

// =============================================================================
// MERGED "MAVLINK TAKEOVER" ENGAGEMENT PAGE (IA restructure Merge B / Phase P-B)
//
// This page unifies the UI SHELL of two former pages —
//   • Payloads.jsx           → SiK-paired link bearer  (POST /payloads/deploy)
//   • SdrMavlinkInject.jsx    → No-pairing SDR bearer   (POST /payloads/mavlink-sdr-inject)
// — behind ONE shared TARGET + EFFECT selection, chosen by a BEARER radio.
//
// SAFETY-CRITICAL INVARIANT (see .omc/plans/console-ia-restructure.md §Merge B):
// the two backend fire paths and their token/gate flows stay INDEPENDENT and
// byte-for-byte INTACT. Nothing is merged, shared, weakened, or bypassed across
// bearers. Each bearer carries its OWN RangeAuthorizationControl effect string,
// its OWN arm token, its OWN bearer-correct confirm token, its OWN SafetyGate
// checklist, plus the shared IFF/fratricide interlock. The merge is UI-only.
//
// FPV Video Capture is intentionally EXCLUDED here (it is RX recon, relocated
// off the weapon surface in a later phase) — see the plan §Merge B.6.
// =============================================================================

// A contact is a CONFIRMED FRIENDLY exactly when IFF interrogation has replied
// and the backend has stamped it so — mirrors the server-side test in
// backend/server.py (_enforce_fire_time_iff / mint_friendly_fire_ack). Firing
// on one is FRATRICIDE and is refused (403) on the routine authorize/deploy
// path; the only licensed path is the deliberate commander friendly-fire ack.
const isFriendly = (d) =>
  !!d && (d.iff_verified === true || d.threat_level === "FRIENDLY (IFF verified)");

// Recognise the backend's fratricide-interlock 403 so a routine engagement that
// races into a freshly-friendly target surfaces the explicit override path,
// not a raw error string. Identical in both source pages.
const isFratricideRefusal = (e) =>
  e?.response?.status === 403 &&
  /FRATRICIDE|CONFIRMED-FRIENDLY|friendly-fire ack/i.test(formatApiError(e) || "");

const CAT_LABEL = {
  kinetic: "KINETIC",
  logical: "LOGICAL",
  protocol: "PROTOCOL",
  denial: "DENIAL",
};

// --- SDR bearer constants (verbatim from SdrMavlinkInject.jsx) ---------------

// Mirrors backend MavlinkSdrInjectBody command pattern + sdr_mavlink_inject.py
// COMMAND_BUILDERS. Kept in sync by convention (bytes come from mavlink_codec).
const COMMANDS = [
  { value: "force_land", label: "FORCE LAND (NAV_LAND)" },
  { value: "rth", label: "RETURN-TO-HOME (RTL)" },
  { value: "disarm", label: "DISARM (COMPONENT_ARM_DISARM)" },
  { value: "flight_termination", label: "FLIGHT TERMINATION" },
  { value: "maneuver_takeover", label: "MANEUVER TAKEOVER" },
];

// Operator-controlled repeat (commander directive: no artificial cap). The
// backend bounds the FINITE one-shot case at 10_000 purely as a memory-DoS
// guard (each repeat expands into an in-memory IQ buffer); continuous=True is
// the truly uncapped path. This is that memory-DoS bound, NOT a timing cap.
const MAX_REPEAT = 10000;

// Payload FEC options — mirrors sdr_mavlink_inject.FEC_CHOICES / backend
// MavlinkSdrInjectBody.fec. Golay(24,12) is the code SiK/RFD900 use.
const FEC_OPTIONS = [
  { value: "none", label: "NONE (transparent raw MAVLink)" },
  { value: "golay", label: "GOLAY(24,12) (SiK/RFD900 payload FEC)" },
];

// Status feed staleness tracking — same pattern as GnssSpoof.jsx / Jamming.jsx.
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

const STATUS_STYLE = {
  AWAITING_ACK:            { color: "var(--accent-warning)", label: "◐ AWAITING ACK", blink: true },
  MAVLINK_INJECT_ACTIVE:   { color: "var(--accent-critical)", label: "▮▮ TRANSMITTING", blink: true },
  MAVLINK_INJECT_COMPLETE: { color: "var(--accent-success)", label: "✓ INJECT COMPLETE", blink: false },
  MAVLINK_INJECT_STOPPED:  { color: "var(--accent-warning)", label: "■ STOPPED (ABORT)", blink: false },
  TX_FAILED:               { color: "var(--accent-critical)", label: "✕ TX FAILED", blink: false },
  TX_TIMEOUT:              { color: "var(--accent-critical)", label: "✕ TX TIMEOUT", blink: false },
};

const BEARER_SIK = "sik";
const BEARER_SDR = "sdr";

export default function Takeover() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  // ---- Deep-link target (same pattern Signals.jsx / KillChain.jsx use) -------
  const [searchParams] = useSearchParams();
  const deepLinkedId = searchParams.get("contact");
  const appliedDeepLinkIdRef = useRef(null);

  // ---- Shared selection state -----------------------------------------------
  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [bearer, setBearer] = useState(BEARER_SIK);

  // ---- SiK bearer state (verbatim from Payloads.jsx) ------------------------
  const [payloads, setPayloads] = useState([]);
  const [gate, setGate] = useState({ open: false, pl: null, broadcast: false, fratricide: false });
  const [authorizing, setAuthorizing] = useState(false);
  // Per-payload operator parameters surfaced in the card:
  //   PL-008 RTH HOME-SPOOF: the FALSE home coordinates injected via DO_SET_HOME.
  //   PL-011 MANEUVER TAKEOVER: the operator-controlled engagement window / continuous.
  //   PL-005 PROPELLER STOP: how many rotors to stop.
  const [spoof, setSpoof] = useState({ lat: "", lon: "", alt: "" });
  const [motorCount, setMotorCount] = useState(4);
  const [takeover, setTakeover] = useState({ duration_s: 8, continuous: false });

  // ---- SDR bearer state (verbatim from SdrMavlinkInject.jsx) -----------------
  const [command, setCommand] = useState("force_land");
  const [centerFreqMhz, setCenterFreqMhz] = useState(915.0);
  const [airRateBps, setAirRateBps] = useState(250000);
  const [deviationHz, setDeviationHz] = useState(62500);
  const [bt, setBt] = useState(0.5);
  const [bitOrder, setBitOrder] = useState("msb");
  const [preambleHex, setPreambleHex] = useState("AAAAAAAA");
  const [syncHex, setSyncHex] = useState("2DD4");
  const [fec, setFec] = useState("none");
  const [txGain, setTxGain] = useState(20);
  const [repeat, setRepeat] = useState(3);
  const [continuous, setContinuous] = useState(false);
  const [gateOpen, setGateOpen] = useState(false);
  const [fratricide, setFratricide] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  // ---- Loaders ---------------------------------------------------------------
  // One shared /detections + /payloads load. Unlike Payloads.jsx (which kept
  // only ACTIVE contacts) we retain the full detections list so the deep-linked
  // ?contact= chip renders even if that contact is not currently ACTIVE; the
  // fallback dropdown derives its own ACTIVE-only subset below. Target id is the
  // only thing the fire paths key on, so this widening weakens no gate.
  const load = async () => {
    try {
      const [p, d] = await Promise.all([api.get("/payloads"), api.get("/detections")]);
      setPayloads(p.data);
      const all = d.data || [];
      setDets(all);
      const active = all.filter((x) => x.status === "ACTIVE");
      // Deep-link priority, then Payloads.jsx's stale-target re-derivation
      // (task #119): keep the current target while it still exists; otherwise
      // fall back to a currently-ACTIVE contact so engagement control is never
      // silently lost when the previous target drops out of ACTIVE.
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

  // Re-run load when the deep-link target changes (mirrors Signals.jsx: the
  // 5s polling closure is fixed at mount and never sees a later deepLinkedId).
  useEffect(() => { if (deepLinkedId) load(); }, [deepLinkedId]); // eslint-disable-line

  // SDR inject session status feed (verbatim from SdrMavlinkInject.jsx).
  const loadStatus = async () => {
    try {
      const { data } = await api.get("/mavlink-sdr-inject/status");
      setSessions(data.sessions || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch {
      setConsecutiveFailures((n) => n + 1);
    }
  };
  useEffect(() => {
    loadStatus();
    const id = setInterval(loadStatus, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  const activeSession = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "MAVLINK_INJECT_ACTIVE");

  // ---- Shared derived --------------------------------------------------------
  const selectedDet = dets.find((d) => d.id === target);
  const friendlySelected = isFriendly(selectedDet);
  const isDeepLinked = !!deepLinkedId && dets.some((d) => d.id === deepLinkedId);
  const activeDets = dets.filter((x) => x.status === "ACTIVE");

  // ===========================================================================
  // SHARED IFF / FRATRICIDE INTERLOCK (lifted from Payloads.jsx — richer IFF)
  // ===========================================================================

  // Friendly-fire interlock: an explicit, visible commander action distinct
  // from firing itself. Calls the real backend endpoint that flips
  // authorized_target on the detection — no client-side bypass of the
  // server-enforced check in /payloads/deploy or /payloads/mavlink-sdr-inject.
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

  // ===========================================================================
  // SiK-PAIRED BEARER — fire path POST /payloads/deploy (verbatim Payloads.jsx)
  //   RangeAuthorizationControl effect="mavlink"
  //   arm token effect="deploy" (CRITICAL severity or broadcast)
  //   SafetyGate default CHECKS (+ fratricide mode)
  // ===========================================================================

  // `iffAck`, when present, is a single-use commander friendly-fire ack minted
  // for THIS target (see doDeployFriendlyOverride). It is the ONLY thing that
  // lets a deploy engage a confirmed friendly; the backend re-verifies it at
  // fire time. When it is present we skip the routine authorized_target gate.
  const doDeploy = async (pl, broadcast, iffAck) => {
    if (!broadcast && !target) { toast.error("No active target selected"); return; }
    if (!broadcast && !iffAck && selectedDet && !selectedDet.authorized_target) {
      toast.error("Target not authorized", {
        description: "Friendly-fire interlock: authorize this target before deploying.",
      });
      return;
    }
    try {
      // Second factor for CRITICAL-severity payloads and ALL broadcasts,
      // fetched right here — this call only ever happens as a direct
      // consequence of the operator's confirmed deploy action. Harmless to
      // fetch unconditionally: the token is single-use/short-TTL and the
      // backend only consumes it when severity === "CRITICAL" or broadcast.
      let arm_token;
      if (pl.severity === "CRITICAL" || broadcast) {
        // F3: arm token is bound to effect="deploy" and (for a single-target
        // deploy) the exact target detection — the backend rejects a token
        // spent on a different effect/target.
        const { data: arm } = await api.post("/arm", {
          effect: "deploy",
          target_detection_id: broadcast ? null : target,
        });
        arm_token = arm.arm_token;
      }
      // Per-payload operator parameters — only sent for the payload they apply
      // to (the backend ignores them for others, but keep the request tight).
      const extra = {};
      if (pl.id === "PL-008") {
        if (spoof.lat !== "") extra.spoof_lat = Number(spoof.lat);
        if (spoof.lon !== "") extra.spoof_lon = Number(spoof.lon);
        if (spoof.alt !== "") extra.spoof_alt = Number(spoof.alt);
      } else if (pl.id === "PL-005") {
        extra.motor_count = Number(motorCount);
      } else if (pl.id === "PL-011") {
        extra.duration_s = Number(takeover.duration_s);
        extra.continuous = !!takeover.continuous;
      }
      const { data } = await api.post("/payloads/deploy", {
        payload_id: pl.id,
        target_detection_id: broadcast ? null : target,
        broadcast,
        arm_token,
        ...extra,
        // Only ever set for a deliberate, commander-authorized fratricide
        // engagement. Omitted entirely for every routine (non-friendly) deploy.
        ...(iffAck ? { iff_friendly_fire_ack: iffAck } : {}),
      });
      // The server reports AWAITING_ACK until the rf-bridge confirms it actually
      // wrote the frame to the real serial radio — reflect that honestly.
      if (data.tx_bridge_subscribed === false) {
        if (!handleEngageBlock({ response: data }, { isCommander, onFixed: load })) {
          toast.error(`${pl.name}: NOT TRANSMITTED`, {
            description: `Nothing reached a radio. Request ${data.request_id?.slice(0, 8)} will TX_TIMEOUT.`,
          });
        }
      } else if (data.status === "AWAITING_ACK") {
        toast.info(`${pl.name} SENT — awaiting bridge ACK`, {
          description: `pkt ${data.length}B · ${broadcast ? "BROADCAST" : `tgt sys=${data.target_system}`} · req ${data.request_id?.slice(0, 8)}`,
        });
      } else {
        toast.success(`${pl.name} DEPLOYED`, {
          description: `pkt ${data.length}B · ${broadcast ? "BROADCAST" : `tgt sys=${data.target_system}`}`,
        });
      }
      load();
    } catch (e) {
      // Graceful handling of the backend fratricide interlock: if a routine
      // deploy is refused because the target is a confirmed friendly, surface
      // the explicit commander override path instead of a raw error toast.
      if (!iffAck && !broadcast && isFratricideRefusal(e)) {
        toast.error("FRATRICIDE INTERLOCK — routine fire refused", {
          description: "Target is IFF-CONFIRMED FRIENDLY. Engage only via the deliberate commander friendly-fire override.",
        });
        setGate({ open: true, pl, broadcast: false, fratricide: true });
        return;
      }
      // Operator-friendly pre-condition translation (RESUME TX / Bring TX Online).
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: load })) return;
      toast.error("Deploy failed", { description: formatApiError(e) });
    }
  };

  // Deliberate, commander-only fratricide override. Called ONLY from the
  // fratricide SafetyGate's confirm (typed ack + checkbox + commander role
  // already enforced there). Mints a single-use, target-bound friendly-fire
  // ack, then deploys carrying it. The one path that can engage a confirmed
  // friendly — never reachable by clicking the normal buttons.
  const doDeployFriendlyOverride = async (pl) => {
    if (!selectedDet) return;
    let ackToken;
    try {
      const { data } = await api.post(`/detections/${selectedDet.id}/friendly-fire-ack`);
      ackToken = data.iff_friendly_fire_ack;
    } catch (e) {
      toast.error("Friendly-fire ack refused", { description: formatApiError(e) });
      return;
    }
    await doDeploy(pl, false, ackToken);
  };

  const deploy = (pl, broadcast) => {
    // A single-target deploy against a CONFIRMED FRIENDLY can never take the
    // routine path — route it into the deliberate, commander-only fratricide
    // override gate. (Broadcast is target-agnostic and keeps its existing flow.)
    if (!broadcast && friendlySelected) {
      setGate({ open: true, pl, broadcast: false, fratricide: true });
      return;
    }
    if (SAFETY_GATED.has(pl.id)) {
      setGate({ open: true, pl, broadcast, fratricide: false });
      return;
    }
    doDeploy(pl, broadcast);
  };

  // ===========================================================================
  // NO-PAIRING SDR BEARER — fire path POST /payloads/mavlink-sdr-inject
  //   (verbatim SdrMavlinkInject.jsx)
  //   RangeAuthorizationControl effect="mavlink_sdr_inject"
  //   arm token effect="mavlink_sdr_inject" + DEDICATED inject-confirm token
  //   SafetyGate MAVLINK_SDR_INJECT_CHECKS (+ fratricide mode)
  // ===========================================================================

  // Preamble/sync must be valid, non-empty, byte-aligned hex (mirrors the
  // backend MavlinkSdrInjectBody regex ^(?:[0-9a-fA-F]{2}){1,64}$).
  const isValidPhyHex = (s) => /^(?:[0-9a-fA-F]{2}){1,64}$/.test(String(s || ""));
  const phyHexValid = isValidPhyHex(preambleHex) && isValidPhyHex(syncHex);
  const canArmSdr = !activeSession && !submitting && !!target && !!command;

  const fireInject = async () => {
    if (!target) { toast.error("No target selected"); return; }
    if (!phyHexValid) {
      toast.error("Invalid preamble/sync hex", {
        description: "Preamble and sync word must be 1–64 bytes of hex (e.g. AAAAAAAA, 2DD4).",
      });
      return;
    }
    setSubmitting(true);
    try {
      // Step 1: arm token bound to effect=mavlink_sdr_inject AND this exact
      // target (F3 — the backend rejects a token spent on a different
      // effect/target).
      const { data: arm } = await api.post("/arm", {
        effect: "mavlink_sdr_inject",
        target_detection_id: target,
      });
      // Step 2: mint the SDR-inject confirmation token RIGHT NOW — this call
      // only happens because the SafetyGate two-step confirm just completed.
      // NOT interchangeable with jam/gnss/deploy confirm tokens.
      const { data: confirm } = await api.post("/mavlink-sdr-inject/confirm", {});
      // Step 3 (fratricide only): the DELIBERATE, single-use, target-bound
      // commander friendly-fire ack — the ONLY thing that can license injecting
      // at a confirmed friendly. Minted here, consumed once by the backend.
      let iffAck;
      if (friendlySelected) {
        const { data: ack } = await api.post(`/detections/${target}/friendly-fire-ack`);
        iffAck = ack.iff_friendly_fire_ack;
      }
      // Step 4: the actual inject request.
      const { data } = await api.post("/payloads/mavlink-sdr-inject", {
        target_detection_id: target,
        command,
        center_freq_mhz: Number(centerFreqMhz),
        air_rate_bps: Number(airRateBps),
        deviation_hz: Number(deviationHz),
        bt: Number(bt),
        bit_order: bitOrder,
        preamble_hex: preambleHex,
        sync_word_hex: syncHex,
        fec,
        tx_gain: Number(txGain),
        repeat: Number(repeat),
        continuous,
        arm_token: arm.arm_token,
        mavlink_sdr_inject_confirm_token: confirm.mavlink_sdr_inject_confirm_token,
        ...(iffAck ? { iff_friendly_fire_ack: iffAck } : {}),
      });
      if (data.tx_bridge_subscribed === false) {
        if (!handleEngageBlock({ response: data }, { isCommander, onFixed: loadStatus })) {
          toast.error("SDR MAVLINK INJECT: NOT TRANSMITTED", {
            description: `Nothing reached a radio. Request ${data.request_id?.slice(0, 8)} will TX_TIMEOUT.`,
          });
        }
      } else {
        toast.info(`SDR MAVLINK INJECT REQUESTED — awaiting bridge ACK`, {
          description: `${data.command?.toUpperCase()} · ${data.center_freq_mhz} MHz · ×${data.repeat} · ` +
            `sys ${data.target_system} · req ${data.request_id?.slice(0, 8)}`,
        });
      }
      loadStatus();
    } catch (e) {
      if (isFratricideRefusal(e)) {
        toast.error("FRATRICIDE INTERLOCK — routine inject refused", {
          description: "Target is IFF-CONFIRMED FRIENDLY. Inject only via the deliberate commander friendly-fire override.",
        });
        setFratricide(true);
        setGateOpen(true);
        return;
      }
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: loadStatus })) return;
      toast.error("SDR MAVLink inject request failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  const openSdrGate = () => {
    // A confirmed friendly can never take the routine path — it opens the gate
    // in fratricide mode (commander role + typed ack enforced in SafetyGate).
    setFratricide(friendlySelected);
    setGateOpen(true);
  };

  // ---- IFF state label for the read-only target chip ------------------------
  const iffLabel = (d) => {
    if (!d) return "—";
    if (isFriendly(d)) return "IFF-CONFIRMED FRIENDLY";
    if (d.authorized_target) return "AUTHORIZED TARGET";
    return d.threat_level || "UNVERIFIED";
  };
  const linkLabel = (d) => (d && (d.control_link_family || d.control_link_protocol || d.protocol)) || "unknown";

  return (
    <div data-testid="page-takeover" className="space-y-6">
      {/* ---- Header ---------------------------------------------------------- */}
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> Per-Contact Engagement
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            MAVLink Takeover
          </h1>
        </div>
      </div>

      {/* ---- EMERGENCY ABORT / STAND-DOWN (always visible, both bearers) ----- */}
      <div
        className="tactical-border p-3 flex items-center justify-between"
        style={{ background: "color-mix(in srgb, var(--accent-critical) 8%, var(--bg-surface))", borderColor: "var(--accent-critical)" }}
      >
        <div className="font-mono text-[11px] uppercase tracking-widest" style={{ color: "var(--accent-critical)" }}>
          <Siren size={14} className="inline mr-2" strokeWidth={2} />
          Halt all RF transmission instantly — either bearer, continuous injects included
        </div>
        <EmergencyAbort />
      </div>

      {/* ===================================================================== */}
      {/* 1 · TARGET                                                            */}
      {/* ===================================================================== */}
      <section className="space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 flex items-center gap-2">
          <TargetIcon size={12} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} /> 1 · Target
        </div>

        {isDeepLinked && selectedDet ? (
          // Deep-linked from a cue (DECIDE / Kill Chain / Signals): pre-filled,
          // READ-ONLY target chip — callsign · model · protocol · IFF · link-class.
          <div
            data-testid="takeover-target-chip"
            className="tactical-border p-4 flex flex-wrap items-center gap-x-6 gap-y-2"
            style={{ background: "var(--bg-surface)", borderColor: friendlySelected ? "var(--accent-critical)" : "var(--border-col)" }}
          >
            <div className="flex items-center gap-2">
              <Crosshair size={16} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
              <span className="font-heading font-black text-xl uppercase tracking-tight">{selectedDet.callsign}</span>
            </div>
            <div className="font-mono text-[11px] text-slate-400">MODEL <span className="text-slate-200">{selectedDet.model || "?"}</span></div>
            <div className="font-mono text-[11px] text-slate-400">PROTOCOL <span className="text-slate-200">{selectedDet.protocol || "?"}</span></div>
            <div className="font-mono text-[11px] text-slate-400">SYS <span className="text-slate-200">{selectedDet.system_id ?? "?"}</span></div>
            <div className="font-mono text-[11px] text-slate-400">LINK <span className="text-slate-200">{linkLabel(selectedDet)}</span></div>
            <div className="font-mono text-[11px]">
              IFF{" "}
              <span
                className="font-bold"
                style={{ color: friendlySelected ? "var(--accent-critical)" : "var(--text-primary)" }}
              >
                {iffLabel(selectedDet)}
              </span>
            </div>
            <span className="font-mono text-[9px] uppercase tracking-widest text-slate-600 ml-auto">read-only · from cue</span>
          </div>
        ) : (
          // No cue: fall back to the existing ACTIVE-detection dropdown selector.
          <div className="flex flex-wrap items-center gap-3">
            <select
              data-testid="target-select"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className="tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
            >
              {activeDets.length === 0 && <option value="">— NO ACTIVE TARGETS —</option>}
              {activeDets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.callsign} · {d.model} · sys={d.system_id}
                  {isFriendly(d)
                    ? " ⚠ IFF-CONFIRMED FRIENDLY"
                    : d.authorized_target ? "" : " (NOT AUTHORIZED)"}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* Shared AUTHORIZE-TARGET / IFF interlock (lifted from Payloads.jsx). */}
        {selectedDet && !friendlySelected && (
          <button
            data-testid="authorize-target-toggle"
            onClick={toggleAuthorize}
            disabled={authorizing}
            className={`inline-flex items-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest transition-colors disabled:opacity-40 ${
              selectedDet.authorized_target
                ? "text-[var(--accent-success)] border-[var(--accent-success)] hover:bg-[var(--accent-success)] hover:text-black"
                : "text-[var(--accent-critical)] border-[var(--accent-critical)] hover:bg-[var(--accent-critical)] hover:text-black"
            }`}
          >
            {selectedDet.authorized_target ? (
              <ShieldCheck size={14} strokeWidth={1.5} />
            ) : (
              <ShieldOff size={14} strokeWidth={1.5} />
            )}
            {selectedDet.authorized_target ? "TARGET AUTHORIZED" : "AUTHORIZE TARGET"}
          </button>
        )}
        {selectedDet && friendlySelected && (
          <span
            data-testid="friendly-target-indicator"
            className="inline-flex items-center gap-2 px-3 py-2 border-2 font-mono text-[10px] font-bold uppercase tracking-widest"
            style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
            title="IFF-confirmed friendly — engaging is fratricide and requires the deliberate commander override."
          >
            <ShieldAlert size={14} strokeWidth={1.75} />
            IFF-CONFIRMED FRIENDLY
          </span>
        )}

        {/* Shared fratricide banner (lifted from Payloads.jsx). */}
        {friendlySelected && (
          <div
            data-testid="fratricide-banner"
            className="p-4 flex items-start gap-3 border-2"
            style={{
              borderColor: "var(--accent-critical)",
              background: "color-mix(in srgb, var(--accent-critical) 16%, var(--bg-surface))",
            }}
          >
            <ShieldAlert size={22} strokeWidth={1.75} style={{ color: "var(--accent-critical)", flexShrink: 0 }} />
            <div className="font-mono text-xs" style={{ color: "var(--text-primary)" }}>
              <div className="font-heading font-black text-base uppercase tracking-tight" style={{ color: "var(--accent-critical)" }}>
                ⚠ TARGET IFF-CONFIRMED FRIENDLY — ENGAGING WILL BE FRATRICIDE
              </div>
              <div className="mt-1 text-slate-300">
                <span className="font-bold" style={{ color: "var(--text-primary)" }}>{selectedDet?.callsign}</span>{" "}
                has replied to IFF interrogation. Routine authorize and single-target engagement are refused for this
                contact. {isCommander
                  ? "A single-target engagement below opens the deliberate, single-use commander friendly-fire override — it is not a normal fire."
                  : "Only a commander may deliberately override this; your role cannot engage a confirmed friendly."}
              </div>
            </div>
          </div>
        )}
      </section>

      {/* ===================================================================== */}
      {/* 2 · BEARER — the ONLY real difference between the two former pages     */}
      {/* ===================================================================== */}
      <section className="space-y-3">
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">2 · Bearer</div>
        <div data-testid="bearer-radio-group" className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label
            data-testid="bearer-option-sik"
            className="flex items-start gap-3 p-4 tactical-border cursor-pointer hover-surface"
            style={bearer === BEARER_SIK ? { borderColor: "var(--accent-info)", background: "color-mix(in srgb, var(--accent-info) 8%, var(--bg-surface))" } : undefined}
          >
            <input
              type="radio"
              name="bearer"
              value={BEARER_SIK}
              checked={bearer === BEARER_SIK}
              onChange={() => setBearer(BEARER_SIK)}
              className="mt-1"
              style={{ accentColor: "var(--accent-info)" }}
            />
            <div>
              <div className="font-mono text-xs font-bold uppercase tracking-widest flex items-center gap-2">
                <Bomb size={13} strokeWidth={1.5} /> SiK-paired link
              </div>
              <div className="font-mono text-[10px] text-slate-400 mt-1 leading-relaxed">
                Fires via the paired SiK/serial radio — <span className="text-slate-300">POST /payloads/deploy</span>.
                MAVLink payload library, byte-accurate frames over the shared NetID.
              </div>
            </div>
          </label>
          <label
            data-testid="bearer-option-sdr"
            className="flex items-start gap-3 p-4 tactical-border cursor-pointer hover-surface"
            style={bearer === BEARER_SDR ? { borderColor: "var(--accent-critical)", background: "color-mix(in srgb, var(--accent-critical) 8%, var(--bg-surface))" } : undefined}
          >
            <input
              type="radio"
              name="bearer"
              value={BEARER_SDR}
              checked={bearer === BEARER_SDR}
              onChange={() => setBearer(BEARER_SDR)}
              className="mt-1"
              style={{ accentColor: "var(--accent-critical)" }}
            />
            <div>
              <div className="font-mono text-xs font-bold uppercase tracking-widest flex items-center gap-2">
                <RadioTower size={13} strokeWidth={1.5} /> No-pairing SDR over-air
              </div>
              <div className="font-mono text-[10px] text-slate-400 mt-1 leading-relaxed">
                Radiates a GFSK-modulated command via the pinned HackRF — <span className="text-slate-300">POST /payloads/mavlink-sdr-inject</span>.
                No SiK pairing; reveals air-PHY controls.
              </div>
            </div>
          </label>
        </div>
      </section>

      {/* ===================================================================== */}
      {/* 3 · EFFECT + SAFETY SPINE — one active bearer flow at a time.          */}
      {/* Each bearer renders its OWN full, byte-for-byte gated flow.            */}
      {/* ===================================================================== */}

      {bearer === BEARER_SIK && (
        <section data-testid="bearer-flow-sik" className="space-y-4">
          {/* SiK per-bearer Range Authorization (effect="mavlink"). */}
          <RangeAuthorizationControl effect="mavlink" label="MAVLINK PAYLOAD DEPLOY" />

          {/* SiK honesty banner (verbatim from Payloads.jsx). */}
          <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "color-mix(in srgb, var(--accent-critical) 10%, var(--bg-surface))" }}>
            <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            <div className="font-mono text-xs text-slate-300">
              <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING — REAL WHEN DEPLOYED:</span>{" "}
              Each payload builds a byte-accurate MAVLink frame (real CRC-16/MCRF4XX) and, when a TX bridge is
              subscribed and owns the radio, transmits it for real — over the SiK radio, or as real RF via the
              pinned HackRF — through the full arm / IFF / range-auth / tx_halt / device-pin spine. Against an
              unencrypted / legacy-MAVLink target this is a real kinetic/logical effect (force-land, disarm,
              flight-termination, spoof-home→RTH, all-motor stop, controlled-landing takeover). It is NOT a
              guaranteed kill, and it does NOT apply to encrypted / FHSS links (DJI, ELRS/CRSF, DSMX…) — jamming
              is the defeat there. GNSS-telemetry denial is protocol-level only; true GNSS spoof stays
              acquisition-plausible with receiver-lock unproven.
            </div>
          </div>

          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">3 · Effect (SiK payload library)</div>

          {/* SiK effect chooser: the PL-* payload cards (verbatim Payloads.jsx),
              FPV Video Capture panel intentionally excluded. */}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-0 tactical-border">
            {payloads.map((p, i) => (
              <div
                key={p.id}
                data-testid={`payload-${p.id}`}
                className={`p-4 tactical-border-r tactical-border-b ${i % 3 === 2 ? "border-r-0" : ""}`}
                style={{ background: "var(--bg-surface)" }}
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                    {CAT_LABEL[p.category]} · {p.id}
                  </span>
                  <span
                    className="px-2 py-0.5 tactical-border font-mono font-bold text-[10px]"
                    style={{ color: THREAT_COLOR[p.severity], borderColor: THREAT_COLOR[p.severity] }}
                  >
                    {p.severity}
                  </span>
                </div>
                <div className="font-heading font-black text-2xl tracking-tighter uppercase mb-2">
                  {p.name}
                </div>
                <div className="font-mono text-[11px] text-slate-400 leading-relaxed min-h-[60px]">
                  {p.description}
                </div>
                <div className="tactical-border-t mt-3 pt-3 font-mono text-[10px] text-slate-500 space-y-1">
                  <div>CMD: <span className="text-slate-300">{p.mav_cmd}</span></div>
                  <div>EFFECT: <span className="text-slate-300">{p.effect}</span></div>
                  <div>DURATION: <span className="text-slate-300">{p.duration_ms} ms</span>
                    {" "}· REVERSIBLE: <span className="text-slate-300">{p.reversible ? "YES" : "NO"}</span></div>
                </div>
                {p.id === "PL-010" && (
                  <div className="mt-4 -mb-1 flex justify-end">
                    <span
                      className="px-2 py-0.5 tactical-border font-mono text-[9px] uppercase tracking-widest"
                      style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
                    >
                      Broadcast-only
                    </span>
                  </div>
                )}
                {p.id === "PL-008" && (
                  <div data-testid="pl008-spoof-inputs" className="mt-3 space-y-2">
                    <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                      Spoofed HOME (false coordinates → RTH)
                    </div>
                    <div className="grid grid-cols-3 gap-2">
                      <input data-testid="pl008-lat" type="number" step="0.0001" placeholder="lat -90..90"
                        value={spoof.lat} onChange={(e) => setSpoof((s) => ({ ...s, lat: e.target.value }))}
                        className="tactical-input tactical-border px-2 py-1 font-mono text-[11px] focus:outline-none focus-accent-info" />
                      <input data-testid="pl008-lon" type="number" step="0.0001" placeholder="lon -180..180"
                        value={spoof.lon} onChange={(e) => setSpoof((s) => ({ ...s, lon: e.target.value }))}
                        className="tactical-input tactical-border px-2 py-1 font-mono text-[11px] focus:outline-none focus-accent-info" />
                      <input data-testid="pl008-alt" type="number" step="1" placeholder="alt m"
                        value={spoof.alt} onChange={(e) => setSpoof((s) => ({ ...s, alt: e.target.value }))}
                        className="tactical-input tactical-border px-2 py-1 font-mono text-[11px] focus:outline-none focus-accent-info" />
                    </div>
                  </div>
                )}
                {p.id === "PL-005" && (
                  <div data-testid="pl005-motor-input" className="mt-3">
                    <label className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                      Motors to stop (1–8)
                      <input data-testid="pl005-motor-count" type="number" min={1} max={8} step={1}
                        value={motorCount}
                        onChange={(e) => setMotorCount(Math.min(8, Math.max(1, Number(e.target.value) || 1)))}
                        className="mt-1 w-full tactical-input tactical-border px-2 py-1 font-mono text-[11px] focus:outline-none focus-accent-info" />
                    </label>
                  </div>
                )}
                {p.id === "PL-011" && (
                  <div data-testid="pl011-takeover-inputs" className="mt-3 space-y-2">
                    <label className="block font-mono text-[10px] uppercase tracking-widest text-slate-500">
                      Engagement window (s) — operator-controlled, no cap
                      <input data-testid="pl011-duration" type="number" min={1} step={1}
                        value={takeover.duration_s} disabled={takeover.continuous}
                        onChange={(e) => setTakeover((t) => ({ ...t, duration_s: Number(e.target.value) || 1 }))}
                        className="mt-1 w-full tactical-input tactical-border px-2 py-1 font-mono text-[11px] focus:outline-none focus-accent-info disabled:opacity-40" />
                    </label>
                    <label className="flex items-center gap-2 font-mono text-[11px] text-slate-300">
                      <input data-testid="pl011-continuous" type="checkbox" checked={takeover.continuous}
                        onChange={(e) => setTakeover((t) => ({ ...t, continuous: e.target.checked }))} />
                      Continuous until stop (EMERGENCY ABORT / tx_halt ends it)
                    </label>
                  </div>
                )}
                <div className="mt-4 grid grid-cols-2 gap-0 tactical-border">
                  <button
                    data-testid={`deploy-target-${p.id}`}
                    onClick={() => deploy(p, false)}
                    disabled={p.id === "PL-010"}
                    title={
                      p.id === "PL-010"
                        ? "Broadcast-only payload — no single-target mode"
                        : friendlySelected
                          ? "Target is IFF-confirmed friendly — opens the deliberate commander fratricide override"
                          : undefined
                    }
                    className={`tactical-border-r px-3 py-2 font-mono text-[10px] uppercase tracking-widest transition-colors scanline-btn disabled:opacity-30 disabled:cursor-not-allowed ${
                      friendlySelected ? "hover-accent-critical" : "hover-accent-info"
                    }`}
                    style={friendlySelected ? { color: "var(--accent-critical)" } : undefined}
                  >
                    {friendlySelected ? "⚠ FRATRICIDE DEPLOY" : "DEPLOY → TGT"}
                  </button>
                  <button
                    data-testid={`deploy-broadcast-${p.id}`}
                    onClick={() => deploy(p, true)}
                    className="px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover-accent-critical transition-colors scanline-btn"
                    style={{ color: "var(--accent-critical)" }}
                  >
                    BROADCAST
                  </button>
                </div>
              </div>
            ))}
          </div>

          {/* SiK SafetyGate — default CHECKS (+ fratricide). Only reachable
              from the SiK deploy flow. */}
          <SafetyGate
            open={gate.open}
            payloadName={gate.pl?.name}
            severity={gate.pl?.severity}
            fratricide={gate.fratricide}
            isCommander={isCommander}
            friendlyCallsign={gate.fratricide ? selectedDet?.callsign : undefined}
            onClose={() => setGate({ open: false, pl: null, broadcast: false, fratricide: false })}
            onConfirm={() => {
              const { pl, broadcast, fratricide: fr } = gate;
              setGate({ open: false, pl: null, broadcast: false, fratricide: false });
              if (fr) {
                doDeployFriendlyOverride(pl);
              } else {
                doDeploy(pl, broadcast);
              }
            }}
          />
        </section>
      )}

      {bearer === BEARER_SDR && (
        <section data-testid="bearer-flow-sdr" className="space-y-4">
          {/* SDR honesty banner + fidelity limits (verbatim SdrMavlinkInject.jsx). */}
          <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "var(--surface-critical)" }}>
            <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
            <div className="font-mono text-xs text-slate-300 space-y-2">
              <div>
                <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
                This GFSK-modulates a byte-accurate MAVLink COMMAND_LONG onto baseband IQ and radiates it over
                the air at the target link's frequency via the pinned TX HackRF — no SiK pairing, no shared NetID.
                A REAL RF transmission of a takeover command; it cannot be recalled once sent. Requires commander
                role, a fresh arm token, an SDR-inject confirmation token (NOT interchangeable with the jam / GNSS
                confirm tokens), the IFF fratricide interlock, AND a live Range Authorization lease for
                effect=mavlink_sdr_inject (arming effect=jam or effect=mavlink does NOT arm this) — all
                independently, every time.
              </div>
              <div
                data-testid="sdr-inject-fidelity-note"
                className="tactical-border p-2"
                style={{ background: "rgba(255,149,0,0.08)", borderColor: "#FF9500", color: "#FFB454" }}
              >
                <span className="font-bold">HONEST CAPABILITY LIMITS (niche adversary path, NOT a universal defeat):</span>{" "}
                Works ONLY against a FIXED-FREQUENCY, UNENCRYPTED MAVLink link (hop disabled). It does NOT follow an
                FHSS / frequency-hopping sequence (e.g. default SiK/RFD900 hopping) — that is not implemented — and it
                is N/A against MAVLink-signed / encrypted / proprietary links (DJI, ELRS/CRSF, DSMX…). For those links
                the defeat is JAMMING, which remains the universal defeat. The backend refuses (422) an
                encrypted/FHSS or unattested-unknown target link rather than transmit uselessly.
              </div>
            </div>
          </div>

          {/* SDR per-bearer Range Authorization (effect="mavlink_sdr_inject"). */}
          <RangeAuthorizationControl effect="mavlink_sdr_inject" label="SDR MAVLINK INJECT" />

          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">3 · Effect (SDR command + air-PHY)</div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <div className="tactical-border p-4 space-y-4" style={{ background: "var(--bg-surface)" }}>
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                Command
              </div>

              <label className="block">
                <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Command</span>
                <select
                  data-testid="sdr-inject-command-select"
                  value={command}
                  onChange={(e) => setCommand(e.target.value)}
                  className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                >
                  {COMMANDS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
              </label>

              {/* Air-PHY controls — revealed ONLY for the SDR bearer. */}
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 pt-2">
                Target Link Air-PHY (match to the real link — measured, not assumed)
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Center Freq (MHz)</span>
                  <input
                    data-testid="sdr-inject-freq-input"
                    type="number" step="0.001" min={1}
                    value={centerFreqMhz}
                    onChange={(e) => setCenterFreqMhz(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Air Rate (bps)</span>
                  <input
                    data-testid="sdr-inject-airrate-input"
                    type="number" step="1000" min={1}
                    value={airRateBps}
                    onChange={(e) => setAirRateBps(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Deviation (Hz)</span>
                  <input
                    data-testid="sdr-inject-deviation-input"
                    type="number" step="1000" min={1}
                    value={deviationHz}
                    onChange={(e) => setDeviationHz(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">BT (0-1)</span>
                  <input
                    data-testid="sdr-inject-bt-input"
                    type="number" step="0.1" min={0.1} max={1}
                    value={bt}
                    onChange={(e) => setBt(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Bit Order</span>
                  <select
                    data-testid="sdr-inject-bitorder-select"
                    value={bitOrder}
                    onChange={(e) => setBitOrder(e.target.value)}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  >
                    <option value="msb">MSB-first (radio default)</option>
                    <option value="lsb">LSB-first (transparent UART)</option>
                  </select>
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Repeat (1-{MAX_REPEAT})</span>
                  <input
                    data-testid="sdr-inject-repeat-input"
                    type="number" min={1} max={MAX_REPEAT} step={1}
                    value={repeat}
                    onChange={(e) => setRepeat(Math.min(MAX_REPEAT, Math.max(1, Number(e.target.value))))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">TX Gain (0-47)</span>
                  <input
                    data-testid="sdr-inject-gain-input"
                    type="number" min={0} max={47}
                    value={txGain}
                    onChange={(e) => setTxGain(Number(e.target.value))}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Preamble (hex)</span>
                  <input
                    data-testid="sdr-inject-preamble-input"
                    type="text"
                    value={preambleHex}
                    onChange={(e) => setPreambleHex(e.target.value)}
                    className={`mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info ${isValidPhyHex(preambleHex) ? "" : "border-[var(--accent-critical)]"}`}
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Sync Word (hex)</span>
                  <input
                    data-testid="sdr-inject-sync-input"
                    type="text"
                    value={syncHex}
                    onChange={(e) => setSyncHex(e.target.value)}
                    className={`mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info ${isValidPhyHex(syncHex) ? "" : "border-[var(--accent-critical)]"}`}
                  />
                </label>
                <label className="block">
                  <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Payload FEC</span>
                  <select
                    data-testid="sdr-inject-fec-select"
                    value={fec}
                    onChange={(e) => setFec(e.target.value)}
                    className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
                  >
                    {FEC_OPTIONS.map((f) => <option key={f.value} value={f.value}>{f.label}</option>)}
                  </select>
                </label>
              </div>

              <label className="flex items-center gap-2 font-mono text-[11px] text-slate-300">
                <input
                  data-testid="sdr-inject-continuous-toggle"
                  type="checkbox"
                  checked={continuous}
                  onChange={(e) => setContinuous(e.target.checked)}
                />
                Continuous re-emit until stopped (no fixed repeat count) — still instantly
                stoppable via EMERGENCY ABORT / tx_halt at the bridge.
              </label>

              <div
                data-testid="sdr-inject-phy-note"
                className="tactical-border p-2 font-mono text-[10px]"
                style={{ background: "rgba(255,149,0,0.08)", borderColor: "#FF9500", color: "#FFB454" }}
              >
                Preamble / sync / FEC must match the TARGET link's ACTUAL PHY (measured from a
                capture). Golay(24,12) is the SiK/RFD900 payload FEC. Matching these RAISES the
                probability the target's packet handler accepts the burst — it does NOT guarantee
                decode (whitening / interleaving / bit-order mismatches will still fail it).
              </div>

              <button
                data-testid="sdr-inject-arm-button"
                disabled={!canArmSdr || !phyHexValid}
                onClick={openSdrGate}
                className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
                  !canArmSdr || !phyHexValid
                    ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                    : "hover-accent-critical"
                }`}
                style={!canArmSdr || !phyHexValid ? undefined : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
              >
                <TargetIcon size={14} strokeWidth={1.5} />
                {friendlySelected ? "ARM SDR INJECT (FRIENDLY — OVERRIDE)" : "ARM SDR MAVLINK INJECT"}
              </button>
            </div>

            <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
              <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-3">
                SDR MAVLink Inject Sessions
              </div>
              {statusUnconfirmed && (
                <div
                  data-testid="sdr-inject-status-unconfirmed-banner"
                  className="mb-3 flex items-center gap-2 px-3 py-2 pulse-crit"
                  style={{ background: "#FF9500", color: "black" }}
                >
                  <ShieldAlert size={14} strokeWidth={2} />
                  <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                    STATUS UNCONFIRMED — inject status feed stale, session states below may be out of date
                  </span>
                </div>
              )}
              {sessions.length === 0 && (
                <div className="font-mono text-xs text-slate-600 text-center py-8">
                  no sdr mavlink inject sessions yet<span className="term-caret" />
                </div>
              )}
              <div className="space-y-2">
                {sessions.map((s) => {
                  const st = STATUS_STYLE[s.status] || { color: "var(--text-muted)", label: s.status, blink: false };
                  return (
                    <div key={s.request_id} data-testid={`sdr-inject-session-${s.request_id}`}
                         className="flex items-center justify-between p-3 tactical-border">
                      <div className="font-mono text-[11px] text-slate-300">
                        {s.command?.toUpperCase()} · {s.center_freq_mhz} MHz · ×{s.repeat} · sys {s.target_system}
                        <div className="text-slate-500 text-[10px]">{s.request_id?.slice(0, 8)}</div>
                      </div>
                      <span
                        data-testid={`sdr-inject-status-${s.request_id}`}
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

          {/* SDR SafetyGate — MAVLINK_SDR_INJECT_CHECKS + dedicated confirm
              token flow (via fireInject) (+ fratricide). Only reachable from
              the SDR arm flow. */}
          <SafetyGate
            open={gateOpen}
            payloadName="SDR MAVLINK INJECT (No-Pairing Over-the-Air Takeover)"
            severity="CRITICAL"
            checks={MAVLINK_SDR_INJECT_CHECKS}
            actionLabel="TRANSMIT"
            irreversibleNote="a real over-the-air MAVLink takeover command — it cannot be recalled once sent"
            fratricide={fratricide}
            isCommander={isCommander}
            friendlyCallsign={fratricide ? selectedDet?.callsign : undefined}
            onClose={() => { setGateOpen(false); setFratricide(false); }}
            onConfirm={() => {
              setGateOpen(false);
              setFratricide(false);
              fireInject();
            }}
          />
        </section>
      )}

      {/* ===================================================================== */}
      {/* ADVANCED · RAW FRAME CRAFTER (collapsed by default, for the EW tech)   */}
      {/* MavlinkConsole's byte-level crafter — governed /mavlink/broadcast path */}
      {/* ===================================================================== */}
      <details data-testid="takeover-advanced-drawer" className="tactical-border" style={{ background: "var(--bg-surface)" }}>
        <summary
          className="px-4 py-3 cursor-pointer font-mono text-xs uppercase tracking-widest flex items-center gap-2 select-none"
          style={{ color: "var(--text-primary)" }}
        >
          <ChevronRight size={14} strokeWidth={1.5} />
          Advanced · Raw MAVLink frame crafter (EW tech)
        </summary>
        <div className="p-4 tactical-border-t">
          <RawFrameCrafter />
        </div>
      </details>
    </div>
  );
}

// =============================================================================
// RawFrameCrafter — MavlinkConsole.jsx's byte-level crafter + hex preview +
// live /api/ws/mavlink stream, inlined here as the Takeover "Advanced" drawer
// (Payloads/SdrMavlinkInject/MavlinkConsole all remain untouched until P-D).
// Fires through the governed POST /mavlink/broadcast path; broadcast TX keeps
// its own arm->confirm two-step. Logic is verbatim from MavlinkConsole.jsx.
// =============================================================================

const MAV_CMD_OPTIONS = [
  { id: 21,  label: "NAV_LAND (21)" },
  { id: 20,  label: "NAV_RETURN_TO_LAUNCH (20)" },
  { id: 400, label: "COMPONENT_ARM_DISARM (400)" },
  { id: 185, label: "DO_FLIGHTTERMINATION (185)" },
  { id: 179, label: "DO_SET_HOME (179)" },
  { id: 245, label: "PREFLIGHT_STORAGE (245)" },
  { id: 246, label: "PREFLIGHT_REBOOT_SHUTDOWN (246)" },
  { id: 209, label: "DO_MOTOR_TEST (209)" },
];

const MSG_IDS = [
  { id: 76, label: "COMMAND_LONG (76)" },
  { id: 0, label: "HEARTBEAT (0)" },
  { id: 11, label: "SET_MODE (11)" },
  { id: 253, label: "STATUSTEXT (253)" },
];

function RawFrameCrafter() {
  const [form, setForm] = useState({
    version: "v2",
    system_id: 255,
    component_id: 190,
    sequence: 0,
    message_id: 76,
    target_system: 1,
    target_component: 1,
    command: 21,
    param1: 0, param2: 0, param3: 0, param4: 0, param5: 0, param6: 0, param7: 0,
  });
  const [preview, setPreview] = useState(null);
  const [stream, setStream] = useState([]);
  const [broadcastFlag, setBroadcastFlag] = useState(false);
  // Broadcast TX is a kinetic, ungated-in-the-protocol action (target_sys=0
  // hits every vehicle on the link). Gate it behind an arm->confirm two-step,
  // mirroring EmergencyAbort, so a single stray click can't blast a broadcast
  // COMMAND_LONG. Single-target TX (broadcastFlag off) fires directly.
  const [broadcastArmed, setBroadcastArmed] = useState(false);
  const [wsStatus, setWsStatus] = useState("connecting"); // connecting | open | reconnecting | no-auth | closed
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  useEffect(() => {
    api.get("/mavlink/packets?limit=25").then((r) => setStream(r.data)).catch(() => {});

    unmountedRef.current = false;

    const MAX_BACKOFF_MS = 15000;
    const NO_AUTH_RETRY_MS = 1500;

    const scheduleReconnect = () => {
      if (unmountedRef.current) return;
      const attempt = attemptRef.current;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attemptRef.current = attempt + 1;
      setWsStatus("reconnecting");
      reconnectTimerRef.current = setTimeout(connect, delay);
    };

    const connect = () => {
      if (unmountedRef.current) return;

      // Re-read token fresh on every attempt so a rotated/late token is picked up.
      const token = localStorage.getItem("cema_token");
      if (!token) {
        setWsStatus("no-auth");
        reconnectTimerRef.current = setTimeout(connect, NO_AUTH_RETRY_MS);
        return;
      }

      setWsStatus((s) => (s === "open" ? s : "connecting"));
      const ws = new WebSocket(`${wsUrl("/api/ws/mavlink")}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setWsStatus("open");
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "packet") {
            setStream((s) => [msg.packet, ...s].slice(0, 40));
          }
        } catch { /* noop */ }
      };

      ws.onerror = () => {
        // onclose will fire right after and handle reconnect scheduling.
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

  const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const craftPreview = async () => {
    try {
      const body = { ...form };
      if (broadcastFlag) { body.target_system = 0; body.target_component = 0; }
      const { data } = await api.post("/mavlink/craft", body);
      setPreview(data);
    } catch (e) { toast.error("Craft failed", { description: formatApiError(e) }); }
  };

  useEffect(() => { craftPreview(); /* refresh preview on change */ // eslint-disable-next-line
  }, [form, broadcastFlag]);

  // Disarm the broadcast confirm after 4s of inactivity, or whenever the
  // operator toggles broadcast mode off.
  useEffect(() => {
    if (!broadcastArmed) return;
    const t = setTimeout(() => setBroadcastArmed(false), 4000);
    return () => clearTimeout(t);
  }, [broadcastArmed]);
  useEffect(() => { if (!broadcastFlag) setBroadcastArmed(false); }, [broadcastFlag]);

  const transmit = () => {
    // Two-step arm->confirm only for broadcast (target_sys=0) TX.
    if (broadcastFlag && !broadcastArmed) { setBroadcastArmed(true); return; }
    broadcast();
  };

  const broadcast = async () => {
    try {
      const body = { ...form };
      if (broadcastFlag) { body.target_system = 0; body.target_component = 0; }
      await api.post("/mavlink/broadcast", body);
      toast.success("Packet transmitted", { description: `msgid=${body.message_id} → sys=${body.target_system}` });
    } catch (e) { toast.error("Broadcast failed", { description: formatApiError(e) }); }
    finally { setBroadcastArmed(false); }
  };

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast.success("Copied to clipboard"); }
    catch { toast.error("Clipboard unavailable"); }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Radio size={12} className="inline mr-2" strokeWidth={1.5} /> MAVLink Console
          </div>
          <h2 className="font-heading font-black text-2xl uppercase tracking-tighter">
            Packet Crafter · Broadcast
          </h2>
        </div>
        <div className="flex items-center gap-3">
          <label data-testid="broadcast-toggle-wrap"
                 className="flex items-center gap-2 tactical-border px-3 py-2 cursor-pointer">
            <input
              data-testid="broadcast-toggle"
              type="checkbox"
              checked={broadcastFlag}
              onChange={(e) => setBroadcastFlag(e.target.checked)}
              style={{ accentColor: "var(--accent-critical)" }}
            />
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-300">
              BROADCAST (target_sys=0)
            </span>
          </label>
          <button
            data-testid="broadcast-btn"
            onClick={transmit}
            className={`flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest transition-colors scanline-btn ${
              broadcastArmed ? "text-white pulse-crit" : "hover:text-black"
            } ${broadcastFlag ? "hover-accent-critical" : "hover-accent-info"}`}
            style={
              broadcastArmed
                ? { background: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                : broadcastFlag
                ? { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                : { color: "var(--accent-info)", borderColor: "var(--accent-info)" }
            }
            title={broadcastFlag
              ? "Broadcast TX (target_sys=0) — arm, then confirm to transmit to all vehicles"
              : "Transmit crafted packet to the single target system"}
          >
            <Zap size={14} strokeWidth={1.5} />
            {broadcastArmed ? "CONFIRM BROADCAST TX" : "TRANSMIT"}
          </button>
          <span data-testid="broadcast-arm-status" role="status" aria-live="assertive" className="sr-only">
            {broadcastArmed ? "Broadcast transmit armed — activate again to confirm" : ""}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Crafter */}
        <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
          <div className="tactical-border-b px-4 py-3 font-mono text-xs uppercase tracking-widest">
            Packet Header · Payload
          </div>
          <div className="p-4 grid grid-cols-2 gap-4 font-mono text-xs">
            <Field label="VERSION" testid="fld-version">
              <select value={form.version} onChange={(e) => upd("version", e.target.value)}
                      className="w-full tactical-input tactical-border px-2 py-1">
                <option value="v2">v2 (0xFD)</option>
                <option value="v1">v1 (0xFE)</option>
              </select>
            </Field>
            <Field label="MESSAGE_ID" testid="fld-msgid">
              <select value={form.message_id} onChange={(e) => upd("message_id", parseInt(e.target.value))}
                      className="w-full tactical-input tactical-border px-2 py-1">
                {MSG_IDS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </Field>
            <NumField label="SYSTEM_ID" val={form.system_id} onChange={(v) => upd("system_id", v)} testid="fld-sysid" />
            <NumField label="COMPONENT_ID" val={form.component_id} onChange={(v) => upd("component_id", v)} testid="fld-compid" />
            <NumField label="SEQUENCE" val={form.sequence} onChange={(v) => upd("sequence", v)} testid="fld-seq" />
            <NumField label="TARGET_SYSTEM" val={broadcastFlag ? 0 : form.target_system}
                       onChange={(v) => upd("target_system", v)} disabled={broadcastFlag} testid="fld-tsys" />

            {form.message_id === 76 && (
              <>
                <Field label="MAV_CMD" testid="fld-cmd">
                  <select value={form.command} onChange={(e) => upd("command", parseInt(e.target.value))}
                          className="w-full tactical-input tactical-border px-2 py-1">
                    {MAV_CMD_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                </Field>
                <NumField label="TARGET_COMPONENT" val={broadcastFlag ? 0 : form.target_component}
                           onChange={(v) => upd("target_component", v)} disabled={broadcastFlag} testid="fld-tcomp" />
                {[1,2,3,4,5,6,7].map((n) => (
                  <NumField key={n} label={`PARAM${n}`} val={form[`param${n}`]}
                             onChange={(v) => upd(`param${n}`, v)} step="0.01" float testid={`fld-p${n}`} />
                ))}
              </>
            )}
          </div>
        </div>

        {/* Preview */}
        <div className="tactical-border term-surface" style={{ background: "var(--bg-terminal)" }}>
          <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
            <span className="font-mono text-xs uppercase tracking-widest" style={{ color: "var(--text-term)" }}>
              Hex Preview
            </span>
            {preview && (
              <button
                data-testid="copy-hex-btn"
                onClick={() => copy(preview.hex)}
                className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-slate-400 hover:text-white"
              >
                <Copy size={12} strokeWidth={1.5} /> COPY HEX
              </button>
            )}
          </div>
          <div className="p-4 font-mono text-xs" style={{ color: "var(--text-term)" }}>
            {!preview && <div className="text-slate-600">crafting<span className="term-caret" /></div>}
            {preview && (
              <>
                <div className="mb-3 text-slate-500">
                  {preview.length} bytes · STX <span className="text-white">{preview.decoded?.stx}</span>
                  {" "}· msgid <span className="text-white">{preview.decoded?.message_id}</span>
                  {" "}· sys <span className="text-white">{preview.decoded?.system_id}</span>
                </div>
                <pre data-testid="hex-preview" className="whitespace-pre leading-5 text-[11px]">
{preview.hexdump.join("\n")}
                </pre>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Live stream */}
      <div className="tactical-border term-surface" style={{ background: "var(--bg-terminal)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <RadioTower size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
            <span className="font-mono text-xs uppercase tracking-widest text-slate-300">Live MAVLink Broadcast Stream</span>
          </div>
          <span
            data-testid="ws-status"
            className={`font-mono text-[10px] uppercase tracking-widest ${
              wsStatus === "open" ? "blink" : wsStatus !== "no-auth" ? "text-slate-500" : ""
            }`}
            style={
              wsStatus === "open"
                ? { color: "var(--accent-info)" }
                : wsStatus === "no-auth"
                ? { color: "var(--accent-critical)" }
                : undefined
            }
            title={
              wsStatus === "open"
                ? "WebSocket connected"
                : wsStatus === "connecting"
                ? "WebSocket connecting…"
                : wsStatus === "reconnecting"
                ? "WebSocket disconnected — reconnecting…"
                : wsStatus === "no-auth"
                ? "Not authenticated — waiting for token"
                : "WebSocket disconnected"
            }
          >
            ●{" "}
            {wsStatus === "open" && "WS LIVE"}
            {wsStatus === "connecting" && "WS CONNECTING"}
            {wsStatus === "reconnecting" && "WS RECONNECTING"}
            {wsStatus === "no-auth" && "WS NO AUTH"}
            {wsStatus === "closed" && "WS OFFLINE"}
          </span>
        </div>
        <div className="max-h-[360px] overflow-y-auto font-mono text-xs">
          {stream.length === 0 && (
            <div className="p-4 text-slate-600">no packets transmitted<span className="term-caret" /></div>
          )}
          {stream.map((p) => (
            <div key={p.id} data-testid={`pkt-${p.id}`} className="tactical-border-b p-3 hover-surface">
              <div className="flex items-center justify-between mb-1">
                <span className="text-slate-500">{p.ts?.replace("T", " ").split(".")[0]}</span>
                <span className="text-[10px] uppercase tracking-widest text-slate-400">
                  msgid <span className="text-white">{p.decoded?.message_id}</span>
                  {" "}· sys <span className="text-white">{p.system_id}</span> → tgt <span className="text-white">{p.target_system}</span>
                  {p.payload_name && <> · <span style={{color:"var(--accent-warning)"}}>{p.payload_name}</span></>}
                </span>
              </div>
              <HexBytes hex={p.hex} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Renders a MAVLink frame hex as space-separated byte pairs on a single
// horizontally-scrolling line (never wrapping mid-byte), with the final two
// bytes — the MAVLink CRC-16/MCRF4XX checksum — highlighted and captioned.
function HexBytes({ hex }) {
  if (!hex) return null;
  const bytes = String(hex).replace(/[^0-9a-fA-F]/g, "").match(/.{1,2}/g) || [];
  const hasCrc = bytes.length >= 2;
  const body = hasCrc ? bytes.slice(0, -2) : bytes;
  const crc = hasCrc ? bytes.slice(-2) : [];
  return (
    <div className="overflow-x-auto">
      <div className="text-[11px] whitespace-nowrap tabular-nums" style={{ color: "var(--text-term)" }}>
        <span>{body.join(" ")}</span>
        {hasCrc && (
          <>
            {" "}
            <span
              className="font-bold"
              style={{ color: "var(--accent-warning)" }}
              title="CRC-16/MCRF4XX frame checksum (final 2 bytes)"
            >
              {crc.join(" ")}
            </span>
            <span className="ml-2 uppercase tracking-widest text-[9px]" style={{ color: "var(--accent-warning)" }}>
              ◄ CRC
            </span>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, children, testid }) {
  return (
    <label data-testid={testid} className="block">
      <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">{label}</div>
      {children}
    </label>
  );
}

function NumField({ label, val, onChange, disabled, step = "1", float = false, testid }) {
  return (
    <Field label={label} testid={testid}>
      <input
        type="number"
        step={step}
        value={val}
        disabled={disabled}
        onChange={(e) => onChange(float ? parseFloat(e.target.value || "0") : parseInt(e.target.value || "0"))}
        className="w-full tactical-input tactical-border px-2 py-1 focus:outline-none focus-accent-info disabled:opacity-40"
      />
    </Field>
  );
}
