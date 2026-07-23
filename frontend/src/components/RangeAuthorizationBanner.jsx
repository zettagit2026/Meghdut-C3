import { useEffect, useState, useCallback } from "react";
import { Radiation } from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";

// Additive, higher-level gate per backend/RANGE_AUTHORIZATION_REDESIGN.md
// section 2.4. Polls GET /api/range-authorization/status independently for
// BOTH effects and renders one full-width red banner per effect that is
// currently enabled. Mounted once in Layout.jsx so it can't be missed by
// navigating away from Jamming.jsx/Payloads.jsx.

const EFFECTS = [
  { effect: "jam", label: "RF JAMMING AUTHORIZED" },
  { effect: "mavlink", label: "MAVLINK PAYLOAD DEPLOY AUTHORIZED" },
];

function fmtRemaining(seconds) {
  if (seconds == null || seconds < 0) return "--:--";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function useRangeAuthStatus(effect) {
  const [status, setStatus] = useState(null);
  const [remaining, setRemaining] = useState(null);

  const poll = useCallback(async () => {
    try {
      const { data } = await api.get("/range-authorization/status", { params: { effect } });
      setStatus(data);
      setRemaining(data?.seconds_remaining ?? null);
    } catch {
      // non-fatal — status polling failure shouldn't spam toasts every tick
    }
  }, [effect]);

  useEffect(() => {
    poll();
    const id = setInterval(poll, 3000);
    return () => clearInterval(id);
  }, [poll]);

  // Live countdown ticking between polls, derived from expires_at when present.
  useEffect(() => {
    if (!status?.enabled || !status?.expires_at) return;
    const tick = () => {
      const ms = new Date(status.expires_at).getTime() - Date.now();
      setRemaining(Math.max(0, Math.floor(ms / 1000)));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [status?.enabled, status?.expires_at]);

  return { status, remaining, refresh: poll };
}

function EffectBanner({ effect, label }) {
  const { status, remaining, refresh } = useRangeAuthStatus(effect);
  const [disabling, setDisabling] = useState(false);

  if (!status?.enabled) return null;

  const disableNow = async () => {
    setDisabling(true);
    try {
      await api.post("/range-authorization", { effect, enabled: false });
      toast.success(`RANGE AUTHORIZATION DISABLED — ${label}`);
      refresh();
    } catch (e) {
      toast.error("Disable failed", { description: formatApiError(e) });
    } finally {
      setDisabling(false);
    }
  };

  return (
    <div
      data-testid={`range-auth-banner-${effect}`}
      className="w-full px-4 py-2 flex items-center justify-center gap-4 font-mono text-xs font-bold uppercase tracking-widest pulse-crit"
      style={{ background: "#FF3B30", color: "#000" }}
    >
      <Radiation size={16} strokeWidth={2} />
      <span>
        RANGE LIVE — {label} — expires in{" "}
        <span data-testid={`range-auth-countdown-${effect}`}>{fmtRemaining(remaining)}</span>
        {status.enabled_by ? <> — armed by {status.enabled_by}</> : null}
      </span>
      <button
        data-testid={`range-auth-disable-${effect}`}
        onClick={disableNow}
        disabled={disabling}
        className="px-3 py-1 bg-black text-[#FF3B30] border border-black hover:bg-white transition-colors disabled:opacity-50"
      >
        [ DISABLE NOW ]
      </button>
    </div>
  );
}

export default function RangeAuthorizationBanner() {
  return (
    <div data-testid="range-authorization-banner" className="w-full flex flex-col">
      {EFFECTS.map(({ effect, label }) => (
        <EffectBanner key={effect} effect={effect} label={label} />
      ))}
    </div>
  );
}
