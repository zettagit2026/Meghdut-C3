import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "@/lib/api";

// Shared by RangeAuthorizationBanner.jsx and RangeAuthorizationControl.jsx —
// both poll GET /api/range-authorization/status for a given effect on their
// own independent timers but with identical polling/staleness semantics, so
// that logic lives here once instead of being duplicated in both components.
//
// task #146: previously a failed poll was swallowed by an empty catch block,
// so if polling died (backend restart, network partition, auth expiry) the
// RANGE LIVE banner and countdown would freeze on the last-known-good
// response with zero indication that monitoring itself had stopped. Because
// the countdown is computed client-side from status.expires_at independent
// of poll success, a frozen countdown can silently hit 00:00 while the real,
// server-confirmed range state is actually unknown — an operator could
// misread that as "safe" / "expired cleanly" when in fact no one can say
// whether the range is still live. This hook tracks last-successful-poll
// timestamp + consecutive failure count (same pattern as SystemHealth.jsx's
// task #144 fix) so callers can render a "STATUS UNCONFIRMED" indicator
// alongside — never instead of — the last-known data.

const POLL_INTERVAL_MS = 3000;
const MAX_CONSECUTIVE_FAILURES = 3;
const STALE_THRESHOLD_MS = POLL_INTERVAL_MS * 4; // ~12s

function fmtRemaining(seconds) {
  if (seconds == null || seconds < 0) return "--:--";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function useRangeAuthStatus(effect) {
  const [status, setStatus] = useState(null);
  const [remaining, setRemaining] = useState(null);
  const [lastSuccessAt, setLastSuccessAt] = useState(null);
  const [consecutiveFailures, setConsecutiveFailures] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const statusRef = useRef(null);

  const poll = useCallback(async () => {
    try {
      const { data } = await api.get("/range-authorization/status", { params: { effect } });
      setStatus(data);
      statusRef.current = data;
      setRemaining(data?.seconds_remaining ?? null);
      setLastSuccessAt(Date.now());
      setConsecutiveFailures(0);
      return data;
    } catch {
      setConsecutiveFailures((n) => n + 1);
      return statusRef.current;
    }
  }, [effect]);

  useEffect(() => {
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => clearInterval(id);
  }, [poll]);

  // Tick a local clock so staleness (time-since-last-success) updates even
  // when no new poll result has arrived at all.
  useEffect(() => {
    const tickId = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tickId);
  }, []);

  // Live countdown ticking between polls, derived from expires_at when
  // present. Deliberately does NOT get suppressed/reset by poll failures —
  // it keeps counting down using the last-known expires_at, which is why
  // the staleness signal below must be surfaced independently: a countdown
  // reaching 00:00 while polling is failing does NOT mean the range is
  // confirmed safe/expired, it means the real state is unknown.
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

  const staleByAge = lastSuccessAt != null && now - lastSuccessAt > STALE_THRESHOLD_MS;
  const staleByFailures = consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;
  const neverSucceeded = lastSuccessAt == null && consecutiveFailures > 0;
  const statusUnconfirmed = staleByAge || staleByFailures || neverSucceeded;

  // Lets a caller apply a status object it obtained directly from a
  // successful mutating call (e.g. the enable/disable POST responses in
  // RangeAuthorizationControl.jsx) without waiting for the next poll tick.
  // A direct successful response is just as good a freshness signal as a
  // successful poll, so this also resets the failure/staleness tracking.
  const applyStatus = useCallback((data) => {
    setStatus(data);
    statusRef.current = data;
    setRemaining(data?.seconds_remaining ?? null);
    setLastSuccessAt(Date.now());
    setConsecutiveFailures(0);
  }, []);

  return {
    status,
    remaining,
    refresh: poll,
    applyStatus,
    statusUnconfirmed,
    lastSuccessAt,
    now,
  };
}

export { useRangeAuthStatus, fmtRemaining };
