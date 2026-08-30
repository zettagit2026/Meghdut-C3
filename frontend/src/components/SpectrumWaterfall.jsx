import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { Activity } from "lucide-react";
import {
  dbmToRGB,
  INFERNO_STOPS,
  SPECTRUM_FLOOR_DBM,
  SPECTRUM_CEIL_DBM,
} from "@/lib/spectrumColormap";

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
      // ABSOLUTE calibration: every color maps to a fixed dBm via the shared
      // colormap (floor/ceiling in lib/spectrumColormap.js) -- no per-frame
      // auto-scaling, so a given color always means the same power level.
      const img = ctx.createImageData(bins, 80);
      for (let y = 0; y < 80; y++) {
        const row = rows[y] || rows[rows.length - 1];
        for (let x = 0; x < bins; x++) {
          const v = row[x] ?? SPECTRUM_FLOOR_DBM;
          const [r, g, b] = dbmToRGB(v);
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
          role="status"
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
        <div className="flex gap-2">
          <canvas
            ref={canvasRef}
            data-testid="spectrum-waterfall-canvas"
            style={{ width: "100%", height: "240px", imageRendering: "pixelated", display: "block", opacity: stale ? 0.35 : 1 }}
          />
          {/* Colorbar legend: without a dBm->color scale the waterfall is not
              interpretable as a measurement. Absolute (fixed floor/ceiling),
              so it reads the same every frame. */}
          <div
            data-testid="spectrum-waterfall-colorbar"
            className="flex items-stretch gap-1 shrink-0"
            aria-label={`power scale ${SPECTRUM_CEIL_DBM} to ${SPECTRUM_FLOOR_DBM} dBm`}
          >
            <div
              style={{
                width: "10px",
                height: "240px",
                borderRadius: "2px",
                background: `linear-gradient(to top, ${INFERNO_STOPS.join(",")})`,
                opacity: stale ? 0.35 : 1,
              }}
            />
            <div className="flex flex-col justify-between font-mono text-[11px] text-slate-500 tabular-nums">
              <span>{SPECTRUM_CEIL_DBM}</span>
              <span className="text-slate-600">dBm</span>
              <span>{SPECTRUM_FLOOR_DBM}</span>
            </div>
          </div>
        </div>
        <div className="flex justify-between font-mono text-[10px] text-slate-500 mt-1">
          <span>freq bins →</span>
          <span>{meta.bins} bins</span>
        </div>
      </div>
    </div>
  );
}
