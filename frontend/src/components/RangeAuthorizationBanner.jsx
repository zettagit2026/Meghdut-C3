import { useState } from "react";
import { Radiation, ShieldAlert } from "lucide-react";
import { api, formatApiError } from "@/lib/api";
import { toast } from "sonner";
import { useRangeAuthStatus, fmtRemaining } from "@/hooks/useRangeAuthStatus";

// Additive, higher-level gate per backend/RANGE_AUTHORIZATION_REDESIGN.md
// section 2.4. Polls GET /api/range-authorization/status independently for
// BOTH effects and renders one full-width red banner per effect that is
// currently enabled. Mounted once in Layout.jsx so it can't be missed by
// navigating away from Jamming.jsx/Payloads.jsx.
//
// Poll-failure/staleness tracking (task #146) lives in the shared
// hooks/useRangeAuthStatus.js hook, used by both this banner and
// RangeAuthorizationControl.jsx.

const EFFECTS = [
  { effect: "jam", label: "RF JAMMING AUTHORIZED" },
  { effect: "mavlink", label: "MAVLINK PAYLOAD DEPLOY AUTHORIZED" },
];

function EffectBanner({ effect, label }) {
  const { status, remaining, refresh, statusUnconfirmed } = useRangeAuthStatus(effect);
  const [disabling, setDisabling] = useState(false);

  // status is only ever the last-known-good response — it is never cleared
  // just because a poll failed — so a session that was enabled stays
  // enabled (and its countdown keeps ticking down toward 00:00) through a
  // run of failed polls. That is exactly the dangerous case task #146
  // covers: the countdown can hit 00:00 while the real, server-confirmed
  // state is unknown, which is why the STATUS UNCONFIRMED indicator below
  // renders alongside — not instead of — this banner, and is never
  // suppressed by the countdown reaching zero.
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
    <div className="w-full flex flex-col">
      <div
        data-testid={`range-auth-banner-${effect}`}
        className="w-full px-4 py-2 flex items-center justify-center gap-4 font-mono text-xs font-bold uppercase tracking-widest pulse-crit"
        style={{ background: "#FF3B30", color: "#000" }}
      >
        <Radiation size={16} strokeWidth={2} />
        <span>
          RANGE LIVE — {label} — expires in{" "}
          <span data-testid={`range-auth-countdown-${effect}`} className="tnum">{fmtRemaining(remaining)}</span>
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
      <div
        data-testid={`range-auth-unconfirmed-${effect}`}
        role="status"
        className="w-full px-4 py-2 items-center justify-center gap-2 pulse-crit"
        style={{
          display: statusUnconfirmed ? "flex" : "none",
          background: "#FF9500",
          color: "black",
        }}
      >
        <ShieldAlert size={14} strokeWidth={2} />
        <span className="font-mono text-[11px] font-bold uppercase tracking-widest">
          STATUS UNCONFIRMED — {label} status feed stale, real range state is NOT verified — the
          countdown above may not reflect reality
        </span>
      </div>
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
