"""Swarm classification (task #122).

TAXONOMY SOURCE: the Army's actual Type-I..IV swarm taxonomy was located in
"CEMA_cUAS_Indigenous_Implementation_Roadmap_Zettawise.docx" (folder:
/Users/biswajit/Desktop/Zettawise/PMO Suraj/ -- one level above this repo),
section "4. SOL-02 | Anti-Swarm Architecture", step 1 ("Swarm taxonomy and
threat model"):

    Type-I:   fixed-wing FPV kamikaze
    Type-II:  multirotor surveillance
    Type-III: loitering munition
    Type-IV:  co-ordinated attack swarm with mesh C2

The companion "CEMA_cUAS_Requirements_Gap_Analysis_Zettawise.docx" (OB-02,
"Swarm Attack Counter-Strategy") independently confirms this is the real,
Army-relevant threat framing ("no swarm taxonomy, no swarm coordination
detection logic ... described anywhere in the presented material" was
flagged there as a CRITICAL gap) and additionally documents the two
concrete techniques this module implements:
  - grouping simultaneous RF detections into swarm candidates via
    "signal clustering ... based on frequency proximity, timing
    correlation, and direction-of-arrival (DoA) bearing" (roadmap doc,
    SOL-02 step 2), and
  - "a timing-correlation analyser that identifies the RF source with the
    highest out-degree of correlation to other swarm members" as a
    swarm-controller candidate (roadmap doc, SOL-02 step 4; gap-analysis
    doc, "Mesh Disruption via Swarm Controller Targeting").

IMPORTANT HONESTY SCOPING (do not remove/soften without re-reading
backend/server.py's own honesty comments around bearing_deg/
bearing_available):

  * Direction-of-arrival (DoA)/bearing is NOT usable here. `bearing_deg` on
    a detection defaults to 0.0 placeholder from every current field-bridge
    ingest source (hackrf_rx.py, ml_classify_bridge.py, thermal_bridge.py,
    iff_beacon_bridge.py); GET /api/sensor/position's `bearing_available`
    flag is hardcoded False system-wide (server.py ~line 1259). Even
    field-bridge/passive_radar_bridge.py's bearing is boresight-only, not a
    verified per-drone bearing, and is not distinguished from the
    placeholder at the stored-detection level. So spatial/DoA-based
    clustering, which the roadmap doc lists as an available signal, is
    NOT implemented -- it would be fabricating precision this system does
    not have. Only TEMPORAL co-occurrence of concurrent ACTIVE detections
    is used to form swarm candidate clusters.

  * Per-drone Type-I/II/III sub-classification (fixed-wing kamikaze vs.
    multirotor surveillance vs. loitering munition) requires a validated
    flight-behaviour/airframe classifier (sustained trajectory shape,
    airframe RF fingerprint, etc.). No such classifier exists in this
    codebase today (model/protocol fields are RF-heuristic/ML guesses --
    see DetectionIngestBody's confidence_type -- not airframe-behaviour
    classifications). This module deliberately does NOT attempt Type-I/
    II/III assignment; it reports `per_drone_type: None` with an explicit
    `per_drone_type_gap` explanation rather than guessing. Only Type-IV
    ("co-ordinated attack swarm with mesh C2") is assigned, and only as a
    labelled CANDIDATE (never "confirmed"), because concurrent-activity
    clustering is a real, honestly-available signal for it.

  * Controller/operator identification: also implemented ONLY as a
    labelled, low-confidence CANDIDATE using the timing-correlation
    heuristic described in the roadmap/gap-analysis docs above (out-degree
    of synchronized reconfirm-event timing across cluster members). This
    is NOT a verified RF-identified controller -- there is no MAC OUI
    field, no hop-sequence/PN-seed field, and no verified per-drone DoA
    bearing anywhere in this system's ingest schema to ground a stronger
    claim. See `identify_controller_candidate()` docstring for exactly
    what additional hardware/data (coherent multi-antenna DoA array per
    SOL-05, RF hop-sequence capture, or protocol-specific ID extraction
    e.g. DJI DroneID) would be needed to upgrade this from a heuristic
    candidate to a verified identification.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Dict, List, Optional

# --- Taxonomy (verbatim from the Army roadmap doc; do not paraphrase away
# the source wording -- see module docstring for the exact source file). ---
SWARM_TAXONOMY: Dict[str, Dict[str, str]] = {
    "Type-I": {
        "label": "Fixed-wing FPV kamikaze",
        "source": "CEMA_cUAS_Indigenous_Implementation_Roadmap_Zettawise.docx, SOL-02 step 1",
    },
    "Type-II": {
        "label": "Multirotor surveillance",
        "source": "CEMA_cUAS_Indigenous_Implementation_Roadmap_Zettawise.docx, SOL-02 step 1",
    },
    "Type-III": {
        "label": "Loitering munition",
        "source": "CEMA_cUAS_Indigenous_Implementation_Roadmap_Zettawise.docx, SOL-02 step 1",
    },
    "Type-IV": {
        "label": "Co-ordinated attack swarm with mesh C2",
        "source": "CEMA_cUAS_Indigenous_Implementation_Roadmap_Zettawise.docx, SOL-02 step 1",
    },
}

PER_DRONE_TYPE_GAP = (
    "Type-I/II/III sub-classification requires a validated flight-behaviour "
    "or airframe RF-fingerprint classifier that does not exist in this "
    "system yet (model/protocol are RF-heuristic/ML guesses, not airframe-"
    "behaviour classifications). Not attempted here rather than guessed."
)

# Two concurrent ACTIVE detections are considered part of the same
# potential swarm window if their [first_seen, last_seen] observation spans
# overlap once each is padded by this many seconds. Deliberately reuses the
# same order of magnitude as DETECTION_MERGE_WINDOW_S (backend/server.py) --
# this is a "were these two contacts active at the same time" check, not a
# claim about physical proximity.
DEFAULT_TEMPORAL_WINDOW_S = 20.0

# Minimum number of concurrent detections required before we are willing to
# call something a swarm candidate at all.
DEFAULT_MIN_CLUSTER_SIZE = 2

# Two reconfirm events (one from each of two different cluster members) are
# considered "synchronized" -- i.e. evidence of shared/relayed timing -- if
# they land within this many seconds of each other.
CONTROLLER_SYNC_TOLERANCE_S = 2.0

# A controller candidate is only reported if at least this many members have
# enough reconfirm history to compute a meaningful synchronization score.
CONTROLLER_MIN_QUALIFYING_MEMBERS = 2
CONTROLLER_MIN_EVENTS_PER_MEMBER = 2


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _windows_overlap(a_start: datetime, a_end: datetime,
                      b_start: datetime, b_end: datetime,
                      pad_s: float) -> bool:
    """True if [a_start,a_end] and [b_start,b_end] overlap once each is
    padded by pad_s seconds on both ends."""
    from datetime import timedelta
    pad = timedelta(seconds=pad_s)
    return (a_start - pad) <= (b_end + pad) and (b_start - pad) <= (a_end + pad)


def _swarm_id_for(member_ids: List[str]) -> str:
    """Deterministic swarm_id derived from the sorted member detection ids,
    so recomputing clusters (e.g. on the next poll cycle) with the same
    membership yields the SAME swarm_id rather than a fresh random one each
    time -- important for the frontend's "Swarms Detected" unique-count
    stat (frontend/src/pages/Dashboard.jsx) and per-detection badges
    (KillChain.jsx) to stay stable across polls."""
    digest = hashlib.sha1("|".join(sorted(member_ids)).encode("utf-8")).hexdigest()
    return f"SWARM-{digest[:8].upper()}"


def _union_find_clusters(detections: List[Dict[str, Any]],
                          temporal_window_s: float) -> List[List[Dict[str, Any]]]:
    n = len(detections)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    spans: List[Optional[tuple]] = []
    for d in detections:
        start = _parse_ts(d.get("first_seen"))
        end = _parse_ts(d.get("last_seen")) or start
        spans.append((start, end) if start and end else None)

    for i in range(n):
        if spans[i] is None:
            continue
        for j in range(i + 1, n):
            if spans[j] is None:
                continue
            if _windows_overlap(spans[i][0], spans[i][1],
                                 spans[j][0], spans[j][1],
                                 temporal_window_s):
                union(i, j)

    groups: Dict[int, List[Dict[str, Any]]] = {}
    for i, d in enumerate(detections):
        if spans[i] is None:
            continue  # can't place an undated detection into any cluster
        groups.setdefault(find(i), []).append(d)
    return list(groups.values())


def identify_controller_candidate(cluster_detections: List[Dict[str, Any]]
                                   ) -> Optional[Dict[str, Any]]:
    """Timing-correlation controller-candidate heuristic (see module
    docstring for the doc citation and honesty caveats).

    For each member with >= CONTROLLER_MIN_EVENTS_PER_MEMBER reconfirm
    events, compute an "out-degree": the number of OTHER qualifying members
    that have at least one reconfirm_events timestamp within
    CONTROLLER_SYNC_TOLERANCE_S seconds of one of THIS member's timestamps.
    The roadmap doc's stated heuristic is "the RF source with the highest
    out-degree of correlation to other swarm members" -- a node whose
    activity is tightly time-synchronized with everyone else's is a
    plausible coordination beacon/master node.

    Returns None (not a guess) if fewer than CONTROLLER_MIN_QUALIFYING_MEMBERS
    members have enough reconfirm history to compute this, or if the top
    out-degree is 0 (no synchronization observed at all).

    This is explicitly NOT a verified controller identification -- it does
    not use RF direction-finding, MAC OUI, or hop-sequence data (none of
    which exist in this system's ingest schema). Upgrading this to a
    verified identification would require, at minimum, one of:
      - a coherent multi-antenna DoA array (SOL-05) giving a real per-drone
        bearing, so a common originating bearing could corroborate this;
      - capture of a shared RF hop-sequence/PN seed across members;
      - protocol-specific ID extraction (e.g. DJI DroneID serial/operator
        location fields) where the protocol is actually decoded.
    None of those exist today, so the result is always labelled
    "confidence": "low" / "heuristic_candidate".
    """
    qualifying = [
        d for d in cluster_detections
        if len(d.get("reconfirm_events") or []) >= CONTROLLER_MIN_EVENTS_PER_MEMBER
        and d.get("id")
    ]
    if len(qualifying) < CONTROLLER_MIN_QUALIFYING_MEMBERS:
        return None

    events_by_id: Dict[str, List[datetime]] = {}
    for d in qualifying:
        parsed = [t for t in (_parse_ts(ts) for ts in d["reconfirm_events"]) if t]
        events_by_id[d["id"]] = parsed

    out_degree: Dict[str, int] = {}
    for d in qualifying:
        my_id = d["id"]
        my_events = events_by_id[my_id]
        synced_with = 0
        for other in qualifying:
            other_id = other["id"]
            if other_id == my_id:
                continue
            other_events = events_by_id[other_id]
            if any(abs((a - b).total_seconds()) <= CONTROLLER_SYNC_TOLERANCE_S
                   for a in my_events for b in other_events):
                synced_with += 1
        out_degree[my_id] = synced_with

    top_id = max(out_degree, key=out_degree.get)
    top_score = out_degree[top_id]
    if top_score <= 0:
        return None

    tied = [mid for mid, score in out_degree.items() if score == top_score]
    return {
        "candidate_detection_id": top_id if len(tied) == 1 else None,
        "tied_candidates": tied if len(tied) > 1 else None,
        "out_degree": top_score,
        "qualifying_member_count": len(qualifying),
        "confidence": "low",
        "method": "heuristic_candidate",
        "basis": (
            "Timing-correlation heuristic (out-degree of synchronized "
            "reconfirm-event timestamps within "
            f"{CONTROLLER_SYNC_TOLERANCE_S}s across cluster members)."
        ),
        "note": (
            "NOT a verified RF-identified controller. No DoA bearing, MAC "
            "OUI, or hop-sequence data exists in this system's ingest "
            "schema to corroborate this. Treat as a prioritisation hint "
            "only, per the roadmap doc's 'prioritise controller "
            "destruction/jamming first' guidance -- not as grounds for "
            "kinetic/RF action on its own."
        ),
    }


def classify_cluster(cluster_detections: List[Dict[str, Any]]) -> Dict[str, Any]:
    member_ids = [d["id"] for d in cluster_detections if d.get("id")]
    protocols = sorted({d.get("protocol", "Unknown") for d in cluster_detections})
    models = sorted({d.get("model", "Unknown UAV") for d in cluster_detections})
    heterogeneous = len(protocols) > 1 or len(models) > 1

    is_swarm = len(member_ids) >= DEFAULT_MIN_CLUSTER_SIZE
    taxonomy_type = "Type-IV" if is_swarm else None

    return {
        "swarm_id": _swarm_id_for(member_ids) if is_swarm else None,
        "member_ids": member_ids,
        "member_count": len(member_ids),
        "protocols": protocols,
        "models": models,
        "heterogeneous": heterogeneous,
        "taxonomy_type": taxonomy_type,
        "taxonomy_label": SWARM_TAXONOMY["Type-IV"]["label"] if is_swarm else None,
        "taxonomy_confidence": "candidate" if is_swarm else None,
        "taxonomy_basis": (
            "Temporal co-occurrence of >=2 concurrent ACTIVE detections "
            "within the correlation window (no DoA/bearing signal used -- "
            "see module docstring)."
        ) if is_swarm else None,
        "per_drone_type": None,
        "per_drone_type_gap": PER_DRONE_TYPE_GAP,
        "controller_candidate": identify_controller_candidate(cluster_detections) if is_swarm else None,
    }


def build_swarm_clusters(detections: List[Dict[str, Any]],
                          *,
                          temporal_window_s: float = DEFAULT_TEMPORAL_WINDOW_S,
                          min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
                          active_only: bool = True) -> List[Dict[str, Any]]:
    """Group detections into swarm candidate clusters and classify each.

    Only ACTIVE detections are considered by default (active_only=True) --
    a swarm is a concurrent-activity phenomenon, so LOST/stale detections
    shouldn't be pulled into a "currently coordinated" cluster. Detections
    missing first_seen/last_seen are skipped (cannot be temporally placed).

    Returns one dict per cluster whose member_count >= min_cluster_size
    (i.e. singleton detections, which are not swarms, are NOT included in
    the returned list -- callers wanting per-detection swarm_id=None for
    non-clustered detections should treat "not present in this list" as
    exactly that).
    """
    pool = [d for d in detections if not active_only or d.get("status") == "ACTIVE"]
    raw_clusters = _union_find_clusters(pool, temporal_window_s)
    results = []
    for cluster in raw_clusters:
        if len(cluster) < min_cluster_size:
            continue
        results.append(classify_cluster(cluster))
    # Stable ordering for deterministic API responses/tests.
    results.sort(key=lambda c: c["swarm_id"] or "")
    return results
