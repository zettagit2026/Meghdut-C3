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
# "advisory_only", "unclassified_signal". Optional/None for any source that
# hasn't been updated yet (backward compatible — absence means "render as
# before").
```

### Enum values

| `confidence_type` | Meaning | Honest frontend rendering |
|---|---|---|
| `heuristic_binary` | Confirmed by a persistence/threshold rule; no real-valued probability exists. | Plain "flagged"/"confirmed" tag. No percentage, no bar — there is no number to show. |
| `ml_probability` | A real softmax probability from a known-flawed model. | Probability bar/percentage, rendered with muted/secondary styling (never "confirmed" green), same caution as today's `MlClassifierBadge`. |
| `protocol_verified` | CRC/protocol-level decode succeeded. | Hard checkmark/verified badge, no percentage (there is no probability — it's pass/fail and it passed). Highest-confidence visual treatment. |
| `advisory_only` | Presence heuristic, explicitly not an identity or threat claim. | Plain neutral "advisory" tag, distinct styling from confirmed detections, no percentage. |
| `unclassified_signal` | Real, energy-gated RF confirmed present (same gate as `ml_probability`), but the classifier's own winning-class softmax probability was below `CEMA_ML_UNCLASSIFIED_MAX_CONFIDENCE` (default **0.6**, aligned with `ML_RECLASSIFY_MIN_CONFIDENCE` — see coherence note below) — i.e. it could not confidently place the signal in any of its 3 known classes (drone/wifi_2_4/wifi_5). This is cheap, zero-new-dependency logic added directly in `ml_classify_bridge.py`, computed from softmax probabilities the classifier already produces — no new model or OOT dependency (e.g. gr-inspector) required. | Distinct "UNCLASSIFIED" tag (not "flagged", not a trusted probability) showing the weak top-guess percentage for context, distinct styling from `ml_probability`. Also counts as an "unconfirmed" detection for `isUnconfirmedDetection()`/`UnconfirmedTag` (2026-07-23 fix) — an explicit "I don't know" from the classifier is at least as uncertain as `heuristic_binary`, and that function previously only checked for `heuristic_binary`, silently missing this case. |

### Bridge -> enum mapping

| Bridge | Ingest call site | `confidence_type` to set |
|---|---|---|
| `field-bridge/hackrf_rx.py`, drone-candidate `det` (persistence-confirmed) | `consecutive_hits[name] >= CONFIRM_CYCLES` block (~line 667) | `"heuristic_binary"` |
| `field-bridge/hackrf_rx.py`, Bluetooth `bt_det` | BT advisory block (~line 640) | `"advisory_only"` |
| `field-bridge/ml_classify_bridge.py`, `det` | after `gated_capture_and_classify` (~line 245) | `"ml_probability"` |
| `field-bridge/droneid_decode_bridge.py`, `det` | after `payload.check_crc()` passes (~line 257) | `"protocol_verified"` |
| `field-bridge/ml_classify_bridge.py`, `det` | when winning-class softmax < `UNCLASSIFIED_MAX_CONFIDENCE` (see `is_unclassified` in `gated_capture_and_classify` caller) | `"unclassified_signal"` |

Notes:
- `field-bridge/mavlink_sniffer.py` (real MAVLink HEARTBEAT decode, sets
  `protocol_confirmed=True`) is a fifth genuine protocol-decode source and
  should also set `confidence_type="protocol_verified"` when it's next
  touched — not included in this pass's minimal wiring since it wasn't in
  the B4 scope list, but the mapping is the same rule: CRC/protocol decode
  succeeded => `protocol_verified`.
- `field-bridge/rf_features.py` (spectral-feature RandomForest classifier,
  backlog C13, added 2026-07-23) is a FUTURE sixth source and would use a
  NEW enum value, `"spectral_features_ml"` (a real probability from a
  different, independent model than the ResNet18 spectrogram classifier —
  deliberately NOT reusing `ml_probability` so operators/frontend can tell
  the two model families apart). NOT wired into ingest yet: as of this
  writing, `rf_features.py` ships feature-extraction + training
  infrastructure only, with no trained model (no real labeled dataset has
  been assembled). Do not add `"spectral_features_ml"` to any ingest call
  site until a model has actually been trained on real labeled data and
  validated on a held-out split.
- `distance_estimated`, `protocol_confirmed`, `ml_gated` etc. remain
  independent booleans; `confidence_type` is additive, not a replacement.
- **Threshold coherence (2026-07-23 fix):** `UNCLASSIFIED_MAX_CONFIDENCE`
  (in `ml_classify_bridge.py`) and `ML_RECLASSIFY_MIN_CONFIDENCE` (in
  `backend/server.py`, 0.60) are intentionally kept EQUAL. With two
  independent cutoffs (previously 0.5 vs 0.60), a wifi_2_4/wifi_5 top-class
  read in the 50-59% gap between them was too weak to trigger the
  wifi-reclassification display fix but also too high to be honestly
  reported as `unclassified_signal` — it silently kept whatever
  `confidence_type` the record already had, with no informational badge at
  all reflecting the actual (low) confidence. Equalizing the two closes
  that gap: anything below `ML_RECLASSIFY_MIN_CONFIDENCE` is now uniformly
  reported as unclassified rather than falling through unqualified. If
  these constants are ever tuned independently again, re-derive one from
  the other rather than letting them drift apart.
- **Merge-match / display-override split (2026-07-23 fix):**
  `ml_classify_bridge.py` always sends the plain heuristic-consistent
  `model`/`protocol`/`threat_level` on the wire (e.g. `"DJI Mini
  (candidate)"`/`"OcuSync/Wi-Fi"`/`MEDIUM`), even for
  `confidence_type=="unclassified_signal"` ingests. Those fields double as
  `backend/server.py`'s merge-match key against `hackrf_rx.py`'s existing
  ACTIVE record (see `detection_ingest`); sending
  `"Unclassified emitter (candidate)"`/`"Unknown"` on the wire (as an
  earlier version of this feature did) would never match that key and
  would silently spawn a second, duplicate ACTIVE detection for the same
  physical contact instead of merging into it. The honest "Unclassified
  emitter (candidate)"/`"Unknown"`/`LOW` DISPLAY the operator sees is now
  computed server-side in `detection_ingest` via
  `_ml_unclassified_display()`, off `confidence_type`, using exactly the
  same split already established for `_ml_wifi_reclassification()` /
  `ML_WIFI_RECLASSIFY_DISPLAY`.

### Frontend rendering rule

Do NOT try to render one universal "confidence meter." Branch on
`confidence_type`:

- `protocol_verified` -> hard verified badge/checkmark, no bar.
- `ml_probability` -> probability bar/percentage, muted secondary styling (as `MlClassifierBadge` already does for `ml_label`/`ml_confidence`).
- `heuristic_binary` -> plain "flagged" tag, no number.
- `advisory_only` -> plain neutral "advisory" tag, no number.
- `unclassified_signal` -> distinct "unclassified" tag with the (weak) top-guess percentage for context, never styled as a trusted result.
- absent/unknown -> fall back to current behavior (just `threat_level`), so older rows or not-yet-wired sources don't break.

### On gr-inspector (evaluated, not integrated)

`~/Desktop/Zettawise/PMO Suraj/tool/gr-inspector` was evaluated as a source
of blind/unknown-signal detection (its "Signal Detector" energy-detection
block plus blind OFDM parameter estimation). Findings:

- It is a real GNU Radio 3.8 **OOT module** (`CMakeLists.txt`, `gr_modtool`
  bindings, C++/Python hybrid blocks) — not a pip-installable library. It
  requires a full GNU Radio 3.8 build environment plus Qt5 and Qwt 6.1.0,
  and its own README documents unresolved pybind11/Qt binding issues on
  some setups. Its TensorFlow-based AMC component is explicitly marked
  "not on GR 3.8 yet" upstream.
- This project's field-bridges are plain Python (`subprocess`+`hackrf_transfer`
  via `iq_capture.py`, PyTorch inference via `gamutrf_infer.py`) with no
  existing GNU Radio runtime dependency at all. Pulling in gr-inspector
  would mean building/maintaining an entire OOT module + GR flowgraph
  runtime as a new deployment dependency, on top of the GamutRF-symlinked
  GNU Radio the project's other bridges do not use for this purpose.
- The specific value gr-inspector would add over `unclassified_signal`
  above is real signal analysis when energy is unclassified — blind OFDM
  carrier-spacing/symbol-time estimation, not just "no known class fits."
  That is not something achievable from the ResNet18 classifier's existing
  softmax output; it would need gr-inspector's (or an equivalent) actual
  DSP blocks.
- Recommendation: **defer** the OOT module build. `unclassified_signal` was
  implemented directly against already-computed softmax probabilities
  (zero new dependencies, no new build system) and captures most
  near-term operator value ("there is a real emitter here we don't
  recognize"). Revisit gr-inspector specifically if/when blind OFDM
  parameter estimation on unclassified emitters becomes an actual
  requirement, not just a nice-to-have — at that point the OOT build
  effort would need to be scoped and tested as its own project (likely on
  the deploy VM, not the Mac dev copy), separate from this field-bridge
  Python codebase.

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
