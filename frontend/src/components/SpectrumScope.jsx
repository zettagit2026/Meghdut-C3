import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { AudioWaveform } from "lucide-react";

// Time-domain oscilloscope trace, driven by real spectrum peak power.
// Peak dBm -> carrier amplitude, so the sine visibly grows/shrinks with
// real signal strength instead of being purely decorative.
const CARRIER_HZ = 3; // visual cycles across the trace width, not an RF frequency
const POINTS = 400;

export default function SpectrumScope() {
  const canvasRef = useRef(null);
  const phaseRef = useRef(0);
  const ampRef = useRef(0.15);
  const [meta, setMeta] = useState({ source: "SIM", peakDbm: null });
  const rafRef = useRef(null);

  useEffect(() => {
    let stopped = false;

    const pollAmplitude = async () => {
      try {
        const { data } = await api.get("/spectrum/waterfall", { params: { bins: 96, rows: 1 } });
        if (stopped) return;
        const row = data.rows?.[0] || [];
        const peak = row.length ? Math.max(...row) : -80;
        const floor = row.length ? Math.min(...row) : -90;
        // Normalize peak power to a 0.08-0.95 amplitude band so a hot signal
        // visibly fills the scope and a quiet one stays a flat-ish line.
        const norm = Math.max(0, Math.min(1, (peak - floor) / 40));
        ampRef.current = 0.08 + norm * 0.87;
        setMeta({ source: data.source, peakDbm: peak });
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

      // the sine trace itself: single series, one hue, 2px line
      const amp = ampRef.current;
      const mid = h / 2;
      ctx.beginPath();
      for (let i = 0; i <= POINTS; i++) {
        const x = (i / POINTS) * w;
        const t = i / POINTS;
        const y = mid - Math.sin(t * Math.PI * 2 * CARRIER_HZ + phaseRef.current) * amp * (mid - 6);
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

      phaseRef.current += 0.08;
      rafRef.current = requestAnimationFrame(draw);
    };

    pollAmplitude();
    const pollId = setInterval(pollAmplitude, 1500);
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
          {meta.source === "HACKRF" ? "● LIVE HACKRF" : "○ SIMULATED"}
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
