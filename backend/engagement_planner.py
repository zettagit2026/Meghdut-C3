"""Prioritized engagement PLANNER (Army objective OB-02, gap-analysis
rated CRITICAL: "Swarm Attack Counter-Strategy" / SOL-02 "Anti-Swarm
Architecture", step: "prioritise controller destruction/jamming first").

WHAT THIS IS (read this before touching anything here)
------------------------------------------------------
This module produces a RANKED ENGAGEMENT *PROPOSAL* -- a decision-support
plan object -- from the current confirmed tracks + swarm candidate clusters.
It ranks which contacts a human commander might choose to engage, and in what
order, applying:

  * SOL-02 swarm-controller-node-first doctrine: the swarm-classifier's
    identified controller CANDIDATE node (swarm_classifier.identify_controller_
    candidate) is ranked highest, because neutralising the coordination node
    can collapse the whole mesh-C2 swarm (Type-IV). It is engaged FIRST, then
    the operator RE-ASSESSES (recompute the plan) to see whether the swarm
    scattered before engaging any remaining member. Sequential + re-assessed,
    NEVER a fire-everything-at-once sweep.
  * threat ordering (CRITICAL > HIGH > MEDIUM > LOW),
  * CONFIRMED-over-TENTATIVE: only contacts backed by a CONFIRMED track are
    proposed at all,
  * hard exclusions: an IFF-verified-friendly contact, or an unconfirmed /
    tentative / coasting track, is NEVER proposed as a target -- it is listed
    in `excluded` with the explicit reason.

WHAT THIS IS NOT -- CRITICAL SAFETY BOUNDARY
--------------------------------------------
This module has NO capability to engage, transmit, jam, spoof, build a MAVLink
frame, or mutate any detection/track. It deliberately imports NOTHING from
mavlink_codec / payload_library builders / the WebSocket manager / the DB. It
is PURE DATA: dicts in, a plan dict out. There is intentionally no code path
here that can cause a transmission.

The plan is a PROPOSAL that REQUIRES HUMAN AUTHORIZATION. Every proposal is
explicitly stamped status="PROPOSED_REQUIRES_HUMAN_AUTHORIZATION" and carries
the exact existing safety-gate chain the commander must clear, per engagement,
via the existing gated endpoints:

    POST /api/payloads/deploy   (single-target)   -- or --
    POST /api/mavlink/broadcast (swarm-wide PL-010)

...each of which independently re-enforces, at fire time and for THAT specific
target: commander role (require_commander), TX-not-halted (_check_tx_not_halted,
fail-closed master kill), a fresh single-use arm token for CRITICAL/broadcast
(_consume_arm_token), the friendly-fire IFF interlock (authorized_target /
_check_iff_friendly_match), and range authorization. This planner cannot and
does not bypass any of them; it only recommends an ORDER. The human authorizes
and fires, every time.

HONESTY
-------
The controller-candidate is a LOW-confidence heuristic (see swarm_classifier
docstring: no verified DoA/MAC-OUI/hop-sequence data exists). The plan carries
that confidence through verbatim and labels the recommendation a
"prioritisation hint", never a verified identification or an authorisation to
fire. The plan never claims an engagement has happened or will happen
automatically.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Lifecycle state strings mirror track_manager.py (kept as literals rather than
# importing, so this module stays a leaf with no import coupling to the track
# manager's asyncio/Mongo-adjacent code -- same "pure leaf" posture as
# swarm_classifier.py).
STATE_CONFIRMED = "CONFIRMED"
STATE_TENTATIVE = "TENTATIVE"
STATE_COASTING = "COASTING"
STATE_DROPPED = "DROPPED"

# IFF-friendly sentinel exactly as server.py sets it
# (_check_iff_friendly_match -> threat_level = "FRIENDLY (IFF verified)").
IFF_FRIENDLY_THREAT = "FRIENDLY (IFF verified)"

# Threat weighting for ranking. Deliberately coarse and explicit.
THREAT_WEIGHT: Dict[str, int] = {
    "CRITICAL": 100,
    "HIGH": 75,
    "MEDIUM": 50,
    "LOW": 25,
}

# SOL-02 controller-node-first doctrine: the identified controller CANDIDATE is
# ranked above every non-controller target regardless of raw threat weight, so
# it is always engaged first (and then re-assessed). Chosen larger than any
# achievable threat weight so it dominates the ordering deterministically.
CONTROLLER_PRIORITY_BONUS = 1000

# Only detections in this status are engageable candidates. Anything already
# AWAITING_ACK / NEUTRALIZED / LOST is not re-proposed.
ENGAGEABLE_STATUS = "ACTIVE"

PROPOSED_STATUS = "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"

PROPOSAL_DISCLAIMER = (
    "This is a DECISION-SUPPORT PROPOSAL only. No engagement has occurred and "
    "none will occur automatically. Nothing in this plan transmits, jams, or "
    "engages anything. Each listed engagement must be individually authorized "
    "and fired by a human commander, who must clear the full existing safety-"
    "gate chain (commander role, TX-not-halted master kill, a fresh single-use "
    "arm token per CRITICAL/broadcast action, the IFF friendly-fire interlock, "
    "and range authorization) via POST /api/payloads/deploy or "
    "POST /api/mavlink/broadcast. This planner only recommends an ORDER; the "
    "human authorizes and fires, every time."
)

DOCTRINE_NOTE = (
    "SOL-02 anti-swarm doctrine: engage the identified swarm-controller "
    "CANDIDATE node FIRST (neutralising the mesh-C2 coordinator can collapse "
    "the swarm), then RE-ASSESS (POST /api/engagement/plan/recompute) to check "
    "whether the swarm scattered before proposing any remaining member. "
    "Sequential and re-assessed -- never a simultaneous fire-everything sweep. "
    "The controller identification is a LOW-confidence timing heuristic (no "
    "verified DoA/MAC-OUI/hop-sequence data exists), a prioritisation hint, "
    "not grounds for action on its own."
)

# The exact human-cleared gates that the EXISTING endpoints re-enforce at fire
# time. Reproduced here as documentation the operator sees on every proposal --
# NOT as something this module checks or can satisfy on the human's behalf.
REQUIRED_HUMAN_CLEARED_GATES = [
    "commander_role (require_commander)",
    "tx_not_halted (_check_tx_not_halted -- fail-closed master kill; must POST "
    "/api/emergency/resume after any abort/restart)",
    "fresh_single_use_arm_token (_consume_arm_token -- required for CRITICAL "
    "severity or any broadcast; obtained via POST /api/arm, one per engagement)",
    "iff_friendly_fire_interlock (authorized_target must be set via POST "
    "/api/detections/{id}/authorize-target, which itself blocks IFF-verified "
    "friendlies unless a commander explicitly overrides + audits)",
    "range_authorization (mavlink effect lease must be enabled)",
]


def _is_iff_friendly(det: Dict[str, Any]) -> bool:
    return bool(det.get("iff_verified")) or det.get("threat_level") == IFF_FRIENDLY_THREAT


def _threat_weight(threat_level: Optional[str]) -> int:
    return THREAT_WEIGHT.get(threat_level or "", 0)


def _confirmed_track_index(tracks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map detection_id -> the best track referencing it, so we can gate a
    detection on whether it is backed by a CONFIRMED (live) track.

    "Best" = prefer a CONFIRMED track over any other state; among ties prefer
    the most recently updated. A detection referenced only by a TENTATIVE or
    COASTING track (or by no track at all) is NOT engageable and will be
    excluded with that reason.
    """
    # State desirability for tie-breaking (higher = more engageable).
    rank = {STATE_CONFIRMED: 3, STATE_COASTING: 2, STATE_TENTATIVE: 1}
    index: Dict[str, Dict[str, Any]] = {}
    for t in tracks:
        state = t.get("state")
        if state == STATE_DROPPED:
            continue
        for det_id in t.get("detection_ids") or []:
            cur = index.get(det_id)
            if cur is None:
                index[det_id] = t
                continue
            # keep the more-engageable / more-recent track
            key_new = (rank.get(state, 0), t.get("last_updated") or "")
            key_cur = (rank.get(cur.get("state"), 0), cur.get("last_updated") or "")
            if key_new > key_cur:
                index[det_id] = t
    return index


def _controller_detection_ids(clusters: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Map controller-candidate detection_id -> {swarm_id, controller_meta}.

    Only a single, non-tied controller candidate (candidate_detection_id set)
    is treated as a controller node for prioritisation. A TIED candidate set
    (tied_candidates) is deliberately NOT collapsed into a single "the
    controller" pick here -- ambiguity is surfaced, not guessed away.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for c in clusters:
        cand = c.get("controller_candidate")
        if not cand:
            continue
        det_id = cand.get("candidate_detection_id")
        if not det_id:
            continue  # tied / no single candidate -> not a prioritisation pick
        out[det_id] = {"swarm_id": c.get("swarm_id"), "controller": cand}
    return out


def _swarm_membership(clusters: List[Dict[str, Any]]) -> Dict[str, str]:
    """Map member detection_id -> swarm_id."""
    out: Dict[str, str] = {}
    for c in clusters:
        sid = c.get("swarm_id")
        for mid in c.get("member_ids") or []:
            out[mid] = sid
    return out


def build_engagement_plan(
    detections: List[Dict[str, Any]],
    clusters: List[Dict[str, Any]],
    tracks: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Produce a ranked engagement PROPOSAL (see module docstring).

    Inputs are plain dicts (no I/O, fully unit-testable):
      * detections: current detection docs (id, callsign, status, threat_level,
        iff_verified, swarm_id, system_id/component_id, ...).
      * clusters: output of swarm_classifier.build_swarm_clusters(...).
      * tracks: output of TrackManager.live_tracks() serialised via to_dict().

    Returns a plan dict:
      {
        "generated_at", "proposal_disclaimer", "doctrine",
        "required_human_cleared_gates",
        "proposals": [ ...ranked proposal dicts... ],
        "excluded": [ {detection_id, callsign, reason} ... ],
        "summary": { counts },
      }
    NOTHING here transmits or mutates anything.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    track_index = _confirmed_track_index(tracks)
    controller_map = _controller_detection_ids(clusters)
    membership = _swarm_membership(clusters)

    proposals: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []

    for det in detections:
        det_id = det.get("id")
        callsign = det.get("callsign")
        if not det_id:
            continue

        # --- Hard exclusion 1: never engage an IFF-verified friendly. -------
        if _is_iff_friendly(det):
            excluded.append({
                "detection_id": det_id, "callsign": callsign,
                "reason": "IFF-verified friendly -- friendly-fire interlock; "
                          "never proposed as a target.",
                "threat_level": det.get("threat_level"),
            })
            continue

        # --- Hard exclusion 2: only currently-ACTIVE detections engageable. --
        if det.get("status") != ENGAGEABLE_STATUS:
            excluded.append({
                "detection_id": det_id, "callsign": callsign,
                "reason": f"detection status={det.get('status')!r} is not "
                          f"{ENGAGEABLE_STATUS!r} (already engaged/lost/awaiting "
                          "ack); not re-proposed.",
            })
            continue

        # --- Hard exclusion 3: CONFIRMED-over-TENTATIVE. Require a CONFIRMED
        #     track. Unconfirmed/tentative/coasting -> excluded with reason. ---
        track = track_index.get(det_id)
        if track is None:
            excluded.append({
                "detection_id": det_id, "callsign": callsign,
                "reason": "no track references this detection -- unconfirmed; "
                          "a contact must be a CONFIRMED track before it can be "
                          "proposed for engagement.",
            })
            continue
        if track.get("state") != STATE_CONFIRMED:
            excluded.append({
                "detection_id": det_id, "callsign": callsign,
                "reason": f"associated track {track.get('track_id')} is "
                          f"{track.get('state')}, not CONFIRMED -- "
                          "coasting/tentative contacts are never proposed.",
                "track_id": track.get("track_id"),
                "track_state": track.get("state"),
            })
            continue

        # --- Eligible target: build a ranked PROPOSAL. ----------------------
        ctrl = controller_map.get(det_id)
        is_controller = ctrl is not None
        swarm_id = det.get("swarm_id") or membership.get(det_id) or (
            ctrl["swarm_id"] if ctrl else None
        )

        threat_w = _threat_weight(det.get("threat_level"))
        controller_bonus = CONTROLLER_PRIORITY_BONUS if is_controller else 0
        priority_score = threat_w + controller_bonus

        if is_controller:
            role = "SWARM_CONTROLLER_CANDIDATE"
        elif swarm_id:
            role = "SWARM_MEMBER"
        else:
            role = "STANDALONE"

        proposal = {
            "detection_id": det_id,
            "callsign": callsign,
            "threat_level": det.get("threat_level"),
            "swarm_id": swarm_id,
            "role": role,
            "is_controller_candidate": is_controller,
            "controller_candidate_confidence": (
                ctrl["controller"].get("confidence") if is_controller else None
            ),
            "controller_candidate_note": (
                ctrl["controller"].get("note") if is_controller else None
            ),
            "priority_score": priority_score,
            "score_breakdown": {
                "threat_weight": threat_w,
                "controller_first_bonus": controller_bonus,
            },
            "track_id": track.get("track_id"),
            "track_state": track.get("state"),
            # Single-target execution path (the safe default). Broadcast PL-010
            # is offered only as an explicitly commander-chosen option for a
            # controller node, and it carries its own friendly-in-earshot risk.
            "recommended_execution_path": (
                "POST /api/payloads/deploy with target_detection_id=<this id> "
                "(single-target). Commander may alternatively choose the "
                "swarm-wide broadcast PL-010 via POST /api/mavlink/broadcast, "
                "but that strikes every MAVLink drone in RF earshot including "
                "any friendly, so single-target is the recommended default."
            ),
            "recommended_payload_hint": (
                "controller-node-first: engage this coordinator, then RE-ASSESS "
                "(recompute the plan) to see if the swarm collapses before "
                "engaging members." if is_controller else
                "single-target takedown per operator selection from GET /api/payloads."
            ),
            "required_human_cleared_gates": list(REQUIRED_HUMAN_CLEARED_GATES),
            "status": PROPOSED_STATUS,
            "rationale": _rationale(det, role, is_controller, ctrl, threat_w),
        }
        proposals.append(proposal)

    # Deterministic ranking: highest priority first; tie-break by callsign then
    # detection_id so the ordering is stable across recomputes/tests.
    proposals.sort(key=lambda p: (
        -p["priority_score"],
        p.get("callsign") or "",
        p["detection_id"],
    ))

    # Assign 1-based rank and sequencing. Controller-candidate proposals are
    # step 1 for their swarm; other members of that same swarm are marked as
    # deferred pending re-assessment after the controller is neutralised.
    controller_by_swarm: Dict[str, str] = {
        p["swarm_id"]: p["detection_id"]
        for p in proposals
        if p["is_controller_candidate"] and p["swarm_id"]
    }
    for i, p in enumerate(proposals, start=1):
        p["rank"] = i
        sid = p["swarm_id"]
        if p["is_controller_candidate"]:
            p["sequence_step"] = 1
            p["defer_pending_reassessment_of"] = None
        elif sid and sid in controller_by_swarm:
            p["sequence_step"] = 2
            p["defer_pending_reassessment_of"] = controller_by_swarm[sid]
            p["deferred_note"] = (
                "Do NOT engage until the swarm controller candidate for "
                f"{sid} ({controller_by_swarm[sid]}) has been engaged and the "
                "plan RE-COMPUTED to confirm this member is still an active, "
                "confirmed, non-friendly threat."
            )
        else:
            p["sequence_step"] = 1
            p["defer_pending_reassessment_of"] = None

    summary = {
        "proposal_count": len(proposals),
        "excluded_count": len(excluded),
        "controller_candidate_count": sum(
            1 for p in proposals if p["is_controller_candidate"]
        ),
        "swarms_with_controller_first_sequencing": len(controller_by_swarm),
        "detections_considered": len(detections),
        "clusters_considered": len(clusters),
        "confirmed_tracks_considered": sum(
            1 for t in tracks if t.get("state") == STATE_CONFIRMED
        ),
    }

    return {
        "generated_at": now.isoformat(),
        "proposal_disclaimer": PROPOSAL_DISCLAIMER,
        "doctrine": DOCTRINE_NOTE,
        "required_human_cleared_gates": list(REQUIRED_HUMAN_CLEARED_GATES),
        "proposals": proposals,
        "excluded": excluded,
        "summary": summary,
    }


def _rationale(det: Dict[str, Any], role: str, is_controller: bool,
               ctrl: Optional[Dict[str, Any]], threat_w: int) -> str:
    parts: List[str] = []
    if is_controller:
        conf = ctrl["controller"].get("confidence") if ctrl else "low"
        parts.append(
            f"Ranked FIRST as swarm-controller CANDIDATE ({conf} confidence "
            "timing heuristic) per SOL-02: neutralising the coordinator may "
            "collapse the swarm. Engage, then re-assess before members."
        )
    elif role == "SWARM_MEMBER":
        parts.append(
            "Swarm member; deferred behind its controller candidate per "
            "controller-node-first doctrine."
        )
    else:
        parts.append("Standalone confirmed hostile track.")
    parts.append(
        f"Threat={det.get('threat_level')} (weight {threat_w}); backed by a "
        "CONFIRMED track. PROPOSAL ONLY -- requires human commander to clear "
        "the full arm/TX-halt/IFF/range gate chain to engage."
    )
    return " ".join(parts)
