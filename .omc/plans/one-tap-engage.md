# ONE-TAP ENGAGE — build contract (MEGHDUT C3)

Approved-to-design by operator 2026-09-07 ("1 go"). Architect pass complete. Collapses the per-shot ceremony to a single confirmed tap WITHOUT weakening any interlock — every gate is already enforced server-side inside the deploy endpoints; the frontend merely hand-operated them. Safety-critical (fire path): each phase gets an independent security-review + verifier before deploy.

## Core insight
The latency is UI choreography, not enforcement depth. `/arm` + `/{effect}/confirm` + `authorize-target` exist so the HUMAN can assemble proof-of-intent to hand to the deploy call — but `deploy_jam`/`deploy_mavlink_sdr_inject`/`deploy_wifi_defeat` re-validate every gate themselves. So the tokens can be minted+burned in ONE server-side transaction with all properties intact (single-use, effect-bound, target-bound, atomic pop), and the human's role collapses to the one thing that needs a human: WHICH target, engage yes/no.

## Two tiers
### WEAPONS-HOT posture (armed ONCE, deliberate)
New in-memory, OFF/HOLD-at-boot, lazily-expired lease `_weapons_posture` (same convention as `_range_authorization`/`_tx_halted`; NOT new persistence):
`{ state: HOLD|TIGHT|FREE, ao: zone_id/bbox, permitted_effects: [...], expires_at, armed_by/at, safety_ack: {AO-level SafetyGate}, iff_registry_loaded: bool }`
- `POST /api/weapons-posture` (commander): reuse `set_range_authorization`'s password step-up + confirm phrase (e.g. `"WEAPONS TIGHT <AO>"`); commander sets state/ao/permitted_effects, ARMS the matching range-auth leases via the existing `/range-authorization` path (unchanged — range-auth stays the real gate), acknowledges the AO-level SafetyGate checklist ONCE, requires `iff_registry_loaded==True` before any non-HOLD. Auto-expiry (30-60 min) + `/emergency/abort` drop to HOLD. Loud audit `WEAPONS_POSTURE_ARMED`.
- The posture is the STANDING AUTHORIZATION `/api/engage` draws on; it does NOT replace any gate, it is an ADDITIONAL required condition.

### `POST /api/engage` (ONE confirmed tap, per target)
Body `{ target_detection_id, engage_confirm, effect_override? }`, `require_commander`. Runs the WHOLE existing chain server-side, in order, each the existing unchanged function:
1. Posture valid/non-expired/state!=HOLD; target position ∈ ao; effect ∈ permitted_effects — else 409 surface.
2. `_check_tx_not_halted()`.
3. Load detection + hostility. **ROE FLOOR (hard): friendly/neutral/civilian-infra or not-classified-hostile → REFUSE, never fire. `/api/engage` NEVER auto-mints a friendly-fire ack** (engaging a friendly stays the separate deliberate `friendly-fire-ack` path).
4. Effect auto-select via `build_effector_recommendations` → `recommended_effector` (doctrine: JAM for DJI/OcuSync, MAVLink-takeover only unencrypted MAVLink, Wi-Fi deauth/ARSDK for identified Parrot/Tello, GNSS-deny as a layer). NOT_FEASIBLE/UNKNOWN/none-clearable/∉permitted → SURFACE the feasibility verdict + reason, do NOT fire. `effect_override` = pick a FEASIBLE failover.
5. Server mint→burn arm token bound {effect,target} in-process (atomic pop unchanged).
6. Server mint→burn the MATCHING effect-specific confirm token (4 non-interchangeable types unchanged). **LOAD-BEARING SAFETY: the confirm token's "deliberate human intent" is RELOCATED not removed = posture-arm (once, deliberate, password step-up + AO SafetyGate) + a deliberate per-target `engage_confirm` gesture (press-and-hold ≥800ms or tap-then-confirm; NEVER a bare click — reject if absent/falsey).**
7. `_enforce_fire_time_iff(...friendly_fire_ack=None)` — friendly hard-blocked 403 (machine-speed fratricide + ROE floor).
8. `_require_range_authorized(effect)` — 409 if lease off/expired.
9. Per-target scope: BSSID/no-broadcast, PMF/identity, system_id-0, encrypted-link fail-closed checks for the selected effect.
10. Dispatch via the existing AWAITING_ACK → WS → tx_ack honest-outcome machine; confirm token forwarded post-consumption as evidence.

**REFACTOR (Phase 0):** extract deploy-body steps 5-10 into internal `_execute_engagement(effect, detection, user, arm_token, confirm_token, ...)` that BOTH `/payloads/{effect}` AND `/api/engage` call → one gate implementation, zero drift, byte-identical.

## ROE gradient
- **HOLD** (default/rest): `/api/engage` refuses 409.
- **TIGHT** (RECOMMENDED FIELDED DEFAULT): one confirmed tap per target, full chain.
- **FREE** (higher-risk, DEFERRED, NOT v1): zone auto-engage of classified hostiles — a SEPARATE supervisor loop (NOT sop_engine, which stays statically inert) calling `/api/engage` internally for contacts that are (i) classified hostile, (ii) in the authorized fire zone, (iii) not in no-strike registry, (iv) FEASIBLE+permitted. Removes the per-target human decision → real fratricide-margin reduction → its own separate arming + proven non-empty no-strike registry + rate cap + hard per-cycle cap + live abort + loud audit; never UNKNOWN/position-less. Ship behind explicit user go, as a follow-on phase.

## Emergency stop stays one-touch
`/emergency/abort` (any operator, one call) sets `_tx_halted=True` AND drops `_weapons_posture` to HOLD AND halts the FREE supervisor. No posture/lease/token survives an abort.

## Interlocks preserved (machine-enforced, unchanged)
commander authority · tx_halted fail-closed · arm token (single-use/effect+target-bound/atomic) · 4 non-interchangeable confirm tokens · fire-time IFF fratricide · range-auth lease · device/BSSID scope + no-broadcast · one-touch kill-switch · honest AWAITING_ACK/tx_ack (no false-green). Backend gates BYTE-IDENTICAL; frontend stops hand-operating them.

## Phasing + gates
- **P0 — refactor (no behavior change):** extract `_execute_engagement`; `/payloads/{effect}` call it. GATE: verifier proves byte-identical + suite green; security-reviewer confirms no gate moved.
- **P1 — posture lease + `/api/engage` (TIGHT only):** GATE: independent security-review on the mint/burn (atomicity, binding, no friendly-fire auto-mint) + verifier adversarial tests (replay, cross-effect, friendly target, out-of-AO, lease-off, expired posture, abort mid-engage).
- **P2 — frontend:** Weapons-Hot panel (extend `EngagementControl.jsx`) + one-tap ENGAGE affordance (deliberate press-and-hold) per track; posture-arm reuses SafetyGate rigor. GATE: render-and-verify in Chrome + verifier false-green.
- **P3 (optional, higher-risk) — WEAPONS FREE supervisor:** separate module + separate arming + no-strike enforcement + rate caps + abort. GATE: dedicated security-review; explicit user go required.

## The load-bearing safety conditions (never water down)
1. Posture-arm requires commander + password step-up + AO SafetyGate ack + loaded IFF/no-strike registry.
2. The per-target tap is a DELIBERATE gesture (press-and-hold), never a bare click.
3. ROE floor: friendly/neutral/civilian infra is NEVER a valid one-tap target; `/api/engage` has no friendly-fire-ack path.
4. sop_engine stays statically transmit-incapable; FREE routes through a separate supervisor, never sop_engine.
If (1) or (2) is weakened, the "no human confirmed THIS shot" objection wins — they are load-bearing, not cosmetic.
