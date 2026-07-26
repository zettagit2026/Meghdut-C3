"""Unit tests for swarm classification (Task #122) -- backend/
swarm_classifier.py. True unit tests: pure functions over plain dicts, no
live server/Mongo/requests, following the same pattern as
test_gnss_spoof_geodesic.py.

Run: pytest backend/tests/test_swarm_classifier.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from swarm_classifier import (  # noqa: E402
    CONTROLLER_SYNC_TOLERANCE_S,
    DEFAULT_MIN_CLUSTER_SIZE,
    PER_DRONE_TYPE_GAP,
    SWARM_TAXONOMY,
    build_swarm_clusters,
    classify_cluster,
    identify_controller_candidate,
)

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _iso(offset_s: float) -> str:
    return (NOW + timedelta(seconds=offset_s)).isoformat()


def _detection(det_id: str, first_s: float, last_s: float, *,
                status: str = "ACTIVE", protocol: str = "MAVLink",
                model: str = "Unknown UAV",
                reconfirm_offsets: list | None = None) -> dict:
    return {
        "id": det_id,
        "status": status,
        "first_seen": _iso(first_s),
        "last_seen": _iso(last_s),
        "protocol": protocol,
        "model": model,
        "reconfirm_events": [_iso(o) for o in (reconfirm_offsets or [first_s, last_s])],
    }


# ---------------------------------------------------------------------
# Taxonomy content -- must match the Army roadmap doc verbatim.
# ---------------------------------------------------------------------
class TestTaxonomyContent:
    def test_four_types_present(self):
        assert set(SWARM_TAXONOMY.keys()) == {"Type-I", "Type-II", "Type-III", "Type-IV"}

    def test_labels_match_source_document(self):
        assert SWARM_TAXONOMY["Type-I"]["label"] == "Fixed-wing FPV kamikaze"
        assert SWARM_TAXONOMY["Type-II"]["label"] == "Multirotor surveillance"
        assert SWARM_TAXONOMY["Type-III"]["label"] == "Loitering munition"
        assert SWARM_TAXONOMY["Type-IV"]["label"] == "Co-ordinated attack swarm with mesh C2"


# ---------------------------------------------------------------------
# build_swarm_clusters: temporal grouping
# ---------------------------------------------------------------------
class TestBuildSwarmClusters:
    def test_single_detection_never_forms_a_cluster(self):
        dets = [_detection("d1", 0, 5)]
        clusters = build_swarm_clusters(dets)
        assert clusters == []

    def test_two_concurrent_detections_form_a_cluster(self):
        dets = [_detection("d1", 0, 10), _detection("d2", 2, 12)]
        clusters = build_swarm_clusters(dets)
        assert len(clusters) == 1
        assert set(clusters[0]["member_ids"]) == {"d1", "d2"}
        assert clusters[0]["member_count"] == 2

    def test_two_far_apart_detections_do_not_cluster(self):
        # 500s apart, way outside the default 20s temporal window.
        dets = [_detection("d1", 0, 5), _detection("d2", 500, 505)]
        clusters = build_swarm_clusters(dets)
        assert clusters == []

    def test_swarm_id_is_deterministic_across_recomputation(self):
        dets = [_detection("d1", 0, 10), _detection("d2", 2, 12)]
        c1 = build_swarm_clusters(dets)
        c2 = build_swarm_clusters(list(reversed(dets)))
        assert c1[0]["swarm_id"] == c2[0]["swarm_id"]

    def test_three_member_cluster_via_chained_overlap(self):
        # d1-d2 overlap, d2-d3 overlap, d1-d3 do not directly overlap --
        # union-find should still merge all three via d2.
        dets = [
            _detection("d1", 0, 10),
            _detection("d2", 8, 25),
            _detection("d3", 23, 40),
        ]
        clusters = build_swarm_clusters(dets)
        assert len(clusters) == 1
        assert set(clusters[0]["member_ids"]) == {"d1", "d2", "d3"}

    def test_lost_detections_excluded_by_default(self):
        dets = [
            _detection("d1", 0, 10, status="ACTIVE"),
            _detection("d2", 2, 12, status="LOST"),
        ]
        clusters = build_swarm_clusters(dets)
        assert clusters == []

    def test_detection_missing_timestamps_is_skipped_not_crashed(self):
        dets = [
            {"id": "d1", "status": "ACTIVE", "first_seen": None, "last_seen": None,
             "protocol": "X", "model": "Y", "reconfirm_events": []},
            _detection("d2", 0, 10),
        ]
        clusters = build_swarm_clusters(dets)
        assert clusters == []

    def test_two_independent_clusters(self):
        dets = [
            _detection("a1", 0, 5), _detection("a2", 1, 6),
            _detection("b1", 1000, 1005), _detection("b2", 1001, 1006),
        ]
        clusters = build_swarm_clusters(dets)
        assert len(clusters) == 2
        ids = {frozenset(c["member_ids"]) for c in clusters}
        assert ids == {frozenset({"a1", "a2"}), frozenset({"b1", "b2"})}


# ---------------------------------------------------------------------
# classify_cluster: taxonomy assignment honesty
# ---------------------------------------------------------------------
class TestClassifyCluster:
    def test_multi_member_cluster_gets_type_iv_candidate(self):
        cluster = [_detection("d1", 0, 10), _detection("d2", 2, 12)]
        result = classify_cluster(cluster)
        assert result["taxonomy_type"] == "Type-IV"
        assert result["taxonomy_confidence"] == "candidate"
        assert result["taxonomy_label"] == SWARM_TAXONOMY["Type-IV"]["label"]

    def test_never_assigns_type_i_ii_iii(self):
        """Regression guard: this module must never fabricate a per-drone
        Type-I/II/III assignment -- that would require a flight-behaviour
        classifier this codebase does not have."""
        cluster = [_detection("d1", 0, 10), _detection("d2", 2, 12)]
        result = classify_cluster(cluster)
        assert result["per_drone_type"] is None
        assert result["per_drone_type_gap"] == PER_DRONE_TYPE_GAP

    def test_heterogeneous_protocol_flagged(self):
        cluster = [
            _detection("d1", 0, 10, protocol="MAVLink"),
            _detection("d2", 2, 12, protocol="DJI OcuSync"),
        ]
        result = classify_cluster(cluster)
        assert result["heterogeneous"] is True
        assert result["protocols"] == ["DJI OcuSync", "MAVLink"]

    def test_homogeneous_protocol_not_flagged(self):
        cluster = [
            _detection("d1", 0, 10, protocol="MAVLink", model="X"),
            _detection("d2", 2, 12, protocol="MAVLink", model="X"),
        ]
        result = classify_cluster(cluster)
        assert result["heterogeneous"] is False


# ---------------------------------------------------------------------
# identify_controller_candidate: timing-correlation heuristic
# ---------------------------------------------------------------------
class TestControllerCandidate:
    def test_returns_none_with_insufficient_history(self):
        cluster = [
            _detection("d1", 0, 10, reconfirm_offsets=[0]),
            _detection("d2", 2, 12, reconfirm_offsets=[2]),
        ]
        assert identify_controller_candidate(cluster) is None

    def test_returns_none_when_no_synchronization_observed(self):
        # Plenty of events each, but never within CONTROLLER_SYNC_TOLERANCE_S
        # of one another.
        cluster = [
            _detection("d1", 0, 100, reconfirm_offsets=[0, 10, 20]),
            _detection("d2", 0, 100, reconfirm_offsets=[5, 15, 25]),
        ]
        assert identify_controller_candidate(cluster) is None

    def test_identifies_highest_out_degree_node(self):
        # d_hub's events are synchronized (within tolerance) with BOTH d1 and
        # d2's events; d1 and d2 are not synchronized with each other.
        tol = CONTROLLER_SYNC_TOLERANCE_S
        cluster = [
            _detection("d_hub", 0, 60, reconfirm_offsets=[0, 20, 40]),
            _detection("d1", 0, 60, reconfirm_offsets=[0 + tol / 2, 100, 200]),
            _detection("d2", 0, 60, reconfirm_offsets=[20 + tol / 2, 300, 400]),
        ]
        candidate = identify_controller_candidate(cluster)
        assert candidate is not None
        assert candidate["candidate_detection_id"] == "d_hub"
        assert candidate["out_degree"] == 2
        assert candidate["confidence"] == "low"
        assert "NOT a verified" in candidate["note"]

    def test_never_claims_high_confidence(self):
        """Regression guard: controller identification must always be
        surfaced as a low-confidence heuristic candidate, never a verified
        claim -- this system has no DoA/MAC-OUI/hop-sequence data to
        actually verify a controller."""
        tol = CONTROLLER_SYNC_TOLERANCE_S
        cluster = [
            _detection("d_hub", 0, 60, reconfirm_offsets=[0, 20]),
            _detection("d1", 0, 60, reconfirm_offsets=[tol / 2, 100]),
        ]
        candidate = identify_controller_candidate(cluster)
        assert candidate["confidence"] == "low"
        assert candidate["method"] == "heuristic_candidate"

    def test_tie_reports_no_single_candidate(self):
        tol = CONTROLLER_SYNC_TOLERANCE_S
        cluster = [
            _detection("d1", 0, 60, reconfirm_offsets=[0, 500]),
            _detection("d2", 0, 60, reconfirm_offsets=[tol / 2, 600]),
            _detection("d3", 0, 60, reconfirm_offsets=[1000, 1100]),
        ]
        candidate = identify_controller_candidate(cluster)
        # d1 and d2 are mutually synchronized (out_degree=1 each); d3 has
        # out_degree=0. Top score is tied between d1 and d2.
        assert candidate is not None
        assert candidate["candidate_detection_id"] is None
        assert set(candidate["tied_candidates"]) == {"d1", "d2"}


# ---------------------------------------------------------------------
# End-to-end: build_swarm_clusters wires classify_cluster + controller
# candidate together for a realistic multi-drone scenario.
# ---------------------------------------------------------------------
class TestEndToEnd:
    def test_realistic_swarm_scenario(self):
        tol = CONTROLLER_SYNC_TOLERANCE_S
        dets = [
            _detection("hub", 0, 60, protocol="MAVLink", model="FPV Racer",
                       reconfirm_offsets=[0, 20, 40]),
            _detection("member1", 0, 60, protocol="ExpressLRS", model="FPV Racer",
                       reconfirm_offsets=[tol / 2, 200]),
            _detection("member2", 0, 60, protocol="DJI OcuSync", model="DJI Mini",
                       reconfirm_offsets=[20 + tol / 2, 300]),
            # unrelated lone contact, far away in time -- must not join.
            _detection("lone", 10_000, 10_005),
        ]
        clusters = build_swarm_clusters(dets)
        assert len(clusters) == 1
        cluster = clusters[0]
        assert cluster["member_count"] == 3
        assert cluster["taxonomy_type"] == "Type-IV"
        assert cluster["heterogeneous"] is True
        assert cluster["controller_candidate"]["candidate_detection_id"] == "hub"
        assert DEFAULT_MIN_CLUSTER_SIZE == 2  # sanity on the module constant used above
