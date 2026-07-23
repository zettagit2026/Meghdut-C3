# Range Authorization Redesign: Replacing `CEMA_AUTHORIZED_RANGE` with a GUI-Controlled Flag

**Status**: Design/threat-model pass. No implementation yet.
**Scope**: Replaces ONLY the backend-env-var layer described in `backend/server.py`'s
module docstring ("the physical bridge host ... independently refuses to transmit
unless `CEMA_AUTHORIZED_RANGE=1` is set in its OWN environment") for both
`field-bridge/jam_bridge.py` (RF jamming) and the MAVLink payload-deploy bridge.

**Explicit decision on record**: the operator has been told this removes a
deliberate defense-in-depth layer (an out-of-band, physical-access-gated
control) and has confirmed they want it removed anyway, on their own hardware,
in their own authorized range. This document does not re-litigate that
decision. It designs the safest achievable replacement.

**Explicitly out of scope / unchanged**: `SafetyGate.jsx`'s two-step confirm,
`jam_confirm_token` issue/consume, `arm_token` issue/consume, the
`authorized_target` friendly-fire interlock, `require_commander` RBAC, and
`_check_tx_not_halted`/emergency-abort. All of these stay exactly as they are.
This redesign adds one new, narrowly-scoped gate; it does not touch the
others.

---

## 1. Threat Model: What Changes

### 1.1 What the env-var layer was actually buying you

`CEMA_AUTHORIZED_RANGE=1` was not "another password." Its security value came
from three properties that have nothing to do with authentication:

- **Out-of-band**: it lives on a different host (the field-bridge), reachable
  only via SSH/console, not via the web app's attack surface at all.
- **Physical/operational friction**: someone has to be at, or have shell
  access to, the actual RF/MAVLink hardware host to flip it — which in
  practice correlates strongly with "someone is standing in the range,
  looking at the antenna, and knows live fire is about to happen."
- **Compromise independence**: a full compromise of the web app (stolen JWT,
  XSS, a backend RCE, a malicious insider with a commander account) still
  cannot cause a live transmission, because the attacker doesn't have a
  session on the bridge host.

Removing it collapses "is this range live" into a single control plane: the
web backend and whoever holds a valid commander credential for it.

### 1.2 New attack surface introduced

| # | Threat | Description | Why it didn't exist before |
|---|--------|-------------|------------------------------|
| T1 | **Stolen/leaked commander JWT** | `create_access_token` issues a bearer JWT good for **12 hours**, no built-in revocation list, validated purely by signature. Anyone holding that token (phished, exfiltrated from a laptop, intercepted on an insecure network, pulled from browser storage via any XSS) can now flip range-authorization ON from *anywhere the API is reachable* — not just from the range. | Previously an attacker with a stolen JWT could still only get as far as minting `jam_confirm_token`/`arm_token` and calling `/payloads/jam` — the bridge would refuse to key up regardless. |
| T2 | **XSS in the frontend** | Any script-injection bug in the React app (dependency vuln, unsanitized render, malicious extension) can silently call the toggle endpoint using the victim's existing session, with no user-visible action required beyond them having the tab open. | Same as T1 — previously capped by the bridge-side gate. |
| T3 | **Session/token replay across network boundaries** | Because the flag now lives in the backend (reachable over the network, likely beyond just localhost), a commander authenticating from a laptop on VPN, a coffee shop, or a misconfigured public-facing deployment can arm live-fire from off-range. This turns a *physical safety control* into a *network security control*, which is a strictly weaker guarantee against sophisticated adversaries and a strictly weaker guarantee against simple human error (a commander flips it on while testing at their desk and forgets). | Previously physically impossible without hands on the bridge box. |
| T4 | **Forgotten/stale authorization** | A durable on/off toggle has no natural "forgetting" mechanism the way "someone has to walk over and SSH in again" does. If nothing time-bounds it, "range hot" could persist for days across multiple sessions/shifts if nobody remembers to turn it off. | The env var required a fresh manual action tied to a specific session on the host; there was no way to "leave it on by accident" across a service restart, and it was set immediately before a test window, not left running indefinitely. |
| T5 | **Insider/rogue-operator single point of failure** | A single commander account, if a person with that role decides to act unilaterally or is coerced, can now both arm the whole system AND execute — with no independent human in the loop on a separate machine. | Previously required a second, physically-present actor with server access (which in practice was often the same senior/trusted operator, but still a distinct action requiring physical presence and technical access, i.e. a soft two-person-adjacent control). |
| T6 | **Blast radius of a single mistake or bug scales up** | A UI bug that fires the toggle-off/toggle-on request unexpectedly (bad button wiring, a stale request replaying after network retry, a test script left pointed at prod) can now flip live-fire state purely in software. | Same class of bug previously could not cause a live TX because the bridge-side check was independent of anything the frontend did. |

### 1.3 What is explicitly NOT made worse

To be fair to the design: `jam_confirm_token`, `arm_token`, and
`authorized_target` remain per-action, single-use, short-TTL, and
independently checked server-side before every single transmit. This
redesign only removes the layer that gated *whether the bridge host is
willing to key up at all*, not the per-shot interlocks. So even in the worst
case (T1/T2 above), an attacker still cannot cause a transmission without
also successfully forging a fresh arm-token + jam-confirm-token flow (which
requires the SafetyGate UI sequence to have run) — unless they also compromise
those, which is a materially harder combined attack. The redesign's job is to
make the *new* single point of failure (the authorization flag itself) as
hard to trigger and as self-limiting as everything else already is.

---

## 2. Mitigations

Recommendation up front: **treat the GUI toggle as equal in ceremony to
arming a weapon, not as equal in ceremony to a settings checkbox.** Concretely:

### 2.1 RBAC — `require_commander` is necessary but not sufficient

Keep `require_commander` (it's the existing, working elevated-role check),
but **do not treat it as sufficient on its own** for this specific action.
Reasons:
- `require_commander` only proves *the JWT claims a commander role*, which
  under T1/T2 is exactly the thing that can be silently forged/replayed.
- The action being gated (whether the whole system can transmit at all) is
  categorically more consequential than any single `/payloads/jam` or
  `/payloads/deploy` call, so it warrants a stronger check than the same role
  gate used for routine commander actions.

Recommendation: introduce a **step-up authentication requirement** (below) on
top of `require_commander`, rather than a new role. A new role
("range-authorizer") is not warranted here — this is a small ops team, adding
a role just fragments who can do it without adding real security (whoever
holds commander already holds the keys to fire; splitting "arm the range"
into a different role only helps if it's actually a *different person* by
policy, which is a process control, not a code control — worth recommending
as an *operational* policy, not a technical gate).

### 2.2 Require re-authentication at the moment of toggling — YES, mandatory

Do **not** accept the toggle-on request on the strength of an existing valid
session token alone. Require the user to re-submit their password (a
step-up/"sudo mode" pattern) in the same request that enables the flag.

- This single control is the direct countermeasure to T1 and T2: a stolen
  bearer token alone is no longer sufficient; a stolen token *and* the
  commander's live password (re-entered at that moment) are required. XSS
  can't exfiltrate a password the user has to type into a native browser
  password field at that instant (assuming the field isn't pre-filled/
  autocompleted into the DOM in a way a malicious script could read — use a
  fresh `<input type=password>` with `autocomplete=off`, not a value bound
  into readable app state).
- Implementation: `POST /api/range-authorization` requires `{enabled: true,
  password: "<current password>"}`; the backend re-verifies via
  `verify_password` against the user's stored hash before flipping the flag,
  in addition to the existing JWT/`require_commander` check. Re-auth should
  be required for the **enable** transition; disabling should be low-friction
  and always available immediately (see 2.3/2.6 — anyone should be able to
  make the range safe quickly, only making it hot should have friction).

### 2.3 Auto-expire — YES, mandatory, and this is the most important control

A durable on/off switch is the single biggest regression versus the old
design (T3/T4). Replace "durable flag" with **"authorization is a
lease with a short TTL, auto-renewed only by continued explicit action."**

- **Recommended TTL: 15 minutes**, matching realistic single-test-run
  duration; short enough that a forgotten toggle self-heals well within a
  test session's slack time, long enough that operators aren't re-entering
  their password every 2 minutes during a live test window.
- Do **not** silently auto-renew on activity (e.g. don't extend just because
  other API calls are happening) — that would let a compromised/lingering
  session keep the range hot indefinitely by generating any traffic at all.
  Renewal must be an explicit re-toggle (which is a low-friction "disable" +
  full "enable" with re-auth, or a dedicated `POST .../extend` that itself
  still requires password re-entry).
- On expiry, revert to OFF automatically and log an `EXPIRED` audit event,
  independent of anyone calling anything — implement as a background check
  identical in spirit to `_expire_pending_ack`/`_expire_pending_jam` already
  in `server.py` (there's precedent for this exact pattern in the codebase).
- This directly bounds the blast radius from T1/T2/T5/T6: even a fully
  successful attack only buys the attacker a live-fire window that self-closes
  in at most 15 minutes, and only if no one notices the mandatory banner
  (2.4) in the meantime.

### 2.4 Persistent, unmissable UI indicator — YES, mandatory

- A full-width, high-contrast (red, animated/pulsing — not just a colored
  dot) banner fixed to the top of **every page** of the app, not just the
  Jamming/Payloads pages, whenever range authorization is ON. Text along the
  lines of: `RANGE LIVE — RF/PAYLOAD TRANSMISSION AUTHORIZED — expires in
  MM:SS — [DISABLE NOW]`.
- Include a live countdown (reinforces 2.3's TTL and gives operators a
  reason to trust "it'll turn off on its own") and a one-click, no-re-auth
  **disable** button directly in the banner — making the range safe should
  never require more friction than making it live.
- This is the direct mitigation for "operator forgets it's live" and gives
  every other person in the room (not just the one commander) a chance to
  notice and challenge an unexpected live state — a passive form of the
  two-person visibility the old physical-access model gave you for free.
- Drive this from the same polling pattern already used for `jam/status`
  (`GET /api/range-authorization/status` polled by a top-level layout
  component, not per-page) so it can't be missed by navigating away from one
  page.

### 2.5 Confirmation phrase — YES, for enabling only

Given this flag is now the single point of failure for the safety property
the whole system depends on, gate enabling it behind more than a click:

- On enable, require the operator to type a fixed confirmation phrase, e.g.
  `AUTHORIZE LIVE RANGE`, into a text field (mirroring GitHub's
  "type the repo name to delete" pattern), in the *same* modal that collects
  the re-entered password from 2.2. This is a deliberate, hard-to-fat-finger,
  hard-to-script-past-without-noticing action — a stray click or a replayed
  request cannot satisfy a free-text phrase match.
- Disabling should NOT require the phrase — friction should be asymmetric
  (hard to turn on, trivial to turn off), matching 2.3/2.4's "safe state
  should always be one click away" principle.
- This does not replace the per-action SafetyGate checklist in
  `Jamming.jsx`/payload-deploy flows — that still runs, unchanged, for every
  individual jam/deploy action. This phrase gates only the higher-level
  "is the range hot at all" switch, once per ~15-minute window instead of
  once per shot.

### 2.6 Audit logging — YES, mandatory, maximal detail

Every transition (enable attempt — success or failure, disable, auto-expiry)
must go through `log_event` (already used throughout `server.py`) with at
minimum:
- actor email/user id, timestamp, action (`RANGE_AUTH_ENABLE` /
  `RANGE_AUTH_DISABLE` / `RANGE_AUTH_EXPIRED` / `RANGE_AUTH_ENABLE_FAILED`),
  source IP (`request.client.host`), and which flag (`jam`/`mavlink`, per
  §5), plus reason on failure (bad password, bad phrase, not commander).
- Failed re-auth attempts should be logged and, ideally, contribute to a
  simple rate-limit/lockout on the re-auth step (reuse whatever login
  throttling already exists, or add a basic N-failures-per-minute guard) —
  otherwise this becomes a low-cost online password-guessing oracle against
  a commander account, a new risk this redesign would otherwise introduce.
- Because this flag is now the single point of failure, its audit trail is
  the primary forensic record if something goes live unexpectedly — treat
  `list_logs`/the mission log for this event type as something that should
  be reviewed after every test session, not just on incident.

---

## 3. Data Model & API Design

### 3.1 Where the flag lives: in-memory, NOT persisted across restart

**Recommendation: in-memory only, defaulting to OFF on every backend
start/restart.** Do not persist "enabled" state in Mongo.

Rationale, directly addressing the prompt's framing:
- Persisting "ON" as a durable, restart-surviving state is **less safe**, not
  more: a backend crash/redeploy/host reboot mid-test should never silently
  restore a live-fire-capable state without a human re-affirming it. The
  entire point of moving this control into the app is to make arming an
  *explicit, momentary, human-attested* act — persisting it defeats that by
  letting a live state outlive the reason the human enabled it (they may
  have intended it only for the process's current lifetime).
- A restart is itself a meaningful discontinuity (new process, potentially
  new code deployed, new day/session) — exactly the kind of boundary where
  you *want* to force re-authorization, the same way the arm-token and
  jam-confirm-token are deliberately in-memory (`_arm_tokens`,
  `_jam_confirm_tokens`) and don't survive restart today. This redesign
  should follow that same existing convention, not break from it.
- What *should* be persisted (in `mission_log`/Mongo, via `log_event`, as
  already designed) is the **audit trail** of enable/disable/expiry events —
  that's the "explicit, auditable" benefit the prompt is right to want,
  without the "silently comes back live" risk. Auditability and persistence
  of the *live flag itself* are separable, and only the former is worth
  keeping.
- Consequence to document clearly in the UI/runbook: **every backend
  restart requires re-arming range authorization from the GUI before any
  live TX is possible again**, exactly mirroring "every new range session
  requires someone to SSH in again" from the old model — this preserves the
  *spirit* of the old control (fresh, deliberate action per session) while
  moving *where* that action happens.

### 3.2 Endpoints

```
GET  /api/range-authorization/status
  -> 200 { enabled: bool, effect: "jam" | "mavlink",
           expires_at: iso8601 | null, seconds_remaining: int | null,
           enabled_by: str | null, enabled_at: iso8601 | null }
  Auth: any authenticated user (get_current_user) — read-only, needed by the
  banner component on every page, and polled by field-bridge services.

POST /api/range-authorization
  Body: { effect: "jam" | "mavlink", enabled: bool,
          password: str,             # required when enabled=true, ignored/optional when false
          confirm_phrase: str }      # required + must equal "AUTHORIZE LIVE RANGE" when enabled=true
  Auth: require_commander
  Behavior:
    - enabled=true:
        - verify_password(password, user's stored hash) — 401/403 + audit log on failure,
          with basic attempt throttling (see 2.6).
        - confirm_phrase must exactly match the fixed phrase — 400 + audit log on mismatch.
        - on success: set in-memory { enabled: True, expires_at: now+15min,
          enabled_by: user.email, enabled_at: now }, log RANGE_AUTH_ENABLE,
          broadcast over the existing ws_manager (like jam_status) so the
          banner updates without polling lag.
    - enabled=false:
        - no password/phrase required (low friction — always available).
        - clear the flag, log RANGE_AUTH_DISABLE, broadcast.
  Returns: same shape as GET .../status.

POST /api/range-authorization/extend   (optional, only if 15 min proves too short in practice)
  Body: { effect: "jam"|"mavlink", password: str }
  Auth: require_commander — same password re-auth as enabling; resets TTL;
  does NOT require the confirm phrase again (already-armed state, just
  extending), still fully audited as RANGE_AUTH_EXTEND.
```

Backend also needs a background expiry task
(`_expire_range_authorization`), following the exact existing pattern of
`_expire_pending_acks`/`_expire_pending_jam`, run from the same
startup-scheduled loop, to flip the flag off and log
`RANGE_AUTH_EXPIRED` when `expires_at` passes with no explicit disable.

### 3.3 How field-bridge services check it

The whole point of this change is moving control off the bridge host, so
`jam_bridge.py` and the MAVLink payload bridge must **query the backend at
request time**, not read a static local env var:

- On receiving a `jam_request` (or the equivalent MAVLink deploy trigger)
  over the WS connection, the bridge calls
  `GET /api/range-authorization/status?effect=jam` (or `mavlink`) using its
  own service credential (bridges should authenticate as a distinct
  machine/service account — reuse the existing JWT auth, with a bridge-only
  account that has no other privileges, not the operator's own token) and
  refuses to key up if `enabled` is false or `expires_at` has passed,
  exactly where `jam_bridge.py:150` and `:198` currently check
  `os.environ.get("CEMA_AUTHORIZED_RANGE")`.
- **Do not** trust a value embedded in the `jam_request` WS payload itself
  for this check (i.e. don't let the backend just say "trust me, it's
  authorized" inside the same message it's already sending) — the bridge
  should make its own independent `GET` call at the moment it's about to key
  up, so a stale/replayed WS message can't carry stale authorization forward
  past an expiry or a disable that happened in between. This preserves the
  one property still worth keeping from the old design: the bridge makes its
  own decision at the moment of transmission, it doesn't just blindly obey
  whatever the message tells it.
- Fail closed on any network/auth error talking to the backend (can't
  reach it, gets a 401/403, timeout) — treat exactly like "not authorized,"
  identical to the current `!= "1"` fail-closed behavior.
- Keep the existing `MIN_CONFIRM_TOKEN_LEN`/`_looks_like_real_confirm_token`
  shape-check on `jam_confirm_token` as-is — that's an unrelated, independent
  belt-and-suspenders check on a different token and shouldn't be touched.

---

## 4. What Explicitly Does Not Change

- `SafetyGate.jsx`'s two-step checklist + ARM & FIRE -> CONFIRM FIRE flow.
- `jam_confirm_token` / `arm_token` issue-once/consume-once mechanics and
  TTLs.
- `authorized_target` friendly-fire interlock on `/detections/{id}`.
- `require_commander` as a baseline gate on every transmit-capable endpoint.
- `_check_tx_not_halted`/emergency-abort precedence over everything.

This redesign is additive: one new gate (`range-authorization`), checked by
the bridges in addition to, not instead of, all the above.

---

## 5. One Flag or Two? — Recommendation: **SEPARATE flags for jam and mavlink**

Keep them independent (`effect: "jam" | "mavlink"` in the API above, two
independent in-memory leases, two independent banner states), for concrete
reasons specific to this system, not just generic caution:

- **Blast radius differs by orders of magnitude.** RF jamming (especially
  GNSS-band jamming, per `JAM_GNSS_BANDS` in `server.py`) affects an
  area-based footprint that can extend well beyond the intended target and
  can affect uninvolved third parties/systems within range (the code itself
  flags GNSS jamming's "proportionally larger effective radius" in the
  `deploy_jam` docstring). MAVLink payload deploy, by contrast, is
  interlocked to a single `authorized_target` detection — its worst-case
  blast radius is bounded to one already-vetted target. Conflating the two
  under one switch means testing/arming one silently arms the
  wider-footprint one too.
- **Distinct legitimate test workflows.** It's entirely normal to want to
  test payload-deploy logic (packet construction, ACK handling, targeting
  workflow) without wanting RF jamming live at all, or vice versa
  (spectrum/jamming characterization runs without any payload/MAVLink
  target present). A single "range hot" switch forces operators into an
  all-or-nothing state that doesn't match how the system is actually
  exercised, and creates pressure to just "leave both on" for convenience —
  which is exactly the wrong incentive for a control designed to bound blast
  radius.
- **The "one thing to remember" argument for a single switch is weaker than
  it looks.** The mandatory persistent banner (2.4) already solves the
  "operator might forget the system is live" problem regardless of how many
  flags there are — it would just say "JAM LIVE" and/or "MAVLINK LIVE"
  independently. Two flags with two clear banners is not meaningfully harder
  to track than one flag, but it is meaningfully safer because it removes the
  single most likely accidental-arm scenario: enabling one capability to
  test it and unknowingly also enabling the other.

Net: two flags, two independent 15-minute leases, two independent banners,
sharing the same endpoint shape via the `effect` parameter and the same
re-auth/confirm-phrase/audit mechanics. Do not add a third "arm everything"
convenience toggle — if an operator genuinely needs both, they perform two
deliberate actions, which is the correct amount of friction for a change
this consequential.

---

## 6. Summary Recommendation Table

| Question | Recommendation |
|---|---|
| RBAC | `require_commander` (existing) + mandatory password re-auth in the same request |
| Re-auth on toggle | Yes, required for enable; not required for disable |
| Auto-expiry | Yes — 15 min lease, auto-revert to OFF, no silent auto-renewal |
| Persistent UI banner | Yes — full-width red banner on every page, with live countdown and one-click disable |
| Confirmation phrase | Yes, for enable only (`AUTHORIZE LIVE RANGE`); not required for disable |
| Audit logging | Yes — full detail via existing `log_event`, including failed attempts, with basic re-auth throttling |
| Persist across restart | No — in-memory only, defaults to OFF on every backend start; only the audit trail persists |
| Bridge-side check | Poll backend `GET /api/range-authorization/status` at time of TX, service-account auth, fail closed on any error |
| One flag or two | Two — separate `jam` and `mavlink` leases, independently armed and independently displayed |
