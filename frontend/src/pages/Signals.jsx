import { useEffect, useRef, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Waves, Upload, ChevronRight, CheckCircle2 } from "lucide-react";
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
  const fileRef = useRef(null);

  const load = async () => {
    try {
      const { data } = await api.get("/detections");
      setDets(data);
      if (!selected && data.length) setSelected(data[0].id);
    } catch (e) {
      toast.error("Load failed", { description: formatApiError(e) });
    }
  };

  useEffect(() => { load(); const id = setInterval(load, 4000); return () => clearInterval(id); }, []); // eslint-disable-line

  const current = dets.find((d) => d.id === selected);

  const advance = async () => {
    if (!current) return;
    try {
      await api.post(`/detections/${current.id}/cema-advance`);
      toast.success("CEMA stage advanced");
      load();
    } catch (e) { toast.error("Advance failed", { description: formatApiError(e) }); }
  };

  const upload = async (e) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const fd = new FormData();
    fd.append("file", f);
    try {
      const { data } = await api.post("/detections/upload", fd, {
        headers: { "Content-Type": "multipart/form-data" },
      });
      toast.success("Ingest complete", {
        description: `${data.upload_meta.file_type} · ${data.callsign}`,
      });
      load();
      setSelected(data.id);
    } catch (err) {
      toast.error("Upload failed", { description: formatApiError(err) });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

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
        <div className="flex items-center gap-2">
          <input
            ref={fileRef}
            data-testid="iq-upload-input"
            type="file"
            accept=".iq,.bin,.pcap,.dat,.raw"
            className="hidden"
            onChange={upload}
          />
          <button
            data-testid="iq-upload-btn"
            onClick={() => fileRef.current?.click()}
            className="flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest hover:bg-white hover:text-black transition-colors scanline-btn"
          >
            <Upload size={14} strokeWidth={1.5} /> INGEST IQ / PCAP
          </button>
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
                  d.id === selected ? "bg-[#0F1626] text-[#00F0FF]" : "text-slate-400 hover:bg-[#0F1626] hover:text-white"
                }`}
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
                  <div className="font-heading font-black text-3xl tracking-tighter uppercase">
                    {current.callsign} <span className="text-slate-500">·</span>{" "}
                    <span style={{ color: "var(--accent-info)" }}>{current.model}</span>
                  </div>
                  <div className="font-mono text-xs text-slate-500 mt-1">
                    PROTOCOL: <span className="text-slate-300">{current.protocol}</span> · SYS-ID: <span className="text-slate-300">{current.system_id}</span> ·
                    ENCRYPT: <span className="text-slate-300">{current.encrypted ? "YES" : "NONE"}</span>
                    {current.upload_meta && <> · SOURCE: <span className="text-slate-300">UPLOAD</span></>}
                  </div>
                </div>
                <button
                  data-testid="cema-advance-btn"
                  onClick={advance}
                  className="flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs uppercase tracking-widest hover:bg-[#00F0FF] hover:text-black transition-colors scanline-btn"
                  style={{ color: "var(--accent-info)", borderColor: "var(--accent-info)" }}
                >
                  ADVANCE STAGE <ChevronRight size={14} strokeWidth={1.5} />
                </button>
              </div>

              {current.upload_meta && (
                <div className="mb-6 tactical-border p-4 font-mono text-xs">
                  <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-2">Upload Metadata</div>
                  <pre className="text-slate-300 whitespace-pre-wrap">
{JSON.stringify(current.upload_meta, null, 2)}
                  </pre>
                </div>
              )}

              <div className="space-y-0 tactical-border">
                {STAGES.map((s, i) => {
                  const done = i < current.cema_stage_index;
                  const active = i === current.cema_stage_index;
                  return (
                    <div key={s.key}
                         className={`p-4 tactical-border-b last:border-b-0 flex items-start gap-4 ${
                           active ? "bg-[#0F1626]" : ""
                         }`}>
                      <div className={`w-10 h-10 tactical-border flex items-center justify-center font-heading font-black text-lg ${
                        done ? "text-[#39FF14] border-[#39FF14]"
                        : active ? "text-[#00F0FF] border-[#00F0FF]"
                        : "text-slate-600"
                      }`}>
                        {done ? <CheckCircle2 size={18} strokeWidth={1.5}/> : i + 1}
                      </div>
                      <div className="flex-1">
                        <div className="flex items-center gap-3">
                          <span className={`font-heading font-black text-lg uppercase tracking-tighter ${
                            active ? "text-[#00F0FF]" : done ? "text-[#39FF14]" : "text-slate-500"
                          }`}>
                            {s.key}
                          </span>
                          {active && <span className="font-mono text-[10px] text-slate-500 blink">● PROCESSING</span>}
                          {done && <span className="font-mono text-[10px] text-[#39FF14]">✓ COMPLETE</span>}
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
