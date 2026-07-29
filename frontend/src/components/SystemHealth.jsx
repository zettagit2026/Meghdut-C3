import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { HeartPulse, ShieldAlert } from "lucide-react";

const DOT = (ok) => ({
  color: ok ? "var(--accent-success)" : "var(--accent-critical)",
});

export default function SystemHealth() {
  const [h, setH] = useState(null);

  useEffect(() => {
    let id;
    const load = async () => {
      try {
        const { data } = await api.get("/health");
        setH(data);
      } catch { /* silent */ }
    };
    load();
    id = setInterval(load, 3000);
    return () => clearInterval(id);
  }, []);

  const rows = [
    ["Backend API", h?.backend],
    ["MongoDB", h?.mongo],
    ["HackRF RX", h?.hackrf],
    ["SiK Radio", h?.sik_radio],
  ];

  return (
    <div
      data-testid="system-health"
      className="tactical-border"
      style={{ background: "var(--bg-surface)" }}
    >
      <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <HeartPulse size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
          <span className="font-mono text-xs uppercase tracking-widest">System Health</span>
        </div>
        <span className="font-mono text-[10px] text-slate-500">poll 3s</span>
      </div>
      {h?.tx_halted && (
        <div
          data-testid="tx-halted-banner"
          className="tactical-border-b px-4 py-2 flex items-center gap-2 pulse-crit"
          style={{ background: "#FF3B30", color: "black" }}
        >
          <ShieldAlert size={14} strokeWidth={2} />
          <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
            TX HALTED — commander must POST /emergency/resume
          </span>
        </div>
      )}
      <div className="p-4 space-y-2 font-mono text-xs">
        {rows.map(([label, ok]) => (
          <div key={label} className="flex items-center justify-between">
            <span className="text-slate-400">{label}</span>
            <span data-testid={`hs-${label.toLowerCase().replace(/\s+/g, "-")}`}
                  style={DOT(!!ok)} className="font-bold uppercase tracking-widest text-[10px]">
              {ok ? "● UP" : "● DOWN"}
            </span>
          </div>
        ))}
        <div className="tactical-border-t pt-2 mt-2 flex items-center justify-between text-slate-500">
          <span>WS clients</span>
          <span className="text-slate-300">{h?.ws_clients ?? "?"}</span>
        </div>
        <div className="flex items-center justify-between text-slate-500">
          <span>Packets TX</span>
          <span className="text-slate-300">{h?.total_packets_tx ?? "?"}</span>
        </div>
      </div>
    </div>
  );
}
