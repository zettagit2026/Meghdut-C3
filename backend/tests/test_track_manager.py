"""Unit tests for the multi-target track manager (OB-04) --
backend/track_manager.py. True unit tests: pure lifecycle state machine over
plain dicts with an injected `now`, no live server / Mongo / requests, same
pattern as test_swarm_classifier.py / test_gnss_spoof_geodesic.py.

Run: pytest backend/tests/test_track_manager.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from track_manager import (  # noqa: E402
    STATE_COASTING,
    STATE_CONFIRMED,
    STATE_DROPPED,
    STATE_TENTATIVE,
    TRACK_CONFIRM_HITS,
    TrackManager,
)

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


def _t(offset_s: float) -> datetime:
    return NOW + timedelta(seconds=offset_s)


def _det(det_id: str, *, source: str = "HACKRF",
         match_model: str = "DJI Mini (candidate)",
         match_protocol: str = "OcuSync/Wi-Fi",
         center_freq_ghz: float = 2.44,
         rssi_dbm: float = -70.0,
         model: str = "Unidentified 2.4GHz Emitter",
         protocol: str = "Unconfirmed (RF heuristic)",
         threat_level: str = "MEDIUM") -> dict:
    return {
        "id": det_id,
        "source": source,
        "match_model": match_model,
        "match_protocol": match_protocol,
        "center_freq_ghz": center_freq_ghz,
        "rssi_dbm": rssi_dbm,
        "model": model,
        "protocol": protocol,
        "threat_level": threat_level,
        "bearing_available": False,
        "bearing_deg": None,
    }


def _events(result) -> list:
    return [e["event"] for e in result["events"]]


class TestBirth:
    def test_first_detection_births_tentative_track(self):
        mgr = TrackManager()
        result = mgr.observe(_det("d1"), now=NOW)
        assert _events(result) == ["BIRTH"]
        tracks = mgr.live_tracks()
        assert len(tracks) == 1
        assert tracks[0].state == STATE_TENTATIVE
        assert tracks[0].hits == 1
        # Dirty snapshot returned for persistence.
        assert result["dirty"][0]["state"] == STATE_TENTATIVE


class TestAssociation:
    def test_followup_detection_associates_not_new_track(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)
        # Same source + classification key + close frequency -> same track.
        result = mgr.observe(_det("d2", center_freq_ghz=2.441), now=_t(3))
        assert len(mgr.live_tracks()) == 1, "must associate, not birth a 2nd track"
        assert _events(result) == ["ASSOCIATE"]
        tr = mgr.live_tracks()[0]
        assert tr.hits == 2
        assert "d1" in tr.detection_ids and "d2" in tr.detection_ids

    def test_out_of_frequency_gate_births_separate_track(self):
        mgr = TrackManager()
        mgr.observe(_det("d1", center_freq_ghz=2.44), now=NOW)
        # 5.8 GHz is far outside the 50 MHz frequency gate -> separate track.
        result = mgr.observe(_det("d2", center_freq_ghz=5.80), now=_t(3))
        assert _events(result) == ["BIRTH"]
        assert len(mgr.live_tracks()) == 2

    def test_different_source_births_separate_track(self):
        mgr = TrackManager()
        mgr.observe(_det("d1", source="HACKRF"), now=NOW)
        result = mgr.observe(_det("d2", source="SIK_RADIO"), now=_t(3))
        assert _events(result) == ["BIRTH"]
        assert len(mgr.live_tracks()) == 2

    def test_nearest_neighbour_picks_closest_track(self):
        mgr = TrackManager()
        # Two tracks in-gate-adjacent but distinct; a new det should pick the
        # frequency-nearest one. Use two sources so both births are separate,
        # then... actually associator gates on source too, so build two tracks
        # on same source but frequencies 100 MHz apart (each births separately).
        mgr.observe(_det("d1", center_freq_ghz=2.40), now=NOW)
        mgr.observe(_det("d2", center_freq_ghz=2.48), now=NOW)
        assert len(mgr.live_tracks()) == 2
        # New det at 2.481 is within gate of the 2.48 track only.
        mgr.observe(_det("d3", center_freq_ghz=2.481), now=_t(3))
        assert len(mgr.live_tracks()) == 2
        # The 2.48 track absorbed it.
        t_hi = [t for t in mgr.live_tracks() if abs(t.center_freq_ghz - 2.481) < 0.01][0]
        assert "d3" in t_hi.detection_ids


class TestConfirmation:
    def test_n_of_m_confirmation_promotes(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)  # hit 1
        mgr.observe(_det("d2"), now=_t(3))  # hit 2
        assert mgr.live_tracks()[0].state == STATE_TENTATIVE
        result = mgr.observe(_det("d3"), now=_t(6))  # hit 3 == TRACK_CONFIRM_HITS
        assert TRACK_CONFIRM_HITS == 3
        assert "CONFIRM" in _events(result)
        assert mgr.live_tracks()[0].state == STATE_CONFIRMED
        assert mgr.live_tracks()[0].confirmed_at is not None

    def test_hits_outside_window_do_not_confirm(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)          # hit 1 @ t0
        mgr.observe(_det("d2"), now=_t(3))        # hit 2 @ t3
        # Third hit far outside the 15s confirmation window from the first:
        # only 2 hits fall within any 15s sliding window at t=30 -> still
        # tentative. (last two hits: t3 and t30 are >15s apart too.)
        result = mgr.observe(_det("d3"), now=_t(30))
        assert "CONFIRM" not in _events(result)
        assert mgr.live_tracks()[0].state == STATE_TENTATIVE


class TestCoastAndDrop:
    def _confirm(self, mgr):
        mgr.observe(_det("d1"), now=NOW)
        mgr.observe(_det("d2"), now=_t(3))
        mgr.observe(_det("d3"), now=_t(6))
        assert mgr.live_tracks()[0].state == STATE_CONFIRMED

    def test_confirmed_coasts_then_drops(self):
        mgr = TrackManager()
        self._confirm(mgr)
        # No observations after t6. Sweep just past coast timeout (last_seen=6,
        # coast timeout 15s -> coast at t>21).
        r1 = mgr.sweep(now=_t(6 + 16))
        assert any(e["event"] == "COAST" for e in r1["events"])
        assert mgr.live_tracks()[0].state == STATE_COASTING
        # Further silence past drop timeout (45s after last_seen=6 -> t>51).
        r2 = mgr.sweep(now=_t(6 + 46))
        assert any(e["event"] == "DROP" for e in r2["events"])
        assert len(mgr.live_tracks()) == 0
        # Drop snapshot returned for audit persistence.
        assert r2["dirty"][0]["state"] == STATE_DROPPED

    def test_coasting_track_reacquires_on_new_detection(self):
        mgr = TrackManager()
        self._confirm(mgr)
        mgr.sweep(now=_t(6 + 16))
        assert mgr.live_tracks()[0].state == STATE_COASTING
        # A fresh observation re-acquires it to CONFIRMED.
        mgr.observe(_det("d4"), now=_t(6 + 20))
        assert mgr.live_tracks()[0].state == STATE_CONFIRMED

    def test_tentative_drops_without_confirmation(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)  # single blip, never confirmed
        # Tentative drop timeout is 12s.
        r = mgr.sweep(now=_t(13))
        assert any(e["event"] == "DROP" for e in r["events"])
        assert len(mgr.live_tracks()) == 0

    def test_miss_counter_increments_on_silent_sweep(self):
        mgr = TrackManager()
        self._confirm(mgr)
        mgr.sweep(now=_t(8))   # baseline sweep (activity at t6 preceded it)
        mgr.sweep(now=_t(10))  # no new obs since last sweep -> miss
        mgr.sweep(now=_t(12))  # still no new obs -> miss
        assert mgr.live_tracks()[0].misses >= 2


class TestConcurrencyBudget:
    def _fill_confirmed(self, mgr, n, base_freq=2.0):
        """Create n CONFIRMED tracks on distinct frequencies/sources."""
        for i in range(n):
            src = f"SRC-{i}"
            f = base_freq + i * 0.10
            mgr.observe(_det(f"{i}-a", source=src, center_freq_ghz=f), now=NOW)
            mgr.observe(_det(f"{i}-b", source=src, center_freq_ghz=f), now=_t(3))
            mgr.observe(_det(f"{i}-c", source=src, center_freq_ghz=f), now=_t(6))

    def test_budget_evicts_coasting_first_and_logs(self):
        mgr = TrackManager(budget_max=3)
        # 2 confirmed tracks + 1 coasting track = 3 (at budget).
        self._fill_confirmed(mgr, 2)
        # third track that we let coast
        mgr.observe(_det("c-a", source="COAST-SRC", center_freq_ghz=3.5), now=NOW)
        mgr.observe(_det("c-b", source="COAST-SRC", center_freq_ghz=3.5), now=_t(3))
        mgr.observe(_det("c-c", source="COAST-SRC", center_freq_ghz=3.5), now=_t(6))
        # Keep the 2 confirmed tracks fresh so only COAST-SRC goes stale.
        for i in range(2):
            mgr.observe(_det(f"{i}-fresh", source=f"SRC-{i}",
                             center_freq_ghz=2.0 + i * 0.10), now=_t(20))
        mgr.sweep(now=_t(6 + 16))  # t22: COAST-SRC (last_seen 6) coasts; others fresh
        assert len(mgr.live_tracks()) == 3
        coasting = [t for t in mgr.live_tracks() if t.state == STATE_COASTING]
        assert len(coasting) == 1
        coasting_id = coasting[0].track_id

        # New contact at budget -> must evict the coasting track (lowest prio),
        # log a CAPACITY_DROP, and birth the new track.
        result = mgr.observe(_det("new", source="NEW-SRC", center_freq_ghz=4.9),
                             now=_t(30))
        evs = _events(result)
        assert "CAPACITY_DROP" in evs
        assert "BIRTH" in evs
        assert mgr.get(coasting_id) is None, "coasting track must be evicted"
        assert len(mgr.live_tracks()) == 3

    def test_budget_never_evicts_confirmed_for_tentative_refuses_loudly(self):
        mgr = TrackManager(budget_max=2)
        # Fill budget entirely with CONFIRMED tracks.
        self._fill_confirmed(mgr, 2)
        assert all(t.state == STATE_CONFIRMED for t in mgr.live_tracks())
        # New contact -> nothing evictable -> refuse loudly, do NOT track it,
        # do NOT sacrifice a confirmed track.
        result = mgr.observe(_det("new", source="NEW-SRC", center_freq_ghz=4.9),
                             now=_t(30))
        assert _events(result) == ["CAPACITY_REFUSED"]
        assert len(mgr.live_tracks()) == 2
        assert all(t.state == STATE_CONFIRMED for t in mgr.live_tracks())

    def test_budget_evicts_tentative_when_no_coasting(self):
        mgr = TrackManager(budget_max=2)
        self._fill_confirmed(mgr, 1)  # 1 confirmed
        mgr.observe(_det("tent", source="TENT-SRC", center_freq_ghz=3.3), now=NOW)  # 1 tentative
        assert len(mgr.live_tracks()) == 2
        tent_id = [t for t in mgr.live_tracks() if t.state == STATE_TENTATIVE][0].track_id
        result = mgr.observe(_det("new", source="NEW-SRC", center_freq_ghz=4.9), now=_t(3))
        assert "CAPACITY_DROP" in _events(result)
        assert mgr.get(tent_id) is None
        # confirmed track survived.
        assert any(t.state == STATE_CONFIRMED for t in mgr.live_tracks())


class TestCountsAndSerialisation:
    def test_counts_and_capacity_flag(self):
        mgr = TrackManager(budget_max=2)
        mgr.observe(_det("d1", source="A", center_freq_ghz=2.0), now=NOW)
        c = mgr.counts()
        assert c["active_tracks"] == 1
        assert c["tracks_at_capacity"] is False
        mgr.observe(_det("d2", source="B", center_freq_ghz=2.5), now=NOW)
        assert mgr.counts()["tracks_at_capacity"] is True

    def test_coasting_track_snapshot_flags_stale(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)
        mgr.observe(_det("d2"), now=_t(3))
        mgr.observe(_det("d3"), now=_t(6))
        mgr.sweep(now=_t(6 + 16))
        snap = mgr.live_tracks()[0].to_dict()
        assert snap["state"] == STATE_COASTING
        assert snap["stale"] is True
        assert snap["confirmed"] is False

    def test_load_existing_rehydrates_live_tracks(self):
        mgr = TrackManager()
        mgr.observe(_det("d1"), now=NOW)
        snaps = [t.to_dict() for t in mgr.live_tracks()]
        # include a dropped doc that must be skipped
        snaps.append({**snaps[0], "track_id": "TRACK-DEAD", "state": STATE_DROPPED})
        mgr2 = TrackManager()
        mgr2.load_existing(snaps)
        assert len(mgr2.live_tracks()) == 1
        assert mgr2.live_tracks()[0].track_id == snaps[0]["track_id"]
