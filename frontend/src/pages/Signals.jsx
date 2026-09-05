import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Waves, CheckCircle2 } from "lucide-react";
import SpectrumWaterfall from "@/components/SpectrumWaterfall";
import SpectrumScope from "@/components/SpectrumScope";

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
    </div>
  );
}
