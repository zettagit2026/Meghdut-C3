import { useEffect, useRef, useState } from "react";
import { api, formatApiError, wsUrl } from "@/lib/api";
import { toast } from "sonner";
import { Radio, Zap, Copy, RadioTower } from "lucide-react";

const MAV_CMD_OPTIONS = [
  { id: 21,  label: "NAV_LAND (21)" },
  { id: 20,  label: "NAV_RETURN_TO_LAUNCH (20)" },
  { id: 400, label: "COMPONENT_ARM_DISARM (400)" },
  { id: 185, label: "DO_FLIGHTTERMINATION (185)" },
  { id: 179, label: "DO_SET_HOME (179)" },
  { id: 245, label: "PREFLIGHT_STORAGE (245)" },
  { id: 246, label: "PREFLIGHT_REBOOT_SHUTDOWN (246)" },
  { id: 209, label: "DO_MOTOR_TEST (209)" },
];

const MSG_IDS = [
  { id: 76, label: "COMMAND_LONG (76)" },
  { id: 0, label: "HEARTBEAT (0)" },
  { id: 11, label: "SET_MODE (11)" },
  { id: 253, label: "STATUSTEXT (253)" },
];

export default function MavlinkConsole() {
  const [dets, setDets] = useState([]);
  const [form, setForm] = useState({
    version: "v2",
    system_id: 255,
    component_id: 190,
    sequence: 0,
    message_id: 76,
    target_system: 1,
    target_component: 1,
    command: 21,
    param1: 0, param2: 0, param3: 0, param4: 0, param5: 0, param6: 0, param7: 0,
  });
  const [preview, setPreview] = useState(null);
  const [stream, setStream] = useState([]);
  const [broadcastFlag, setBroadcastFlag] = useState(false);
  // Broadcast TX is a kinetic, ungated-in-the-protocol action (target_sys=0
  // hits every vehicle on the link). Gate it behind an arm->confirm two-step,
  // mirroring EmergencyAbort, so a single stray click can't blast a broadcast
  // COMMAND_LONG. Single-target TX (broadcastFlag off) fires directly.
  const [broadcastArmed, setBroadcastArmed] = useState(false);
  const [wsStatus, setWsStatus] = useState("connecting"); // connecting | open | reconnecting | no-auth | closed
  const wsRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const attemptRef = useRef(0);
  const unmountedRef = useRef(false);

  useEffect(() => {
    api.get("/detections").then((r) => setDets(r.data)).catch(() => {});
    api.get("/mavlink/packets?limit=25").then((r) => setStream(r.data)).catch(() => {});

    unmountedRef.current = false;

    const MAX_BACKOFF_MS = 15000;
    const NO_AUTH_RETRY_MS = 1500;

    const scheduleReconnect = () => {
      if (unmountedRef.current) return;
      const attempt = attemptRef.current;
      const delay = Math.min(1000 * 2 ** attempt, MAX_BACKOFF_MS);
      attemptRef.current = attempt + 1;
      setWsStatus("reconnecting");
      reconnectTimerRef.current = setTimeout(connect, delay);
    };

    const connect = () => {
      if (unmountedRef.current) return;

      // Re-read token fresh on every attempt so a rotated/late token is picked up.
      const token = localStorage.getItem("cema_token");
      if (!token) {
        setWsStatus("no-auth");
        reconnectTimerRef.current = setTimeout(connect, NO_AUTH_RETRY_MS);
        return;
      }

      setWsStatus((s) => (s === "open" ? s : "connecting"));
      const ws = new WebSocket(`${wsUrl("/api/ws/mavlink")}?token=${encodeURIComponent(token)}`);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setWsStatus("open");
      };

      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === "packet") {
            setStream((s) => [msg.packet, ...s].slice(0, 40));
          }
        } catch { /* noop */ }
      };

      ws.onerror = () => {
        // onclose will fire right after and handle reconnect scheduling.
      };

      ws.onclose = () => {
        if (wsRef.current === ws) wsRef.current = null;
        if (unmountedRef.current) return;
        scheduleReconnect();
      };
    };

    connect();

    return () => {
      unmountedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (wsRef.current) {
        const ws = wsRef.current;
        wsRef.current = null;
        ws.onopen = null;
        ws.onmessage = null;
        ws.onerror = null;
        ws.onclose = null;
        ws.close();
      }
    };
  }, []);

  const upd = (k, v) => setForm((f) => ({ ...f, [k]: v }));

  const craftPreview = async () => {
    try {
      const body = { ...form };
      if (broadcastFlag) { body.target_system = 0; body.target_component = 0; }
      const { data } = await api.post("/mavlink/craft", body);
      setPreview(data);
    } catch (e) { toast.error("Craft failed", { description: formatApiError(e) }); }
  };

  useEffect(() => { craftPreview(); /* refresh preview on change */ // eslint-disable-next-line
  }, [form, broadcastFlag]);

  // Disarm the broadcast confirm after 4s of inactivity, or whenever the
  // operator toggles broadcast mode off.
  useEffect(() => {
    if (!broadcastArmed) return;
    const t = setTimeout(() => setBroadcastArmed(false), 4000);
    return () => clearTimeout(t);
  }, [broadcastArmed]);
  useEffect(() => { if (!broadcastFlag) setBroadcastArmed(false); }, [broadcastFlag]);

  const transmit = () => {
    // Two-step arm->confirm only for broadcast (target_sys=0) TX.
    if (broadcastFlag && !broadcastArmed) { setBroadcastArmed(true); return; }
    broadcast();
  };

  const broadcast = async () => {
    try {
      const body = { ...form };
      if (broadcastFlag) { body.target_system = 0; body.target_component = 0; }
      await api.post("/mavlink/broadcast", body);
      toast.success("Packet transmitted", { description: `msgid=${body.message_id} → sys=${body.target_system}` });
    } catch (e) { toast.error("Broadcast failed", { description: formatApiError(e) }); }
    finally { setBroadcastArmed(false); }
  };

  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); toast.success("Copied to clipboard"); }
    catch { toast.error("Clipboard unavailable"); }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
            <Radio size={12} className="inline mr-2" strokeWidth={1.5} /> MAVLink Console
          </div>
          <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
            Packet Crafter · Broadcast
          </h1>
        </div>
        <div className="flex items-center gap-3">
          <label data-testid="broadcast-toggle-wrap"
                 className="flex items-center gap-2 tactical-border px-3 py-2 cursor-pointer">
            <input
              data-testid="broadcast-toggle"
              type="checkbox"
              checked={broadcastFlag}
              onChange={(e) => setBroadcastFlag(e.target.checked)}
              style={{ accentColor: "var(--accent-critical)" }}
            />
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-300">
              BROADCAST (target_sys=0)
            </span>
          </label>
          <button
            data-testid="broadcast-btn"
            onClick={transmit}
            className={`flex items-center gap-2 px-4 py-2 tactical-border font-mono text-xs font-bold uppercase tracking-widest transition-colors scanline-btn ${
              broadcastArmed ? "text-white pulse-crit" : "hover:text-black"
            } ${broadcastFlag ? "hover-accent-critical" : "hover-accent-info"}`}
            style={
              broadcastArmed
                ? { background: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                : broadcastFlag
                ? { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
                : { color: "var(--accent-info)", borderColor: "var(--accent-info)" }
            }
            title={broadcastFlag
              ? "Broadcast TX (target_sys=0) — arm, then confirm to transmit to all vehicles"
              : "Transmit crafted packet to the single target system"}
          >
            <Zap size={14} strokeWidth={1.5} />
            {broadcastArmed ? "CONFIRM BROADCAST TX" : "TRANSMIT"}
          </button>
          <span data-testid="broadcast-arm-status" role="status" aria-live="assertive" className="sr-only">
            {broadcastArmed ? "Broadcast transmit armed — activate again to confirm" : ""}
          </span>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {/* Crafter */}
        <div className="tactical-border" style={{ background: "var(--bg-surface)" }}>
          <div className="tactical-border-b px-4 py-3 font-mono text-xs uppercase tracking-widest">
            Packet Header · Payload
          </div>
          <div className="p-4 grid grid-cols-2 gap-4 font-mono text-xs">
            <Field label="VERSION" testid="fld-version">
              <select value={form.version} onChange={(e) => upd("version", e.target.value)}
                      className="w-full tactical-input tactical-border px-2 py-1">
                <option value="v2">v2 (0xFD)</option>
                <option value="v1">v1 (0xFE)</option>
              </select>
            </Field>
            <Field label="MESSAGE_ID" testid="fld-msgid">
              <select value={form.message_id} onChange={(e) => upd("message_id", parseInt(e.target.value))}
                      className="w-full tactical-input tactical-border px-2 py-1">
                {MSG_IDS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </Field>
            <NumField label="SYSTEM_ID" val={form.system_id} onChange={(v) => upd("system_id", v)} testid="fld-sysid" />
            <NumField label="COMPONENT_ID" val={form.component_id} onChange={(v) => upd("component_id", v)} testid="fld-compid" />
            <NumField label="SEQUENCE" val={form.sequence} onChange={(v) => upd("sequence", v)} testid="fld-seq" />
            <NumField label="TARGET_SYSTEM" val={broadcastFlag ? 0 : form.target_system}
                       onChange={(v) => upd("target_system", v)} disabled={broadcastFlag} testid="fld-tsys" />

            {form.message_id === 76 && (
              <>
                <Field label="MAV_CMD" testid="fld-cmd">
                  <select value={form.command} onChange={(e) => upd("command", parseInt(e.target.value))}
                          className="w-full tactical-input tactical-border px-2 py-1">
                    {MAV_CMD_OPTIONS.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
                  </select>
                </Field>
                <NumField label="TARGET_COMPONENT" val={broadcastFlag ? 0 : form.target_component}
                           onChange={(v) => upd("target_component", v)} disabled={broadcastFlag} testid="fld-tcomp" />
                {[1,2,3,4,5,6,7].map((n) => (
                  <NumField key={n} label={`PARAM${n}`} val={form[`param${n}`]}
                             onChange={(v) => upd(`param${n}`, v)} step="0.01" float testid={`fld-p${n}`} />
                ))}
              </>
            )}
          </div>
        </div>

        {/* Preview */}
        <div className="tactical-border term-surface" style={{ background: "var(--bg-terminal)" }}>
          <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
            <span className="font-mono text-xs uppercase tracking-widest" style={{ color: "var(--text-term)" }}>
              Hex Preview
            </span>
            {preview && (
              <button
                data-testid="copy-hex-btn"
                onClick={() => copy(preview.hex)}
                className="flex items-center gap-1 font-mono text-[10px] uppercase tracking-widest text-slate-400 hover:text-white"
              >
                <Copy size={12} strokeWidth={1.5} /> COPY HEX
              </button>
            )}
          </div>
          <div className="p-4 font-mono text-xs" style={{ color: "var(--text-term)" }}>
            {!preview && <div className="text-slate-600">crafting<span className="term-caret" /></div>}
            {preview && (
              <>
                <div className="mb-3 text-slate-500">
                  {preview.length} bytes · STX <span className="text-white">{preview.decoded?.stx}</span>
                  {" "}· msgid <span className="text-white">{preview.decoded?.message_id}</span>
                  {" "}· sys <span className="text-white">{preview.decoded?.system_id}</span>
                </div>
                <pre data-testid="hex-preview" className="whitespace-pre leading-5 text-[11px]">
{preview.hexdump.join("\n")}
                </pre>
              </>
            )}
          </div>
        </div>
      </div>

      {/* Live stream */}
      <div className="tactical-border term-surface" style={{ background: "var(--bg-terminal)" }}>
        <div className="tactical-border-b px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <RadioTower size={14} strokeWidth={1.5} style={{ color: "var(--accent-info)" }} />
            <span className="font-mono text-xs uppercase tracking-widest text-slate-300">Live MAVLink Broadcast Stream</span>
          </div>
          <span
            data-testid="ws-status"
            className={`font-mono text-[10px] uppercase tracking-widest ${
              wsStatus === "open" ? "blink" : wsStatus !== "no-auth" ? "text-slate-500" : ""
            }`}
            style={
              wsStatus === "open"
                ? { color: "var(--accent-info)" }
                : wsStatus === "no-auth"
                ? { color: "var(--accent-critical)" }
                : undefined
            }
            title={
              wsStatus === "open"
                ? "WebSocket connected"
                : wsStatus === "connecting"
                ? "WebSocket connecting…"
                : wsStatus === "reconnecting"
                ? "WebSocket disconnected — reconnecting…"
                : wsStatus === "no-auth"
                ? "Not authenticated — waiting for token"
                : "WebSocket disconnected"
            }
          >
            ●{" "}
            {wsStatus === "open" && "WS LIVE"}
            {wsStatus === "connecting" && "WS CONNECTING"}
            {wsStatus === "reconnecting" && "WS RECONNECTING"}
            {wsStatus === "no-auth" && "WS NO AUTH"}
            {wsStatus === "closed" && "WS OFFLINE"}
          </span>
        </div>
        <div className="max-h-[360px] overflow-y-auto font-mono text-xs">
          {stream.length === 0 && (
            <div className="p-4 text-slate-600">no packets transmitted<span className="term-caret" /></div>
          )}
          {stream.map((p) => (
            <div key={p.id} data-testid={`pkt-${p.id}`} className="tactical-border-b p-3 hover-surface">
              <div className="flex items-center justify-between mb-1">
                <span className="text-slate-500">{p.ts?.replace("T", " ").split(".")[0]}</span>
                <span className="text-[10px] uppercase tracking-widest text-slate-400">
                  msgid <span className="text-white">{p.decoded?.message_id}</span>
                  {" "}· sys <span className="text-white">{p.system_id}</span> → tgt <span className="text-white">{p.target_system}</span>
                  {p.payload_name && <> · <span style={{color:"var(--accent-warning)"}}>{p.payload_name}</span></>}
                </span>
              </div>
              <HexBytes hex={p.hex} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// Renders a MAVLink frame hex as space-separated byte pairs on a single
// horizontally-scrolling line (never wrapping mid-byte), with the final two
// bytes — the MAVLink CRC-16/MCRF4XX checksum — highlighted and captioned so
// the "CRC-16 MCRF4XX" beat in the walkthrough has something to point at.
function HexBytes({ hex }) {
  if (!hex) return null;
  const bytes = String(hex).replace(/[^0-9a-fA-F]/g, "").match(/.{1,2}/g) || [];
  const hasCrc = bytes.length >= 2;
  const body = hasCrc ? bytes.slice(0, -2) : bytes;
  const crc = hasCrc ? bytes.slice(-2) : [];
  return (
    <div className="overflow-x-auto">
      <div className="text-[11px] whitespace-nowrap tabular-nums" style={{ color: "var(--text-term)" }}>
        <span>{body.join(" ")}</span>
        {hasCrc && (
          <>
            {" "}
            <span
              className="font-bold"
              style={{ color: "var(--accent-warning)" }}
              title="CRC-16/MCRF4XX frame checksum (final 2 bytes)"
            >
              {crc.join(" ")}
            </span>
            <span className="ml-2 uppercase tracking-widest text-[9px]" style={{ color: "var(--accent-warning)" }}>
              ◄ CRC
            </span>
          </>
        )}
      </div>
    </div>
  );
}

function Field({ label, children, testid }) {
  return (
    <label data-testid={testid} className="block">
      <div className="text-[10px] uppercase tracking-widest text-slate-500 mb-1">{label}</div>
      {children}
    </label>
  );
}

function NumField({ label, val, onChange, disabled, step = "1", float = false, testid }) {
  return (
    <Field label={label} testid={testid}>
      <input
        type="number"
        step={step}
        value={val}
        disabled={disabled}
        onChange={(e) => onChange(float ? parseFloat(e.target.value || "0") : parseInt(e.target.value || "0"))}
        className="w-full tactical-input tactical-border px-2 py-1 focus:outline-none focus-accent-info disabled:opacity-40"
      />
    </Field>
  );
}
