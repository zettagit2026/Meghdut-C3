import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Waves, CheckCircle2, Signal } from "lucide-react";
import SpectrumWaterfall from "@/components/SpectrumWaterfall";
import SpectrumScope from "@/components/SpectrumScope";

// Analog FPV video bridge panel (field-bridge/fpv_video_bridge.py), relocated
// here from the now-retired Payloads.jsx (a weapons page), where it was
// misfiled and unreachable. It is a passive-adjacent, RX-only recon capture
// (HackRF RX + AM-envelope demod of an analog FPV transmitter) — not a
// cyber-physical weapons effect — so it belongs on this DETECT/analysis
// page, not with deployable payloads.
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

const STAGES = [
  { key: "CAPTURE",   what: "Wide-band RF acquisition via multi-channel SDR",       how: "IQ streams @ 25 MSPS across sub-GHz to 6 GHz" },
  { key: "ANALYZE",   what: "FFT & spectrogram feature extraction",                 how: "AMC + energy detection to isolate carriers" },
  { key: "SEGREGATE", what: "Per-emitter clustering",                               how: "DoA + fingerprinting separates concurrent UAVs" },
  { key: "DEMODULATE",what: "Recover baseband symbols",                             how: "FHSS/OFDM/GFSK/QPSK demod chains" },
  { key: "DECODE",    what: "Frame boundary + protocol ID",                         how: "MAVLink v1/v2 · DJI · ExpressLRS parsers" },
  { key: "DECRYPT",   what: "Break weak / known-key encryption",                    how: "Known-plaintext + weak-CSPRNG attacks" },
  { key: "EXPLOIT",   what: "Craft & inject spoofed commands",                      how: "Broadcast COMMAND_LONG · MAVFTP wipe · BMS abuse" },
];

export default function Signals() {
  const [dets, setDets] = useState([]);
  const [selected, setSelected] = useState(null);
  // Deep-link support: Dashboard/DetectionHistory link here as
  // /signals?contact=<id> so an operator clicking a CEMA-stage cell on the
  // main tactical views lands directly on that contact's pipeline trace
  // instead of having to hunt for it in the contacts rail.
  const [searchParams] = useSearchParams();
  const deepLinkedId = searchParams.get("contact");
  const appliedDeepLinkIdRef = useRef(null);

  const load = async () => {
    try {
      const { data } = await api.get("/detections");
      setDets(data);
      setSelected((prev) => {
        if (deepLinkedId && appliedDeepLinkIdRef.current !== deepLinkedId && data.some((d) => d.id === deepLinkedId)) {
          appliedDeepLinkIdRef.current = deepLinkedId;
          return deepLinkedId;
        }
        if (!prev && data.length) {
          return data[0].id;
        }
        return prev;
      });
    } catch (e) {
      toast.error("Load failed", { description: formatApiError(e) });
    }
  };

  useEffect(() => { load(); const id = setInterval(load, 4000); return () => clearInterval(id); }, []); // eslint-disable-line

  // The polling effect above intentionally has an empty dep array so the
  // interval isn't torn down/recreated on every render; its `load` closure
  // is therefore fixed at mount and never sees a later `deepLinkedId`. This
  // effect re-runs `load()` (with a fresh closure) whenever the deep-link
  // target changes, so navigating from ?contact=A to ?contact=B without a
  // remount still jumps to the new contact.
  useEffect(() => { if (deepLinkedId) load(); }, [deepLinkedId]); // eslint-disable-line

  const current = dets.find((d) => d.id === selected);

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Waves size={12} className="inline mr-2" strokeWidth={1.5} /> Signal Analysis
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            CEMA 7-Stage Pipeline
          </h1>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
        <SpectrumWaterfall />
        <SpectrumScope />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-0 tactical-border">
        <div className="lg:col-span-1 tactical-border-r" style={{ background: "var(--bg-surface)" }}>
          <div className="tactical-border-b px-4 py-3 font-mono text-[10px] uppercase tracking-widest text-slate-400">
            Contacts
          </div>
          <div className="max-h-[600px] overflow-y-auto">
            {dets.map((d) => (
              <button
                key={d.id}
                data-testid={`select-${d.id}`}
                onClick={() => setSelected(d.id)}
                className={`w-full text-left p-3 tactical-border-b font-mono text-xs transition-colors ${
                  d.id === selected ? "" : "text-slate-400 hover-surface hover:text-[var(--text-primary)]"
                }`}
                style={d.id === selected ? { background: "var(--hover-surface)", color: "var(--accent-info)" } : undefined}
              >
                <div className="flex items-center justify-between">
                  <span>{d.callsign}</span>
                  <span className="text-[10px] text-slate-500">{d.cema_stage}</span>
                </div>
                <div className="text-[10px] text-slate-500 mt-0.5">{d.model}</div>
              </button>
            ))}
            {dets.length === 0 && (
              <div className="p-4 font-mono text-xs text-slate-600">No contacts.</div>
            )}
          </div>
        </div>

        <div className="lg:col-span-3 p-6" style={{ background: "var(--bg-surface)" }}>
          {!current && (
            <div className="font-mono text-xs text-slate-600">Select a contact to trace pipeline.</div>
          )}
          {current && (
            <>
              <div className="flex items-start justify-between mb-6">
                <div>
                  <div className="font-heading font-black text-3xl tracking-tighter uppercase flex items-center gap-3 flex-wrap">
                    <span>
                      {current.callsign} <span className="text-slate-500">·</span>{" "}
                      <span style={{ color: "var(--accent-info)" }}>{current.model}</span>
                    </span>
                    <span
                      className="font-mono text-xs font-bold uppercase tracking-widest px-2 py-1 tactical-border"
                      style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
                    >
                      {STAGES[current.cema_stage_index]?.key || "COMPLETE"}
                      {" "}{Math.min(current.cema_stage_index + 1, STAGES.length)}/{STAGES.length}
                    </span>
                  </div>
                  <div className="font-mono text-xs text-slate-500 mt-1">
                    PROTOCOL: <span className="text-slate-300">{current.protocol}</span> · SYS-ID: <span className="text-slate-300">{current.system_id}</span> ·
                    ENCRYPT: <span className="text-slate-300">{current.encrypted ? "YES" : "NONE"}</span>
                    {current.source && <> · SOURCE: <span className="text-slate-300">{current.source}</span></>}
                  </div>
                </div>
              </div>

              <div className="space-y-0 tactical-border">
                {STAGES.map((s, i) => {
                  const done = i < current.cema_stage_index;
                  const active = i === current.cema_stage_index;
                  const stageColor = done ? "var(--accent-success)" : active ? "var(--accent-info)" : undefined;
                  return (
                    <div key={s.key}
                         className="p-4 tactical-border-b last:border-b-0 flex items-start gap-4"
                         style={active ? {
                           borderLeft: "3px solid var(--accent-info)",
                           background: "color-mix(in srgb, var(--accent-info) 12%, var(--bg-surface))",
                         } : undefined}>
                      <div className={`w-10 h-10 tactical-border flex items-center justify-center font-heading font-black text-lg ${!done && !active ? "text-slate-600" : ""}`}
                           style={{ color: stageColor, borderColor: stageColor }}>
                        {done ? <CheckCircle2 size={18} strokeWidth={1.5}/> : i + 1}
                      </div>
                      <div className="flex-1">
                        <div className="flex items-center gap-3">
                          <span className={`font-heading font-black text-lg uppercase tracking-tighter ${!active && !done ? "text-slate-500" : ""}`}
                                style={{ color: stageColor }}>
                            {s.key}
                          </span>
                          {active && <span className="font-mono text-[10px] text-slate-500 blink">● PROCESSING</span>}
                          {done && <span className="font-mono text-[10px]" style={{ color: "var(--accent-success)" }}>✓ COMPLETE</span>}
                        </div>
                        <div className="font-mono text-xs text-slate-300 mt-1">{s.what}</div>
                        <div className="font-mono text-[10px] text-slate-500 mt-0.5">{s.how}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>

      {/* FPV RECON --------------------------------------------------------
          Relocated from the now-retired Payloads (weapons) page, where this
          RX-only analog FPV video capture was misfiled and unreachable. It
          belongs here, on the DETECT/analysis side: it is a recon capture
          (HackRF RX + AM-envelope demod of an analog FPV transmitter), not
          a cyber-physical weapons effect. The backend /fpv/* endpoints take
          no contact id -- the field bridge listens on one fixed analog
          channel -- so this is rendered as its own contact-independent
          section rather than wired to the contact selected above. */}
      <div data-testid="fpv-recon-section" className="space-y-3">
        <div className="flex items-center gap-2">
          <Signal size={14} strokeWidth={1.5} style={{ color: "var(--accent-warning)" }} />
          <h2 className="font-heading font-black text-2xl uppercase tracking-tighter">FPV Recon</h2>
        </div>
        <div
          className="font-mono text-[10px] p-3 tactical-border"
          style={{ color: "var(--accent-warning)", borderColor: "var(--accent-warning)" }}
        >
          UNTESTED PROTOTYPE — RX-only analog FPV video capture (HackRF + AM-envelope demod).
          Not validated against a live analog FPV transmitter. Snapshot-only, never continuous
          streaming, and DJI digital / FHSS video is never decoded here. Global capability,
          independent of the contact selected above.
        </div>
        <FpvVideoPanel />
      </div>
    </div>
  );
}
