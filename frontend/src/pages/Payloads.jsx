import { useEffect, useRef, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { handleEngageBlock } from "@/lib/engageFix";
import { Bomb, AlertTriangle, Target as TargetIcon, ShieldCheck, ShieldOff, Signal, ShieldAlert } from "lucide-react";
import SafetyGate, { SAFETY_GATED } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import { THREAT_COLOR } from "@/lib/threatLevels";
import { useAuth } from "@/context/AuthContext";

// A contact is a CONFIRMED FRIENDLY exactly when IFF interrogation has replied
// and the backend has stamped it so — mirrors the server-side test in
// backend/server.py (_enforce_fire_time_iff / mint_friendly_fire_ack). Firing
// on one is FRATRICIDE and is refused (403) on the routine authorize/deploy
// path; the only licensed path is the deliberate commander friendly-fire ack.
const isFriendly = (d) =>
  !!d && (d.iff_verified === true || d.threat_level === "FRIENDLY (IFF verified)");

// Recognise the backend's fratricide-interlock 403 so a routine deploy that
// races into a freshly-friendly target surfaces the explicit override path,
// not a raw error string.
const isFratricideRefusal = (e) =>
  e?.response?.status === 403 &&
  /FRATRICIDE|CONFIRMED-FRIENDLY|friendly-fire ack/i.test(formatApiError(e) || "");

const CAT_LABEL = {
  kinetic: "KINETIC",
  logical: "LOGICAL",
  protocol: "PROTOCOL",
  denial: "DENIAL",
};

// Analog FPV video bridge panel (field-bridge/fpv_video_bridge.py), relocated
// here from Dashboard.jsx: FPV capture is an operator-DECIDED action (an
// explicit "go capture something right now" command), not passive dashboard
// telemetry that just displays automatically -- so it belongs alongside the
// other deployable payload capabilities in this file, not on the main
// Dashboard's live-telemetry surface.
//
// HONESTY NOTE: this panel renders exactly what the backend /api/fpv/*
// endpoints report, including the pipeline's own disclosed limitations --
// it does NOT imply a validated, continuous video feed. See
// field-bridge/fpv_video_bridge.py's module docstring for the full
// disclosure this mirrors: AM-envelope + naive scanline reconstruction,
// UNTESTED against a live analog FPV transmitter, snapshot-only (not
// continuous streaming), and DJI digital video is never decoded here.
function FpvVideoPanel() {
  const [meta, setMeta] = useState(null);
  const [imgKey, setImgKey] = useState(0);
  const [captureState, setCaptureState] = useState("idle"); // idle | pending | timeout
  const [captureError, setCaptureError] = useState(null);

  // Guards against setState-after-unmount: load() is invoked repeatedly by
  // the polling intervals below across the component's whole lifetime (not
  // just once per effect run), so a single ref flipped on unmount -- rather
  // than a per-effect "let cancelled" local -- is what actually protects
  // every in-flight call. Same intent as the cancelled-flag pattern used in
  // DetectionHistory.jsx's CadencePanel and GnssSpoof.jsx's preview fetch.
  const cancelledRef = useRef(false);
  useEffect(() => {
    return () => { cancelledRef.current = true; };
  }, []);

  const load = async () => {
    try {
      const { data } = await api.get("/fpv/latest-frame");
      if (cancelledRef.current) return;
      setMeta((prev) => {
        // Real completion signal: a genuinely new captured_at timestamp
        // after a capture was requested -- no fake instant "success".
        if (captureState === "pending" && data.available &&
            data.captured_at && data.captured_at !== prev?.captured_at) {
          setCaptureState("idle");
        }
        return data;
      });
      if (cancelledRef.current) return;
      setImgKey((k) => k + 1);
    } catch { /* silent */ }
  };

  useEffect(() => {
    load();
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captureState]);

  // While a capture is pending, poll faster so the operator sees the real
  // result land as soon as the field bridge's --poll mode picks up the
  // request and finishes one capture+demod+ingest cycle.
  useEffect(() => {
    if (captureState !== "pending") return undefined;
    const fastId = setInterval(load, 2000);
    // Give up waiting after 90s (capture is a multi-second real HackRF
    // RX + demod cycle, plus bridge poll latency) -- honestly report
    // "not confirmed yet" rather than spinning forever.
    const timeoutId = setTimeout(() => setCaptureState((s) => (s === "pending" ? "timeout" : s)), 90000);
    return () => { clearInterval(fastId); clearTimeout(timeoutId); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captureState]);

  const triggerCapture = async () => {
    setCaptureError(null);
    setCaptureState("pending");
    try {
      await api.post("/fpv/capture-request", {});
      toast.success("Capture requested", {
        description: "Waiting for the field bridge to run one real HackRF capture+demod cycle.",
      });
    } catch (e) {
      setCaptureState("idle");
      setCaptureError(formatApiError(e));
      toast.error("Failed to request capture", { description: formatApiError(e) });
    }
  };

  const available = meta?.available;

  return (
    <div data-testid="fpv-video-panel" className="tactical-border" style={{ background: "var(--bg-surface)" }}>
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Signal size={14} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">FPV Video Capture (RX-only, prototype)</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            data-testid="fpv-capture-now-btn"
            onClick={triggerCapture}
            disabled={captureState === "pending"}
            className="px-3 py-1 font-mono text-[10px] font-bold uppercase tracking-widest tactical-border"
            style={{
              color: captureState === "pending" ? "var(--accent-warning)" : "var(--accent-info)",
              borderColor: captureState === "pending" ? "var(--accent-warning)" : "var(--accent-info)",
              cursor: captureState === "pending" ? "wait" : "pointer",
              background: "transparent",
            }}
          >
            {captureState === "pending" ? "◌ CAPTURING…" : "▶ CAPTURE NOW"}
          </button>
          <span
            className="px-2 py-0.5 font-mono text-[10px] font-bold uppercase tracking-widest tactical-border"
            style={{
              color: available ? "var(--accent-success)" : "var(--accent-warning)",
              borderColor: available ? "var(--accent-success)" : "var(--accent-warning)",
            }}
          >
            {available ? "● FRAME CAPTURED" : "◌ NO CAPTURE YET"}
          </span>
        </div>
      </div>
      <div className="p-4 space-y-3">
        {captureState === "pending" && (
          <div className="font-mono text-[10px] p-2 tactical-border"
               style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}>
            Capture requested — waiting for the field bridge (fpv_video_bridge.py --poll)
            to pick it up and run one real capture+demod cycle. This panel updates only
            when a genuinely new frame timestamp is confirmed, not on a fixed timer.
          </div>
        )}
        {captureState === "timeout" && (
          <div className="font-mono text-[10px] p-2 tactical-border"
               style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}>
            No new frame confirmed within 90s. The field bridge may not be running in
            --poll mode, or the HackRF is unavailable. Check field-bridge logs.
            <button onClick={() => setCaptureState("idle")}
                    className="ml-2 underline" style={{ color: "var(--accent-critical)" }}>
              dismiss
            </button>
          </div>
        )}
        {captureError && (
          <div className="font-mono text-[10px]" style={{ color: "var(--accent-critical)" }}>{captureError}</div>
        )}
        {available ? (
          <>
            <img
              key={imgKey}
              src={`${api.defaults.baseURL}/fpv/latest-frame.png?_=${imgKey}`}
              alt="Reconstructed AM-envelope snapshot from analog FPV capture"
              className="w-full tactical-border"
              style={{ background: "#000", imageRendering: "pixelated" }}
              onError={(e) => { e.currentTarget.style.display = "none"; }}
            />
            <div className="grid grid-cols-2 gap-2 font-mono text-[10px] text-slate-400">
              <div>Channel: <span className="text-slate-200">{meta.channel}</span></div>
              <div>Freq: <span className="text-slate-200">{(meta.center_freq_hz / 1e6).toFixed(3)} MHz</span></div>
              <div>Captured: <span className="text-slate-200">{meta.captured_at}</span></div>
              <div>Demod: <span className="text-slate-200">{meta.demod_method}</span></div>
            </div>
            <div
              className="font-mono text-[10px] p-2 tactical-border"
              style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
            >
              Prototype snapshot, not live video. DJI digital video is never decoded.
            </div>
          </>
        ) : (
          <div className="font-mono text-[10px] text-slate-500">
            No capture yet — click CAPTURE NOW.
          </div>
        )}
      </div>
    </div>
  );
}

export default function Payloads() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";
  const [payloads, setPayloads] = useState([]);
  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [gate, setGate] = useState({ open: false, pl: null, broadcast: false, fratricide: false });
  const [authorizing, setAuthorizing] = useState(false);
  // Per-payload operator parameters surfaced in the card:
  //   PL-008 RTH HOME-SPOOF: the FALSE home coordinates injected via DO_SET_HOME.
  //   PL-011 MANEUVER TAKEOVER: the operator-controlled engagement window / continuous.
  //   PL-005 PROPELLER STOP: how many rotors to stop.
  const [spoof, setSpoof] = useState({ lat: "", lon: "", alt: "" });
  const [motorCount, setMotorCount] = useState(4);
  const [takeover, setTakeover] = useState({ duration_s: 8, continuous: false });

  const load = async () => {
    try {
      const [p, d] = await Promise.all([api.get("/payloads"), api.get("/detections")]);
      setPayloads(p.data);
      const active = d.data.filter((x) => x.status === "ACTIVE");
      setDets(active);
      // TASK #119 (OB-04 concurrent multi-drone handling audit): the backend
      // has no single-active-engagement lock — multiple detections can be
      // independently ACTIVE/AWAITING_ACK/authorized at once (see
      // backend/server.py's per-request _arm_tokens/_pending_acks, which are
      // keyed by token/request_id, not by a single global "current target").
      // The only real gap this audit found was here: `target` is a plain
      // dropdown selection that used to go stale whenever the currently
      // selected contact left the ACTIVE list (e.g. it moved to
      // AWAITING_ACK the instant THIS operator deployed against it, or a
      // different operator/bridge event changed its status) while another
      // drone was still concurrently active. A stale `target` id matched no
      // <option>, silently dropped `selectedDet` to undefined, and hid the
      // authorize-target control — exactly the same class of stale-
      // reference bug task #65 already fixed for Signals.jsx's contact
      // selection. Re-derive here on every poll instead of only when empty,
      // so the dropdown always falls back to a currently-active contact
      // when the previous target concurrently drops out of ACTIVE, instead
      // of the operator losing engagement control over a second drone that
      // is still legitimately active.
      setTarget((prev) => {
        if (prev && active.some((x) => x.id === prev)) return prev;
        return active.length ? active[0].id : "";
      });
    } catch (e) { toast.error("Load failed", { description: formatApiError(e) }); }
  };
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []); // eslint-disable-line

  const selectedDet = dets.find((d) => d.id === target);
  const friendlySelected = isFriendly(selectedDet);

  // Friendly-fire interlock: an explicit, visible commander action distinct
  // from "deploy" itself. Calls the real backend endpoint that flips
  // authorized_target on the detection — no client-side bypass of the
  // server-enforced check in /payloads/deploy.
  const toggleAuthorize = async () => {
    if (!selectedDet) return;
    // Routine target authorization can NEVER license firing on a confirmed
    // friendly — the backend refuses it with 403. Don't even attempt it; the
    // deliberate fratricide-override flow is the only path (see the override
    // control rendered in place of this toggle for friendly contacts).
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

  // `iffAck`, when present, is a single-use commander friendly-fire ack minted
  // for THIS target (see doDeployFriendlyOverride). It is the ONLY thing that
  // lets a deploy engage a confirmed friendly; the backend re-verifies it at
  // fire time. When it is present we skip the routine authorized_target gate
  // (a friendly can never satisfy that gate by design).
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
      // consequence of the operator's confirmed deploy action (either the
      // SafetyGate ARM & FIRE -> CONFIRM FIRE sequence for gated payloads,
      // or this direct button click for non-gated ones), same convention as
      // Jamming.jsx's fireJam(). Harmless to fetch unconditionally: the
      // token is single-use/short-TTL and the backend only consumes it when
      // spec.severity === "CRITICAL" or broadcast is true.
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
      // The server no longer claims success the instant the frame hits the
      // WS — it now reports AWAITING_ACK until the rf-bridge confirms it
      // actually wrote the frame to the real serial radio. Reflect that
      // honestly here instead of a blanket "DEPLOYED" toast.
      if (data.tx_bridge_subscribed === false) {
        // Honest false-green guard: the backend accepted the request (HTTP 200,
        // AWAITING_ACK) but NO cema-rf-bridge is subscribed, so nothing was
        // written to any radio — it will TX_TIMEOUT. Translate this into the
        // plain-language "TX subsystem OFFLINE — Bring TX Online" fix (a button
        // for commanders) instead of a raw "start cema-rf-bridge" shell hint.
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
      // deploy is refused because the target is a confirmed friendly (e.g. it
      // flipped to IFF-verified between selection and fire), surface the
      // explicit commander override path instead of a raw error toast.
      if (!iffAck && !broadcast && isFratricideRefusal(e)) {
        toast.error("FRATRICIDE INTERLOCK — routine fire refused", {
          description: "Target is IFF-CONFIRMED FRIENDLY. Engage only via the deliberate commander friendly-fire override.",
        });
        setGate({ open: true, pl, broadcast: false, fratricide: true });
        return;
      }
      // Operator-friendly pre-condition translation: if the fire was blocked
      // because TX is HALTED or range-auth is OFF, show WHY in plain language
      // with the fix as a button (RESUME TX / Bring TX Online) instead of the
      // raw backend "POST /api/emergency/resume" text.
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: load })) return;
      toast.error("Deploy failed", { description: formatApiError(e) });
    }
  };

  // Deliberate, commander-only fratricide override. Called ONLY from the
  // fratricide SafetyGate's confirm (typed ack + checkbox + commander role
  // already enforced there). Mints a single-use, target-bound friendly-fire
  // ack, then deploys carrying it. This is the one path that can engage a
  // confirmed friendly — it is never reachable by clicking the normal buttons.
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
    // override gate. (Broadcast is target-agnostic — target_detection_id is
    // null — so it is unaffected and keeps its existing flow.)
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

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Bomb size={12} className="inline mr-2" strokeWidth={1.5} /> Payload Library
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Cyber-Physical Weapons
          </h1>
        </div>
        <div className="flex items-center gap-3">
          <TargetIcon size={16} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <select
            data-testid="target-select"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            className="tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
          >
            {dets.length === 0 && <option value="">— NO ACTIVE TARGETS —</option>}
            {dets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.callsign} · {d.model} · sys={d.system_id}
                {isFriendly(d)
                  ? " ⚠ IFF-CONFIRMED FRIENDLY"
                  : d.authorized_target ? "" : " (NOT AUTHORIZED)"}
              </option>
            ))}
          </select>
          {selectedDet && !friendlySelected && (
            <button
              data-testid="authorize-target-toggle"
              onClick={toggleAuthorize}
              disabled={authorizing}
              className={`flex items-center gap-2 px-3 py-2 tactical-border font-mono text-[10px] uppercase tracking-widest transition-colors disabled:opacity-40 ${
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
            // Routine authorize is impossible for a confirmed friendly — show a
            // hard fratricide indicator here instead of the authorize toggle.
            <span
              data-testid="friendly-target-indicator"
              className="flex items-center gap-2 px-3 py-2 border-2 font-mono text-[10px] font-bold uppercase tracking-widest"
              style={{ color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
              title="IFF-confirmed friendly — engaging is fratricide and requires the deliberate commander override."
            >
              <ShieldAlert size={14} strokeWidth={1.75} />
              IFF-CONFIRMED FRIENDLY
            </span>
          )}
        </div>
      </div>

      <RangeAuthorizationControl effect="mavlink" label="MAVLINK PAYLOAD DEPLOY" />

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
              has replied to IFF interrogation. Routine authorize and single-target deploy are refused for this
              contact. {isCommander
                ? "A single-target DEPLOY below opens the deliberate, single-use commander friendly-fire override — it is not a normal deploy."
                : "Only a commander may deliberately override this; your role cannot engage a confirmed friendly."}
            </div>
          </div>
        </div>
      )}

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

      <FpvVideoPanel />

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
      <SafetyGate
        open={gate.open}
        payloadName={gate.pl?.name}
        severity={gate.pl?.severity}
        fratricide={gate.fratricide}
        isCommander={isCommander}
        friendlyCallsign={gate.fratricide ? selectedDet?.callsign : undefined}
        onClose={() => setGate({ open: false, pl: null, broadcast: false, fratricide: false })}
        onConfirm={() => {
          const { pl, broadcast, fratricide } = gate;
          setGate({ open: false, pl: null, broadcast: false, fratricide: false });
          if (fratricide) {
            doDeployFriendlyOverride(pl);
          } else {
            doDeploy(pl, broadcast);
          }
        }}
      />
    </div>
  );
}
