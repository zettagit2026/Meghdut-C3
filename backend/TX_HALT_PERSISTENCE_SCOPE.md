# TX-Halt Persistence — Scoping Doc (Task #136)

**Status**: scoping only, no implementation yet.
**Trigger**: 2026-07-29 gap audit — `_tx_halted` (server.py:535) is a plain
in-memory global. A backend restart after an emergency abort silently resets
it to `False`, re-enabling jam/MAVLink-injection/GNSS-spoof TX with no
operator action and no record that it happened.

## 1. Current control flow (as read from server.py)

- `_tx_halted = False` — module-level global, line 535.
- Set `True` by `POST /api/emergency/abort` (line 3451-3468, `get_current_user`
  — any authenticated operator, not commander-gated — intentionally, this is
  a safety stop not a privileged action).
- Set `False` by `POST /api/emergency/resume` (line 3471-3484,
  `require_commander` — clearing a halt IS commander-gated).
- Checked by `_check_tx_not_halted()` (line 538-541), called before
  `/payloads/deploy`, `/mavlink/broadcast`, jam deploy, GNSS spoof deploy —
  raises HTTP 409 if halted.
- Never read from or written to Mongo anywhere. Never included in
  `GET /api/health` today (confirmed against `backend/tests/test_new_endpoints.py`
  `TestHealth.test_health_shape`, which asserts a fixed field list that does
  not include `tx_halted`).
- Both abort and resume already call `log_event()` (ABORT kind) and broadcast
  a WS message — so the *operator-driven* transitions are already audited.
  The gap is specifically the *unaudited, unattended* transition that happens
  silently at process start.

## 2. Existing precedent in this codebase for safety-state persistence

This is the key finding and it drives the recommendation below.

Every other safety-adjacent piece of transient state in this file is
**deliberately in-memory only, and deliberately fails to the safe side on
restart**:

- `_range_authorization` (lines 421-441): explicit comment says "in-memory
  ONLY (never persisted to Mongo, never survives a restart — defaults OFF
  every boot...)". Range authorization is a *permissive* lease (jam/mavlink
  allowed) — its safe default is OFF/disabled, and that's exactly what a
  restart gives it for free, with zero extra code.
- `_arm_tokens`, `_jam_confirm_tokens`: same in-memory, reset-to-empty-on-
  restart convention. Their safe default is "no valid token" — also free on
  restart.
- `_pending_acks`, `_range_auth_failures`, `_login_failures`: same pattern —
  in-memory, reset-on-restart, and the reset direction is always the
  conservative one (no pending optimistic state, no accumulated lockout
  state carried over, but also no bypass of a lockout since a restart during
  an active attack is not a realistic operator workflow here).

**There is no precedent anywhere in this codebase for persisting
safety-critical state to Mongo to survive a restart.** The established
convention is: keep it in memory, and make sure the in-memory default is the
conservative one for whatever that piece of state gates.

`_tx_halted` is the one place this codebase's own convention was violated:
its in-memory default (`False` = TX enabled) is the *permissive* direction,
not the conservative one. Every other piece of ephemeral safety state in
this file defaults to "off/no/locked" on restart; `_tx_halted` alone defaults
to "go."

## 3. Recommendation: fail-closed default, not Mongo persistence

**Do not persist `_tx_halted` to Mongo. Change its default so a fresh process
always starts TX-HALTED, and require an explicit commander `/emergency/resume`
call to clear it — regardless of what state it was in before the restart.**

Reasoning:

1. **Consistency with established codebase convention.** This is not a new
   pattern — it's fixing `_tx_halted` to follow the same rule already
   applied to range authorization, arm tokens, and jam-confirm tokens in the
   same file. No new infrastructure, no new failure mode class (Mongo
   read-path latency/availability now gating every TX call), no new drift
   risk between "what Mongo says" and "what memory says."
2. **A restart is exactly the class of event that should NOT be trusted to
   remember "everything was fine."** The scenario this task is scoped
   against — crash, redeploy, host reboot — is precisely the situation where
   you have the least confidence about what state the system was actually in
   right before the restart, and the highest incentive to make the machine
   re-prove it's safe rather than assume so. This mirrors the `ml-classify-
   bridge` incident class explicitly cited in the task: don't let a process
   restart silently resume unattended behavior.
3. **The "operationally annoying on planned maintenance reboots" cost is
   real but small and correctly placed.** A commander already has to take an
   explicit action (`POST /api/emergency/resume`, which is already
   commander-gated) to bring TX back up. Requiring that one extra step after
   *every* planned restart is a small, visible, auditable cost paid by the
   only role authorized to lift a halt anyway — it does not add a new
   permission gate, it just makes an already-required gate fire slightly
   more often. Compare that to the alternative failure mode — TX silently
   re-enabling itself with no operator in the loop — which is unacceptable
   for a system that can jam, spoof GNSS, or inject MAVLink frames.
4. **A persisted "remember what the operator last set" model sounds more
   convenient but is strictly worse here.** It reintroduces exactly the
   failure this task exists to close, just one layer down: now "was TX
   halted" depends on trusting that the last Mongo write actually landed
   before the crash, that the read on the way back up succeeds, and that
   nothing raced between the crash and the write. It also means a *corrupted
   or stale* Mongo document (bad migration, replica lag, wrong DB pointed at
   in a redeploy) could resume TX with no operator input at all — silently
   reintroducing the identical bug this task is trying to close, just via a
   database instead of a Python global.

**Net**: fail-closed-by-default is both the more defensible safety posture
and the lower-engineering-cost option, and it is what this codebase already
does everywhere else. Recommend it without reservation.

## 4. Audit trail on startup

Regardless of which model were chosen, log the startup TX-halt state
explicitly — this is cheap and unambiguous. With the fail-closed
recommendation, there are exactly two cases to log from `startup()`
(server.py:826):

- Every boot: `log_event("TX_HALT_STARTUP", "Backend started in TX-HALTED state (fail-closed default) — a commander must POST /api/emergency/resume to enable TX.", actor="SYSTEM")`.

There is no "resuming prior emergency-abort" case to log, because the
recommendation is to never resume any state automatically — startup is
unconditionally halted, unconditionally logged as such, every single time.
This also means the startup log line is always the same message, which
makes it trivially greppable in `mission_log`/the hash-chained audit report
for after-action review ("was TX ever silently re-enabled" becomes "search
for any TX-permitting transition NOT preceded by a commander-attributed
`ABORT`/resume log line").

## 5. Race/consistency

Not applicable to the recommended design — there is no Mongo read on the TX
check path at all, so no staleness/latency tradeoff exists. Confirmed via
`backend/Dockerfile` (`CMD ["uvicorn", "server:app", "--host", "0.0.0.0",
"--port", "8001"]`, no `--workers` flag) that this backend runs as a single
uvicorn process today — so even if a future task did choose to persist to
Mongo for some other reason, there is currently only one in-memory copy of
`_tx_halted` to keep consistent, not N. Worth a one-line note in code if this
is ever changed to multi-worker, but out of scope for this task.

## 6. Backward compatibility with existing tests

- `backend/tests/test_new_endpoints.py::TestEmergencyAbort` only calls
  `POST /api/emergency/abort` and checks the response + a `mission_log` entry
  with `kind == "ABORT"`; it never calls `/emergency/resume` and never
  asserts `_tx_halted`'s pre-abort value. **Unaffected** by making the
  default `True` instead of `False`.
- `backend/tests/test_new_endpoints.py::TestHealth.test_health_shape` asserts
  a fixed field allowlist that does not include `tx_halted` today. Adding a
  `tx_halted` field to `/api/health` (recommended, see plan below) is
  additive and does not break this test, but the follow-up build task should
  add an explicit assertion for the new field.
- No test file anywhere in `backend/tests/` currently asserts `_tx_halted`
  starts `False`, deploys a payload without first calling `/emergency/resume`,
  or otherwise assumes TX is enabled by default. **However**: any test that
  calls `/payloads/deploy`, `/mavlink/broadcast`, jam-deploy, or GNSS-spoof-
  deploy endpoints without an explicit `/emergency/resume` call first will
  start failing with HTTP 409 once the fail-closed default ships (grep
  `backend/tests/test_e2e_deploy_bridge.py`, `test_jam_bluetooth_band.py`,
  `test_gnss_spoof_geodesic.py` for this in the follow-up task before
  merging — they were not in scope to fully trace here but are the obvious
  candidates given their names).

## 7. Implementation plan (for the follow-up build task — do not build yet)

1. `backend/server.py` line 535: change `_tx_halted = False` to
   `_tx_halted = True` with an updated comment explaining the fail-closed
   rationale (point back to this doc).
2. `backend/server.py` `startup()` (line 826): add the
   `log_event("TX_HALT_STARTUP", ...)` call described in §4, after Mongo
   indexes are created (log_event needs `db` available, which it already is
   by that point in the function).
3. `backend/server.py` `GET /api/health` (line 3309): add a `tx_halted: bool`
   field to the response so the dashboard/pre-demo check surfaces the halt
   state without requiring a separate call — closes the "nothing in
   `/api/health` reflects this" gap named in the task.
4. No Mongo collection changes, no new schema, no new endpoint needed —
   `/emergency/abort` and `/emergency/resume` stay exactly as they are today.
5. `backend/tests/test_new_endpoints.py`: extend `TestHealth.test_health_shape`
   to assert `tx_halted` is present and add a new test asserting a fresh
   process (or at minimum, a test ordered before any abort/resume call in the
   module) sees `tx_halted is True` by default; add a startup-log assertion
   (`kind == "TX_HALT_STARTUP"` present in `/logs`) if the test harness can
   observe a real process boot.
6. Audit `test_e2e_deploy_bridge.py`, `test_jam_bluetooth_band.py`,
   `test_gnss_spoof_geodesic.py` (and any other test hitting a TX-gated
   endpoint) and add an explicit `POST /api/emergency/resume` commander call
   in setup/fixtures wherever a test currently assumes TX starts enabled.
7. Update the frontend (if it surfaces `/api/health` on a dashboard tile,
   per the "System health (dashboard tile + pre-demo check)" comment at
   server.py:3306-3308) to show a TX-HALTED banner distinct from the
   existing abort banner, since this will now be the default state on every
   fresh deploy/restart until a commander explicitly resumes.
