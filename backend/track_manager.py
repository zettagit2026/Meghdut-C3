"""Multi-target track manager (Army objective OB-04).

WHAT THIS IS
------------
A LAYER on top of the existing detection ingest/merge/expiry flow in
backend/server.py. It does NOT replace that flow. `detections` remain exactly
what they were -- Mongo rows, one per (source + immutable match_model/
match_protocol) contact, merged within DETECTION_MERGE_WINDOW_S and expired to
LOST after DETECTION_STALE_TIMEOUT_S. This module adds the missing concept the
Army requirement (OB-04, "concurrent handling of many simultaneous targets",
spec >=16 scalable to 32, Army ask 40-50) actually needs and which did not
exist before: a persistent TRACK with a stable identity that survives across
many detection updates and has an explicit birth -> confirmation -> coast ->
death lifecycle.

WHY A SEPARATE LAYER (not more fields on `detections`)
------------------------------------------------------
The detection merge logic answers "is this ingest the same physical emitter I
already have a row for, within 20s?" -- an identity/dedup question keyed on
classification strings. A track answers a different question: "how long have I
been continuously tracking this contact, is it CONFIRMED enough to trust, and
have I lost it?" -- a lifecycle question. Bolting N-of-M confirmation and
coast/death state onto the (live, load-tested) detection upsert would entangle
two concerns; a track manager keeps them separate and independently testable.

RELATIONSHIP TO swarm_classifier.py
-----------------------------------
swarm_classifier.py answers "are several concurrent contacts a coordinated
swarm?" (temporal co-occurrence clustering ACROSS contacts). The track manager
answers "is THIS one contact a persistent, confirmed track OVER TIME?" They are
complementary and deliberately do not share logic: one clusters across space of
contacts, the other tracks a single contact's lifecycle through time.

ARCHITECTURE: in-memory index, Mongo mirror
-------------------------------------------
The live lifecycle logic runs over an in-memory dict of Track objects on the
single-worker asyncio loop (mirrors how the existing code keeps live state such
as _pending_acks / _arm_tokens in-process). Every mutation is mirrored to a
Mongo `tracks` collection so tracks persist for the mission log / audit trail
and survive a restart (server.py reloads non-DROPPED tracks into the index on
startup, the same "state lives in Mongo, survives reboot" property the
`detections` collection already has). The in-memory index is authoritative for
the hot path; Mongo is the durable mirror.

SCOPE BOUNDARIES (deliberate, documented)
-----------------------------------------
* Association is SINGLE-HYPOTHESIS gated nearest-neighbour, NOT JPDA/MHT. One
  detection associates to at most one track (the closest in-gate track); an
  unassociated detection births a new tentative track. Multiple-hypothesis
  tracking is explicitly out of scope for this platform's data quality (no
  real per-drone DoA bearing exists -- see server.py's bearing_deg honesty
  comments and swarm_classifier.py's DoA caveat).
* This is DEFENSIVE tracking infrastructure only. Nothing here engages,
  jams, or takes down anything. It maintains situational awareness.
* Bearing is carried through as a best-estimate field but is NOT used as an
  association gate, because bearing on this hardware is a coarse single-antenna
  guess / placeholder (same honesty scoping as swarm_classifier.py). Association
  gates on source + classification consistency + frequency proximity only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# =====================================================================
# Lifecycle parameters -- tuned to THIS platform's real ingest cadence.
#
# Real cadence (verified in field-bridge scripts referenced by server.py):
#   * hackrf_rx.py re-confirms the same contact roughly every ~3s (well inside
#     DETECTION_MERGE_WINDOW_S=20s).
#   * ml_classify_bridge.py posts a real ML label roughly every ~12s per band,
#     often slower under HackRF device-lock contention.
# So a healthy, continuously-present contact produces an associated observation
# every few seconds. The thresholds below are chosen against that reality.
# =====================================================================

# N-of-M confirmation. A TENTATIVE track is promoted to CONFIRMED once it has
# accumulated at least TRACK_CONFIRM_HITS associated observations within a
# sliding TRACK_CONFIRM_WINDOW_S window. At ~3s hackrf cadence, 3 hits within
# 15s means a contact must genuinely persist for ~2 re-confirmation cycles
# before we assert it as a confirmed track -- long enough to reject a single
# transient blip, short enough to confirm a real contact in well under 20s.
TRACK_CONFIRM_HITS = 3
TRACK_CONFIRM_WINDOW_S = 15.0

# TENTATIVE death: an unconfirmed track that goes silent this long is dropped
# outright (a transient blip that never earned confirmation). Deliberately
# shorter than the confirmed-track coast timeout -- we spend little patience on
# a contact that never confirmed.
TRACK_TENTATIVE_DROP_TIMEOUT_S = 12.0

# CONFIRMED -> COASTING: a confirmed track with no associated observation for
# this long is flagged COASTING (still tracked, but explicitly stale / no
# longer observed -- NOT presented as a live confirmed contact). ~5 missed
# hackrf cycles. Comfortably longer than one ml_classify interval so a slow ML
# cycle alone never coasts a track that hackrf is still re-confirming.
TRACK_COAST_TIMEOUT_S = 15.0

# COASTING -> DROPPED (track death): a coasting track that is still not
# re-acquired this long after its last observation is dropped. This is the
# total silence budget for a once-confirmed track before we admit it is gone.
TRACK_DROP_TIMEOUT_S = 45.0

# Association frequency gate. A detection only associates to an existing track
# if their centre frequencies are within this many GHz (50 MHz). Combined with
# the exact source + classification-key match below, this is the "gate" of the
# gated nearest-neighbour associator. 50 MHz tolerates the coarse centre-freq
# quantisation of the RSSI sweep without letting genuinely different band
# occupants collapse into one track.
TRACK_FREQ_GATE_GHZ = 0.05

# Concurrency budget (OB-04). Default sized to the spec ceiling (32). Raising it
# is a one-line change here; documented path to the Army's 40-50 ask is: bump
# this constant. The constraint is memory/CPU on the single worker, not a
# protocol limit, so 32 is a conservative, honest default rather than a hard
# ceiling. When the budget is exceeded the manager evicts the lowest-priority
# track (see _evict_for_budget) and LOGS it -- capacity truncation is NEVER
# silent on this counter-UAS system.
TRACK_BUDGET_MAX = 32

# Bound on per-track observation history kept in memory / persisted, mirroring
# RECONFIRM_EVENTS_CAP on detections so a long-lived track's document can't grow
# without limit under fast ingest cadence.
TRACK_OBS_HISTORY_CAP = 50


# Lifecycle states.
STATE_TENTATIVE = "TENTATIVE"
STATE_CONFIRMED = "CONFIRMED"
STATE_COASTING = "COASTING"
STATE_DROPPED = "DROPPED"

# Threat levels we treat as "high threat" for the never-evict-a-confirmed-high-
# threat-track-for-a-tentative-one budget rule. Kept explicit and small; a
# FRIENDLY (IFF verified) or LOW contact is not protected from eviction the way
# a HIGH/CRITICAL confirmed track is.
HIGH_THREAT_LEVELS = {"HIGH", "CRITICAL"}


def _parse_ts(ts: Any) -> Optional[datetime]:
    if isinstance(ts, datetime):
        return ts
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


class Track:
    """A single persistent track with lifecycle state and a best estimate.

    A track is NOT a detection. It references the detections that have been
    associated to it (detection_ids) and maintains its own identity, lifecycle
    state, and running best estimate derived from the latest associated
    observation. Honesty: the `state` field is authoritative -- a COASTING
    track is a track we are no longer observing, and callers MUST render it as
    such (stale), never as a live confirmed contact.
    """

    __slots__ = (
        "track_id", "state", "source", "match_model", "match_protocol",
        "first_seen", "last_seen", "last_updated",
        "hits", "misses",
        "center_freq_ghz", "rssi_dbm", "model", "protocol", "threat_level",
        "bearing_deg", "bearing_available",
        "detection_ids", "observations",
        "confirmed_at", "coasted_at", "dropped_at",
        "sources_seen",
        "_seen_at_last_sweep",
    )

    def __init__(self, detection: Dict[str, Any], now: datetime) -> None:
        self.track_id = f"TRACK-{uuid.uuid4().hex[:8].upper()}"
        self.state = STATE_TENTATIVE
        # Association key -- reuses the SAME immutable classification key the
        # detection merge logic uses (source + match_model/match_protocol), so
        # track association is consistent with how detections already merge.
        self.source = detection.get("source")
        self.match_model = detection.get("match_model") or detection.get("model")
        self.match_protocol = detection.get("match_protocol") or detection.get("protocol")

        self.first_seen = now
        self.last_seen = now
        self.last_updated = now
        self.hits = 1
        self.misses = 0

        # Running best estimate (from the associated detection).
        self.center_freq_ghz = detection.get("center_freq_ghz")
        self.rssi_dbm = detection.get("rssi_dbm")
        self.model = detection.get("model")
        self.protocol = detection.get("protocol")
        self.threat_level = detection.get("threat_level")
        self.bearing_deg = detection.get("bearing_deg")
        self.bearing_available = bool(detection.get("bearing_available"))

        # Provenance / association history.
        self.detection_ids: List[str] = []
        det_id = detection.get("id")
        if det_id:
            self.detection_ids.append(det_id)
        self.observations: List[Dict[str, Any]] = []
        self.sources_seen: List[str] = [self.source] if self.source else []

        self.confirmed_at: Optional[datetime] = None
        self.coasted_at: Optional[datetime] = None
        self.dropped_at: Optional[datetime] = None

        self._seen_at_last_sweep = now
        self._record_observation(detection, now)

    # ---- observation bookkeeping ------------------------------------------
    def _record_observation(self, detection: Dict[str, Any], now: datetime) -> None:
        self.observations.append({
            "ts": now.isoformat(),
            "detection_id": detection.get("id"),
            "center_freq_ghz": detection.get("center_freq_ghz"),
            "rssi_dbm": detection.get("rssi_dbm"),
        })
        if len(self.observations) > TRACK_OBS_HISTORY_CAP:
            self.observations = self.observations[-TRACK_OBS_HISTORY_CAP:]

    def hits_within(self, window_s: float, now: datetime) -> int:
        cutoff = now - timedelta(seconds=window_s)
        return sum(1 for o in self.observations
                   if (_parse_ts(o["ts"]) or now) >= cutoff)

    # ---- association ------------------------------------------------------
    def freq_distance(self, detection: Dict[str, Any]) -> Optional[float]:
        """Absolute centre-frequency distance (GHz) between this track's
        current estimate and a candidate detection, or None if either lacks a
        frequency. Used both as the association gate and as the nearest-neighbour
        tie-break metric."""
        a = self.center_freq_ghz
        b = detection.get("center_freq_ghz")
        if a is None or b is None:
            return None
        return abs(a - b)

    def update_from(self, detection: Dict[str, Any], now: datetime) -> None:
        """Associate a detection to this track: bump hit counter, refresh the
        best estimate, and (if this track was COASTING) re-acquire it back to
        CONFIRMED. Promotion TENTATIVE->CONFIRMED is evaluated by the manager
        after this call."""
        self.hits += 1
        self.last_seen = now
        self.last_updated = now
        det_id = detection.get("id")
        if det_id and det_id not in self.detection_ids:
            self.detection_ids.append(det_id)
        src = detection.get("source")
        if src and src not in self.sources_seen:
            self.sources_seen.append(src)
        # Refresh running best estimate from the latest observation.
        self.center_freq_ghz = detection.get("center_freq_ghz", self.center_freq_ghz)
        self.rssi_dbm = detection.get("rssi_dbm", self.rssi_dbm)
        self.model = detection.get("model", self.model)
        self.protocol = detection.get("protocol", self.protocol)
        self.threat_level = detection.get("threat_level", self.threat_level)
        if detection.get("bearing_available"):
            self.bearing_deg = detection.get("bearing_deg")
            self.bearing_available = True
        self._record_observation(detection, now)
        # Re-acquisition: a coasting track that gets a fresh observation is a
        # live confirmed contact again.
        if self.state == STATE_COASTING:
            self.state = STATE_CONFIRMED
            self.coasted_at = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialise for the Mongo mirror and the API. `state` is the honest,
        authoritative lifecycle field; `stale` is a convenience flag callers can
        use to avoid rendering a coasting track as a live confirmed contact."""
        def iso(dt: Optional[datetime]) -> Optional[str]:
            return dt.isoformat() if dt else None
        return {
            "track_id": self.track_id,
            "state": self.state,
            "stale": self.state == STATE_COASTING,
            "confirmed": self.state == STATE_CONFIRMED,
            "source": self.source,
            "match_model": self.match_model,
            "match_protocol": self.match_protocol,
            "first_seen": iso(self.first_seen),
            "last_seen": iso(self.last_seen),
            "last_updated": iso(self.last_updated),
            "hits": self.hits,
            "misses": self.misses,
            "center_freq_ghz": self.center_freq_ghz,
            "rssi_dbm": self.rssi_dbm,
            "model": self.model,
            "protocol": self.protocol,
            "threat_level": self.threat_level,
            "bearing_deg": self.bearing_deg,
            "bearing_available": self.bearing_available,
            "detection_ids": list(self.detection_ids),
            "observation_count": len(self.observations),
            "observations": list(self.observations),
            "sources_seen": list(self.sources_seen),
            "confirmed_at": iso(self.confirmed_at),
            "coasted_at": iso(self.coasted_at),
            "dropped_at": iso(self.dropped_at),
        }


class TrackManager:
    """In-memory index of live tracks with a Mongo-mirror-friendly API.

    Pure lifecycle logic (no I/O): `observe()` and `sweep()` mutate in-memory
    state and RETURN structured events + dirty track snapshots. The server-side
    wrapper is responsible for the side effects (Mongo upserts, audit
    log_event, WebSocket). This keeps the state machine unit-testable with no
    Mongo/asyncio, exactly like swarm_classifier.py's pure functions.
    """

    def __init__(self, budget_max: int = TRACK_BUDGET_MAX) -> None:
        self.budget_max = budget_max
        self._tracks: Dict[str, Track] = {}

    # ---- introspection ----------------------------------------------------
    def live_tracks(self) -> List[Track]:
        return list(self._tracks.values())

    def get(self, track_id: str) -> Optional[Track]:
        return self._tracks.get(track_id)

    def counts(self) -> Dict[str, Any]:
        confirmed = sum(1 for t in self._tracks.values() if t.state == STATE_CONFIRMED)
        tentative = sum(1 for t in self._tracks.values() if t.state == STATE_TENTATIVE)
        coasting = sum(1 for t in self._tracks.values() if t.state == STATE_COASTING)
        active = len(self._tracks)
        return {
            "active_tracks": active,
            "tracks_confirmed": confirmed,
            "tracks_tentative": tentative,
            "tracks_coasting": coasting,
            "tracks_budget_max": self.budget_max,
            "tracks_at_capacity": active >= self.budget_max,
        }

    def load_existing(self, docs: List[Dict[str, Any]]) -> None:
        """Rehydrate the in-memory index from persisted, non-DROPPED track docs
        at startup, so a restart doesn't lose live tracks (mirrors how ACTIVE
        detections survive a reboot in the `detections` collection). Best-effort
        reconstruction of the object from its serialised form."""
        for d in docs:
            if d.get("state") == STATE_DROPPED:
                continue
            t = Track.__new__(Track)
            t.track_id = d["track_id"]
            t.state = d.get("state", STATE_TENTATIVE)
            t.source = d.get("source")
            t.match_model = d.get("match_model")
            t.match_protocol = d.get("match_protocol")
            t.first_seen = _parse_ts(d.get("first_seen"))
            t.last_seen = _parse_ts(d.get("last_seen"))
            t.last_updated = _parse_ts(d.get("last_updated"))
            t.hits = d.get("hits", 0)
            t.misses = d.get("misses", 0)
            t.center_freq_ghz = d.get("center_freq_ghz")
            t.rssi_dbm = d.get("rssi_dbm")
            t.model = d.get("model")
            t.protocol = d.get("protocol")
            t.threat_level = d.get("threat_level")
            t.bearing_deg = d.get("bearing_deg")
            t.bearing_available = bool(d.get("bearing_available"))
            t.detection_ids = list(d.get("detection_ids") or [])
            t.observations = list(d.get("observations") or [])
            t.sources_seen = list(d.get("sources_seen") or [])
            t.confirmed_at = _parse_ts(d.get("confirmed_at"))
            t.coasted_at = _parse_ts(d.get("coasted_at"))
            t.dropped_at = _parse_ts(d.get("dropped_at"))
            t._seen_at_last_sweep = t.last_seen
            self._tracks[t.track_id] = t

    # ---- association ------------------------------------------------------
    def _find_associated_track(self, detection: Dict[str, Any]) -> Optional[Track]:
        """Single-hypothesis gated nearest-neighbour associator.

        Gate (all must hold):
          * same `source` AND same immutable classification key
            (match_model/match_protocol) -- the SAME basis the detection merge
            logic uses, so track association never contradicts detection merge;
          * centre frequency within TRACK_FREQ_GATE_GHZ (or frequency missing on
            either side, in which case the classification-key match alone
            carries the gate);
          * track is not DROPPED.
        Among all in-gate tracks pick the nearest in feature space (smallest
        centre-frequency distance), tie-broken by most recently updated. One
        detection thus associates to at most one track.
        """
        src = detection.get("source")
        m_model = detection.get("match_model") or detection.get("model")
        m_proto = detection.get("match_protocol") or detection.get("protocol")

        best: Optional[Track] = None
        best_key = None
        for t in self._tracks.values():
            if t.state == STATE_DROPPED:
                continue
            if t.source != src:
                continue
            if t.match_model != m_model or t.match_protocol != m_proto:
                continue
            fd = t.freq_distance(detection)
            if fd is not None and fd > TRACK_FREQ_GATE_GHZ:
                continue  # outside the frequency gate
            # Nearest-neighbour: smallest freq distance (None sorts last),
            # tie-broken by most-recent update.
            last_upd = t.last_updated or t.last_seen
            key = (fd if fd is not None else float("inf"),
                   -(last_upd.timestamp() if last_upd else 0.0))
            if best_key is None or key < best_key:
                best_key = key
                best = t
        return best

    # ---- the ingest hook --------------------------------------------------
    def observe(self, detection: Dict[str, Any],
                now: Optional[datetime] = None) -> Dict[str, Any]:
        """Associate one ingested detection to a track (or birth a new tentative
        track), applying N-of-M confirmation. Returns
        {"events": [...], "dirty": [snapshot,...]}.

        `events` items are dicts with an `event` key in {BIRTH, CONFIRM,
        REACQUIRE, ASSOCIATE, CAPACITY_DROP, CAPACITY_REFUSED}. The server
        wrapper turns the operationally-significant ones into audit log_event
        entries. `dirty` snapshots are the tracks the caller must persist.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        events: List[Dict[str, Any]] = []
        dirty: List[Dict[str, Any]] = []

        existing = self._find_associated_track(detection)
        if existing is not None:
            existing.update_from(detection, now)
            # N-of-M confirmation.
            if (existing.state == STATE_TENTATIVE
                    and existing.hits_within(TRACK_CONFIRM_WINDOW_S, now) >= TRACK_CONFIRM_HITS):
                existing.state = STATE_CONFIRMED
                existing.confirmed_at = now
                events.append({
                    "event": "CONFIRM", "track_id": existing.track_id,
                    "state": existing.state, "hits": existing.hits,
                })
            else:
                events.append({
                    "event": "ASSOCIATE", "track_id": existing.track_id,
                    "state": existing.state, "hits": existing.hits,
                })
            dirty.append(existing.to_dict())
            return {"events": events, "dirty": dirty}

        # No association -> birth a new TENTATIVE track, subject to budget.
        if len(self._tracks) >= self.budget_max:
            evicted = self._evict_for_budget()
            if evicted is None:
                # Every live track is a protected (confirmed high-threat / or
                # non-evictable) track -- we are genuinely out of capacity and
                # must NOT silently drop the new contact. Surface it loudly.
                events.append({
                    "event": "CAPACITY_REFUSED",
                    "detection_id": detection.get("id"),
                    "source": detection.get("source"),
                    "budget_max": self.budget_max,
                    "reason": ("track budget full and no lower-priority track "
                               "available to evict; new contact NOT tracked"),
                })
                return {"events": events, "dirty": dirty}
            evicted.state = STATE_DROPPED
            evicted.dropped_at = now
            del self._tracks[evicted.track_id]
            events.append({
                "event": "CAPACITY_DROP", "track_id": evicted.track_id,
                "evicted_state_before": evicted.to_dict()["state"],
                "budget_max": self.budget_max,
                "reason": "track budget exceeded; evicted lowest-priority track",
            })
            dirty.append(evicted.to_dict())

        track = Track(detection, now)
        self._tracks[track.track_id] = track
        events.append({
            "event": "BIRTH", "track_id": track.track_id,
            "state": track.state, "source": track.source,
        })
        dirty.append(track.to_dict())
        return {"events": events, "dirty": dirty}

    def _evict_for_budget(self) -> Optional[Track]:
        """Pick the lowest-priority track to evict when the budget is exceeded,
        or None if nothing is safe to evict.

        Priority policy (evict the least valuable first):
          1. COASTING tracks (already no longer observed), oldest last_seen
             first.
          2. TENTATIVE tracks (never confirmed), oldest last_seen first.
        NEVER evict a CONFIRMED track to make room for a new TENTATIVE one --
        and in particular never a CONFIRMED high-threat track. If the only
        tracks present are CONFIRMED, return None (caller must refuse the new
        contact loudly rather than sacrifice a confirmed track)."""
        coasting = [t for t in self._tracks.values() if t.state == STATE_COASTING]
        if coasting:
            return min(coasting, key=lambda t: (t.last_seen or datetime.min.replace(tzinfo=timezone.utc)))
        tentative = [t for t in self._tracks.values() if t.state == STATE_TENTATIVE]
        if tentative:
            return min(tentative, key=lambda t: (t.last_seen or datetime.min.replace(tzinfo=timezone.utc)))
        return None

    # ---- periodic lifecycle sweep -----------------------------------------
    def sweep(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Time-driven lifecycle advance (run periodically, mirroring server.py's
        _stale_detections_loop). Applies miss counting and the age-based
        transitions: TENTATIVE->DROPPED, CONFIRMED->COASTING, COASTING->DROPPED.
        Returns {"events": [...], "dirty": [snapshot,...]}. DROPPED tracks are
        removed from the in-memory index (their final snapshot is returned for
        persistence to the audit trail)."""
        if now is None:
            now = datetime.now(timezone.utc)
        events: List[Dict[str, Any]] = []
        dirty: List[Dict[str, Any]] = []

        for t in list(self._tracks.values()):
            changed = False
            # Miss counter: a sweep in which the track received no new
            # observation since the previous sweep counts as a miss.
            if t.last_seen == t._seen_at_last_sweep:
                t.misses += 1
                changed = True
            t._seen_at_last_sweep = t.last_seen

            age = (now - t.last_seen).total_seconds() if t.last_seen else 0.0

            if t.state == STATE_TENTATIVE and age > TRACK_TENTATIVE_DROP_TIMEOUT_S:
                t.state = STATE_DROPPED
                t.dropped_at = now
                del self._tracks[t.track_id]
                events.append({"event": "DROP", "track_id": t.track_id,
                               "from_state": STATE_TENTATIVE,
                               "reason": "tentative track never confirmed and went silent"})
                dirty.append(t.to_dict())
                continue
            if t.state == STATE_CONFIRMED and age > TRACK_COAST_TIMEOUT_S:
                t.state = STATE_COASTING
                t.coasted_at = now
                events.append({"event": "COAST", "track_id": t.track_id,
                               "from_state": STATE_CONFIRMED,
                               "reason": "confirmed track not re-observed within coast timeout"})
                dirty.append(t.to_dict())
                continue
            if t.state == STATE_COASTING and age > TRACK_DROP_TIMEOUT_S:
                t.state = STATE_DROPPED
                t.dropped_at = now
                del self._tracks[t.track_id]
                events.append({"event": "DROP", "track_id": t.track_id,
                               "from_state": STATE_COASTING,
                               "reason": "coasting track not re-acquired within drop timeout"})
                dirty.append(t.to_dict())
                continue

            if changed:
                dirty.append(t.to_dict())

        return {"events": events, "dirty": dirty}
