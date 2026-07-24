import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { AudioWaveform } from "lucide-react";

// Time-domain oscilloscope trace, driven by real spectrum peak power.
// Peak dBm -> carrier amplitude, so the sine visibly grows/shrinks with
// real signal strength instead of being purely decorative.
const CARRIER_HZ = 14; // visual cycles across the trace width, not an RF frequency
const POINTS = 600;
const PHASE_STEP = 0.55; // fast scroll, like a real scope's continuous sweep

// Calibrated against this deployment's actual measured range (2026-07-16/17
// field tests): quiet/just-above-confirm-threshold sits around -70 to -65dBm,
// strong contacts (DJI close-in, SiK near-field) have hit -6 to -22dBm.
// Tightening the span to this real range (vs. the earlier generic -70/55)
// makes weak-vs-strong contacts visually distinct instead of everything
// pinning near max amplitude.
const FLOOR_DBM = -72;
const SPAN_DB = 68;
const AMP_MIN = 0.05;
const AMP_MAX = 0.95;

// How fast the displayed amplitude chases the polled target. Higher = snappier,
// more like a real analyzer's near-instant response; lower = smoother/slower.
const AMP_LERP = 0.18;
const NOISE_FLOOR_PX = 1.6; // subtle per-point "grass" jitter, like real RF noise on a scope

export default function SpectrumScope() {
  const canvasRef = useRef(null);
  const phaseRef = useRef(0);
  const targetAmpRef = useRef(0.08);
  const displayAmpRef = useRef(0.08);
  const [meta, setMeta] = useState({ source: "NONE", peakDbm: null });
  const rafRef = useRef(null);

  useEffect(() => {
    let stopped = false;

    const pollAmplitude = async () => {
      try {
        // Drive amplitude from CONFIRMED detections, not the raw last-ingested
        // waterfall row. hackrf_rx.py cycles through SiK-915 / DJI-2.4G / DJI-5.8G
        // and overwrites the same "last ingest" each time it posts a band, so a
        // waterfall-based amplitude could show a flat trace right when the DJI's
        // own 2.4GHz reading isn't the most recently ingested band, even though
        // it's actively being detected. Detections persist per contact instead.
        const { data } = await api.get("/detections");
        if (stopped) return;
        const now = Date.now();
        const recent = (data || []).filter((d) => {
          const age = now - new Date(d.last_seen).getTime();
          return (d.source === "HACKRF" || d.source === "SIK_RADIO" || d.source === "SIK_RF_HEURISTIC") && age < 10000;
        });
        if (recent.length) {
          const peak = Math.max(...recent.map((d) => d.rssi_dbm));
          const norm = Math.max(0, Math.min(1, (peak - FLOOR_DBM) / SPAN_DB));
          targetAmpRef.current = AMP_MIN + norm * (AMP_MAX - AMP_MIN);
          setMeta({ source: "HACKRF", peakDbm: peak });
        } else {
          targetAmpRef.current = AMP_MIN;
          setMeta({ source: "NONE", peakDbm: null });
        }
      } catch { /* keep last amplitude, silent */ }
    };

    const draw = () => {
      const canvas = canvasRef.current;
      if (!canvas || stopped) return;
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);

      // recessive grid, per dataviz mark spec (grid stays quiet, trace carries the data)
      ctx.strokeStyle = "rgba(148, 163, 184, 0.12)";
      ctx.lineWidth = 1;
      for (let gx = 0; gx <= 8; gx++) {
        const x = (gx / 8) * w;
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
      }
      ctx.beginPath(); ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();

      // Chase the polled target smoothly frame-to-frame instead of snapping
      // on each 1.5s poll, like a real analyzer's continuously-updating trace.
      displayAmpRef.current += (targetAmpRef.current - displayAmpRef.current) * AMP_LERP;
      const amp = displayAmpRef.current;
      const mid = h / 2;

      ctx.beginPath();
      for (let i = 0; i <= POINTS; i++) {
        const x = (i / POINTS) * w;
        const t = i / POINTS;
        const jitter = (Math.random() - 0.5) * NOISE_FLOOR_PX; // real-RF "grass" texture
        const y = mid - Math.sin(t * Math.PI * 2 * CARRIER_HZ + phaseRef.current) * amp * (mid - 6) + jitter;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }
      // canvas can't read CSS custom properties directly; this matches --accent-info (#00F0FF)
      ctx.strokeStyle = "#00F0FF";
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.shadowColor = "#00F0FF";
      ctx.shadowBlur = 4;
      ctx.stroke();
      ctx.shadowBlur = 0;

      phaseRef.current += PHASE_STEP;
      rafRef.current = requestAnimationFrame(draw);
    };

    pollAmplitude();
    const pollId = setInterval(pollAmplitude, 1000);
    rafRef.current = requestAnimationFrame(draw);
    return () => {
      stopped = true;
      clearInterval(pollId);
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  return (
    <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <AudioWaveform size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">RF Scope (Time Domain)</span>
        </div>
        <span
          className="font-mono text-[10px] uppercase tracking-widest px-2 py-0.5 tactical-border"
          style={{
            color: meta.source === "HACKRF" ? "var(--accent-success)" : "var(--accent-warning)",
            borderColor: meta.source === "HACKRF" ? "var(--accent-success)" : "var(--accent-warning)",
          }}
        >
          {meta.source === "HACKRF" ? "● LIVE HACKRF" : "○ NO SIGNAL"}
          {meta.peakDbm != null && <span className="ml-2 text-slate-400">{meta.peakDbm.toFixed(1)} dBm pk</span>}
        </span>
      </div>
      <div className="p-3">
        <canvas
          ref={canvasRef}
          data-testid="spectrum-scope-canvas"
          style={{ width: "100%", height: "160px", display: "block" }}
        />
        <div className="font-mono text-[10px] text-slate-500 mt-1">
          Carrier amplitude tracks real peak signal power — not a decorative animation.
        </div>
      </div>
    </div>
  );
}
