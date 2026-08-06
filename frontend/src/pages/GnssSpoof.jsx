import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { Crosshair, AlertTriangle, ShieldAlert } from "lucide-react";
import SafetyGate, { GNSS_SPOOF_CHECKS } from "@/components/SafetyGate";
import RangeAuthorizationControl from "@/components/RangeAuthorizationControl";

// Mirrors backend GNSS_SPOOF_MAX_DURATION_S / hackrf_jam.GNSS_SPOOF_MAX_DURATION_S —
// deliberately much shorter than jamming's 10s cap. See
// field-bridge/GNSS_SPOOF_ARCHITECTURE.md §2 for why.
const MAX_DURATION_S = 3.0;
const DEFAULT_DURATION_S = 2.0;
const MIN_ATTESTATION_LEN = 20;

// task #146: /gnss-spoof/status polling previously swallowed failures in an
// empty catch block, so if polling died (backend restart, network
// partition, auth expiry) the spoof session cards would freeze on their
// last-known status with zero indication that monitoring itself had
// stopped. Same tracking pattern as SystemHealth.jsx (task #144) and
// Jamming.jsx (task #146): consecutive-failure count + last-success
// timestamp, ~4x this page's own 2s poll interval for the staleness
// threshold.
const POLL_INTERVAL_MS = 2000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4; // ~8s

const STATUS_STYLE = {
  AWAITING_ACK:        { color: "var(--accent-warning)", label: "◐ AWAITING ACK", blink: true },
  GNSS_SPOOF_ACTIVE:   { color: "var(--accent-critical)", label: "▮▮ TRANSMITTING", blink: true },
  GNSS_SPOOF_COMPLETE: { color: "var(--accent-success)", label: "✓ SPOOF COMPLETE", blink: false },
  GNSS_SPOOF_STOPPED:  { color: "var(--accent-warning)", label: "■ STOPPED (ABORT)", blink: false },
  TX_FAILED:           { color: "var(--accent-critical)", label: "✕ TX FAILED", blink: false },
  TX_TIMEOUT:          { color: "var(--accent-critical)", label: "✕ TX TIMEOUT", blink: false },
};

export default function GnssSpoof() {
  const [trueLat, setTrueLat] = useState("");
  const [trueLon, setTrueLon] = useState("");
  const [trueAltM, setTrueAltM] = useState("0");
  const [fakeOffsetM, setFakeOffsetM] = useState(300);
  const [fakeBearingDeg, setFakeBearingDeg] = useState(0);
  const [durationS, setDurationS] = useState(DEFAULT_DURATION_S);
  const [txGain, setTxGain] = useState(20);
  const [attestation, setAttestation] = useState("");

  const [preview, setPreview] = useState(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState(null);

  const [gateOpen, setGateOpen] = useState(false);
  const [sessions, setSessions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  const loadStatus = async () => {
    try {
      const { data } = await api.get("/gnss-spoof/status");
      setSessions(data.sessions || []);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
    } catch (e) {
      setConsecutiveFailures((n) => n + 1);
    }
  };
  useEffect(() => { loadStatus(); const id = setInterval(loadStatus, POLL_INTERVAL_MS); return () => clearInterval(id); }, []);

  // Local clock tick so staleness (time-since-last-success) updates even
  // between poll ticks — same pattern as SystemHealth.jsx / Jamming.jsx.
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  const active = sessions.find((s) => s.status === "AWAITING_ACK" || s.status === "GNSS_SPOOF_ACTIVE");

  const hasValidTruePosition = trueLat !== "" && trueLon !== "" && !Number.isNaN(Number(trueLat)) &&
    !Number.isNaN(Number(trueLon));
  const attestationValid = attestation.trim().length >= MIN_ATTESTATION_LEN;

  // Fetch the LIVE preview whenever the inputs that affect the fabricated
  // position change — this is the single most important gate per the
  // architecture doc §5b: the operator must see the EXACT fake lat/lon
  // BEFORE any token is minted, computed by the backend (not guessed by
  // this page), so "what the preview showed" and "what gets transmitted"
  // are provably the same numbers (backend recomputes identically at fire
  // time from the same inputs).
  useEffect(() => {
    if (!hasValidTruePosition) { setPreview(null); return; }
    let cancelled = false;
    setPreviewLoading(true);
    setPreviewError(null);
    api.post("/gnss-spoof/preview", {
      true_lat: Number(trueLat),
      true_lon: Number(trueLon),
      true_alt_m: Number(trueAltM) || 0,
      fake_offset_m: Number(fakeOffsetM),
      fake_bearing_deg: Number(fakeBearingDeg),
    }).then(({ data }) => {
      if (!cancelled) setPreview(data);
    }).catch((e) => {
      if (!cancelled) { setPreview(null); setPreviewError(formatApiError(e)); }
    }).finally(() => {
      if (!cancelled) setPreviewLoading(false);
    });
    return () => { cancelled = true; };
  }, [hasValidTruePosition, trueLat, trueLon, trueAltM, fakeOffsetM, fakeBearingDeg]);

  // The checklist item whose text is DYNAMICALLY built from the live
  // preview response — per architecture doc §5b, this is NOT a generic
  // "Confirm spoof?" button. If the preview hasn't loaded yet, the gate
  // simply isn't offered (see the ARM button's disabled condition below) —
  // the operator can never reach a checklist showing stale/templated text.
  const gateChecks = preview
    ? [
        ...GNSS_SPOOF_CHECKS,
        `Target will receive FAKE position ${preview.distance_description}. This is REAL RF, ` +
          `not a preview of the effect — reviewed and correct.`,
      ]
    : GNSS_SPOOF_CHECKS;

  const fireGnssSpoof = async () => {
    if (!preview) {
      toast.error("No live preview available — cannot fire without a current fabricated-position computation.");
      return;
    }
    setSubmitting(true);
    try {
      // Step 1: a fresh arm token — gnss_spoof is unconditionally CRITICAL
      // severity, same second factor as jamming/broadcast payload deploys.
      const { data: arm } = await api.post("/arm", { effect: "gnss_spoof" });  // F3: bound to gnss_spoof
      // Step 2: mint the gnss_spoof confirmation token RIGHT NOW, carrying
      // the attestation text — this call only happens because the
      // SafetyGate checklist + two-click ARM & FIRE -> CONFIRM FIRE
      // sequence just completed (see onConfirm below). NOT interchangeable
      // with jam_confirm_token (see backend/server.py's
      // _consume_gnss_spoof_confirm_token).
      const { data: confirm } = await api.post("/gnss-spoof/confirm", {
        friendly_asset_attestation: attestation,
      });
      // Step 3: the actual spoof request — friendly_asset_attestation is
      // RE-SUBMITTED here and must match what was sent to /confirm, or the
      // backend rejects with 400 (defense against the text being swapped
      // between confirm and fire).
      const { data } = await api.post("/payloads/gnss-spoof", {
        band: "gps_l1",
        duration_s: durationS,
        tx_gain: txGain,
        fake_offset_m: Number(fakeOffsetM),
        fake_bearing_deg: Number(fakeBearingDeg),
        true_lat: Number(trueLat),
        true_lon: Number(trueLon),
        true_alt_m: Number(trueAltM) || 0,
        friendly_asset_attestation: attestation,
        arm_token: arm.arm_token,
        gnss_spoof_confirm_token: confirm.gnss_spoof_confirm_token,
      });
      toast.info(`GNSS SPOOF REQUESTED — awaiting bridge ACK`, {
        description: `${data.freq_mhz} MHz · ${data.duration_s}s · fake ${data.fake_lat?.toFixed(6)},` +
          `${data.fake_lon?.toFixed(6)} · req ${data.request_id?.slice(0, 8)}`,
      });
      loadStatus();
    } catch (e) {
      toast.error("GNSS spoof request failed", { description: formatApiError(e) });
    } finally {
      setSubmitting(false);
    }
  };

  const canArm = !active && !submitting && hasValidTruePosition && attestationValid && !!preview && !previewLoading;

  return (
    <div className="space-y-6">
      <div>
        <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-1">
          <Crosshair size={12} className="inline mr-2" strokeWidth={1.5} /> GNSS L1 Soft-Kill
        </div>
        <h1 className="font-heading font-black text-5xl uppercase tracking-tighter">
          GNSS Spoof (Deception)
        </h1>
      </div>

      <div className="tactical-border p-4 flex items-start gap-3" style={{ background: "#1A0A08" }}>
        <AlertTriangle size={16} strokeWidth={1.5} style={{ color: "var(--accent-critical)" }} />
        <div className="font-mono text-xs text-slate-300">
          <span className="font-bold" style={{ color: "var(--accent-critical)" }}>WARNING:</span>{" "}
          This transmits a REAL, structurally valid GPS L1 C/A signal carrying a FABRICATED position —
          a deception effect, not a denial effect. Hard-capped at {MAX_DURATION_S}s per request (shorter than
          jamming's cap — a single bad position report is enough to trigger the target's failsafe). Requires
          commander role, a fresh arm token, a gnss-spoof confirmation token (NOT interchangeable with the
          jam confirmation token) minted at the instant you complete the checklist below, a required
          friendly-asset attestation captured and logged verbatim, AND a live Range Authorization lease for
          effect=gnss_spoof (arming effect=jam does NOT arm this) — all independently, every time.
        </div>
      </div>

      <RangeAuthorizationControl effect="gnss_spoof" label="GNSS SPOOF" />

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="tactical-border p-4 space-y-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
            Last-Known-True Position
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">True Lat</span>
              <input
                data-testid="gnss-spoof-true-lat-input"
                type="number" step="0.000001"
                value={trueLat}
                onChange={(e) => setTrueLat(e.target.value)}
                placeholder="28.613900"
                className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">True Lon</span>
              <input
                data-testid="gnss-spoof-true-lon-input"
                type="number" step="0.000001"
                value={trueLon}
                onChange={(e) => setTrueLon(e.target.value)}
                placeholder="77.209000"
                className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>
          </div>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">True Alt (m)</span>
            <input
              data-testid="gnss-spoof-true-alt-input"
              type="number" step="1"
              value={trueAltM}
              onChange={(e) => setTrueAltM(e.target.value)}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 pt-2">
            Fabricated-Position Offset
          </div>

          <div className="grid grid-cols-2 gap-3">
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Offset (m)</span>
              <input
                data-testid="gnss-spoof-offset-input"
                type="number" min={1} step={10}
                value={fakeOffsetM}
                onChange={(e) => setFakeOffsetM(Number(e.target.value))}
                className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>
            <label className="block">
              <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">Bearing (deg)</span>
              <input
                data-testid="gnss-spoof-bearing-input"
                type="number" min={0} max={359} step={1}
                value={fakeBearingDeg}
                onChange={(e) => setFakeBearingDeg(Number(e.target.value))}
                className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
              />
            </label>
          </div>

          <div
            data-testid="gnss-spoof-preview"
            className="tactical-border p-3 font-mono text-xs"
            style={{ background: "rgba(0,240,255,0.06)", borderColor: "var(--accent-info, #00F0FF)" }}
          >
            {previewLoading && <span className="text-slate-500">computing live preview…</span>}
            {!previewLoading && previewError && <span style={{ color: "var(--accent-critical)" }}>{previewError}</span>}
            {!previewLoading && !previewError && !preview && (
              <span className="text-slate-600">enter a valid true lat/lon to compute the fabricated position</span>
            )}
            {!previewLoading && preview && (
              <>
                <div className="text-white font-bold mb-1">LIVE PREVIEW — FABRICATED POSITION</div>
                <div className="text-slate-300">{preview.distance_description}</div>
                <div className="text-slate-500 mt-1">
                  fake: {preview.fake_lat?.toFixed(6)}, {preview.fake_lon?.toFixed(6)} (alt {preview.fake_alt_m}m)
                </div>
              </>
            )}
          </div>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Duration (s, max {MAX_DURATION_S})
            </span>
            <input
              data-testid="gnss-spoof-duration-input"
              type="number" min={0.5} max={MAX_DURATION_S} step={0.5}
              value={durationS}
              onChange={(e) => setDurationS(Math.min(MAX_DURATION_S, Math.max(0.5, Number(e.target.value))))}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">TX Gain (0-47)</span>
            <input
              data-testid="gnss-spoof-gain-input"
              type="number" min={0} max={47}
              value={txGain}
              onChange={(e) => setTxGain(Number(e.target.value))}
              className="mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none focus:border-[#00F0FF]"
            />
          </label>

          <label className="block">
            <span className="font-mono text-[10px] uppercase tracking-widest text-slate-500">
              Friendly-Asset Attestation (required, min {MIN_ATTESTATION_LEN} chars)
            </span>
            <textarea
              data-testid="gnss-spoof-attestation-input"
              value={attestation}
              onChange={(e) => setAttestation(e.target.value)}
              rows={3}
              placeholder="Confirmed: no friendly GPS-dependent assets (own drones/vehicles) within [radius] of target position. Reviewed friendly asset tracker at [time]."
              className={`mt-1 w-full bg-black/50 tactical-border px-3 py-2 font-mono text-xs text-white focus:outline-none ${
                attestation && !attestationValid ? "border-[#FF3B30]" : "focus:border-[#00F0FF]"
              }`}
            />
          </label>

          <button
            data-testid="gnss-spoof-arm-button"
            disabled={!canArm}
            onClick={() => setGateOpen(true)}
            className={`w-full flex items-center justify-center gap-2 px-4 py-3 font-mono text-xs font-bold uppercase tracking-widest border scanline-btn transition-colors ${
              !canArm
                ? "opacity-30 border-slate-700 text-slate-600 cursor-not-allowed"
                : "text-[#FF3B30] border-[#FF3B30] hover:bg-[#FF3B30] hover:text-black"
            }`}
          >
            ARM GNSS SPOOF
          </button>
        </div>

        <div className="tactical-border p-4" style={{ background: "var(--bg-surface)" }}>
          <div className="font-mono text-[10px] uppercase tracking-widest text-slate-500 mb-3">
            GNSS Spoof Sessions
          </div>
          {statusUnconfirmed && (
            <div
              data-testid="gnss-spoof-status-unconfirmed-banner"
              className="mb-3 flex items-center gap-2 px-3 py-2 pulse-crit"
              style={{ background: "#FF9500", color: "black" }}
            >
              <ShieldAlert size={14} strokeWidth={2} />
              <span className="font-mono text-[10px] font-bold uppercase tracking-widest">
                STATUS UNCONFIRMED — gnss spoof status feed stale, session states below may be out of date
              </span>
            </div>
          )}
          {sessions.length === 0 && (
            <div className="font-mono text-xs text-slate-600 text-center py-8">
              no gnss spoof sessions yet<span className="term-caret" />
            </div>
          )}
          <div className="space-y-2">
            {sessions.map((s) => {
              const st = STATUS_STYLE[s.status] || { color: "var(--text-muted)", label: s.status, blink: false };
              return (
                <div key={s.request_id} data-testid={`gnss-spoof-session-${s.request_id}`}
                     className="flex items-center justify-between p-3 tactical-border">
                  <div className="font-mono text-[11px] text-slate-300">
                    {s.freq_mhz} MHz · {s.duration_s}s · gain={s.tx_gain}
                    <div className="text-slate-500 text-[10px]">{s.request_id?.slice(0, 8)}</div>
                  </div>
                  <span
                    data-testid={`gnss-spoof-status-${s.request_id}`}
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
        payloadName="GNSS L1 SOFT-KILL (GPS L1 C/A Spoof)"
        severity="CRITICAL"
        checks={gateChecks}
        actionLabel="TRANSMIT"
        irreversibleNote="a real RF transmission carrying a fabricated position — it cannot be recalled once sent"
        onClose={() => setGateOpen(false)}
        onConfirm={() => {
          setGateOpen(false);
          fireGnssSpoof();
        }}
      />
    </div>
  );
}
