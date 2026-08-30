import { useEffect, useRef, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Bomb, AlertTriangle, Target as TargetIcon, ShieldCheck, ShieldOff, Signal } from "lucide-react";
import SafetyGate, { SAFETY_GATED } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import { THREAT_COLOR } from "@/lib/threatLevels";

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
          <div className="font-mono text-[10px] text-red-400">{captureError}</div>
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
  const [payloads, setPayloads] = useState([]);
  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [gate, setGate] = useState({ open: false, pl: null, broadcast: false });
  const [authorizing, setAuthorizing] = useState(false);

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

  // Friendly-fire interlock: an explicit, visible commander action distinct
  // from "deploy" itself. Calls the real backend endpoint that flips
  // authorized_target on the detection — no client-side bypass of the
  // server-enforced check in /payloads/deploy.
  const toggleAuthorize = async () => {
    if (!selectedDet) return;
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

  const doDeploy = async (pl, broadcast) => {
    if (!broadcast && !target) { toast.error("No active target selected"); return; }
    if (!broadcast && selectedDet && !selectedDet.authorized_target) {
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
      const { data } = await api.post("/payloads/deploy", {
        payload_id: pl.id,
        target_detection_id: broadcast ? null : target,
        broadcast,
        arm_token,
      });
      // The server no longer claims success the instant the frame hits the
      // WS — it now reports AWAITING_ACK until the rf-bridge confirms it
      // actually wrote the frame to the real serial radio. Reflect that
      // honestly here instead of a blanket "DEPLOYED" toast.
      if (data.status === "AWAITING_ACK") {
        toast.info(`${pl.name} SENT — awaiting bridge ACK`, {
          description: `pkt ${data.length}B · ${broadcast ? "BROADCAST" : `tgt sys=${data.target_system}`} · req ${data.request_id?.slice(0, 8)}`,
        });
      } else {
        toast.success(`${pl.name} DEPLOYED`, {
          description: `pkt ${data.length}B · ${broadcast ? "BROADCAST" : `tgt sys=${data.target_system}`}`,
        });
      }
      load();
    } catch (e) { toast.error("Deploy failed", { description: formatApiError(e) }); }
  };

  const deploy = (pl, broadcast) => {
    if (SAFETY_GATED.has(pl.id)) {
      setGate({ open: true, pl, broadcast });
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
            className="bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus-accent-info"
          >
            {dets.length === 0 && <option value="">— NO ACTIVE TARGETS —</option>}
            {dets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.callsign} · {d.model} · sys={d.system_id}
                {d.authorized_target ? "" : " (NOT AUTHORIZED)"}
              </option>
            ))}
          </select>
          {selectedDet && (
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
        </div>
      </div>

      <RangeAuthorizationControl effect="mavlink" label="MAVLINK PAYLOAD DEPLOY" />

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "color-mix(in srgb, var(--accent-critical) 10%, var(--bg-surface))" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
          Payload deployment generates a valid MAVLink COMMAND_LONG frame with real CRC-16/MCRF4XX and
          transmits it on the internal WebSocket bus. When routed to a real SDR TX chain this becomes a
          kinetic/logical attack. Evaluation build only.
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
            <div className="mt-4 grid grid-cols-2 gap-0 tactical-border">
              <button
                data-testid={`deploy-target-${p.id}`}
                onClick={() => deploy(p, false)}
                disabled={p.id === "PL-010"}
                title={p.id === "PL-010" ? "Broadcast-only payload — no single-target mode" : undefined}
                className="tactical-border-r px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover-accent-info transition-colors scanline-btn disabled:opacity-30 disabled:cursor-not-allowed"
              >
                DEPLOY → TGT
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
        onClose={() => setGate({ open: false, pl: null, broadcast: false })}
        onConfirm={() => {
          const { pl, broadcast } = gate;
          setGate({ open: false, pl: null, broadcast: false });
          doDeploy(pl, broadcast);
        }}
      />
    </div>
  );
}
