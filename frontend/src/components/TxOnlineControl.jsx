import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { Power, PowerOff } from "lucide-react";
import { toast } from "sonner";

// Commander-only control that performs the SiK-radio handoff from the GUI —
// replacing the out-of-console `systemctl` step a fielded operator used to need
// on a shell. "Bring TX Online" calls POST /api/tx/online (stop sniffer, start
// rf-bridge + jam-bridge); "Stand Down" calls POST /api/tx/standdown (reverse).
// Both are commander-gated server-side (require_commander) and audited to the
// hash-chained mission log; this only OFFERS the control to commanders. Like
// ResumeTx/EmergencyAbort it uses a two-step click-to-arm/click-to-confirm
// pattern so an accidental single click never flips the transmit subsystem.
//
// SAFETY: bringing TX online does NOT clear the master TX-halt — the fire path
// stays blocked until a commander also presses RESUME TX. This button never
// touches the halt/arm/range-auth/IFF gates.
export default function TxOnlineControl({ bridgesOnline, onChanged }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  // Reset any half-armed confirm if the observed state flips underneath us
  // (e.g. another console performed the handoff).
  useEffect(() => { setConfirming(false); }, [bridgesOnline]);

  const goingOnline = !bridgesOnline;

  const run = async () => {
    setBusy(true);
    try {
      const path = goingOnline ? "/tx/online" : "/tx/standdown";
      const { data } = await api.post(path);
      if (goingOnline) {
        toast.success("TX ONLINE", {
          description: `SiK handed to transmit (owner: ${data?.host_sik_owner || "rf-bridge"}). TX-halt still applies until RESUME TX.`,
        });
      } else {
        toast.info("TX STOOD DOWN", {
          description: `SiK returned to the passive sniffer (owner: ${data?.host_sik_owner || "sniffer"}). Logged.`,
        });
      }
      onChanged?.();
    } catch (e) {
      toast.error(goingOnline ? "Bring TX Online failed" : "Stand Down failed", {
        description: formatApiError(e),
      });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  const Icon = goingOnline ? Power : PowerOff;
  const idleLabel = goingOnline ? "BRING TX ONLINE" : "STAND DOWN";
  const confirmLabel = goingOnline ? "CONFIRM ONLINE" : "CONFIRM STAND DOWN";
  const tone = goingOnline ? "var(--accent-success)" : "var(--accent-warning)";

  return (
    <>
      <button
        data-testid="tx-online-btn"
        onClick={confirming ? run : () => setConfirming(true)}
        disabled={busy}
        className="flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest transition-colors border scanline-btn"
        style={
          confirming
            ? { background: tone, borderColor: tone, color: "#050810" }
            : { color: tone, borderColor: tone }
        }
        title={
          goingOnline
            ? "Hand the SiK radio to the transmit bridges (commander-gated). Does not clear the TX-halt."
            : "Return the SiK radio to the passive RX sniffer (commander-gated)."
        }
      >
        <Icon size={14} strokeWidth={1.5} />
        {confirming ? confirmLabel : idleLabel}
      </button>
      <span data-testid="tx-online-status" role="status" aria-live="assertive" className="sr-only">
        {busy
          ? "TX handoff in progress"
          : confirming
          ? `${idleLabel} armed — activate again to confirm`
          : ""}
      </span>
    </>
  );
}
