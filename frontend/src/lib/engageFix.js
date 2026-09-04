// engageFix.js — turn a BLOCKED engagement into an operator-friendly toast that
// carries the ONE button which fixes the pre-condition, and (for the two
// commander-gated fixes) apply that fix by calling the same commander-gated
// endpoint the panel uses. Shared by Payloads.jsx and Jamming.jsx so the fire
// flows translate a backend refusal identically — the operator never sees a raw
// `POST /api/...` string.
//
// It NEVER re-fires automatically: it fixes the pre-condition and asks the
// operator to press fire again, preserving the deliberate SafetyGate spine.

import { toast } from "sonner";
import { api, formatApiError } from "@/lib/api";
import { classifyEngageBlock, FIX } from "@/lib/engagementGate";

// Apply a commander-gated fix. Returns a plain-language result.
async function applyFix(fix) {
  if (fix === FIX.resume) {
    await api.post("/emergency/resume");
    return "TX resumed. Fire again to engage.";
  }
  if (fix === FIX.online) {
    await api.post("/tx/online");
    return "TX brought online. Fire again to engage.";
  }
  return null;
}

// Show the translated block. Returns true if it recognized (and handled) the
// block, false if the caller should fall back to its generic error toast.
//   opts.isCommander : whether to offer the commander-gated fix button.
//   opts.onFixed     : called after a fix succeeds (e.g. refresh health).
export function handleEngageBlock({ error, response } = {}, opts = {}) {
  const block = classifyEngageBlock({ error, response });
  if (!block) return false;

  const { isCommander = false, onFixed } = opts;
  const commanderFix = block.fix === FIX.resume || block.fix === FIX.online;

  // Range-auth arming requires the password/confirm-phrase modal, so we point
  // the operator to the always-visible range-authorization banner rather than
  // firing a one-click endpoint. Still plain language, still no raw API text.
  if (block.fix === FIX.rangeAuth) {
    toast.error(block.title, {
      description: isCommander
        ? `${block.message} Arm it from the RANGE AUTHORIZATION banner at the top of the console.`
        : `${block.message} A commander must arm it from the range-authorization banner.`,
    });
    return true;
  }

  if (commanderFix && isCommander) {
    toast.error(block.title, {
      description: block.message,
      action: {
        label: block.fixLabel,
        onClick: async () => {
          try {
            const msg = await applyFix(block.fix);
            toast.success(block.fixLabel + " — done", { description: msg });
            onFixed?.();
          } catch (e) {
            toast.error(block.fixLabel + " failed", { description: formatApiError(e) });
          }
        },
      },
    });
    return true;
  }

  // Recognized block, but the current user cannot apply the fix (non-commander),
  // or there is no one-click fix (helper unavailable). Explain in plain language
  // who/what is needed — never the raw backend string.
  toast.error(block.title, {
    description: commanderFix
      ? `${block.message} A commander must press ${block.fixLabel}.`
      : block.message,
  });
  return true;
}
