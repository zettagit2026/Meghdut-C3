"""Effector-Selection engine -- the HONESTY CORE of MEGHDUT C3 decision support
(RFI Northern Command 4.5.4 / 4.5.6 / 4.5.7; feeds 4.2.2 manual C2).

WHAT THIS IS (read this before touching anything here)
------------------------------------------------------
Given the current contacts + the ranked engagement PLAN + a snapshot of effector
availability, this module produces, per contact, a PROPOSED effector
recommendation: an explainable threat score, an honest per-effector feasibility
matrix (jam / GNSS-deny / MAVLink-takeover), a recommended effector with a
failover order, and a duplicate-engagement flag. It is a sibling of
`engagement_planner` and `sop_engine`: same PROPOSED-only, commander-cued
posture.

WHAT THIS IS NOT -- CRITICAL SAFETY BOUNDARY (governing invariant)
-----------------------------------------------------------------
This module has NO capability to engage, transmit, jam, spoof, build or inject a
MAVLink frame, clear the master TX halt, mint/consume an arm token, satisfy the
friendly-fire or range-authorization interlocks, or mutate any detection/track.
It is PURE DATA: plain
dicts in, a plan dict out. It imports NOTHING that can transmit -- only stdlib,
`threat_library` (pure library lookups) and `mavlink_codec.classify_override_link`
(a pure, fail-closed protocol classifier). There is intentionally no code path
here that can cause a transmission. This inertness is STATIC-ENFORCED by
`tests/test_effector_selection.py` (AST + source scan), mirroring
`tests/test_sop_engine.py`.

The `effector_availability` argument is a READ-ONLY SNAPSHOT the caller passes
in. This module READS `tx_halted` / `bridge_up` / `range_auth_enabled` to say
whether an effector is *currently clearable*; it NEVER clears, sets, or bypasses
any of them. Every recommendation is stamped
status="PROPOSED_REQUIRES_HUMAN_AUTHORIZATION" and carries execution_paths as
DOCUMENTATION STRINGS ONLY. The commander executes every engagement through the
EXISTING gated endpoints; there is NO execute endpoint here.

HONESTY (governing invariant #2)
--------------------------------
Every feasibility verdict is grounded in real code, never invented capability:
  * `mavlink_codec.classify_override_link` -> encrypted / legacy_mavlink /
    unknown (fail-closed on unknown), AND
  * the matched threat-library entry's `countermeasures`
    (`cyber_takeover_applicable`, `gnss_deny_applicable`, `jam_bands`).
Encrypted/FHSS link -> takeover NOT_FEASIBLE (inject is a NO-OP). Unknown link
-> takeover UNKNOWN (never a confident "viable"; needs link-type ID / operator
attestation). GNSS-deny -> always the v1 placeholder verdict (receiver-lock
unproven), NEVER a plain FEASIBLE. Jam -> universally applicable as an RF deny,
but its *effectiveness* is power/proximity/band physics, not software-decidable,
so it is FEASIBLE_UNVERIFIED_RANGE. **When the two takeover signals DISAGREE the
conservative (fail-closed / NOT_FEASIBLE) verdict is taken and BOTH signals are
surfaced in the rationale.**

Position honesty (two-lane, from sop_engine): proximity/range scoring ONLY for
contacts with a real `position_source`; a position-less contact still gets full
protocol-based feasibility but `proximity_factor: null` with a note -- never a
guessed distance.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import threat_library
from mavlink_codec import classify_override_link

# ==========================================================================
# Constants -- values reused from engagement_planner (copied as literals, NOT
# imported: this module must stay a pure leaf whose only non-stdlib imports are
# threat_library + mavlink_codec.classify_override_link, statically enforced).
# ==========================================================================

# Mirrors engagement_planner.PROPOSED_STATUS verbatim.
PROPOSED_STATUS = "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"

# Mirrors engagement_planner.THREAT_WEIGHT values verbatim (deliberately coarse
# and explicit -- an integer weight, never a blended 0-1 float).
THREAT_WEIGHT: Dict[str, int] = {
    "CRITICAL": 100,
    "HIGH": 75,
    "MEDIUM": 50,
    "LOW": 25,
}

# Categorical confidence factor -- from confidence_type, NEVER a blended float.
# (category label, integer contribution). Anything unrecognised scores 0.
CONFIDENCE_FACTOR: Dict[str, Dict[str, Any]] = {
    "protocol_verified": {"category": "HIGH", "value": 20},
    "broadcast_decoded": {"category": "HIGH", "value": 20},
    "remoteid_decoded": {"category": "HIGH", "value": 20},
    "rf_signature": {"category": "MEDIUM", "value": 10},
    "heuristic_binary": {"category": "MEDIUM", "value": 10},
    "heuristic": {"category": "MEDIUM", "value": 10},
}
_CONFIDENCE_UNKNOWN = {"category": "UNKNOWN", "value": 0}

# Fixed positional-track bonus for a contact that carries a real decoded
# position but no measured range to a defended asset. This is a TRACK-QUALITY
# bonus for having a real position, NOT a distance estimate -- no distance is
# guessed (governing invariant: position honesty).
POSITION_TRACK_BONUS = 5

# Feasibility verdict vocabulary.
FEASIBLE = "FEASIBLE"
FEASIBLE_UNVERIFIED_RANGE = "FEASIBLE_UNVERIFIED_RANGE"
FEASIBLE_PLACEHOLDER_V1 = "FEASIBLE_PLACEHOLDER_V1"
NOT_FEASIBLE = "NOT_FEASIBLE"
UNKNOWN = "UNKNOWN"

# The verdicts that count as "an effector is feasible to propose".
_FEASIBLE_VERDICTS = frozenset(
    {FEASIBLE, FEASIBLE_UNVERIFIED_RANGE, FEASIBLE_PLACEHOLDER_V1}
)

# Effector names + the effector_availability snapshot key each reads.
EFF_JAM = "jam"
EFF_GNSS_DENY = "gnss_deny"
EFF_MAVLINK_TAKEOVER = "mavlink_takeover"

# effector name -> availability snapshot key (the module READS these flags).
_AVAILABILITY_KEY: Dict[str, str] = {
    EFF_JAM: "jam",
    EFF_GNSS_DENY: "gnss_spoof",
    EFF_MAVLINK_TAKEOVER: "mavlink_sdr_inject",
}

# execution_paths -- DOCUMENTATION STRINGS ONLY. These describe the EXISTING
# commander-gated endpoints the human uses; this module never calls them.
EXECUTION_PATHS: Dict[str, str] = {
    EFF_JAM: (
        "POST /api/payloads/jam (commander-gated: require_commander + "
        "TX-not-halted master kill + range authorization; the human fires)."
    ),
    EFF_GNSS_DENY: (
        "POST /api/payloads/gnss-spoof (commander-gated; v1 placeholder "
        "maturity -- receiver-lock unproven; the human fires)."
    ),
    EFF_MAVLINK_TAKEOVER: (
        "POST /api/payloads/mavlink-sdr-inject with target_detection_id=<this "
        "id> (commander-gated: require_commander + TX-not-halted + fresh "
        "single-use arm token + range authorization; the human fires)."
    ),
}

# Reuses engagement_planner's disclaimer wording (the DECISION-SUPPORT PROPOSAL
# framing + the exact PROPOSED_REQUIRES_HUMAN_AUTHORIZATION posture), adapted to
# the effector-selection endpoints. This module recommends; the human authorizes
# and fires, every time.
PROPOSAL_DISCLAIMER = (
    "This is a DECISION-SUPPORT PROPOSAL only. No engagement has occurred and "
    "none will occur automatically. Nothing in this recommendation transmits, "
    "jams, spoofs, or injects anything. Each listed effector must be "
    "individually authorized and fired by a human commander, who must clear the "
    "full existing safety-gate chain (commander role, TX-not-halted master "
    "kill, a fresh single-use arm token where required, the friendly-fire "
    "interlock, and range authorization) via the existing gated payload "
    "endpoints. This engine only recommends an effector and a failover ORDER "
    "and reads a read-only availability snapshot; it clears nothing. The human "
    "authorizes and fires, every time."
)

DOCTRINE_NOTE = (
    "Effector doctrine: an UNENCRYPTED legacy-MAVLink link is best defeated by "
    "the surgical RC-override takeover (prefer it when feasible AND currently "
    "clearable), falling back to jam. An ENCRYPTED/FHSS or DJI link is not "
    "injectable, so jam is the primary defeat with a GNSS-denial layer where "
    "applicable. Jam is a universal RF deny but its effectiveness is a "
    "power/proximity/band physics question, not software-decidable "
    "(FEASIBLE_UNVERIFIED_RANGE). GNSS denial is a v1 placeholder "
    "(FEASIBLE_PLACEHOLDER_V1), never a proven kill. Recommendation TEXT only "
    "-- never an autonomous switch."
)


# ==========================================================================
# Helpers
# ==========================================================================
def _threat_weight(threat_level: Optional[str]) -> int:
    return THREAT_WEIGHT.get(threat_level or "", 0)


def _link_identifier(contact: Dict[str, Any]) -> str:
    """Best-available control-link identifier string for classify_override_link.

    classify_override_link does case-insensitive substring matching with
    encrypted taking precedence over legacy, so joining every available link
    identifier is safe: an OcuSync family or an ELRS protocol is still caught as
    encrypted, and a MAVLink protocol string is caught as legacy. Empty -> the
    classifier returns 'unknown' (fail-closed)."""
    parts = [
        contact.get("control_link_family"),
        contact.get("control_link_protocol"),
        contact.get("protocol"),
        contact.get("family"),
    ]
    return " ".join(str(p) for p in parts if p)


def _countermeasures_for(contact: Dict[str, Any],
                         threat_lib: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The matched threat-library entry's `countermeasures` block for a contact.

    Prefer an explicit `contact['countermeasures']` (the enriched contact may
    already carry the matched block); otherwise best-effort match the contact
    against the threat library. A matching/library failure NEVER raises -- it
    yields None, which drives the honest UNKNOWN/NOT_FEASIBLE verdicts rather
    than a fabricated capability."""
    explicit = contact.get("countermeasures")
    if isinstance(explicit, dict):
        return explicit
    try:
        observation = {
            "band": contact.get("band"),
            "control_link_family": (
                contact.get("control_link_family") or contact.get("family")
            ),
        }
        remoteid = contact.get("remoteid")
        if isinstance(remoteid, dict):
            observation["remoteid"] = remoteid
        match = threat_library.match_detection(observation, threat_lib)
    except Exception:
        return None
    best = (match or {}).get("best") or {}
    cm = best.get("countermeasures")
    return cm if isinstance(cm, dict) else None


def _availability_for(effector: str, availability: Dict[str, Any]) -> Dict[str, Any]:
    key = _AVAILABILITY_KEY[effector]
    slot = availability.get(key)
    return slot if isinstance(slot, dict) else {}


def _is_clearable(effector: str, availability: Dict[str, Any]) -> bool:
    """Whether an effector is CURRENTLY clearable per the read-only snapshot:
    bridge_up AND range_auth_enabled AND NOT tx_halted. The module only READS
    these; it never clears anything."""
    if availability.get("tx_halted"):
        return False
    slot = _availability_for(effector, availability)
    return bool(slot.get("bridge_up")) and bool(slot.get("range_auth_enabled"))


def _availability_reason(effector: str, availability: Dict[str, Any]) -> str:
    """Human-readable reason an effector is / is not clearable right now."""
    if availability.get("tx_halted"):
        return ("all TX halted (master kill) -- unavailable until the commander "
                "resumes transmit (POST /api/emergency/resume).")
    slot = _availability_for(effector, availability)
    if not slot:
        return f"no availability snapshot for {effector!r}."
    if not slot.get("bridge_up"):
        return (f"TX bridge down for {effector!r} -- no transmit consumer "
                "connected; unavailable until the bridge is up.")
    if not slot.get("range_auth_enabled"):
        return (f"range authorization not enabled for {effector!r} -- the "
                "commander must enable the effect lease before it can fire.")
    return (f"clearable now for {effector!r} (bridge up, range authorization "
            "enabled, TX not halted) -- still requires the human to fire.")


# ==========================================================================
# Threat scoring (4.5.4) -- explainable integer/categorical score_breakdown
# ==========================================================================
def _score_breakdown(contact: Dict[str, Any],
                     plan_proposal: Optional[Dict[str, Any]],
                     position_known: bool) -> Dict[str, Any]:
    threat_level = contact.get("threat_level")
    tw = _threat_weight(threat_level)

    # controller_bonus: JOIN from the engagement plan, do NOT recompute.
    controller_bonus_value = 0
    controller_basis = ("no engagement-plan proposal joined for this contact; "
                        "controller-first bonus not applicable.")
    if plan_proposal is not None:
        sb = plan_proposal.get("score_breakdown") or {}
        controller_bonus_value = int(sb.get("controller_first_bonus") or 0)
        controller_basis = (
            "joined from engagement_plan proposal "
            f"(is_controller_candidate={plan_proposal.get('is_controller_candidate')}); "
            "value taken from the plan, not recomputed."
        )

    conf_type = contact.get("confidence_type")
    conf = CONFIDENCE_FACTOR.get(conf_type or "", _CONFIDENCE_UNKNOWN)

    if position_known:
        proximity_factor: Optional[Dict[str, Any]] = {
            "category": "POSITION_KNOWN",
            "value": POSITION_TRACK_BONUS,
            "basis": (
                f"real decoded position present (position_source="
                f"{contact.get('position_source')!r}); a track-quality bonus for "
                "having a genuine fix. NO defended-asset reference supplied, so "
                "NO distance/range is computed or guessed."
            ),
        }
    else:
        proximity_factor = None

    return {
        "threat_weight": {
            "value": tw,
            "basis": (f"threat_level={threat_level!r} -> weight {tw} "
                      "(THREAT_WEIGHT, mirrors engagement_planner)."),
        },
        "controller_bonus": {
            "value": controller_bonus_value,
            "basis": controller_basis,
        },
        "confidence_factor": {
            "category": conf["category"],
            "value": conf["value"],
            "basis": (f"confidence_type={conf_type!r} -> {conf['category']} "
                      "(categorical, never a blended float)."),
        },
        "proximity_factor": proximity_factor,
        "proximity_note": (
            None if position_known else
            "position-less contact (no position_source) -- no proximity/range "
            "factor; no distance is guessed (position honesty)."
        ),
    }


def _threat_score(score_breakdown: Dict[str, Any]) -> int:
    """Explainable INTEGER total: sum of the numeric component values. Never a
    fabricated blended 0-1 float."""
    total = 0
    for key in ("threat_weight", "controller_bonus", "confidence_factor"):
        comp = score_breakdown.get(key) or {}
        total += int(comp.get("value") or 0)
    prox = score_breakdown.get("proximity_factor")
    if isinstance(prox, dict):
        total += int(prox.get("value") or 0)
    return total


# ==========================================================================
# Feasibility matrix (the crux) -- honest per-effector verdicts
# ==========================================================================
def _feasibility(contact: Dict[str, Any],
                 countermeasures: Optional[Dict[str, Any]],
                 link_class: str) -> Dict[str, Dict[str, Any]]:
    cm = countermeasures or {}
    gnss_applicable = cm.get("gnss_deny_applicable")
    cyber_applicable = cm.get("cyber_takeover_applicable")
    jam_bands = cm.get("jam_bands")

    # ---- JAM: always FEASIBLE_UNVERIFIED_RANGE (universal RF deny; the actual
    # effectiveness is power/proximity/band physics, not software-decidable). --
    jam_rationale = (
        "RF jamming is a universal deny applicable to any link; effectiveness "
        "is a power/proximity/band-physics question, NOT software-decidable, so "
        "range is UNVERIFIED."
    )
    if isinstance(jam_bands, list) and jam_bands:
        jam_rationale += f" Library jam_bands: {jam_bands}."
    jam = {"verdict": FEASIBLE_UNVERIFIED_RANGE, "rationale": jam_rationale}

    # ---- GNSS-deny: FEASIBLE_PLACEHOLDER_V1 when applicable (v1 receiver-lock
    # unproven -- NEVER a plain FEASIBLE); else UNKNOWN/NOT_FEASIBLE per what is
    # decidable. --------------------------------------------------------------
    if gnss_applicable is True:
        gnss = {
            "verdict": FEASIBLE_PLACEHOLDER_V1,
            "rationale": (
                "countermeasures.gnss_deny_applicable=true, BUT the GNSS-denial "
                "effector is a v1 placeholder (receiver-lock/effect unproven) -- "
                "reported as a placeholder, never a proven kill."
            ),
        }
    elif gnss_applicable is False:
        gnss = {
            "verdict": NOT_FEASIBLE,
            "rationale": (
                "countermeasures.gnss_deny_applicable=false -- GNSS denial has "
                "no expected effect on this platform (e.g. non-GNSS / manual "
                "flight)."
            ),
        }
    else:
        gnss = {
            "verdict": UNKNOWN,
            "rationale": (
                "gnss_deny_applicable is not decidable for this contact (no "
                "matched library countermeasures) -- honest UNKNOWN, not an "
                "assumed capability."
            ),
        }

    # ---- MAVLink-takeover: decided by BOTH classify_override_link AND
    # countermeasures.cyber_takeover_applicable. On DISAGREEMENT take the
    # conservative (fail-closed) verdict and surface BOTH signals. -------------
    mav = _takeover_verdict(link_class, cyber_applicable, contact)

    return {"jam": jam, "gnss_deny": gnss, "mavlink_takeover": mav}


def _takeover_verdict(link_class: str,
                      cyber_applicable: Optional[bool],
                      contact: Dict[str, Any]) -> Dict[str, Any]:
    link_str = _link_identifier(contact) or "(none)"
    sig = (f"classify_override_link({link_str!r})={link_class!r}; "
           f"countermeasures.cyber_takeover_applicable={cyber_applicable!r}")

    if link_class == "encrypted":
        # Encrypted/FHSS -> inject is a NO-OP. NOT_FEASIBLE regardless. If the
        # library somehow said applicable=true, that is a disagreement -> still
        # conservative NOT_FEASIBLE, both surfaced.
        rationale = (
            "encrypted/FHSS control link -- RC-override / MAVLink inject is a "
            "NO-OP against a crypto-bound or frequency-hopping link. NOT "
            f"feasible. [{sig}]"
        )
        if cyber_applicable is True:
            rationale += (" SIGNALS DISAGREE (library flags cyber-takeover "
                          "applicable, but the link classifies as encrypted); "
                          "taking the conservative fail-closed verdict.")
        verdict = NOT_FEASIBLE

    elif link_class == "legacy_mavlink":
        if cyber_applicable is False:
            # DISAGREEMENT: classifier says legacy (overridable) but the library
            # says takeover is NOT applicable -> conservative NOT_FEASIBLE.
            verdict = NOT_FEASIBLE
            rationale = (
                "SIGNALS DISAGREE: the control link classifies as legacy "
                "MAVLink (RC-override plausible), but the matched library entry "
                "reports cyber_takeover_applicable=false. Taking the "
                f"conservative (fail-closed) NOT_FEASIBLE verdict. [{sig}]"
            )
        elif cyber_applicable is True:
            verdict = FEASIBLE
            rationale = (
                "both signals agree: legacy/unencrypted MAVLink link AND "
                "library cyber_takeover_applicable=true -- RC-override / "
                f"MAVLink command injection is viable. [{sig}]"
            )
        else:  # None -- no library flag, but the classifier is a positive ID.
            verdict = FEASIBLE
            rationale = (
                "control link classifies as legacy/unencrypted MAVLink -- "
                "RC-override is viable; the threat library carried no explicit "
                "cyber_takeover flag, so the verdict rests on the link "
                f"classification alone. [{sig}]"
            )

    else:  # unknown
        # NEVER a confident "viable". Fail closed to UNKNOWN.
        verdict = UNKNOWN
        rationale = (
            "control link is unidentified/empty -- takeover feasibility is "
            "UNKNOWN (never assumed viable). Needs a positive link-type "
            f"identification or explicit operator attestation. [{sig}]"
        )
        if cyber_applicable is True:
            rationale += (" NOTE: the matched library entry flags cyber-takeover "
                          "applicable, but without a confirmed link class this "
                          "cannot be upgraded to feasible.")
        elif cyber_applicable is False:
            rationale += (" NOTE: the matched library entry flags cyber-takeover "
                          "NOT applicable, reinforcing non-viability.")

    return {"verdict": verdict, "rationale": rationale, "link_class": link_class}


# ==========================================================================
# Recommended effector + failover (4.5.6/4.5.7) -- recommendation text ONLY
# ==========================================================================
def _doctrine_order(feasible: Dict[str, str]) -> List[str]:
    """Preference order over the FEASIBLE effectors per doctrine.

    * legacy-MAVLink takeover feasible -> prefer the surgical takeover, then jam,
      then a GNSS-deny layer.
    * otherwise (encrypted / unknown / DJI) -> jam primary, GNSS-deny layer,
      takeover last (it will not be in `feasible` for encrypted anyway)."""
    if EFF_MAVLINK_TAKEOVER in feasible:
        preference = [EFF_MAVLINK_TAKEOVER, EFF_JAM, EFF_GNSS_DENY]
    else:
        preference = [EFF_JAM, EFF_GNSS_DENY, EFF_MAVLINK_TAKEOVER]
    return [e for e in preference if e in feasible]


def _recommend(feasibility: Dict[str, Dict[str, Any]],
               availability: Dict[str, Any]) -> Dict[str, Any]:
    feasible = {
        name: v["verdict"]
        for name, v in feasibility.items()
        if v["verdict"] in _FEASIBLE_VERDICTS
    }
    ordered = _doctrine_order(feasible)

    recommended: Optional[str] = None
    for eff in ordered:
        if _is_clearable(eff, availability):
            recommended = eff
            break

    if recommended is not None:
        rationale = (
            f"Recommended effector: {recommended!r} -- highest doctrine-preferred "
            f"FEASIBLE effector that is currently clearable "
            f"({_availability_reason(recommended, availability)}) Recommendation "
            "TEXT only; the commander authorizes and fires."
        )
    elif ordered:
        rationale = (
            "No feasible effector is currently clearable -- every feasible "
            "option is unavailable per the read-only snapshot (see "
            "failover_order reasons). No recommendation can be cleared until the "
            "commander restores availability. Recommendation TEXT only."
        )
    else:
        rationale = (
            "No effector is FEASIBLE for this contact (all verdicts are "
            "NOT_FEASIBLE/UNKNOWN) -- nothing is recommended. Obtain a positive "
            "link-type identification / operator attestation before proposing a "
            "takeover."
        )

    failover_order: List[Dict[str, Any]] = []
    for eff in ordered:
        if eff == recommended:
            continue
        failover_order.append({
            "effector": eff,
            "feasible": True,
            "available": _is_clearable(eff, availability),
            "reason": _availability_reason(eff, availability),
        })

    return {
        "recommended_effector": recommended,
        "recommended_rationale": rationale,
        "failover_order": failover_order,
    }


# ==========================================================================
# Public entry point
# ==========================================================================
def build_effector_recommendations(
    contacts: List[Dict[str, Any]],
    engagement_plan: Dict[str, Any],
    effector_availability: Dict[str, Any],
    already_engaged_detection_ids: Any,
    *,
    threat_lib: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Build PROPOSED effector recommendations (see module docstring).

    Inputs are plain dicts (no I/O, fully unit-testable):
      * contacts: SOP contacts (`_sop_current_contacts()`), enriched.
      * engagement_plan: output of `build_engagement_plan(...)` -- CONSUMED and
        joined by detection_id (controller bonus, rank, exclusions); never edited.
      * effector_availability: a READ-ONLY snapshot,
        {tx_halted, jam:{bridge_up,range_auth_enabled},
         gnss_spoof:{bridge_up,range_auth_enabled,maturity},
         mavlink_sdr_inject:{bridge_up,range_auth_enabled}}.
      * already_engaged_detection_ids: iterable of detection_ids already engaged
        (from the _pending_* maps) -- drives dedup_status.
      * threat_lib: optional pre-loaded threat library dict (passed to
        threat_library.match_detection).

    Returns a recommendation dict. NOTHING here transmits or mutates anything.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    availability = effector_availability or {}
    already_engaged = set(already_engaged_detection_ids or [])

    plan = engagement_plan or {}
    plan_proposals = {
        p.get("detection_id"): p
        for p in plan.get("proposals", []) or []
        if p.get("detection_id")
    }
    plan_excluded = {
        e.get("detection_id"): e
        for e in plan.get("excluded", []) or []
        if e.get("detection_id")
    }

    recommendations: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for contact in contacts or []:
        det_id = contact.get("detection_id")
        callsign = contact.get("callsign")

        if not det_id:
            excluded.append({
                "detection_id": None,
                "callsign": callsign,
                "reason": "contact has no detection_id -- cannot build a "
                          "per-detection recommendation.",
            })
            continue

        # Inherit engagement-plan exclusions (friendly / unconfirmed / not
        # ACTIVE ...). A contact the plan excluded is NOT proposed here either.
        plan_ex = plan_excluded.get(det_id)
        if plan_ex is not None:
            excluded.append({
                "detection_id": det_id,
                "callsign": callsign or plan_ex.get("callsign"),
                "reason": ("excluded by engagement plan: "
                           f"{plan_ex.get('reason')}"),
                "threat_level": plan_ex.get("threat_level"),
            })
            continue

        plan_proposal = plan_proposals.get(det_id)
        position_known = bool(contact.get("position_source"))

        score_breakdown = _score_breakdown(contact, plan_proposal, position_known)
        threat_score = _threat_score(score_breakdown)

        link_class = classify_override_link(_link_identifier(contact))
        countermeasures = _countermeasures_for(contact, threat_lib)
        feasibility = _feasibility(contact, countermeasures, link_class)

        rec = _recommend(feasibility, availability)

        dedup_engaged = det_id in already_engaged
        dedup_status = {
            "already_engaged": dedup_engaged,
            "reason": (
                (f"detection {det_id} is already under active engagement "
                 "(present in the pending-engagement maps) -- do NOT re-engage; "
                 "recommendation retained for situational awareness only.")
                if dedup_engaged else
                "not currently under an active engagement."
            ),
        }

        recommendations.append({
            "detection_id": det_id,
            "callsign": callsign,
            "threat_level": contact.get("threat_level"),
            "threat_score": threat_score,
            "score_breakdown": score_breakdown,
            "position_known": position_known,
            "feasibility": feasibility,
            "recommended_effector": rec["recommended_effector"],
            "recommended_rationale": rec["recommended_rationale"],
            "failover_order": rec["failover_order"],
            "dedup_status": dedup_status,
            "engagement_proposal_rank": (
                plan_proposal.get("rank") if plan_proposal else None
            ),
            "execution_paths": dict(EXECUTION_PATHS),
            "status": PROPOSED_STATUS,
        })

    # Deterministic ranking: highest threat_score first, then callsign then
    # detection_id, so ordering is stable across recomputes/tests.
    recommendations.sort(key=lambda r: (
        -int(r["threat_score"]),
        r.get("callsign") or "",
        r["detection_id"],
    ))

    summary = {
        "recommendation_count": len(recommendations),
        "excluded_count": len(excluded),
        "contacts_considered": len(contacts or []),
        "already_engaged_count": sum(
            1 for r in recommendations if r["dedup_status"]["already_engaged"]
        ),
        "tx_halted": bool(availability.get("tx_halted")),
        "recommendations_with_clearable_effector": sum(
            1 for r in recommendations if r["recommended_effector"] is not None
        ),
    }

    return {
        "generated_at": now.isoformat(),
        "disclaimer": PROPOSAL_DISCLAIMER,
        "doctrine": DOCTRINE_NOTE,
        "effector_availability_echo": availability,
        "recommendations": recommendations,
        "excluded": excluded,
        "summary": summary,
        "status": PROPOSED_STATUS,
    }
