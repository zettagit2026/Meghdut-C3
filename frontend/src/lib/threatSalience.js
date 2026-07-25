// Alert-salience helpers for CRITICAL-threat contacts (Task #38 / C5).
//
// Design intent (see also backend/DETECTION_DISPLAY_MODEL.md for the sibling
// "don't overstate identity" principle this project already follows -- this
// is the mirror-image problem: "don't understate urgency"):
//
// A CRITICAL threat sitting in a dense, fast-scrolling table must be
// noticeably more salient than a MEDIUM/LOW row, AND a CRITICAL contact that
// JUST appeared must be more salient still than one that has been sitting on
// screen, already noticed, for the last 10 minutes. Today both look
// identical: a static colored badge, no matter how old.
//
// Two independent, additive layers, deliberately NOT full-screen/blocking
// (this project already established that dense-table scannability must not
// be interrupted -- see the deep-link-vs-modal precedent for CEMA/Kill Chain
// navigation):
//
//   1. A brief, DECAYING flash/glow (`isRecentCritical`) on rows where the
//      threat is CRITICAL and `first_seen` is within the last
//      CRITICAL_FLASH_WINDOW_MS -- draws the eye to what's NEW, then gets out
//      of the way. This is intentionally a one-shot, time-bounded CSS
//      animation (see `.threat-critical-flash` in index.css), NOT an
//      infinite pulse -- `.pulse-crit` (infinite) already exists in this
//      codebase but is reserved for active safety-interlock states
//      (EmergencyAbort, RangeAuthorizationBanner/Control, SafetyGate,
//      KillChain fire buttons) where continuous urgency is correct because
//      the underlying condition is still live. A newly-detected contact is a
//      one-time event, not an ongoing interlock -- an infinite pulse on every
//      CRITICAL row would very quickly become visual noise operators tune
//      out (the alert-fatigue failure mode this task explicitly warns
//      against), and would also collide/compete visually with the small
//      number of rows that legitimately need continuous attention.
//
//   2. A persistent-but-quiet visual weight increase (`.threat-critical-static`,
//      a left border accent) for ALL CRITICAL rows regardless of age -- so
//      even after the flash decays, a CRITICAL contact remains distinguishable
//      at a glance from MEDIUM/LOW rows in a dense table, without any motion
//      or color intensity that would compete with the flash's job of marking
//      what's NEW. HIGH/MEDIUM/LOW never get either treatment: only CRITICAL
//      is rare and serious enough to warrant escalation, by design -- treating
//      every threat level as urgent is exactly how alert fatigue starts.
export const CRITICAL_FLASH_WINDOW_MS = 8000;

export function isRecentCritical(d, now) {
  if (!d || d.threat_level !== "CRITICAL") return false;
  const seenAt = d.first_seen || d.last_seen;
  if (!seenAt) return false;
  const t = new Date(seenAt).getTime();
  if (Number.isNaN(t)) return false;
  const age = now - t;
  return age >= 0 && age < CRITICAL_FLASH_WINDOW_MS;
}

export function isStaticCritical(d) {
  return !!d && d.threat_level === "CRITICAL";
}
