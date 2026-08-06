"""Unit tests for the prioritized engagement PLANNER (OB-02 / SOL-02) --
backend/engagement_planner.py.

True unit tests: pure functions over plain dicts, no live server/Mongo/
requests, following the same pattern as test_swarm_classifier.py. These prove
the SAFETY-CRITICAL properties an adversarial review will scrutinise:

  * controller-node-first ranking (SOL-02),
  * IFF-verified friendlies are NEVER proposed (excluded with reason),
  * unconfirmed / tentative / coasting tracks are NEVER proposed,
  * threat ordering among eligible targets,
  * the plan is INERT: every proposal is a PROPOSAL requiring human auth, and
    the module exposes no transmit/engage capability at all.

Run: pytest backend/tests/test_engagement_planner.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engagement_planner import (  # noqa: E402
    CONTROLLER_PRIORITY_BONUS,
    IFF_FRIENDLY_THREAT,
    PROPOSED_STATUS,
    build_engagement_plan,
)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
def _det(det_id, *, callsign=None, status="ACTIVE", threat="HIGH",
         iff_verified=False, swarm_id=None, system_id=1):
    return {
        "id": det_id,
        "callsign": callsign or det_id,
        "status": status,
        "threat_level": threat,
        "iff_verified": iff_verified,
        "swarm_id": swarm_id,
        "system_id": system_id,
        "component_id": 1,
    }


def _track(track_id, det_ids, *, state="CONFIRMED", last_updated="2026-08-06T12:00:00+00:00"):
    return {
        "track_id": track_id,
        "state": state,
        "detection_ids": list(det_ids),
        "last_updated": last_updated,
    }


def _cluster(swarm_id, member_ids, *, controller_det_id=None, confidence="low"):
    controller = None
    if controller_det_id is not None:
        controller = {
            "candidate_detection_id": controller_det_id,
            "tied_candidates": None,
            "confidence": confidence,
            "method": "heuristic_candidate",
            "note": "NOT a verified RF-identified controller.",
        }
    return {
        "swarm_id": swarm_id,
        "member_ids": list(member_ids),
        "controller_candidate": controller,
    }


def _ids(plan):
    return [p["detection_id"] for p in plan["proposals"]]


def _excluded_reason(plan, det_id):
    for e in plan["excluded"]:
        if e["detection_id"] == det_id:
            return e["reason"]
    return None


# --------------------------------------------------------------------------
# Controller-node-first (SOL-02)
# --------------------------------------------------------------------------
def test_controller_node_ranked_first():
    """Even a MEDIUM-threat controller candidate outranks a CRITICAL member."""
    dets = [
        _det("D-CTRL", threat="MEDIUM", swarm_id="SWARM-1"),
        _det("D-MEM", threat="CRITICAL", swarm_id="SWARM-1"),
    ]
    tracks = [_track("T1", ["D-CTRL"]), _track("T2", ["D-MEM"])]
    clusters = [_cluster("SWARM-1", ["D-CTRL", "D-MEM"], controller_det_id="D-CTRL")]

    plan = build_engagement_plan(dets, clusters, tracks)

    assert _ids(plan)[0] == "D-CTRL"
    top = plan["proposals"][0]
    assert top["is_controller_candidate"] is True
    assert top["rank"] == 1
    assert top["sequence_step"] == 1
    assert top["score_breakdown"]["controller_first_bonus"] == CONTROLLER_PRIORITY_BONUS


def test_member_deferred_pending_reassessment():
    dets = [
        _det("D-CTRL", threat="HIGH", swarm_id="SWARM-1"),
        _det("D-MEM", threat="HIGH", swarm_id="SWARM-1"),
    ]
    tracks = [_track("T1", ["D-CTRL"]), _track("T2", ["D-MEM"])]
    clusters = [_cluster("SWARM-1", ["D-CTRL", "D-MEM"], controller_det_id="D-CTRL")]

    plan = build_engagement_plan(dets, clusters, tracks)
    member = next(p for p in plan["proposals"] if p["detection_id"] == "D-MEM")
    assert member["sequence_step"] == 2
    assert member["defer_pending_reassessment_of"] == "D-CTRL"
    assert "deferred_note" in member


# --------------------------------------------------------------------------
# IFF friendly exclusion (never engage a friendly)
# --------------------------------------------------------------------------
def test_iff_verified_friendly_excluded_via_flag():
    dets = [_det("D-FRIEND", iff_verified=True, threat="HIGH")]
    tracks = [_track("T1", ["D-FRIEND"])]
    plan = build_engagement_plan(dets, [], tracks)

    assert "D-FRIEND" not in _ids(plan)
    assert "friendly" in _excluded_reason(plan, "D-FRIEND").lower()


def test_iff_verified_friendly_excluded_via_threat_label():
    dets = [_det("D-FRIEND", iff_verified=False, threat=IFF_FRIENDLY_THREAT)]
    tracks = [_track("T1", ["D-FRIEND"])]
    plan = build_engagement_plan(dets, [], tracks)

    assert "D-FRIEND" not in _ids(plan)
    assert _excluded_reason(plan, "D-FRIEND") is not None


def test_friendly_controller_candidate_still_excluded():
    """A contact that is BOTH the controller candidate AND IFF-friendly must
    be excluded -- the friendly-fire interlock wins over controller-first."""
    dets = [
        _det("D-CTRL", iff_verified=True, threat="CRITICAL", swarm_id="SWARM-1"),
        _det("D-MEM", threat="HIGH", swarm_id="SWARM-1"),
    ]
    tracks = [_track("T1", ["D-CTRL"]), _track("T2", ["D-MEM"])]
    clusters = [_cluster("SWARM-1", ["D-CTRL", "D-MEM"], controller_det_id="D-CTRL")]

    plan = build_engagement_plan(dets, clusters, tracks)
    assert "D-CTRL" not in _ids(plan)
    assert _excluded_reason(plan, "D-CTRL") is not None


# --------------------------------------------------------------------------
# Confirmed-over-tentative / coasting / unconfirmed exclusion
# --------------------------------------------------------------------------
def test_tentative_track_excluded():
    dets = [_det("D1", threat="HIGH")]
    tracks = [_track("T1", ["D1"], state="TENTATIVE")]
    plan = build_engagement_plan(dets, [], tracks)
    assert "D1" not in _ids(plan)
    assert "not CONFIRMED" in _excluded_reason(plan, "D1")


def test_coasting_track_excluded():
    dets = [_det("D1", threat="HIGH")]
    tracks = [_track("T1", ["D1"], state="COASTING")]
    plan = build_engagement_plan(dets, [], tracks)
    assert "D1" not in _ids(plan)
    assert "not CONFIRMED" in _excluded_reason(plan, "D1")


def test_no_track_excluded_as_unconfirmed():
    dets = [_det("D1", threat="HIGH")]
    plan = build_engagement_plan(dets, [], [])
    assert "D1" not in _ids(plan)
    assert "unconfirmed" in _excluded_reason(plan, "D1").lower()


def test_non_active_status_excluded():
    dets = [_det("D1", threat="HIGH", status="AWAITING_ACK")]
    tracks = [_track("T1", ["D1"])]
    plan = build_engagement_plan(dets, [], tracks)
    assert "D1" not in _ids(plan)


def test_confirmed_track_preferred_over_tentative_dup():
    """If a detection is referenced by both a TENTATIVE and a CONFIRMED track,
    the CONFIRMED one wins and the detection is engageable."""
    dets = [_det("D1", threat="HIGH")]
    tracks = [
        _track("T-tent", ["D1"], state="TENTATIVE", last_updated="2026-08-06T12:00:05+00:00"),
        _track("T-conf", ["D1"], state="CONFIRMED", last_updated="2026-08-06T12:00:00+00:00"),
    ]
    plan = build_engagement_plan(dets, [], tracks)
    assert "D1" in _ids(plan)
    assert plan["proposals"][0]["track_id"] == "T-conf"


# --------------------------------------------------------------------------
# Threat ordering among eligible non-controller targets
# --------------------------------------------------------------------------
def test_threat_ordering():
    dets = [
        _det("D-low", threat="LOW"),
        _det("D-crit", threat="CRITICAL"),
        _det("D-med", threat="MEDIUM"),
        _det("D-high", threat="HIGH"),
    ]
    tracks = [_track(f"T{i}", [d["id"]]) for i, d in enumerate(dets)]
    plan = build_engagement_plan(dets, [], tracks)
    assert _ids(plan) == ["D-crit", "D-high", "D-med", "D-low"]


def test_ranking_is_deterministic():
    dets = [_det("D-b", threat="HIGH"), _det("D-a", threat="HIGH")]
    tracks = [_track("T1", ["D-b"]), _track("T2", ["D-a"])]
    p1 = build_engagement_plan(dets, [], tracks)
    p2 = build_engagement_plan(list(reversed(dets)), [], list(reversed(tracks)))
    assert _ids(p1) == _ids(p2)  # stable tie-break by callsign


# --------------------------------------------------------------------------
# The plan is INERT -- a PROPOSAL, never an action
# --------------------------------------------------------------------------
def test_every_proposal_is_marked_proposed_and_requires_gates():
    dets = [_det("D1", threat="CRITICAL")]
    tracks = [_track("T1", ["D1"])]
    plan = build_engagement_plan(dets, [], tracks)
    for p in plan["proposals"]:
        assert p["status"] == PROPOSED_STATUS
        assert p["required_human_cleared_gates"]  # non-empty
        gates = " ".join(p["required_human_cleared_gates"]).lower()
        assert "arm_token" in gates
        assert "tx_not_halted" in gates or "tx-not-halted" in gates
        assert "iff" in gates
        assert "commander_role" in gates


def test_plan_carries_disclaimer_and_no_engagement_language():
    dets = [_det("D1", threat="HIGH")]
    tracks = [_track("T1", ["D1"])]
    plan = build_engagement_plan(dets, [], tracks)
    assert "PROPOSAL" in plan["proposal_disclaimer"]
    assert "no engagement has occurred" in plan["proposal_disclaimer"].lower()
    # honesty: never phrases anything as engaged/auto-engaged
    blob = str(plan).lower()
    assert "auto-fire" not in blob
    assert "engaging now" not in blob


def test_planner_module_exposes_no_transmit_capability():
    """Structural guarantee: the planner imports nothing that can transmit /
    build a frame / touch the DB or WebSocket. If a future edit adds such an
    import, this test fails loudly."""
    import engagement_planner
    src = Path(engagement_planner.__file__).read_text()
    for forbidden in ("mavlink_codec", "broadcast_takedown", "ws_manager",
                      "payload_library", "db.detections", "PAYLOAD_BUILDERS"):
        # allowed only inside comments/docstrings referencing them by name;
        # assert they are never IMPORTED as executable dependencies.
        assert f"import {forbidden}" not in src
        assert f"from {forbidden}" not in src


def test_tied_controller_candidate_not_treated_as_controller():
    """A tied controller candidate (candidate_detection_id=None) must not
    promote any single member to controller-first -- ambiguity is surfaced,
    not guessed."""
    dets = [
        _det("D1", threat="HIGH", swarm_id="SWARM-1"),
        _det("D2", threat="HIGH", swarm_id="SWARM-1"),
    ]
    tracks = [_track("T1", ["D1"]), _track("T2", ["D2"])]
    cluster = _cluster("SWARM-1", ["D1", "D2"])
    cluster["controller_candidate"] = {
        "candidate_detection_id": None,
        "tied_candidates": ["D1", "D2"],
        "confidence": "low",
    }
    plan = build_engagement_plan(dets, [cluster], tracks)
    assert all(p["is_controller_candidate"] is False for p in plan["proposals"])
