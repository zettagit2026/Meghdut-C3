// One-shot audio cue for newly-appeared CRITICAL contacts (Task #38 / C5).
//
// Deliberately NOT a new dependency: this project has no existing audio
// infrastructure (no <audio> assets, no sound library), so this uses the
// browser-native Web Audio API to synthesize a single short two-tone beep --
// zero bytes of audio asset, zero npm package.
//
// Alert-fatigue guardrails, by design:
//   - Fires ONCE per detection id, ever (tracked in a module-level Set) --
//     never loops, never re-fires on re-render or re-poll of the same
//     contact. A CRITICAL contact that has already announced itself does not
//     need to keep announcing itself; that is exactly the kind of repetition
//     that trains operators to ignore the sound.
//   - CRITICAL only. HIGH/MEDIUM/LOW never make sound -- an audio cue is the
//     loudest (literally) escalation tool available, so it is reserved for
//     the rarest, most serious category.
//   - User-muteable, persisted in localStorage, and OFF by default is not
//     chosen here -- default ON, because a missed high-threat alert has real
//     consequences per this project's brief -- but the mute control is a
//     single click and the preference persists across sessions/reloads.
const PLAYED_IDS = new Set();
const MUTE_KEY = "meghdut.criticalAlertMuted";

export function isCriticalAlertMuted() {
  try {
    return localStorage.getItem(MUTE_KEY) === "1";
  } catch {
    return false;
  }
}

export function setCriticalAlertMuted(muted) {
  try {
    localStorage.setItem(MUTE_KEY, muted ? "1" : "0");
  } catch {
    /* ignore (private browsing / storage disabled) */
  }
}

let audioCtx = null;
function getAudioContext() {
  if (audioCtx) return audioCtx;
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return null;
  audioCtx = new Ctor();
  return audioCtx;
}

// Short, unmistakable but non-alarming two-tone "notice" beep (~220ms total).
// Intentionally not a klaxon/siren -- this fires on every newly-seen
// CRITICAL contact, potentially several times a shift, so it needs to be
// noticeable without being unpleasant enough to make operators want to mute
// it permanently (which would defeat the point).
function playBeep() {
  const ctx = getAudioContext();
  if (!ctx) return;
  if (ctx.state === "suspended") ctx.resume().catch(() => {});
  const now = ctx.currentTime;
  [880, 1175].forEach((freq, i) => {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "square";
    osc.frequency.value = freq;
    const start = now + i * 0.11;
    const end = start + 0.09;
    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.12, start + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);
    osc.connect(gain).connect(ctx.destination);
    osc.start(start);
    osc.stop(end + 0.02);
  });
}

// Call once per poll cycle with the list of currently-recent-CRITICAL
// detection ids (see isRecentCritical in threatSalience.js). Plays at most
// one beep per id, ever, and is a no-op if muted.
export function announceNewCriticalContacts(recentCriticalIds) {
  if (isCriticalAlertMuted()) return;
  let firedAny = false;
  for (const id of recentCriticalIds) {
    if (!PLAYED_IDS.has(id)) {
      PLAYED_IDS.add(id);
      firedAny = true;
    }
  }
  if (firedAny) playBeep();
}
