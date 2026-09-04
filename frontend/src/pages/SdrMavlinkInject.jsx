import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { handleEngageBlock } from "@/lib/engageFix";
import { RadioTower, AlertTriangle, ShieldAlert, Target as TargetIcon } from "lucide-react";
import SafetyGate, { MAVLINK_SDR_INJECT_CHECKS } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";
import { useAuth } from "@/context/AuthContext";

// A contact is a CONFIRMED FRIENDLY exactly when IFF interrogation has replied
// and the backend stamped it so — mirrors backend/server.py's
// _enforce_fire_time_iff. Injecting a takeover command at one is FRATRICIDE and
// is refused (403) on the routine path; the only licensed path is the
// deliberate, single-use commander friendly-fire ack.
const isFriendly = (d) =>
  !!d && (d.iff_verified === true || d.threat_level === "FRIENDLY (IFF verified)");

const isFratricideRefusal = (e) =>
  e?.response?.status === 403 &&
  /FRATRICIDE|CONFIRMED-FRIENDLY|friendly-fire ack/i.test(formatApiError(e) || "");

// Mirrors backend MavlinkSdrInjectBody command pattern + sdr_mavlink_inject.py
// COMMAND_BUILDERS. Kept in sync by convention (bytes come from mavlink_codec).
const COMMANDS = [
  { value: "force_land", label: "FORCE LAND (NAV_LAND)" },
  { value: "rth", label: "RETURN-TO-HOME (RTL)" },
  { value: "disarm", label: "DISARM (COMPONENT_ARM_DISARM)" },
  { value: "flight_termination", label: "FLIGHT TERMINATION" },
  { value: "maneuver_takeover", label: "MANEUVER TAKEOVER" },
];

const MAX_REPEAT = 20;

// Status feed staleness tracking — same pattern as GnssSpoof.jsx / Jamming.jsx.
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4;

const STATUS_STYLE = {
  AWAITING_ACK:            { color: "var(--accent-warning)", label: "◐ AWAITING ACK", blink: true },
  MAVLINK_INJECT_ACTIVE:   { color: "var(--accent-critical)", label: "▮▮ TRANSMITTING", blink: true },
  MAVLINK_INJECT_COMPLETE: { color: "var(--accent-success)", label: "✓ INJECT COMPLETE", blink: false },
  MAVLINK_INJECT_STOPPED:  { color: "var(--accent-warning)", label: "■ STOPPED (ABORT)", blink: false },
  TX_FAILED:               { color: "var(--accent-critical)", label: "✕ TX FAILED", blink: false },
  TX_TIMEOUT:              { color: "var(--accent-critical)", label: "✕ TX TIMEOUT", blink: false },
};

export default function SdrMavlinkInject() {
  const { user } = useAuth();
  const isCommander = user?.role === "commander";

  const [dets, setDets] = useState([]);
  const [target, setTarget] = useState("");
  const [command, setCommand] = useState("force_land");
  const [centerFreqMhz, setCenterFreqMhz] = useState(915.0);
  const [airRateBps, setAirRateBps] = useState(250000);
  const [deviationHz, setDeviationHz] = useState(62500);
  const [bt, setBt] = useState(0.5);
  const [bitOrder, setBitOrder] = useState("msb");
  const [txGain, setTxGain] = useState(20);
  const [repeat, setRepeat] = useState(3);

  const [gateOpen, setGateOpen] = useState(false);
  const [fratricide, setFratricide] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const loadDets = async () => {
    try { const { data } = await api.get("/detections"); setDets(data || []); } catch { /* keep last */ }
  };
  const loadStatus = async () => {
    try {
      const { data } = await api.get("/mavlink-sdr-inject/status");
      setSessions(data.sessions || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch {
      setConsecutiveFailures((n) => n + 1);
    }
  };
  useEffect(() => {
    loadDets(); loadStatus();
    const id = setInterval(() => { loadDets(); loadStatus(); }, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  const active = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "MAVLINK_INJECT_ACTIVE");
  const selectedDet = dets.find((d) => d.id === target);
  const friendlySelected = isFriendly(selectedDet);

  const canArm = !active && !submitting && !!target && !!command;

  // The actual arm->confirm(->ack)->fire sequence, run ONLY after the SafetyGate
  // two-step confirm completes (see onConfirm below).
  const fireInject = async () => {
    if (!target) { toast.error("No target selected"); return; }
    setSubmitting(true);
    try {
      // Step 1: arm token bound to effect=mavlink_sdr_inject AND this exact
      // target (F3 — the backend rejects a token spent on a different
      // effect/target).
      const { data: arm } = await api.post("/arm", {
        effect: "mavlink_sdr_inject",
        target_detection_id: target,
      });
      // Step 2: mint the SDR-inject confirmation token RIGHT NOW — this call
      // only happens because the SafetyGate two-step confirm just completed.
      // NOT interchangeable with jam/gnss confirm tokens.
      const { data: confirm } = await api.post("/mavlink-sdr-inject/confirm", {});
      // Step 3 (fratricide only): the DELIBERATE, single-use, target-bound
      // commander friendly-fire ack — the ONLY thing that can license injecting
      // at a confirmed friendly. Minted here, consumed once by the backend.
      let iffAck;
      if (friendlySelected) {
        const { data: ack } = await api.post(`/detections/${target}/friendly-fire-ack`);
        iffAck = ack.iff_friendly_fire_ack;
      }
      // Step 4: the actual inject request.
      const { data } = await api.post("/payloads/mavlink-sdr-inject", {
        target_detection_id: target,
        command,
        center_freq_mhz: Number(centerFreqMhz),
        air_rate_bps: Number(airRateBps),
        deviation_hz: Number(deviationHz),
        bt: Number(bt),
        bit_order: bitOrder,
        tx_gain: Number(txGain),
        repeat: Number(repeat),
        arm_token: arm.arm_token,
        mavlink_sdr_inject_confirm_token: confirm.mavlink_sdr_inject_confirm_token,
        ...(iffAck ? { iff_friendly_fire_ack: iffAck } : {}),
      });
      if (data.tx_bridge_subscribed === false) {
        // Honest false-green guard: backend accepted (AWAITING_ACK) but NO
        // sdr-mavlink bridge is subscribed, so nothing radiated — will TX_TIMEOUT.
        if (!handleEngageBlock({ response: data }, { isCommander, onFixed: loadStatus })) {
          toast.error("SDR MAVLINK INJECT: NOT TRANSMITTED", {
            description: `Nothing reached a radio. Request ${data.request_id?.slice(0, 8)} will TX_TIMEOUT.`,
          });
        }
      } else {
        toast.info(`SDR MAVLINK INJECT REQUESTED — awaiting bridge ACK`, {
          description: `${data.command?.toUpperCase()} · ${data.center_freq_mhz} MHz · ×${data.repeat} · ` +
            `sys ${data.target_system} · req ${data.request_id?.slice(0, 8)}`,
        });
      }
      loadStatus();
    } catch (e) {
      if (isFratricideRefusal(e)) {
        toast.error("FRATRICIDE INTERLOCK — routine inject refused", {
          description: "Target is IFF-CONFIRMED FRIENDLY. Inject only via the deliberate commander friendly-fire override.",
        });
        setFratricide(true);
        setGateOpen(true);
        return;
      }
      if (handleEngageBlock({ error: e }, { isCommander, onFixed: loadStatus })) return;
      toast.error("SDR MAVLink inject request failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  const openGate = () => {
    // A confirmed friendly can never take the routine path — it opens the gate
    // in fratricide mode (commander role + typed ack enforced in SafetyGate).
    setFratricide(friendlySelected);
    setGateOpen(true);
  };

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <RadioTower size={12} className="inline mr-2" strokeWidth={1.5} /> No-Pairing SDR Takeover
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          SDR MAVLink Inject
        </h1>
      </div>

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "var(--surface-critical)" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300 space-y-2">
          <div>
            <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
            This GFSK-modulates a byte-accurate MAVLink COMMAND_LONG onto baseband IQ and radiates it over
            the air at the target link's frequency via the pinned TX HackRF — no SiK pairing, no shared NetID.
            A REAL RF transmission of a takeover command; it cannot be recalled once sent. Requires commander
            role, a fresh arm token, an SDR-inject confirmation token (NOT interchangeable with the jam / GNSS
            confirm tokens), the IFF fratricide interlock, AND a live Range Authorization lease for
            effect=mavlink_sdr_inject (arming effect=jam or effect=mavlink does NOT arm this) — all
            independently, every time.
          </div>
          <div
            data-testid="sdr-inject-fidelity-note"
            className="tactical-border p-2"
            style={{ background: "rgba(255,149,0,0.08)", borderColor: "#FF9500", color: "#FFB454" }}
          >
            <span className="font-bold">HONEST CAPABILITY LIMITS (niche adversary path, NOT a universal defeat):</span>{" "}
            Works ONLY against a FIXED-FREQUENCY, UNENCRYPTED MAVLink link (hop disabled). It does NOT follow an
            FHSS / frequency-hopping sequence (e.g. default SiK/RFD900 hopping) — that is not implemented — and it
            is N/A against MAVLink-signed / encrypted / proprietary links (DJI, ELRS/CRSF, DSMX…). For those links
            the defeat is JAMMING, which remains the universal defeat. The backend refuses (422) an
            encrypted/FHSS or unattested-unknown target link rather than transmit uselessly.
          </div>
        </div>
      </div>

      <RangeAuthorizationControl effect="mavlink_sdr_inject" label="SDR MAVLINK INJECT" />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="tactical-border p-4 space-y-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
            Target &amp; Command
          </div>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Target Detection</span>
            <select
              data-testid="sdr-inject-target-select"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
            >
              <option value="">— SELECT ACTIVE TARGET —</option>
              {dets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.callsign || d.id?.slice(0, 8)} · {d.protocol || "?"} {isFriendly(d) ? "· ⚠ FRIENDLY" : ""}
                </option>
              ))}
            </select>
          </label>

          {friendlySelected && (
            <div
              data-testid="sdr-inject-fratricide-banner"
              className="p-3 flex items-start gap-2 border-2 font-mono text-xs"
              style={{ borderColor: "var(--accent-critical)", background: "color-mix(in srgb, var(--accent-critical) 12%, var(--bg-surface))" }}
            >
              <ShieldAlert size={16} strokeWidth={1.75} style={{ color: "var(--accent-critical)", flexShrink: 0 }} />
              <span className="text-slate-300">
                <span className="font-bold" style={{ color: "var(--accent-critical)" }}>IFF-CONFIRMED FRIENDLY.</span>{" "}
                Injecting a takeover command here is FRATRICIDE. There is no standing override —{" "}
                {isCommander
                  ? "proceeding opens the deliberate commander friendly-fire override (single-use, loudly audited)."
                  : "only a commander may deliberately override this; your role cannot engage a confirmed friendly."}
              </span>
            </div>
          )}

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Command</span>
            <select
              data-testid="sdr-inject-command-select"
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
            >
              {COMMANDS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
          </label>

          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 pt-2">
            Target Link Air-PHY (match to the real link — measured, not assumed)
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Center Freq (MHz)</span>
              <input
                data-testid="sdr-inject-freq-input"
                type="number" step="0.001" min={1}
                value={centerFreqMhz}
                onChange={(e) => setCenterFreqMhz(Number(e.target.value))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Air Rate (bps)</span>
              <input
                data-testid="sdr-inject-airrate-input"
                type="number" step="1000" min={1}
                value={airRateBps}
                onChange={(e) => setAirRateBps(Number(e.target.value))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Deviation (Hz)</span>
              <input
                data-testid="sdr-inject-deviation-input"
                type="number" step="1000" min={1}
                value={deviationHz}
                onChange={(e) => setDeviationHz(Number(e.target.value))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">BT (0-1)</span>
              <input
                data-testid="sdr-inject-bt-input"
                type="number" step="0.1" min={0.1} max={1}
                value={bt}
                onChange={(e) => setBt(Number(e.target.value))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Bit Order</span>
              <select
                data-testid="sdr-inject-bitorder-select"
                value={bitOrder}
                onChange={(e) => setBitOrder(e.target.value)}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              >
                <option value="msb">MSB-first (radio default)</option>
                <option value="lsb">LSB-first (transparent UART)</option>
              </select>
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Repeat (1-{MAX_REPEAT})</span>
              <input
                data-testid="sdr-inject-repeat-input"
                type="number" min={1} max={MAX_REPEAT} step={1}
                value={repeat}
                onChange={(e) => setRepeat(Math.min(MAX_REPEAT, Math.max(1, Number(e.target.value))))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">TX Gain (0-47)</span>
              <input
                data-testid="sdr-inject-gain-input"
                type="number" min={0} max={47}
                value={txGain}
                onChange={(e) => setTxGain(Number(e.target.value))}
                className="mt-1 w-full tactical-input tactical-border px-3 py-2 font-mono text-xs focus:outline-none focus-accent-info"
              />
            </label>
          </div>

          <button
            data-testid="sdr-inject-arm-button"
            disabled={!canArm}
            onClick={openGate}
            className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
              !canArm
                ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                : "hover-accent-critical"
            }`}
            style={!canArm ? undefined : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }}
          >
            <TargetIcon size={14} strokeWidth={1.5} />
            {friendlySelected ? "ARM SDR INJECT (FRIENDLY — OVERRIDE)" : "ARM SDR MAVLINK INJECT"}
          </button>
        </div>

        <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-3">
            SDR MAVLink Inject Sessions
          </div>
          {statusUnconfirmed && (
            <div
              data-testid="sdr-inject-status-unconfirmed-banner"
              className="mb-3 flex items-center gap-2 px-3 py-2 pulse-crit"
              style={{ background: "#FF9500", color: "black" }}
            >
              <ShieldAlert size={14} strokeWidth={2} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                STATUS UNCONFIRMED — inject status feed stale, session states below may be out of date
              </span>
            </div>
          )}
          {sessions.length === 0 && (
            <div className="font-mono text-xs text-slate-600 text-center py-8">
              no sdr mavlink inject sessions yet<span className="term-caret" />
            </div>
          )}
          <div className="space-y-2">
            {sessions.map((s) => {
              const st = STATUS_STYLE[s.status] || { color: "var(--text-muted)", label: s.status, blink: false };
              return (
                <div key={s.request_id} data-testid={`sdr-inject-session-${s.request_id}`}
                     className="flex items-center justify-between p-3 tactical-border">
                  <div className="font-mono text-[11px] text-slate-300">
                    {s.command?.toUpperCase()} · {s.center_freq_mhz} MHz · ×{s.repeat} · sys {s.target_system}
                    <div className="text-slate-500 text-[10px]">{s.request_id?.slice(0, 8)}</div>
                  </div>
                  <span
                    data-testid={`sdr-inject-status-${s.request_id}`}
                    className={`px-3 py-1.5 tactical-border font-mono text-[10px] uppercase tracking-widest ${st.blink ? "blink" : ""}`}
                    style={{ color: st.color, borderColor: st.color }}
                  >
                    {st.label}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <SafetyGate
        open={gateOpen}
        payloadName="SDR MAVLINK INJECT (No-Pairing Over-the-Air Takeover)"
        severity="CRITICAL"
        checks={MAVLINK_SDR_INJECT_CHECKS}
        actionLabel="TRANSMIT"
        irreversibleNote="a real over-the-air MAVLink takeover command — it cannot be recalled once sent"
        fratricide={fratricide}
        isCommander={isCommander}
        friendlyCallsign={fratricide ? selectedDet?.callsign : undefined}
        onClose={() => { setGateOpen(false); setFratricide(false); }}
        onConfirm={() => {
          setGateOpen(false);
          setFratricide(false);
          fireInject();
        }}
      />
    </div>
  );
}
