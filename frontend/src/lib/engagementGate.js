// engagementGate.js — shared logic for the operator-facing engage-flow.
//
// Three jobs:
//   1. deriveTxChips(health): turn the backend /health tx_subsystem block into
//      plain-language, color-coded status chips, grouped so they parse
//      structurally (group: "transmit" | "authorization" | "link") instead of
//      one flat pipe-separated row. Labels are deliberately UNAMBIGUOUS: the
//      transmit-bridge chip reads "TX BRIDGES: ONLINE/OFFLINE" and the master
//      transmit-halt chip reads "MASTER-HALT: CLEARED/ENGAGED" so the two
//      never sit side by side reading as opposites of the same word ("TX
//      OFFLINE" next to "TX LIVE" was the S1 operator-reported confusion —
//      those are two DIFFERENT subsystems: bridges being brought online vs.
//      the master halt being cleared).
//   2. deriveReadiness(health): the ONE plain-language fire-readiness verdict
//      ("READY TO FIRE" vs "NOT READY — <single next action>"), worst-blocker
//      first, mirroring the exact precondition order the backend gates fire
//      in (tx_halted -> bridges_online -> range_auth). This is the single
//      source of truth for that verdict — every surface that needs it
//      (header strip, Engagement Control panel) calls this, never a second
//      copy of the priority logic.
//   3. classifyEngageBlock(): translate a BLOCKED fire — whether it came back
//      as an HTTP error (tx-halt / range-auth) or as a 200 response that never
//      actually transmitted (no TX bridge subscribed) — into a plain-language
//      reason plus the ONE button that fixes it. A fielded operator must never
//      see a raw `POST /api/...` backend string; this is what intercepts it.
//
// This file NEVER weakens a safety gate. It only describes state and maps a
// backend refusal to the correct GUI fix control. The commander still has to
// press the fix button, which calls the same commander-gated endpoint.

import { formatApiError } from "@/lib/api";

// The dedicated MAVLink TX radio, surfaced as a stable identity chip so the
// operator can confirm at a glance which physical radio is wired for transmit.
// (Device-pinned; see project bring-up notes.)
export const TX_RADIO_LABEL = "930c";

export const TONE = {
  ok: "ok",         // green — good/ready
  warn: "warn",     // amber — attention/precondition not met
  crit: "crit",     // red — halted/blocked
  idle: "idle",     // muted — unknown/not-applicable
};

// Map a tone to the console's CSS custom properties (AA-legible in both themes;
// these vars are defined for light + dark in the app theme).
export function toneColor(tone) {
  switch (tone) {
    case TONE.ok: return "var(--accent-success)";
    case TONE.warn: return "var(--accent-warning)";
    case TONE.crit: return "var(--accent-critical)";
    default: return "var(--text-muted)";
  }
}

// Safe accessor: returns the tx_subsystem block with conservative defaults so a
// missing/partial /health never crashes the UI or paints a falsely-green chip.
export function readTxSubsystem(health) {
  const tx = health?.tx_subsystem || {};
  return {
    bridges_online: !!tx.bridges_online,
    tx_halted: tx.tx_halted !== false, // default HALTED (fail-closed) if unknown
    sik_link_up: !!tx.sik_link_up,
    sik_owner: tx.sik_owner ?? null,
    sik_link_mode: tx.sik_link_mode ?? null, // "tx" (rf-bridge owns+consumer up) | "rx" (recent sniffer RX) | null (down)
    tx_bridge_consumers: Array.isArray(tx.tx_bridge_consumers) ? tx.tx_bridge_consumers : [],
    range_auth: tx.range_auth || {},
    _present: !!health?.tx_subsystem,
  };
}

// True iff ANY range-auth effect lease is currently armed.
export function anyRangeAuthArmed(txSub) {
  const ra = txSub?.range_auth || {};
  return Object.values(ra).some((l) => l && l.enabled);
}

// Group labels for the header's clustered layout (GROUP, don't string) — the
// UI reads these off each chip's `group` field rather than re-deriving them.
export const CHIP_GROUP = {
  transmit: "transmit",           // bridges-online + master-halt + radio identity
  authorization: "authorization", // range-auth lease
  link: "link",                   // SiK radio link
};

// Build the ordered chip list for the header indicator + the panel.
export function deriveTxChips(health) {
  const tx = readTxSubsystem(health);
  const raArmed = anyRangeAuthArmed(tx);
  return [
    {
      key: "tx-online",
      group: CHIP_GROUP.transmit,
      // Deliberately "TX BRIDGES: …" — NEVER bare "TX ONLINE/OFFLINE" — so this
      // can never be misread as the opposite of the master-halt chip below.
      // This chip answers "are the transmit bridges brought up?".
      label: tx.bridges_online ? "TX BRIDGES: ONLINE" : "TX BRIDGES: OFFLINE",
      tone: tx.bridges_online ? TONE.ok : TONE.warn,
      title: tx.bridges_online
        ? "Transmit bridges are online and own the SiK radio."
        : "Transmit bridges are offline — a commander must Bring TX Online before firing.",
    },
    {
      key: "tx-halt",
      group: CHIP_GROUP.transmit,
      // Deliberately "MASTER-HALT: …" — NEVER bare "TX HALTED/LIVE" — so this
      // can never be misread as the opposite of the TX-bridges chip above.
      // This chip answers "is the master transmit halt (tx_halted) in effect?".
      label: tx.tx_halted ? "MASTER-HALT: ENGAGED" : "MASTER-HALT: CLEARED",
      tone: tx.tx_halted ? TONE.crit : TONE.ok,
      title: tx.tx_halted
        ? "Master transmit halt is IN EFFECT — a commander must RESUME TX before firing."
        : "Transmit halt is cleared — the fire path is live.",
    },
    {
      key: "sik-link",
      group: CHIP_GROUP.link,
      // Owner-aware, honest label. UP is only ever set from genuine liveness the
      // backend proved (rf-bridge owns the SiK with a connected TX consumer, or a
      // recent sniffer RX). Show WHICH mode so "UP" is never ambiguous.
      label: tx.sik_link_up
        ? (tx.sik_link_mode === "tx" ? "SiK LINK UP (TX)" : "SiK LINK UP (RX)")
        : "SiK LINK DOWN",
      tone: tx.sik_link_up ? TONE.ok : TONE.warn,
      title: tx.sik_link_up
        ? (tx.sik_link_mode === "tx"
            ? "SiK radio link is up — owned by rf-bridge for transmit (a TX bridge consumer is connected)."
            : "SiK radio link is up (recent RX confirmed by the passive sniffer).")
        : "No recent SiK RX and no connected TX bridge — link may be down or idle.",
    },
    {
      key: "tx-radio",
      group: CHIP_GROUP.transmit,
      label: `TX RADIO ${TX_RADIO_LABEL}`,
      tone: tx.bridges_online ? TONE.ok : TONE.idle,
      title: `Dedicated MAVLink TX radio (${TX_RADIO_LABEL}). ${
        tx.bridges_online ? "Owned by rf-bridge (transmit-ready)." : "Not currently held for transmit."
      }`,
    },
    {
      key: "range-auth",
      group: CHIP_GROUP.authorization,
      label: raArmed ? "RANGE-AUTH ARMED" : "RANGE-AUTH NONE",
      tone: raArmed ? TONE.ok : TONE.warn,
      title: raArmed
        ? "A live-range authorization lease is armed."
        : "No range-authorization lease armed — arm it for the intended effect before firing.",
    },
  ];
}

// ---- Blocked-fire translation ------------------------------------------------
// Recognized fix kinds the UI knows how to offer a button for.
export const FIX = {
  resume: "resume",       // clear the TX halt (POST /emergency/resume)
  online: "online",       // bring TX bridges online (POST /tx/online)
  rangeAuth: "range_auth", // arm the range-authorization lease (GUI banner/control)
};

// Classify a blocked engagement. Accepts either:
//   { error }    — a thrown axios error from a deploy/jam call, OR
//   { response } — a 200 response body that reports it did NOT transmit
//                  (tx_bridge_subscribed === false).
// Returns null when it is not a recognized precondition block (caller should
// fall back to its generic error toast); otherwise a plain-language descriptor
// with the single fix control to offer.
export function classifyEngageBlock({ error, response } = {}) {
  // Case A: 200-but-nothing-transmitted (no TX bridge subscribed) => OFFLINE.
  if (response && response.tx_bridge_subscribed === false) {
    return {
      reason: "offline",
      title: "TX subsystem OFFLINE",
      message:
        "No transmit bridge is online, so nothing was sent to the radio. " +
        "Bring TX Online, then fire again.",
      fix: FIX.online,
      fixLabel: "Bring TX Online",
    };
  }

  if (!error) return null;
  const status = error?.response?.status;
  const text = (formatApiError(error) || "").toLowerCase();

  // Case B: master TX halt in effect.
  if (
    status === 409 &&
    (text.includes("halt") || text.includes("emergency abort") || text.includes("emergency/resume"))
  ) {
    return {
      reason: "halted",
      title: "Transmit is HALTED",
      message:
        "The master transmit halt is in effect (emergency abort). " +
        "A commander must RESUME TX before this can fire.",
      fix: FIX.resume,
      fixLabel: "Resume TX",
    };
  }

  // Case C: range-authorization lease is OFF.
  if (status === 409 && text.includes("range authorization")) {
    return {
      reason: "range_auth",
      title: "Range authorization not armed",
      message:
        "The live-range authorization lease for this effect is OFF. " +
        "A commander must arm it before this can transmit.",
      fix: FIX.rangeAuth,
      fixLabel: "Arm Range-Auth",
    };
  }

  // Case D: TX bridge control helper not installed / unreachable (503 from the
  // /tx/online endpoint path, surfaced if a fire flow ever triggers it).
  if (status === 503 && text.includes("helper")) {
    return {
      reason: "helper_unavailable",
      title: "TX control helper unavailable",
      message:
        "The transmit host's bridge-control helper is not available, so TX " +
        "cannot be brought online from here. This needs the deploy-time host install.",
      fix: null,
      fixLabel: null,
    };
  }

  return null;
}

// ---- Fire-readiness verdict --------------------------------------------------
// THE single plain-language answer to "can I fire right now, and if not, what
// is the ONE next action?" — worst-blocker-first, mirroring the exact order
// the backend preconditions actually gate in (tx_halted -> bridges_online ->
// range_auth). This does not evaluate a new rule; it reads the same
// tx_subsystem state deriveTxChips reads and orders it by what the operator
// must fix FIRST. Every surface that shows a readiness verdict (the header
// strip, the Engagement Control panel) calls this — never a second copy of
// the priority logic.
export function deriveReadiness(health) {
  const tx = readTxSubsystem(health);
  const raArmed = anyRangeAuthArmed(tx);

  if (tx.tx_halted) {
    return {
      ready: false,
      tone: TONE.crit,
      action: "RESUME TX",
      fix: FIX.resume,
      detail: "Fire path BLOCKED — transmit is HALTED. A commander must RESUME TX.",
    };
  }
  if (!tx.bridges_online) {
    return {
      ready: false,
      tone: TONE.warn,
      action: "BRING TX ONLINE",
      fix: FIX.online,
      detail: "Not ready — TX bridges are OFFLINE. A commander must Bring TX Online.",
    };
  }
  if (!raArmed) {
    return {
      ready: false,
      tone: TONE.warn,
      action: "ARM RANGE-AUTH",
      fix: FIX.rangeAuth,
      detail: "TX online — arm RANGE-AUTH for the intended effect before firing.",
    };
  }
  return {
    ready: true,
    tone: TONE.ok,
    action: null,
    fix: null,
    detail: "Engagement path READY — TX online, halt cleared, range-auth armed.",
  };
}
