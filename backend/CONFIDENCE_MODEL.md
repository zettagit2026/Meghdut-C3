# Detection Confidence Model (ADR — B4)

## Status
Accepted (design), partially implemented.

## Context

`/api/detections/ingest` (see `backend/server.py`, `DetectionIngestBody`) is fed
by four independent bridges, each with a fundamentally different notion of
"confidence":

| # | Source | File | Existing field(s) | What it actually measures |
|---|--------|------|--------------------|----------------------------|
| 1 | RSSI/persistence heuristic | `field-bridge/hackrf_rx.py` (drone-candidate ingest) | none (binary: posted only when `consecutive_hits[name] >= CONFIRM_CYCLES`) | "Energy above floor persisted for N cycles." No probability, no verification. |
| 2 | ML classifier softmax | `field-bridge/ml_classify_bridge.py` | `ml_label`, `ml_confidence` (0-1), `ml_gated` | A real softmax probability from a closed-world 3-class ResNet18 model (drone / wifi_2_4 / wifi_5, no reject class). Empirically hallucinates >99% "drone" confidence on pure noise-floor energy at 3.6GHz. Energy-gating (`ml_gated`) mitigates but does not eliminate this. |
| 3 | CRC-verified protocol decode | `field-bridge/droneid_decode_bridge.py` | `threat_level: "HIGH"`, free-text `notes` | Binary pass/fail: a DUML/OcuSync frame either passes CRC and decodes, or it's discarded (`check_crc()` fails -> not reported at all). When it fires, this is categorically more certain than any RSSI or softmax signal — it is a verified protocol decode, not an inference. |
| 4 | Bluetooth advisory | `field-bridge/hackrf_rx.py` (`bt_det` block) | `threat_level: "LOW"`, `model: "Bluetooth device (advisory)"` | Presence-only heuristic (rapid-hop 2.4GHz signature). Explicitly NOT a device-identity or threat claim. |
| — | Cadence-analysis stats (B3) | (separate enrichment) | interval regularity / cross-session gap stats | Enrichment on top of an already-confirmed detection, not itself a confirmation signal. Out of scope for this field — does not get a `confidence_type`. |

These four are not commensurable on one 0-1 scale. Averaging a CRC pass/fail
against a 0.73 softmax score, or blending a presence-only BT heuristic into
the same number as a verified protocol decode, would manufacture false
precision and actively mislead an operator. **We reject a single blended
"confidence score" as a design outcome.**

## Decision

Add one new field, `confidence_type`, to `DetectionIngestBody` — a small
closed enum describing the **epistemic category** of the detection, alongside
the existing raw per-source fields (`ml_confidence`, `ml_label`, `ml_gated`,
`threat_level`, etc.), which are kept exactly as they are. `confidence_type`
does not replace or derive a number from them; it tells the frontend *which
renderer* is honest for this row.

```python
# backend/server.py, DetectionIngestBody
confidence_type: Optional[str] = None
# One of: "heuristic_binary", "ml_probability", "protocol_verified",
# "advisory_only". Optional/None for any source that hasn't been updated yet
# (backward compatible — absence means "render as before").
```

### Enum values

| `confidence_type` | Meaning | Honest frontend rendering |
|---|---|---|
| `heuristic_binary` | Confirmed by a persistence/threshold rule; no real-valued probability exists. | Plain "flagged"/"confirmed" tag. No percentage, no bar — there is no number to show. |
| `ml_probability` | A real softmax probability from a known-flawed model. | Probability bar/percentage, rendered with muted/secondary styling (never "confirmed" green), same caution as today's `MlClassifierBadge`. |
| `protocol_verified` | CRC/protocol-level decode succeeded. | Hard checkmark/verified badge, no percentage (there is no probability — it's pass/fail and it passed). Highest-confidence visual treatment. |
| `advisory_only` | Presence heuristic, explicitly not an identity or threat claim. | Plain neutral "advisory" tag, distinct styling from confirmed detections, no percentage. |

### Bridge -> enum mapping

| Bridge | Ingest call site | `confidence_type` to set |
|---|---|---|
| `field-bridge/hackrf_rx.py`, drone-candidate `det` (persistence-confirmed) | `consecutive_hits[name] >= CONFIRM_CYCLES` block (~line 667) | `"heuristic_binary"` |
| `field-bridge/hackrf_rx.py`, Bluetooth `bt_det` | BT advisory block (~line 640) | `"advisory_only"` |
| `field-bridge/ml_classify_bridge.py`, `det` | after `gated_capture_and_classify` (~line 245) | `"ml_probability"` |
| `field-bridge/droneid_decode_bridge.py`, `det` | after `payload.check_crc()` passes (~line 257) | `"protocol_verified"` |

Notes:
- `field-bridge/mavlink_sniffer.py` (real MAVLink HEARTBEAT decode, sets
  `protocol_confirmed=True`) is a fifth genuine protocol-decode source and
  should also set `confidence_type="protocol_verified"` when it's next
  touched — not included in this pass's minimal wiring since it wasn't in
  the B4 scope list, but the mapping is the same rule: CRC/protocol decode
  succeeded => `protocol_verified`.
- `distance_estimated`, `protocol_confirmed`, `ml_gated` etc. remain
  independent booleans; `confidence_type` is additive, not a replacement.

### Frontend rendering rule

Do NOT try to render one universal "confidence meter." Branch on
`confidence_type`:

- `protocol_verified` -> hard verified badge/checkmark, no bar.
- `ml_probability` -> probability bar/percentage, muted secondary styling (as `MlClassifierBadge` already does for `ml_label`/`ml_confidence`).
- `heuristic_binary` -> plain "flagged" tag, no number.
- `advisory_only` -> plain neutral "advisory" tag, no number.
- absent/unknown -> fall back to current behavior (just `threat_level`), so older rows or not-yet-wired sources don't break.

Implemented as `frontend/src/components/ConfidenceTypeBadge.jsx`, rendered
next to the existing `threat_level` badge and `MlClassifierBadge` in
`Dashboard.jsx` and `DetectionHistory.jsx`.

## Consequences

**Easier:**
- Operators can tell at a glance whether a HIGH/MEDIUM/LOW tag rests on a
  verified decode, a probability, a heuristic, or a mere advisory — without
  us inventing a fake unified number.
- New detection sources (e.g. a future TDOA/radar bridge, or `mavlink_sniffer.py`)
  slot into one of the four existing categories, or justify a fifth enum
  value with its own rationale — the model doesn't need architectural rework.
- `threat_level` continues to carry severity/priority as it does today;
  `confidence_type` is orthogonal (certainty basis), so the two are not
  conflated.

**Harder / deferred:**
- No single sortable "confidence %" column across all detections — this is
  intentional; a global sort-by-confidence feature would need its own
  explicit, documented (and probably per-type) ordering rule rather than a
  shared field.
- `mavlink_sniffer.py` wiring and any historical-data backfill (`confidence_type`
  is `None` for detections ingested before this change) are left as follow-up,
  not done in this pass.
