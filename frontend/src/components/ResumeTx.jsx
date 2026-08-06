import { useEffect, useState } from "react";
import { api, formatApiError } from "@/lib/api";
import { Power } from "lucide-react";
import { toast } from "sonner";

// Commander-only control that CLEARS the TX-halt safety state by calling the
// existing commander-gated endpoint (POST /emergency/resume). Resuming TX
// re-arms the transmit/takedown path, so — exactly like EmergencyAbort — it
// uses a two-step "click to arm, click again to confirm" pattern rather than
// firing on a single accidental click. The backend enforces require_commander;
// this button only offers the control to commander-role users, and a 403 (or
// any error) surfaces as an error toast rather than a false success.
export default function ResumeTx() {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!confirming) return;
    const t = setTimeout(() => setConfirming(false), 4000);
    return () => clearTimeout(t);
  }, [confirming]);

  const resume = async () => {
    setBusy(true);
    try {
      await api.post("/emergency/resume");
      // Let the existing /api/health poll clear the tx-halted banner naturally.
      toast.success("TX RESUMED", { description: "Transmit path re-armed. Logged." });
    } catch (e) {
      toast.error("Resume failed", { description: formatApiError(e) });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <>
      <button
        data-testid="resume-tx-btn"
        onClick={confirming ? resume : () => setConfirming(true)}
        disabled={busy}
        className={`flex items-center gap-2 px-4 py-2 font-mono text-xs font-bold uppercase tracking-widest transition-colors border scanline-btn ${
          confirming
            ? "text-black border-black pulse-crit"
            : "text-black border-black hover:bg-black hover:text-[#FF3B30]"
        }`}
        style={confirming ? { background: "black", color: "#FF3B30" } : undefined}
        title="Clear the TX-halt safety state and re-arm the transmit path"
      >
        <Power size={14} strokeWidth={1.5} />
        {confirming ? "CONFIRM RESUME" : "RESUME TX"}
      </button>
      <span data-testid="resume-tx-status" role="status" aria-live="assertive" className="sr-only">
        {busy
          ? "Resuming TX, request in progress"
          : confirming
          ? "Resume TX armed — activate again to confirm"
          : ""}
      </span>
    </>
  );
}
