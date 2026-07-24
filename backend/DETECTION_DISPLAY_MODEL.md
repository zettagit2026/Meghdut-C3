# Detection Display Model — Primary Label Logic (ADR — B5)

## Status
Accepted (design). Backend override for `heuristic_binary` implemented
2026-07-24 (`_heuristic_display()` in `backend/server.py`). Frontend
consumption of the new `original_model`/`original_protocol` values for this
case is a follow-up (UX Architect pass) — the data is already flowing, only
the visual treatment of the existing "was: X" annotation needs review/copy
changes.

**SUPERSEDING CORRECTION (2026-07-24, same day, URGENT):** the "wire/display
split" design described later in this document ("the wire fields
(`model`/`protocol`) stay byte-identical for merge-matching purposes") was
**factually wrong about what the merge query actually matched against**, and
that error was a live, severe, confirmed bug — not just a documentation
error. See the new **"Merge-match key architecture (corrected)"** section
below, which is now the authoritative description of how matching works.
Everything else in this document (the primary/secondary label design table,
the per-`confidence_type` rules) is unaffected and still accurate — only the
*mechanism* used to keep re-confirmation merging working across display
overrides was broken and has been fixed.

## Context

Operator complaint (2026-07-24, verbatim): *"the dashboard shows model as
dji mini even it is not confirmed. big confusion. the logic of the
displayed info is critical."*

This is a genuine confusion, not a cosmetic nit, and the earlier fix
(`isUnconfirmedDetection()` + `UnconfirmedTag`, see `CONFIDENCE_MODEL.md`)
did not resolve it. That fix appends a small muted `(unconfirmed)` string
*next to* the model name. But the model name itself — "DJI Mini
(candidate)" — is still the PRIMARY, large, high-contrast text in the row.
A tiny secondary disclaimer next to a big confident-looking manufacturer
name does not fix the confusion; on a fast-moving tactical console, the eye
reads the big text and the small text is easy to miss entirely. In a
mission-critical detection system, **the primary label itself must never
assert more identity than has actually been earned.**

The existing codebase already has the right *mechanism* for this — it just
hasn't been applied to the case the operator is pointing at. Two prior
display-override precedents exist in `backend/server.py`:

- `_ml_wifi_reclassification()` — when the ML classifier decisively says
  "this is Wi-Fi, not a drone," the primary label is overridden to "Wi-Fi
  2.4GHz (ML reclassified)" while the wire `model`/`protocol` fields stay
  the original heuristic guess, for merge-matching. The heuristic guess
  moves into `original_model`/`original_protocol`, rendered as a muted
  "was: X" secondary line.
- `_ml_unclassified_display()` — same pattern, for `unclassified_signal`.

Both of these fire only on a *later* ML ingest that arrives after the
heuristic's initial detection. Neither covers the much more common case:
a detection that is, and may remain for its entire lifetime, **pure
`heuristic_binary`** — RSSI/persistence-confirmed, no ML opinion ever
arrived (or the ML bridge is down/slow), no protocol decode. Today that
detection's PRIMARY label is `body.model` verbatim from `hackrf_rx.py`:
literally the string `"DJI Mini (candidate)"` or `"MAVLink craft
(candidate)"` (see `field-bridge/hackrf_rx.py` lines ~709-711 — these are
the only two heuristic model strings that currently exist). "(candidate)"
is buried inside the manufacturer name, in the same font weight and color
as the manufacturer name — it reads as a hedge on confidence, not as "this
manufacturer attribution is fabricated."

## Decision — the governing rule

**A specific manufacturer/model name may appear as the PRIMARY label only
when a genuine protocol-level decode occurred. Every other `confidence_type`
gets a generic, honest, category-level primary label. The heuristic's
specific guess (if any) is always demoted to secondary/muted text.**

This is a single rule, not five special cases, and it generalizes cleanly
to future confidence types: ask "did anything actually decode/verify a
specific identity here?" If no, don't print one as the big text.

### Per-`confidence_type` primary/secondary specification

| `confidence_type` | PRIMARY (large/prominent) | SECONDARY (muted, if any) | Rationale |
|---|---|---|---|
| `protocol_verified` | The real, specific model/protocol from the CRC-verified decode (`body.model`/`body.protocol`, unmodified) | — none needed | Genuinely earned. A DUML/OcuSync frame that passed CRC and decoded IS that model. No hedge required. |
| `ml_probability` — wifi-reclassified case (`_ml_wifi_reclassification` fires) | "Wi-Fi 2.4GHz (ML reclassified)" / "Wi-Fi 5GHz (ML reclassified)" (already implemented) | "was: DJI Mini (candidate)" via `original_model` (already implemented) | A real, decisive ML conclusion correcting a wrong heuristic guess — earned, though category-level (the model doesn't claim a specific manufacturer, just "this is Wi-Fi not a drone"). Already correct — no change. |
| `ml_probability` — decisive `ml_label=="drone"` case, no protocol decode | **Currently a gap, not fully covered by this pass — see "Known gap" below.** Recommended: a generic drone-band confirmation, e.g. "Confirmed Drone-Band Emitter" or "Drone Signature (ML-confirmed, model unknown)" — NOT the heuristic's specific manufacturer guess. | "possible match: DJI Mini (candidate)" via `original_model` | The 3-class ResNet18 classifier (drone / wifi_2_4 / wifi_5) has no concept of manufacturer or model — it cannot know "DJI Mini" vs. any other quadcopter. Its "drone" vote genuinely upgrades confidence that *some* drone-shaped signal is present, but the specific manufacturer/model attached to that record still comes entirely from the RSSI-heuristic's coarse pattern match (`"DJI" in name`), not from ML. Displaying "DJI Mini" as primary here overstates what was confirmed exactly as much as the pure heuristic case does — it just happens to now sit behind a green-looking "ml_probability" badge, which is arguably *more* misleading, not less. |
| `heuristic_binary` (the case the operator flagged) | Generic RF category text, e.g. "Unidentified 2.4GHz Emitter" (DJI-shaped heuristic match) or "Unidentified RF Emitter — SiK/MAVLink band" (MAVLink-shaped heuristic match). **Implemented** via `_heuristic_display()`. | "possible match: DJI Mini (candidate)" / "possible match: MAVLink craft (candidate)" via `original_model`/`original_protocol` (reusing the existing "was: X" secondary-line mechanism — copy needs a UX pass, see below) | This is bare RSSI/persistence pattern-matching against a name string in `hackrf_rx.py`. No ML opinion, no protocol decode. Nothing earns "DJI" specifically — 2.4GHz is saturated with ordinary Wi-Fi/Bluetooth that can trip the same persistence threshold. Given this is a mission-critical system where operator trust in the displayed identity has real consequences (misallocated attention, false escalation, or — just as dangerous — desensitization/alarm fatigue when "DJI Mini" turns out to be a neighbor's Wi-Fi router often enough that operators start ignoring the tag entirely), the primary label must not print a manufacturer name it did not earn. |
| `unclassified_signal` | "Unclassified emitter (candidate)" (already implemented via `_ml_unclassified_display`) | "was: DJI Mini (candidate)" via `original_model` (already implemented) | Already correct — an explicit "classifier doesn't know" state, honestly labeled. No change needed. |
| `advisory_only` | "Bluetooth device (advisory)" (already implemented, set directly by `hackrf_rx.py`) | — none needed | Already correct — already generic, already explicitly non-identity-claiming, already the lowest visual weight (`ADVISORY` badge, neutral gray). No change needed. |
| absent/unknown (legacy rows, pre-dates `confidence_type`) | Fall back to existing behavior: raw `model`/`protocol` as-is | — none | Backward compatibility — cannot retroactively know which category an old row belongs to. Out of scope to backfill (already noted as a deferred item in `CONFIDENCE_MODEL.md`). |

### Known gap: `ml_label=="drone"` decisive-confirmation case

The table above identifies a real gap this pass does **not** close:
today, when `ml_classify_bridge.py` sends a decisive `ml_label=="drone"`
read, `detection_ingest` flips `confidence_type` to `"ml_probability"` (see
`ml_is_decisive` block, `backend/server.py` ~line 1521) but does **not**
run `model`/`protocol` through any display override — the record keeps
showing the original RSSI-heuristic's specific manufacturer guess
("DJI Mini (candidate)"), now merely wearing a different (arguably more
trusted-looking) confidence badge. This reproduces the exact same "unearned
specific name as primary" problem the operator flagged, just gated behind a
different confidence_type. It was intentionally NOT included in this pass's
implementation (kept minimal per this task's scope: fix the flagged
`heuristic_binary` case cleanly first) but should be treated as a
same-priority follow-up, using the identical mechanism: a new
`_ml_drone_generic_display()` returning a generic "Confirmed Drone-Band
Emitter" pair, gated on `ml_label == "drone"` and decisive confidence,
mirroring `_ml_wifi_reclassification()` structurally. Flagging this
explicitly so it isn't silently dropped.

## Merge-match key architecture (corrected, 2026-07-24)

### The bug this section replaces

The original design (kept below, in "The wire/display split", for
historical context) claimed the merge-match query "keys on
`{source, model, protocol, status: ACTIVE}`" and that this was fine because
"the wire fields... stay byte-identical for merge-matching purposes." That
second claim was **false in practice**: the query matched the STORED
`model`/`protocol` (i.e. the DISPLAY value, sitting in the database) against
the RAW incoming `body.model`/`body.protocol` (the wire value, from the
current ingest). Those are only the same thing for a document that has
**never** had a display override applied. The instant any of the three
override paths (`_ml_wifi_reclassification`, `_ml_unclassified_display`,
`_heuristic_display`) wrote a display value into the stored `model`/
`protocol` fields, the document's stored value permanently diverged from
the raw literal every field-bridge script keeps sending on every
re-confirmation cycle (hackrf_rx.py's ~3s heartbeat never changes its
payload). From that point on, `detection_ingest`'s `find_one()` could never
find that document again by `{model, protocol}` — every subsequent
re-confirmation silently created a brand-new duplicate ACTIVE detection for
the same physical contact, forever. This affected any detection that had
ever been wifi-reclassified, flagged unclassified, or (once deployed)
generic-heuristic-displayed — i.e. potentially a large fraction of live
detections, since ML classification and heuristic display are common paths,
not edge cases.

### The fix: a dedicated, immutable match-key field

Rather than patch the query to special-case `original_model`/
`original_protocol` (which exist for display/audit purposes and are
themselves conditionally populated — `None` until the first override
fires), `detection_ingest` now maintains two fields whose **entire purpose**
is merge-matching and nothing else:

- `match_model` — set exactly once, at document creation, to the raw
  `body.model` from the very first ingest that created the record.
- `match_protocol` — same, for `body.protocol`.

**No code path — none of the three display-override branches, no future
one either — is permitted to write to these fields after creation.** This
is the key architectural invariant: match-key fields and display fields are
now fully decoupled data, not "the same field, sometimes overridden, if you
squint at it right." A field whose job is "find this document again" must
never share storage with a field whose job is "show the operator something
honest" — those two jobs have fundamentally different mutability
requirements, and conflating them is exactly what caused this incident.

The merge query becomes:

```python
existing = await db.detections.find_one({
    "source": body.source,
    "status": "ACTIVE",
    "last_seen": {"$gt": since},
    "$or": [
        {"match_model": body.model, "match_protocol": body.protocol},
        {"match_model": {"$exists": False}, "model": body.model, "protocol": body.protocol},
    ],
})
```

The second `$or` branch is a backward-compatibility fallback for documents
that predate this fix (no `match_model` field yet). It reproduces the OLD
(buggy) matching behavior, which is still correct for any legacy document
that has never been display-overridden. On any update that finds a
document via this fallback branch, the update handler backfills
`match_model`/`match_protocol` from the existing document (or the current
raw ingest if absent), so that document self-heals onto the primary
match-key path for all future cycles. Documents that were ALREADY display-
overridden before this fix shipped, and therefore already have a display
value baked into `model`/`protocol` with no `match_model`, will not be
found by the fallback either — those are pre-existing duplicates in live
data and require a separate, careful data-cleanup pass (see "Live data
impact" below); this query cannot retroactively repair them.

### Verified trace through all four required scenarios

1. **Heuristic-only detection, re-confirmed indefinitely.** Creation sets
   `match_model = body.model` (e.g. `"DJI Mini (candidate)"`). Every
   subsequent hackrf_rx.py cycle sends that same literal; `match_model`
   is never touched by the `_heuristic_display` branch (it only ever
   writes `model`/`protocol`/`original_model`/`original_protocol`). Query
   matches on `match_model` every time → same document updated, no
   duplicate, regardless of how many times the generic display string is
   re-applied to `model`.
2. **Wifi-reclassified detection, later re-confirmed by hackrf_rx.py.**
   `_ml_wifi_reclassification` overwrites `model`/`protocol` to the Wi-Fi
   display strings but — like every override branch — never touches
   `match_model`/`match_protocol`. hackrf_rx.py's next raw POST still
   carries the original heuristic literal, which still equals the
   document's untouched `match_model`. Query matches → same document
   updated (display fields stay as the Wi-Fi reclassification, since the
   `else` branch that would re-apply `_heuristic_display` only fires when
   neither `wifi_display` nor `unclassified_display` fired this cycle —
   unchanged, pre-existing precedence logic).
3. **Unclassified-signal detection, likewise re-confirmed.** Identical
   reasoning to (2): `_ml_unclassified_display` writes `model`/`protocol`/
   `original_model`/`original_protocol` only; `match_model`/`match_protocol`
   remain the original raw literal from creation, so hackrf_rx.py's next
   cycle still matches.
4. **A genuinely new, different physical contact.** A new `source`/
   `model`/`protocol` combination that has never been seen has no document
   anywhere with a matching `match_model`/`match_protocol` (nor, for a
   brand-new contact, any legacy document to fall back to) — `find_one()`
   returns `None`, and `detection_ingest` falls through to the creation
   branch exactly as before, correctly creating one new document rather
   than merging into an unrelated existing one. This is unaffected by the
   fix: the `source` field and the "no match found" `None` case behave
   identically to the pre-fix query.

### Live data impact — follow-up required, NOT done as part of this fix

This bug has been live in production (172.16.16.196, primary) since the
wifi-reclassification and unclassified-signal override paths shipped —
i.e. it may already have created duplicate ACTIVE detections for any
contact that was ever ML-reclassified or flagged unclassified before this
fix deployed. **A live-data audit and cleanup is a necessary follow-up but
is deliberately NOT performed as part of this code fix** — identifying and
merging/retiring duplicate ACTIVE detections in a live mission system is a
separate, careful operation (need to decide which duplicate is canonical,
how to preserve `reconfirm_events` history, whether to notify operators of
count corrections, etc.) and should be scoped and executed independently,
ideally by whoever owns write access to the primary database, with its own
verification pass.

## The wire/display split (superseded — kept for historical context only)

`field-bridge/hackrf_rx.py`'s heuristic-created detections POST specific
model strings ("DJI Mini (candidate)", "MAVLink craft (candidate)") purely
because `backend/server.py`'s `detection_ingest` merge-match query keys on
`{source, model, protocol, status: ACTIVE}` (see top of `detection_ingest`).
Changing these wire-level strings would break re-confirmation merging
between `hackrf_rx.py`'s ~3s heartbeat re-posts and any later ML/protocol
ingest for the same physical contact — an already-documented, load-bearing
constraint from the B4 pass.

This is the exact same tension `_ml_wifi_reclassification()` and
`_ml_unclassified_display()` already solved, and the same solution applies:
**the wire fields (`model`/`protocol` as sent by `hackrf_rx.py`) stay
byte-identical for merge-matching purposes; the DISPLAY fields the operator
actually sees are computed server-side in `detection_ingest`, stored back
into the SAME `model`/`protocol` columns the frontend reads (the frontend
has no separate "display" field — it renders `d.model`/`d.protocol`
directly), with the raw heuristic guess preserved in
`original_model`/`original_protocol`.**

**NOTE: the claim above that the wire fields "stay byte-identical for
merge-matching purposes" describes only the WIRE side correctly — it does
NOT describe the actual merge-match query, which (before the 2026-07-24
fix) matched against the STORED `model`/`protocol`, not a dedicated
match-key. See "Merge-match key architecture (corrected)" above for the
accurate, current description.**

Concretely: `_heuristic_display()` is a new function, structurally
identical to `_ml_wifi_reclassification()`/`_ml_unclassified_display()`:

```python
HEURISTIC_GENERIC_DISPLAY = {
    "DJI Mini (candidate)": ("Unidentified 2.4GHz Emitter", "Unconfirmed (RF heuristic)"),
    "MAVLink craft (candidate)": ("Unidentified RF Emitter — SiK/MAVLink band", "Unconfirmed (RF heuristic)"),
}

def _heuristic_display(model, protocol, confidence_type):
    """Returns (display_model, display_protocol) when this detection has
    ONLY a bare RSSI/persistence heuristic behind it -- no ML opinion, no
    protocol decode -- else None. Mirrors _ml_wifi_reclassification() /
    _ml_unclassified_display(): wire model/protocol stay the heuristic's
    raw guess for merge-matching; this substitutes the honest generic
    category name into the DISPLAYED model/protocol fields, moving the raw
    guess into original_model/original_protocol."""
    if confidence_type != "heuristic_binary":
        return None
    return HEURISTIC_GENERIC_DISPLAY.get(model)
```

Applied at both:
1. **First creation** (`det.update({...})` block) — a heuristic-only
   detection gets the generic display from the moment it's created, not
   just on a later re-confirmation.
2. **Merge-update** (`existing` branch) — critically, the override must be
   **re-applied on every re-confirmation as long as `confidence_type` is
   still `heuristic_binary`**, not just once. `hackrf_rx.py` re-posts the
   SAME raw `body.model`/`body.protocol` every ~3s; if the override were
   only computed once at creation, nothing would need to defend against
   drift — but if a later ingest ever legitimately reverts `confidence_type`
   back toward `heuristic_binary` context (it currently never does — see
   `CONFIDENCE_MODEL.md`'s "never reset back to heuristic_binary" note —
   but the override function is defensively re-evaluated per-ingest anyway,
   the same as the wifi/unclassified overrides are, rather than "set once
   and trust it forever").

Only fires when `confidence_type == "heuristic_binary"` — i.e. never
overrides a display that a real ML or protocol signal already earned. If a
later ingest's `wifi_display` or `unclassified_display` or (future)
`ml_drone_display` fires, those take precedence and this generic override
does not apply (those branches already run first / are mutually exclusive
by construction — see implementation).

## Frontend consequence (for the follow-up UX Architect pass)

No new frontend field is needed. The frontend already renders
`d.model`/`d.protocol` as primary text and `d.original_model`/
`d.original_protocol` (when present and different) as a muted secondary
"was: X" line in both `Dashboard.jsx` and `DetectionHistory.jsx`. Once the
backend override above populates `model="Unidentified 2.4GHz Emitter"` +
`original_model="DJI Mini (candidate)"` for these rows, the existing
mechanism already renders the correct information hierarchy with **zero
frontend code changes required for the data flow to work**.

What the UX Architect pass should still do:
1. **Reword the secondary line's copy** for this case. "was: DJI Mini
   (candidate)" (past tense, implies a corrected mistake) is the right
   phrasing for the ML-wifi-reclassification case (something WAS
   classified as X, then corrected), but wrong for the heuristic case
   (nothing was ever confirmed; there's no correction, just a demoted
   guess). Recommended: a `possible match: X` or `pattern match: X` phrasing
   for the `heuristic_binary` case specifically — i.e. the label
   ("was:" vs "possible match:") should itself be conditional on why
   `original_model` is populated, not a single fixed string. This may want
   a small enum on the backend (e.g. `original_model_reason:
   "reclassified" | "unconfirmed_pattern_match"`) so the frontend doesn't
   have to reverse-engineer intent from `confidence_type` alone — left as
   an implementation decision for that pass.
2. Re-check `ConfidenceTypeBadge`'s `FLAGGED` heuristic_binary badge
   copy/tooltip still reads correctly now that the primary text next to it
   is generic rather than a manufacturer name (it should — the tooltip
   already explains "no real-valued probability exists").
3. Decide whether `UnconfirmedTag`'s `(unconfirmed)` marker is now
   redundant next to a generic primary label for the `heuristic_binary`
   case (arguably yes — a label that already says "Unidentified 2.4GHz
   Emitter" doesn't need a follow-up "(unconfirmed)" appended) and simplify
   if so. Leave `unclassified_signal`'s use of `UnconfirmedTag` untouched —
   that case's primary label ("Unclassified emitter (candidate)") still
   benefits from the marker since "candidate" alone reads ambiguous.
4. Implement/verify the drone-decisive-ML gap noted above once
   `_ml_drone_generic_display()` is added.

## Consequences

**Easier:**
- The rule "no specific model name unless genuinely decoded" is a single,
  memorable, defensible policy an operator can be told and trust, rather
  than five ad-hoc special cases.
- Extends cleanly: any future confidence type just answers "did this earn a
  specific identity?" — if not, it's a `_<sourcetype>_display()` function
  following the exact same wire/display split already established twice.
- Zero merge-matching risk: wire fields are untouched, exactly as the two
  existing overrides already prove out.

**Harder / deferred:**
- The `ml_label=="drone"` decisive-confirmation gap (see above) is real and
  not fixed by this pass — tracked explicitly, not silently dropped.
- Frontend copy ("was:" vs "possible match:") needs a wording decision by
  the UX Architect pass; shipping the backend change alone means the UI
  will show technically-correct but awkwardly-worded secondary text
  ("was: DJI Mini (candidate)") until that copy pass lands. This is a
  strict improvement over today (specific name no longer PRIMARY) even
  before the copy is polished, so it is safe to ship independently.
