# GNSS L1 Civil-Signal Spoofing ("Soft-Kill") — Architecture Spec (Task #103)

Status: DESIGN ONLY. No production code in this doc beyond interface
sketches. Implementation is a separate, follow-up task — see §7.

## 0. Scope and inherited constraints

This extends the existing jam capability's safety-gate pattern
(`field-bridge/jam_bridge.py`, `backend/server.py`'s `/payloads/jam` +
`/jam/confirm` + `/range-authorization`, `frontend/src/pages/Payloads.jsx` +
`SafetyGate.jsx`) to a new, structurally different effect: instead of
transmitting band-limited noise to deny GNSS reception, this capability
transmits a **synthesized, structurally valid GPS L1 C/A signal carrying a
fabricated position** — a deception effect, not a denial effect. A
GPS-dependent flight controller that acquires and tracks this fake signal
either (a) reports an implausible position jump and enters its
flyaway/self-preservation failsafe, or (b) has its EKF pulled toward a false
position, causing an unexpected geofence-breach RTH. Per the completed
Security Architect scoping pass, this reuses the existing HackRF TX chain;
the new engineering work is signal-synthesis software plus a second,
independent safety-gate chain.

**Adjustment to the original safety-architecture recommendation (per
explicit user instruction, given/binding for this design):** the "no
civilian GNSS asset in range" attestation is DROPPED for this deployment
context (battlefield, no civilians present). The **friendly-asset
attestation** (own troops/drones/vehicles depending on GPS) and the
**mandatory human-readable payload-preview-before-confirm gate** are Kept
and are non-negotiable — they protect against fratricide and operator
error, which are in-scope regardless of civilian presence.

### Reference technique (not copied)

The synthesis approach — generate a fake NAV message (ephemeris/almanac)
for a fictitious-but-structurally-valid receiver position, modulate it onto
C/A PRN codes at 1575.42 MHz, and feed the IQ stream to an SDR TX — is the
same technique documented by **osqzss/gps-sdr-sim** (MIT-licensed, OSI
permissive), the de facto public reference implementation for this exact
capability. No code from that or any other project should be copied; it is
cited here the same way ExpressLRS's timing constants were cited earlier
this session — as "the standard reference technique this design follows,"
for the implementer to reimplement cleanly against this project's own
license posture and code style. Local search (`~/Desktop/Zettawise/PMO
Suraj/tool/`, `~/Desktop/zettagit/`) found no local copy of gps-sdr-sim,
gnss-sdr, or any GPS civil-signal simulator/spoofer — only unrelated GPS
*receiver*-side code (ArduPilot/PX4 GPS drivers, SITL GPS backends). If a
local reference is desired before implementation starts, vendoring
osqzss/gps-sdr-sim (MIT) under a third_party/ directory with attribution
preserved is compliant with this project's OSI-permissive-only policy;
using it as a read-only algorithmic reference without vendoring is also
fine and probably lower-friction.

## 1. Module structure

New file: `field-bridge/gnss_spoof_bridge.py`, structurally parallel to
`jam_bridge.py` (own class `GnssSpoofBridge`, own WS message types, own
local abort/halt state) — NOT merged into `jam_bridge.py`, because the two
effects have different token types, different range-authorization effect
strings, different payload-preview content, and mixing them raises the risk
of a future edit accidentally sharing a gate that must stay separate.

New file: `field-bridge/gnss_signal_synth.py` — pure signal-synthesis
module, no networking/WS/HTTP code at all. This is the DSP core Task B
(§7) implements. It is imported by `gnss_spoof_bridge.py` exactly as
`hackrf_jam.transmit_burst` is imported by `jam_bridge.py`, and is
independently unit-testable (feed it a true position + fake offset,
assert it emits a valid IQ file of correct sample rate/duration) without
any HackRF hardware present.

```
field-bridge/
  gnss_signal_synth.py      # NEW — DSP: ephemeris/almanac synth, C/A PRN
                             #   generation, NAV message encoding, IQ file
                             #   or IQ-stream generation for hackrf_transfer.
  gnss_spoof_bridge.py       # NEW — WS bridge + safety gates, parallel to
                             #   jam_bridge.py; imports gnss_signal_synth
                             #   and (reused) hackrf_jam.transmit-style TX
                             #   invocation, OR a small transmit_gnss_spoof()
                             #   wrapper added to hackrf_jam.py that shells
                             #   out to hackrf_transfer with a pre-built IQ
                             #   file instead of hackrf_jam's synthetic
                             #   noise generator (see §2).
  hackrf_jam.py              # EXTEND MINIMALLY: add
                             #   GNSS_SPOOF_MAX_DURATION_S constant and (if
                             #   the cleaner fit) a transmit_iq_file()
                             #   helper that factors the existing
                             #   hackrf_transfer subprocess invocation in
                             #   transmit_burst() out into something both
                             #   noise-burst and spoof-burst can call. Do
                             #   NOT change transmit_burst()'s existing
                             #   behavior/signature — jamming must be
                             #   provably unaffected by this change.
  jam_bridge.py               # UNCHANGED.
```

Why not extend `hackrf_jam.py`/`jam_bridge.py` in place: `hackrf_jam.py`'s
`transmit_burst()` synthesizes band-limited **noise** in-process
(`numpy`-generated) and streams it straight to `hackrf_transfer`. GNSS
spoofing needs a fundamentally different signal (structured PRN-coded,
NAV-message-bearing IQ, generated by `gnss_signal_synth.py`, likely written
to a temp IQ file first given C/A code generation cost) fed to the same
`hackrf_transfer` subprocess mechanics. The cleanest boundary is: keep
`transmit_burst()` exactly as-is for jamming, add a narrow
`transmit_iq_file(path, freq_mhz, duration_s, tx_gain, stop_event,
on_started)` helper in `hackrf_jam.py` that both `transmit_burst()`
internals and the new spoof path can share (same subprocess-management /
abort-mid-transmission logic, different payload source). This keeps the
proven, already-audited jam TX path untouched while giving spoof a
matching, equally-audited TX invocation instead of a second bespoke one.

## 2. Duration cap: 3 seconds, not 10

Jamming's `MAX_DURATION_S = 10` is a "convenience" cap sized for a
noise-denial burst where more seconds just means more denial, bounded
mainly by RF exposure/detectability concerns. Spoofing has a different risk
shape and should use a much shorter cap:

- **Recommended: `GNSS_SPOOF_MAX_DURATION_S = 3.0` seconds**, with a
  default requested duration of 2.0s.

Justification:
- A GPS receiver's tracking-loop pull-in and position-fix update happens on
  the order of the receiver's fix interval (typically 1 Hz, sometimes up to
  5-10 Hz on modern receivers) plus loss-of-lock/reacquisition dynamics.
  Public GPS-spoofing literature (the same body of work behind the
  gps-sdr-sim-class tooling cited above) reports that a receiver already
  tracking a real signal typically transitions to tracking a stronger fake
  signal and reports a new fix within roughly one to a few fix intervals
  once the fake signal captures the tracking loop — i.e., low
  single-digit seconds is the empirically-reported window for the
  fix/position-jump to occur, not tens of seconds.
- The failsafe trigger this capability targets (geofence-breach RTH or an
  implausible-position-jump flyaway safeguard) fires off a SINGLE bad
  position report crossing a threshold, not sustained exposure — unlike
  jamming, there is no "more seconds = more effect" scaling once the fake
  fix has been accepted. Transmitting longer than needed only adds
  detectability risk and fratricide exposure with no additional benefit.
  A short, hard cap enforces "the minimum empirically needed," per the
  task's requirement, rather than reusing jamming's convenience default.
- 3s is deliberately shorter than jamming's 10s cap specifically so an
  operator (or a bug) cannot accidentally request a jamming-length spoof
  burst — the cap itself is part of the safety design, not just a tuning
  knob.

This cap is enforced in THREE places, mirroring jamming's defense-in-depth:
`gnss_signal_synth.py` refuses to synthesize a longer IQ payload, the
backend's `GnssSpoofRequestBody` Pydantic model clamps
`duration_s = min(requested, GNSS_SPOOF_MAX_DURATION_S)` before ever
building the WS message, and `gnss_spoof_bridge.py` clamps again
independently before calling the TX helper (never trusting the WS payload's
duration as authoritative, same posture as jam_bridge.py toward all other
spoof-request fields).

## 3. Backend: new `effect=gnss_spoof` range-authorization type

`backend/server.py`'s `RangeAuthorizationBody.effect` pattern changes from:

```python
effect: str = Field(pattern="^(jam|mavlink)$")
```
to:
```python
effect: str = Field(pattern="^(jam|mavlink|gnss_spoof)$")
```

and `RANGE_AUTH_EFFECTS = ("jam", "mavlink")` becomes `("jam", "mavlink",
"gnss_spoof")`. This is the ONLY change to the existing range-authorization
machinery — the lease/expiry/password-reauth/confirm-phrase logic in
`RANGE_AUTHORIZATION_REDESIGN.md` is effect-parameterized already and needs
no other modification. Arming `effect=jam` must NOT implicitly arm
`effect=gnss_spoof` — they are tracked as separate keys in whatever dict/
store backs `_pending`/lease state today (confirm this is already
keyed by effect, not a single global boolean, before implementing — from
the grep results it appears to already be effect-keyed, consistent with
`mavlink` and `jam` already being independent).

`gnss_spoof_bridge.py`'s `is_range_authorized("gnss_spoof")` reuses
`GET /api/range-authorization/status?effect=gnss_spoof` verbatim (same
endpoint, new effect value) — no new backend route needed for this part.

## 4. New token pair: `gnss_spoof_arm_token` / `gnss_spoof_confirm_token`

**Design choice: separate, effect-discriminated tokens, NOT a shared token
mechanism with an `effect` field.** Rationale: jam_bridge.py's own
docstring is explicit that `jam_confirm_token`'s value is proof a jam-
specific confirm step occurred, and its floor-length check
(`_looks_like_real_confirm_token`) is deliberately dumb/shape-only — it
trusts the backend's earlier validation. If jam and spoof shared one token
type with an `effect` discriminator, a bug in a caller (or a future
copy-paste) that forwards a valid `jam_confirm_token` where a spoof
confirm was expected would be silently accepted by a shape check that
can't tell the difference. Distinct token types make that class of bug a
hard `422`/`403` at the backend instead of a silent cross-effect
authorization leak. This costs a small amount of duplicated plumbing and
buys the "must not be interchangeable" property the task requires
structurally rather than by convention.

`backend/server.py` additions (interface sketch, not full implementation):

```python
_gnss_spoof_confirm_tokens: Dict[str, datetime] = {}
GNSS_SPOOF_CONFIRM_TTL_S = 60  # short-lived, mirrors JAM_CONFIRM_TTL_S

def _issue_gnss_spoof_confirm_token() -> Dict[str, Any]: ...
def _consume_gnss_spoof_confirm_token(token: Optional[str]) -> None: ...
    # raises HTTPException(400/403) same pattern as _consume_jam_confirm_token

class GnssSpoofRequestBody(BaseModel):
    band: str = Field(pattern="^(gps_l1)$")   # only gps_l1 supported at launch;
                                                # galileo_e1/beidou_b1/glonass_l1
                                                # are explicitly OUT OF SCOPE for
                                                # this task (different NAV message
                                                # formats/PRNs — separate DSP work)
    duration_s: float = 2.0                    # clamped server-side, see §2
    tx_gain: int = 20
    fake_offset_m: float                       # REQUIRED, no default — see §5
    fake_bearing_deg: float                     # REQUIRED, no default — see §5
    true_lat: float                             # last-known-true position,
    true_lon: float                             #   REQUIRED — operator/UI must
    true_alt_m: float                           #   supply this (from current
                                                 #   target detection telemetry)
    friendly_asset_attestation: str              # REQUIRED, free-text or
                                                 # structured — see §5, must be
                                                 # non-empty and logged verbatim
    arm_token: str                               # required unconditionally —
                                                 # gnss_spoof is always CRITICAL
    gnss_spoof_confirm_token: str                 # required unconditionally

class GnssSpoofConfirmBody(BaseModel):
    friendly_asset_attestation: str  # re-submitted at confirm time too —
                                      # binds the attestation text to THIS
                                      # specific confirm token mint, so the
                                      # audit trail shows exactly what the
                                      # operator attested to at the moment
                                      # the token was minted, not just that
                                      # *some* attestation field was filled
                                      # in somewhere earlier in the flow.
```

New endpoints, parallel to `/jam/confirm` and `/payloads/jam`:

```python
@api.post("/gnss-spoof/confirm")
async def gnss_spoof_confirm(body: GnssSpoofConfirmBody,
                              user: Dict = Depends(require_commander)):
    """Mints a single-use gnss_spoof_confirm_token. Requires
    body.friendly_asset_attestation to be non-empty (reject with 400
    otherwise) — this endpoint is the durable record of WHAT was attested,
    tied to WHEN the token was minted, logged to mission_log immediately
    (not just embedded in the later /payloads/gnss-spoof call) so the
    attestation survives even if the subsequent request never arrives."""
    ...
    await log_event("gnss_spoof_attestation", ...)  # see §6
    ...

@api.post("/payloads/gnss-spoof")
async def deploy_gnss_spoof(body: GnssSpoofRequestBody,
                             user: Dict = Depends(require_commander)):
    """Same layered-gate shape as deploy_jam(): require_commander,
    _consume_gnss_spoof_confirm_token(body.gnss_spoof_confirm_token),
    arm_token check (always required — gnss_spoof is unconditionally
    CRITICAL severity, like jam), tx_halted check. Additionally: reject
    (400) if body.friendly_asset_attestation doesn't match what was
    attested at /gnss-spoof/confirm time for this session (defense against
    a caller swapping the attestation text between confirm and fire).
    Computes the fabricated position server-side (true_lat/lon/alt +
    fake_offset_m at fake_bearing_deg -> fake_lat/fake_lon, via a plain
    geodesic offset calc — small helper, no new dependency needed) so the
    EXACT fabricated coordinates are known and loggable BEFORE the WS
    message is sent, not left for the bridge to compute independently
    (single source of truth for what "the preview showed" vs "what gets
    transmitted" — these must be the same numbers).
    Logs full request + computed fake position to mission_log (§6), then
    sends {"type": "gnss_spoof_request", ...} over the same
    /api/ws/mavlink control WS jam_request already uses."""
    ...
```

`GNSS_SPOOF_MAX_DURATION_S = 3.0` constant added near
`JAM_MAX_DURATION_S` in `backend/server.py`, per §2.

## 5. Friendly-asset attestation and payload preview (the two non-negotiable gates)

### 5a. Friendly-asset attestation

- A REQUIRED structured field, not a checkbox: `friendly_asset_attestation`
  (free-text, minimum length enforced, e.g. 20 chars) that the operator
  must actively type, e.g.: *"Confirmed: no friendly GPS-dependent assets
  (own drones/vehicles) within [radius] of target position. Reviewed
  friendly asset tracker at [time]."* The exact wording is a frontend/UX
  decision for the follow-up task, but the backend must reject empty/
  trivial values (reuse the same "reject trivially fabricated values"
  posture as `_looks_like_real_confirm_token`'s length floor — e.g. refuse
  strings under ~20 chars or equal to placeholder text like "n/a"/"none"/
  "confirmed").
- Captured at TWO points and cross-checked (§4): once when
  `/gnss-spoof/confirm` mints the confirm token (durable record even if
  the flow is abandoned before firing), and again re-submitted with
  `/payloads/gnss-spoof` (must match, else 400) — this closes the gap
  where a checkbox "vanishes" after being ticked once with no lasting
  record, which the task explicitly calls out as insufficient.
- Logged verbatim to `mission_log` at both points (§6), never summarized
  to a boolean.

### 5b. Payload preview (the single most important new gate)

Before the frontend even shows the final CONFIRM step, the UI must display
the EXACT fabricated position in human-readable form. This requires the
backend to expose a preview-computation path the frontend calls BEFORE
minting any tokens, so the operator sees real numbers, not a template:

```python
@api.post("/gnss-spoof/preview")
async def gnss_spoof_preview(body: GnssSpoofPreviewBody,
                              user: Dict = Depends(require_commander)):
    """Pure computation, no tokens minted, nothing transmitted, nothing
    logged as an authorization event (may still be logged as an
    INFO-level 'preview viewed' breadcrumb for UX-diagnostics, but this is
    NOT part of the safety-gate audit chain). Body: true_lat, true_lon,
    true_alt_m, fake_offset_m, fake_bearing_deg. Returns:
      { fake_lat, fake_lon, fake_alt_m,
        offset_m, bearing_deg, bearing_compass (e.g. "047° NE"),
        distance_description: "312 m offset, bearing 047° (NE) from
                                 last-known-true position" }
    This is what SafetyGate's checklist step must render verbatim — e.g.
    'Target will receive FAKE position: 312 m NE (bearing 047°) of its
    last known true position (lat/lon shown). Duration: 2.0s at 1575.42
    MHz (GPS L1).' — not a generic 'Confirm spoof?' button."""
```

The frontend flow (extends `Payloads.jsx` / `SafetyGate.jsx`):

1. Operator selects target + a "GNSS Spoof" action (new payload entry or a
   dedicated `GnssSpoof.jsx` panel — recommend a dedicated panel, mirroring
   how jamming got its own `Jamming.jsx` rather than living inside
   `Payloads.jsx`'s generic payload-card grid, since this has its own
   required input fields, not just a fire button).
2. Operator enters/reviews `fake_offset_m` and `fake_bearing_deg` (with
   sane defaults, e.g. 300m at a bearing away from any known friendly
   position) and the friendly-asset attestation text field.
3. Frontend calls `POST /gnss-spoof/preview` with current true position +
   requested offset/bearing. Response populates a NEW required checklist
   item in a `GNSS_SPOOF_CHECKS` array (parallel to `JAM_CHECKS`) whose
   text is DYNAMICALLY built from the preview response, e.g.:
   `` `Target will receive FAKE position ${distance_description}. This is
   REAL RF, not a preview of the effect — reviewed and correct.` ``
   — SafetyGate.jsx already supports a `checks` prop taking an array of
   strings, so this requires only that the parent page build that array
   from live preview data instead of a static constant, no changes to
   SafetyGate.jsx's own rendering logic.
4. Operator ticks all checks (now including the fake-position review item)
   + the friendly-asset-attestation text field, clicks ARM & FIRE, is asked
   to CONFIRM FIRE (existing two-click pattern, unchanged).
5. On confirm: frontend calls `POST /gnss-spoof/confirm` (mints token,
   attestation logged durably), then `POST /payloads/gnss-spoof` (fires,
   with `arm_token` fetched via existing `POST /arm` exactly as
   `Payloads.jsx`'s `doDeploy` already does for CRITICAL payloads).

## 6. Audit logging

Reuse the existing `mission_log` collection + `log_event()` helper (backend
line ~857) and the hash-chain mechanism already computed over
`mission_log` entries (lines ~2634-2744) — no new logging infrastructure.
New log event kinds:

- `gnss_spoof_preview_viewed` (INFO, not part of the authorization chain,
  best-effort) — timestamp, actor, requested offset/bearing, computed fake
  position.
- `gnss_spoof_attestation` (logged at `/gnss-spoof/confirm`) — actor,
  timestamp, full `friendly_asset_attestation` text verbatim, confirm token
  id (hashed/truncated, not the raw token, matching whatever convention
  `_issue_jam_confirm_token`'s logging already uses if any — check before
  implementing whether jam logs token values at all; if it doesn't, don't
  start doing so for spoof either, to keep the two effects' audit shape
  consistent).
- `gnss_spoof_fired` (logged at `/payloads/gnss-spoof`, BEFORE sending the
  WS message) — actor, timestamp, arm_token id, gnss_spoof_confirm_token
  id, true_lat/lon/alt, fake_lat/lon/alt, offset_m, bearing_deg,
  duration_s, freq_mhz, tx_gain, request_id, friendly_asset_attestation
  text (repeated here too, so a single log query for `gnss_spoof_fired`
  is self-contained and doesn't require joining against the earlier
  `gnss_spoof_attestation` entry).
- `gnss_spoof_ack` (logged from `_handle_gnss_spoof_ack`, mirroring
  `_handle_jam_ack`) — phase (started/complete/failed/stopped), request_id.

Because this reuses `mission_log`'s existing hash chain, every one of these
entries becomes tamper-evident the same way jam/deploy events already are
— no separate immutability mechanism needs to be built.

## 7. Recommended split of follow-up implementation work

This task's scope naturally splits along a DSP/plumbing boundary, and the
two halves have almost no overlapping expertise:

**Task A — Safety-gate plumbing (backend + frontend), no DSP knowledge
required:**
- `backend/server.py`: `effect=gnss_spoof` range-authorization value,
  `GnssSpoofRequestBody`/`GnssSpoofConfirmBody`/`GnssSpoofPreviewBody`
  models, `/gnss-spoof/preview`, `/gnss-spoof/confirm`,
  `/payloads/gnss-spoof` endpoints, `_gnss_spoof_confirm_tokens` store,
  `GNSS_SPOOF_MAX_DURATION_S` constant, geodesic offset helper (true
  lat/lon + offset/bearing -> fake lat/lon — trivial, no GPS-domain
  knowledge needed), mission_log event kinds from §6, `_handle_gnss_spoof_ack`.
- `frontend/src/pages/GnssSpoof.jsx` (new, or extend `Payloads.jsx`),
  `GNSS_SPOOF_CHECKS` dynamic-checklist wiring into `SafetyGate.jsx`
  (SafetyGate.jsx itself likely needs NO changes — confirm this during
  implementation), friendly-asset-attestation text input, live preview
  fetch-and-render.
- `field-bridge/gnss_spoof_bridge.py`'s WS-handling/gate-chain skeleton
  (Gates A/B/C mirroring jam_bridge.py exactly, per this doc's §1) can be
  written by this same team, STUBBING OUT the call into
  `gnss_signal_synth.py` (e.g. call a function that raises
  `NotImplementedError` or generates a placeholder IQ file) so the full
  gate chain is testable end-to-end before the DSP exists.
- Owner recommendation: **Backend Architect** for the server.py/token/
  range-auth work, **Frontend Developer** for the UI, working from this
  doc; low risk, no new hardware/DSP judgment calls needed.

**Task B — GPS L1 C/A signal-synthesis DSP, needs GNSS-domain knowledge:**
- `field-bridge/gnss_signal_synth.py`: fabricated ephemeris/almanac
  generation for a plausible-but-false position, GPS L1 C/A PRN code
  generation, NAV message encoding (subframes 1-3 minimum for a receiver
  to accept a position fix), BPSK modulation onto the 1.023 Mcps C/A
  chipping rate, IQ sample generation at a sample rate compatible with
  HackRF (matching `SAMPLE_RATE_HZ = 20_000_000` already used in
  `hackrf_jam.py`, or determining whether a lower rate specific to L1 C/A
  is more appropriate — this is exactly the kind of parameter a DSP
  specialist should own, not this design doc).
- `field-bridge/hackrf_jam.py`'s `transmit_iq_file()` factoring (§1) — this
  touches the proven jam TX path and should be reviewed by whoever owns
  DSP/RF correctness, even though it's a small, mechanical refactor.
- Owner recommendation: **Embedded Firmware Engineer** or **AI Engineer**
  (whichever has closer SDR/DSP experience on this team) — this is
  genuinely a different skill set from Task A and should NOT block on
  Task A's completion; Task B can be developed and unit-tested against
  synthetic true-position inputs entirely independently, then wired into
  Task A's bridge skeleton's stubbed call site once both are ready.

**Sequencing:** Task A and Task B can run in parallel. Task A's bridge
skeleton with a stubbed synth call lets safety-gate testing (arm/confirm/
abort/range-authorization/attestation/preview) proceed without waiting on
DSP work; Task B's synth module is independently unit-testable without any
bridge/backend code. Integration (wiring B's real output into A's stub) is
a short final step once both land — recommend a brief joint review at that
point given this is the point where an RF-correctness bug and a
safety-gate bug could compound.

## 8. What is explicitly NOT in scope for either follow-up task

- Galileo E1 / BeiDou B1I / GLONASS L1OF spoofing — different NAV message
  formats and (for GLONASS) FDMA channelization; `band` is validated to
  `gps_l1` only at the backend (§4) until a future task adds these.
- `--continuous`-style repeated spoof bursts — same reasoning as
  jam_bridge.py's module docstring: an unattended, WS-triggered,
  continuous transmission with no local human able to abort locally is a
  materially different risk and is out of scope here.
- Any change to `hackrf_jam.py`'s `transmit_burst()` behavior/signature —
  jamming must be provably unaffected by this work.
