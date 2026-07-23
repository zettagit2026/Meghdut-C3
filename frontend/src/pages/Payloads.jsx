import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Bomb, AlertTriangle, Target as TargetIcon } from "lucide-react";
import SafetyGate, { SAFETY_GATED } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";

const SEV_COLOR = {
  LOW: "var(--accent-success)",
  MEDIUM: "var(--accent-warning)",
  HIGH: "#FF8A00",
  CRITICAL: "var(--accent-critical)",
};

const CAT_LABEL = {
  kinetic: "KINETIC",
  logical: "LOGICAL",
  protocol: "PROTOCOL",
  denial: "DENIAL",
};

export default function Payloads() {
  const [payloads, setPayloads] = useState([]);
  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [gate, setGate] = useState({ open: false, pl: null, broadcast: false });

  const load = async () => {
    try {
      const [p, d] = await Promise.all([api.get("/payloads"), api.get("/detections")]);
      setPayloads(p.data);
      const active = d.data.filter((x) => x.status === "ACTIVE");
      setDets(active);
      if (!target && active.length) setTarget(active[0].id);
    } catch (e) { toast.error("Load failed", { description: formatApiError(e) }); }
  };
  useEffect(() => { load(); const id = setInterval(load, 5000); return () => clearInterval(id); }, []); // eslint-disable-line

  const doDeploy = async (pl, broadcast) => {
    if (!broadcast && !target) { toast.error("No active target selected"); return; }
    try {
      const { data } = await api.post("/payloads/deploy", {
        payload_id: pl.id,
        target_detection_id: broadcast ? null : target,
        broadcast,
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
            className="bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
          >
            {dets.length === 0 && <option value="">— NO ACTIVE TARGETS —</option>}
            {dets.map((d) => (
              <option key={d.id} value={d.id}>
                {d.callsign} · {d.model} · sys={d.system_id}
              </option>
            ))}
          </select>
        </div>
      </div>

      <RangeAuthorizationControl effect="mavlink" label="MAVLINK PAYLOAD DEPLOY" />

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "#1A0A08" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
          Payload deployment generates a valid MAVLink COMMAND_LONG frame with real CRC-16/MCRF4XX and
          transmits it on the internal WebSocket bus. When routed to a real SDR TX chain this becomes a
          kinetic/logical attack. Evaluation build only.
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-0 tactical-border">
        {payloads.map((p, i) => (
          <div
            key={p.id}
            data-testid={`payload-${p.id}`}
            className={`p-5 tactical-border-r tactical-border-b ${i % 3 === 2 ? "border-r-0" : ""}`}
            style={{ background: "var(--bg-surface)" }}
          >
            <div className="flex items-center justify-between mb-2">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
                {CAT_LABEL[p.category]} · {p.id}
              </span>
              <span
                className="px-2 py-0.5 tactical-border font-mono font-bold text-[10px]"
                style={{ color: SEV_COLOR[p.severity], borderColor: SEV_COLOR[p.severity] }}
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
            <div className="mt-4 grid grid-cols-2 gap-0 tactical-border">
              <button
                data-testid={`deploy-target-${p.id}`}
                onClick={() => deploy(p, false)}
                disabled={p.id === "PL-010"}
                className="tactical-border-r px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn disabled:opacity-30"
              >
                DEPLOY → TGT
              </button>
              <button
                data-testid={`deploy-broadcast-${p.id}`}
                onClick={() => deploy(p, true)}
                className="px-3 py-2 font-mono text-[10px] uppercase tracking-widest hover:bg-[#FF3B30] hover:text-black transition-colors scanline-btn"
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
