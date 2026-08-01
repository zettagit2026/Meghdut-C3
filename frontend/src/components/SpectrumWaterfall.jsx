import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Activity } from "lucide-react";

// Power (dBm) -> color, cool-to-hot heatmap
function dbmToColor(v, min, max) {
  const t = Math.max(0, Math.min(1, (v - min) / (max - min || 1)));
  // dark blue -> cyan -> yellow -> red
  const stops = [
    [0.0, [10, 10, 40]],
    [0.35, [0, 120, 200]],
    [0.6, [0, 220, 200]],
    [0.8, [255, 220, 0]],
    [1.0, [255, 40, 40]],
  ];
  for (let i = 0; i < stops.length - 1; i++) {
    const [t0, c0] = stops[i];
    const [t1, c1] = stops[i + 1];
    if (t >= t0 && t <= t1) {
      const f = (t - t0) / (t1 - t0 || 1);
      return [
        Math.round(c0[0] + (c1[0] - c0[0]) * f),
        Math.round(c0[1] + (c1[1] - c0[1]) * f),
        Math.round(c0[2] + (c1[2] - c0[2]) * f),
      ];
    }
  }
  return [255, 40, 40];
}

const POLL_INTERVAL_MS = 1500;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 3;

export default function SpectrumWaterfall() {
  const canvasRef = useRef(null);
  const historyRef = useRef([]); // array of rows (each an array of dBm bins), newest first
  const [meta, setMeta] = useState({ bins: 0, source: "NONE" });
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  useEffect(() => {
    let stopped = false;
    const poll = async () => {
      try {
        const { data } = await api.get("/spectrum/waterfall", { params: { bins: 96, rows: 1 } });
        if (stopped) return;
        setMeta({ bins: data.bins, source: data.source });
        setLastSuccessAt(Date.now());
        setConsecutiveFailures(0);
        const newRows = data.rows || [];
        historyRef.current = [...newRows, ...historyRef.current].slice(0, 80);
        draw();
      } catch {
        if (stopped) return;
        setConsecutiveFailures((n) => n + 1);
      }
    };
    const draw = () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rows = historyRef.current;
      if (!rows.length) return;
      const bins = rows[0].length;
      const h = rows.length;
      canvas.width = bins;
      canvas.height = 80;
      const ctx = canvas.getContext("2d");
      const flat = rows.flat();
      const min = Math.min(...flat);
      const max = Math.max(...flat);
      const img = ctx.createImageData(bins, 80);
      for (let y = 0; y < 80; y++) {
        const row = rows[y] || rows[rows.length - 1];
        for (let x = 0; x < bins; x++) {
          const v = row[x] ?? min;
          const [r, g, b] = dbmToColor(v, min, max);
          const idx = (y * bins + x) * 4;
          img.data[idx] = r;
          img.data[idx + 1] = g;
          img.data[idx + 2] = b;
          img.data[idx + 3] = 255;
        }
      }
      ctx.putImageData(img, 0, 0);
    };
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => { stopped = true; clearInterval(id); };
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const stale = staleByAge || staleByFailures || neverSucceeded;
  const isReal = meta.source === "HACKRF" && !stale;

  return (
    <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Activity size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">RF Spectrum Waterfall</span>
        </div>
        <span
          className="font-mono text-[10px] uppercase tracking-widest px-2 py-0.5 tactical-border"
          style={{
            color: stale ? "var(--accent-critical)" : isReal ? "var(--accent-success)" : "var(--accent-warning)",
            borderColor: stale ? "var(--accent-critical)" : isReal ? "var(--accent-success)" : "var(--accent-warning)",
          }}
        >
          {stale ? "◌ STALE" : isReal ? "● LIVE HACKRF" : "○ NO SIGNAL"}
        </span>
      </div>
      <div className="p-3">
        <canvas
          ref={canvasRef}
          data-testid="spectrum-waterfall-canvas"
          style={{ width: "100%", height: "240px", imageRendering: "pixelated", display: "block", opacity: stale ? 0.35 : 1 }}
        />
        <div className="flex justify-between font-mono text-[10px] text-slate-500 mt-1">
          <span>freq bins →</span>
          <span>{meta.bins} bins</span>
        </div>
      </div>
    </div>
  );
}
