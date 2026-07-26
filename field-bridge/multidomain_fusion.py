#!/usr/bin/env python3
"""Multi-domain (RF + thermal + acoustic) confidence fusion — software
groundwork for task #123, building on task #83's scoping
(CAMERA_THERMAL_ACOUSTIC_SCOPE.md) and the RF-only classical-ML "second
opinion" fusion pattern already established in this codebase
(field-bridge/ml_calibration.py's softmax-entropy/OOD gating combined with
field-bridge/ml_classify_bridge.py's confidence-gated ingest logic).

=============================================================================
WHAT THIS IS (and is not)
=============================================================================
This module combines independent per-modality detection confidences (RF,
thermal, acoustic) into ONE fused confidence number, using a principled
statistical combination rule (log-likelihood-ratio / "log-odds" summation —
see "WHY THIS FUSION APPROACH" below), instead of an ad-hoc weighted average.

STATUS: SOFTWARE-ONLY, HARDWARE-BLOCKED FOR TWO OF THREE INPUTS. No thermal
camera, optical camera, or acoustic array hardware exists in this project
(see CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.1/3 — confirmed repeatedly). This
module is fully unit-testable today with synthetic per-modality confidence
values (see test_multidomain_fusion.py) because the fusion MATH does not
require real sensor data to validate — only the eventual thermal_bridge.py /
acoustic_bridge.py producers of real confidence values do. In production
today, only the RF modality can be populated with a real number; thermal and
acoustic inputs are accepted by this module's API but have no real producer
yet. See `confidence_type = "multidomain_fused"` note below and
CONFIDENCE_MODEL.md for the enum-value discipline this follows.

This module is NOT wired into any live ingest path, systemd service, or
backend endpoint. Per CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4's explicit
recommendation ("Do not build cross-modality fusion/merge logic in this
first pass"), this stays a tested-but-unplugged module until a real
non-RF sensor exists to feed it and a real correlation key (e.g. shared
bearing/geolocation) exists to justify actually merging two independent
sensors' detections of "the same object" in the live system.

=============================================================================
WHY THIS FUSION APPROACH (log-likelihood-ratio / log-odds summation)
=============================================================================
Rejected: a plain weighted average of per-modality confidences. This
codebase's own CONFIDENCE_MODEL.md already rejects "a single blended
confidence score" for exactly this reason when combining RF sources of
different epistemic types ("Averaging a CRC pass/fail against a 0.73 softmax
score ... would manufacture false precision"). The same objection applies,
even more so, across sensing modalities: a plain average of e.g. RF=0.9 and
acoustic=0.1 yields 0.5, which reads as "moderately confident" when the
honest read is "these two independent sensors flatly disagree" — a materially
different, and more operationally useful, statement.

Chosen: treat each modality's confidence as an independent piece of evidence
for the binary hypothesis H = "this is really a drone" vs. not-H, and combine
them via Bayesian log-odds summation (this is the same mathematical
combination rule used in naive-Bayes classifiers, in spam filtering
("Bayesian" spam scores), and in classical multi-sensor track-fusion
literature for combining conditionally-independent sensor likelihoods):

    logit(p) = ln(p / (1 - p))                     # log-odds of one modality
    fused_logit = sum(w_i * logit(p_i) for each present modality i)
    fused_p = sigmoid(fused_logit) = 1 / (1 + exp(-fused_logit))

Why this is defensible over an ad-hoc average:
  1. It has a real derivation: under the assumption that each modality's
     evidence is conditionally independent given the true/false hypothesis
     (a standard, explicitly-stated simplifying assumption — not asserted
     as exact physical fact, since e.g. thermal and optical both depend on
     line-of-sight and so are not perfectly independent in practice; RF vs.
     thermal vs. acoustic are a much better fit for this assumption since
     they rely on genuinely different physical observables per
     CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.0), log-odds summation IS the
     Bayes-optimal way to combine independent evidence — it is not an
     invented heuristic.
  2. Agreement amplifies correctly: two modalities that each mildly favor
     "drone" (p=0.7 each) sum in log-odds space to a MORE confident fused
     result than either alone — matching the intuition that independently
     corroborating evidence should increase confidence, which a plain
     average structurally cannot do (average of 0.7 and 0.7 is just 0.7).
  3. Conflict is visible, not hidden: two modalities that strongly disagree
     (p=0.95 vs. p=0.05) produce log-odds of opposite sign and largely
     cancel toward a near-0.5 fused probability — which correctly reads as
     "genuinely uncertain / conflicting evidence," not as a confident
     detection. This module additionally computes and reports an explicit
     `conflict_detected` flag (see below) so a conflicting-but-mathematically-
     near-0.5 result is never silently rendered as an ordinary medium-
     confidence detection — it is flagged as a disagreement for a human
     operator to resolve, consistent with this project's standing "never
     manufacture false precision" convention.
  4. Missing modalities degrade gracefully by construction: a modality that
     is absent contributes nothing to the sum (not a 0.0/artificial-neutral
     logit(0.5)=0 entry that would require special-casing) — single-modality
     input reduces the formula to `fused_p = p_rf`, i.e. an identity
     operation, with no fabricated cross-modality signal.

Per-modality weights (`DEFAULT_MODALITY_WEIGHTS`) let a modality with a less
mature/reliable detection method (per CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.2:
acoustic is "the least mature... useful as a corroborating/cueing signal, not
a standalone primary detector") contribute proportionally less log-odds
evidence than a more mature modality, without discarding it entirely. This
is a documented, adjustable design knob, not an ad-hoc magic number: weight
1.0 = full trust in the modality's own confidence calibration; weight < 1.0
= "this modality's confidence numbers are less reliably calibrated, count
its evidence at a discount." These starting weights are engineering
judgment calls (informed by the scope doc's maturity ranking), not measured
empirically (there is no field data yet to measure against) — they are
exposed as a parameter specifically so they can be recalibrated once real
false-positive-rate data exists per modality (CAMERA_THERMAL_ACOUSTIC_SCOPE.md
Sec.4's acoustic caution, Sec.7's field-validation step).

=============================================================================
`confidence_type` — reuses "multidomain_fused", a genuinely new epistemic
category per CONFIDENCE_MODEL.md's discipline
=============================================================================
CONFIDENCE_MODEL.md's existing enum (`heuristic_binary`, `ml_probability`,
`protocol_verified`, `advisory_only`, `unclassified_signal`,
`bistatic_radar_detection`) is an EPISTEMIC-CATEGORY enum, not a per-sensor
one — see that document's explicit design principle, also restated in
CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4 (which is why that document
recommends REUSING `ml_probability` for a single-sensor YOLO-style detector
rather than inventing a new value per sensor).

A cross-modality LOG-ODDS-COMBINED confidence is, however, genuinely a new
epistemic category distinct from every existing value: it is not a single
model's raw probability (`ml_probability`), not a pass/fail decode
(`protocol_verified`), not a single persistence heuristic
(`heuristic_binary`), and not a single physical-layer detection statistic
(`bistatic_radar_detection`) — it is a DERIVED number computed by combining
two or more OTHER confidence values, with a materially different meaning
(a fused joint estimate, potentially reporting a `conflict_detected` flag
that has no analogue in any single-sensor confidence type). That justifies
the new value added here: `"multidomain_fused"`.

HONESTY NOTE (matching this module's own docstring conventions elsewhere in
this codebase, e.g. thermal_radiometric.py): as of this writing,
`"multidomain_fused"` is implemented and tested but NOT wired into any
production ingest path, and in the one case it hypothetically could be used
in production today (fusing e.g. multiple independent RF-derived confidences
— HackRF heuristic + ML classifier — for the SAME contact), it has not been
wired there either, since no correlation-key/same-contact logic exists yet
to justify combining two RF sources of the same physical detection (see
CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.4's "Fusion question" — this module
implements the general N-modality combination math but a real integration
still needs that same-contact correlation key, RF or otherwise). The module
is ready to accept real thermal_bridge.py / acoustic_bridge.py confidence
values the moment that hardware and those bridges exist; today it can only
honestly be exercised with RF-derived and synthetic inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

CONFIDENCE_TYPE_MULTIDOMAIN_FUSED = "multidomain_fused"

# Clamp bound to avoid logit(0) / logit(1) = +/-inf from a modality reporting
# an exact 0.0 or 1.0 confidence (e.g. a protocol-decode-style pass/fail
# upstream value coerced to a probability). This mirrors the "never let one
# extreme input dominate/blow up the combination" caution already present in
# ml_calibration.py's entropy/OOD handling.
_EPS = 1e-6

# Engineering-judgment starting weights per CAMERA_THERMAL_ACOUSTIC_SCOPE.md
# Sec.2's maturity ranking (thermal/optical most mature, acoustic least
# mature -> "corroborating signal, not standalone primary detector"). Not
# empirically fitted -- no field false-positive-rate data exists yet to fit
# against. Recalibrate once it does (see module docstring).
DEFAULT_MODALITY_WEIGHTS: Dict[str, float] = {
    "rf": 1.0,
    "thermal": 1.0,
    "optical": 0.9,
    "acoustic": 0.6,
}

# If two present modalities' individual log-odds have opposite sign and both
# exceed this magnitude, they are "confidently disagreeing" rather than
# merely noisy-near-neutral -- flag as a conflict rather than silently
# reporting whatever the arithmetic sum works out to.
_CONFLICT_LLR_MAGNITUDE_THRESHOLD = 1.0  # logit(0.73) ~= 1.0


def _clamp_prob(p: float) -> float:
    return min(max(p, _EPS), 1.0 - _EPS)


def _logit(p: float) -> float:
    p = _clamp_prob(p)
    return math.log(p / (1.0 - p))


def _sigmoid(x: float) -> float:
    # Numerically stable sigmoid.
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass
class ModalityEvidence:
    """One modality's independent detection confidence for a single
    candidate contact. `confidence` is a 0-1 probability of "this is really
    a drone" from that modality's own detector -- e.g. RF's existing
    `ml_confidence` (0-1 softmax, per CONFIDENCE_MODEL.md's `ml_probability`
    row), or a hypothetical future thermal/acoustic detector's own
    probability output. `present=False` (or omitting the modality from the
    input list entirely) means "this sensor did not report anything for
    this contact" -- NOT "this sensor reported 0% confidence"; those are
    different statements and this module does not conflate them (see
    `fuse_confidences` docstring)."""
    modality: str
    confidence: Optional[float]
    present: bool = True

    def is_usable(self) -> bool:
        return self.present and self.confidence is not None


@dataclass
class FusionResult:
    fused_confidence: float
    contributing_modalities: List[str]
    conflict_detected: bool
    per_modality_llr: Dict[str, float] = field(default_factory=dict)
    confidence_type: str = CONFIDENCE_TYPE_MULTIDOMAIN_FUSED
    note: str = ""


def fuse_confidences(
    evidences: List[ModalityEvidence],
    weights: Optional[Dict[str, float]] = None,
) -> FusionResult:
    """Combine independent per-modality confidences into one fused
    confidence via weighted log-odds (log-likelihood-ratio) summation. See
    module docstring "WHY THIS FUSION APPROACH" for the justification.

    Graceful degradation:
      - No usable modalities (empty list, or all `present=False` /
        `confidence=None`) -> returns fused_confidence=0.5 (maximum-entropy
        "no evidence" prior), contributing_modalities=[], and a `note`
        explaining no signal was fabricated. Callers MUST check
        `contributing_modalities` before treating 0.5 as a real read --
        this is intentionally the same neutral value a genuine 50/50 fused
        result could also produce, by design (this module never invents a
        confident number from zero evidence).
      - Exactly one usable modality -> the log-odds sum reduces to that one
        modality's own (weighted) logit; with weight 1.0 this is
        mathematically an identity, i.e. fused_confidence == that
        modality's own confidence. No cross-modality signal is fabricated
        for a single-modality read.
      - Multiple usable modalities that agree -> fused_confidence is more
        extreme (closer to 0 or 1) than any single input, reflecting
        corroboration.
      - Multiple usable modalities that conflict -> fused_confidence moves
        toward 0.5 (the log-odds partially or fully cancel) AND
        `conflict_detected=True` is set when the disagreement is strong
        enough (see `_CONFLICT_LLR_MAGNITUDE_THRESHOLD`), so a conflicting
        read is never silently presented as an ordinary medium-confidence
        detection.
    """
    weights = weights or DEFAULT_MODALITY_WEIGHTS
    usable = [e for e in evidences if e.is_usable()]

    if not usable:
        return FusionResult(
            fused_confidence=0.5,
            contributing_modalities=[],
            conflict_detected=False,
            per_modality_llr={},
            note=(
                "No usable modality evidence provided (all absent or "
                "confidence=None). Returning the neutral 0.5 prior rather "
                "than fabricating a signal -- callers must check "
                "contributing_modalities before treating this as a real "
                "read."
            ),
        )

    per_modality_llr: Dict[str, float] = {}
    fused_logit = 0.0
    for e in usable:
        w = weights.get(e.modality, 1.0)
        llr = w * _logit(e.confidence)  # type: ignore[arg-type]
        per_modality_llr[e.modality] = llr
        fused_logit += llr

    fused_confidence = _sigmoid(fused_logit)

    conflict_detected = False
    llr_values = list(per_modality_llr.values())
    for i in range(len(llr_values)):
        for j in range(i + 1, len(llr_values)):
            a, b = llr_values[i], llr_values[j]
            if (a > _CONFLICT_LLR_MAGNITUDE_THRESHOLD and b < -_CONFLICT_LLR_MAGNITUDE_THRESHOLD) or (
                b > _CONFLICT_LLR_MAGNITUDE_THRESHOLD and a < -_CONFLICT_LLR_MAGNITUDE_THRESHOLD
            ):
                conflict_detected = True

    note = ""
    if conflict_detected:
        note = (
            "Conflicting modality evidence: at least two modalities "
            "confidently disagree. fused_confidence has moved toward 0.5 "
            "as a mathematical consequence, but treat this contact as "
            "UNRESOLVED CONFLICT for operator review, not as a genuine "
            "medium-confidence detection."
        )
    elif len(usable) == 1:
        note = (
            f"Single-modality input ({usable[0].modality}) -- "
            "fused_confidence is that modality's own confidence, "
            "unchanged; no cross-modality corroboration is possible or "
            "implied."
        )

    return FusionResult(
        fused_confidence=fused_confidence,
        contributing_modalities=[e.modality for e in usable],
        conflict_detected=conflict_detected,
        per_modality_llr=per_modality_llr,
        note=note,
    )
