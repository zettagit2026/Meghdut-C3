import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { Siren } from "lucide-react";
import { toast } from "sonner";

export default function EmergencyAbort() {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  const abort = async () => {
    setBusy(true);
    try {
      await api.post("/emergency/abort");
      toast.error("EMERGENCY ABORT", { description: "All TX halted. Ceasefire logged." });
    } catch (e) {
      toast.error("Abort failed", { description: formatApiError(e) });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <>
    <button
      data-testid="emergency-abort-btn"
      onClick={confirming ? abort : () => setConfirming(true)}
      disabled={busy}
      className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest transition-colors border scanline-btn ${
        confirming ? "text-white pulse-crit" : "hover:text-white"
      }`}
      style={
        confirming
          ? { background: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
          : { color: "var(--accent-critical)", borderColor: "var(--accent-critical)" }
      }
      onMouseEnter={(e) => { if (!confirming) e.currentTarget.style.background = "var(--accent-critical)"; }}
      onMouseLeave={(e) => { if (!confirming) e.currentTarget.style.background = ""; }}
      title="Halt all RF transmissions instantly"
    >
      <Siren size={14} strokeWidth={1.5} />
      {confirming ? "CONFIRM ABORT" : "EMERGENCY ABORT"}
    </button>
    <span data-testid="emergency-abort-status" role="status" aria-live="assertive" className="sr-only">
      {busy
        ? "Emergency abort, request in progress"
        : confirming
        ? "Emergency abort armed — activate again to confirm"
        : ""}
    </span>
    </>
  );
}
