"""Endpoint wiring tests for the Effector-Selection recommendations routes
(RFI Northern Command 4.5.4 / 4.5.6 / 4.5.7 -- server.py P3 wiring).

Same in-process pattern as test_sop_rules.py / test_zones.py: importing
backend/server.py only needs the env vars SET (motor is lazy). The endpoint
coroutines and the shared compute helper are driven directly with the
read-only inputs monkeypatched to in-memory fakes -- _sop_current_contacts,
_compute_engagement_plan, the pending-engagement maps, the range-authorization
lease accessor, ws_manager (TX-bridge subscription) and the master TX-halt flag
-- so nothing touches Mongo/ws and the hash-chained mission log is captured, not
written.

What is covered (per .omc/plans/decision-effector-selection-engine.md, Tests
#5, mirroring the SOP no-TX suite):
  * GET /effector/recommendations and POST /effector/recommendations/recompute
    are BOTH commander-gated (route introspection + the require_commander 403);
  * a COMMANDER gets a 200-shape recommendation payload from each;
  * POST writes EXACTLY ONE EFFECTOR_RECOMMENDATION audit event;
  * the returned payload carries the PROPOSED_REQUIRES_HUMAN_AUTHORIZATION
    posture (top-level + per recommendation);
  * SAFETY: the compute helper, the availability-snapshot assembler and the two
    handlers reference NO tx-halt-clear / arm-token / deploy / broadcast symbol
    (source introspection, mirroring test_sop_rules' no-TX-spine scan).

Run: pytest backend/tests/test_effector_endpoints.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("DB_NAME", "test_db_unused")
os.environ.setdefault("JWT_SECRET", "test-secret-unused")
os.environ.setdefault("ADMIN_EMAIL", "test-admin@unused.local")
os.environ.setdefault("ADMIN_PASSWORD", "test-password-unused")
os.environ.setdefault("IFF_BRIDGE_API_KEY", "test-iff-bridge-key-unused")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import server as srv

COMMANDER = {"email": "cmdr@unused.local", "role": "commander"}
OPERATOR = {"email": "op@unused.local", "role": "operator"}

PROPOSED = "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"


# --------------------------------------------------------------------------
# Fixtures: monkeypatch the read-only inputs the compute helper reads.
# --------------------------------------------------------------------------
def _contacts():
    # One legacy-MAVLink positioned contact (recommendable) and one encrypted
    # DJI contact (jam-primary, takeover NOT_FEASIBLE) -- both still PROPOSED.
    return [
        {"detection_id": "D1", "callsign": "HOSTILE-1", "threat_level": "HIGH",
         "confidence_type": "protocol_verified", "protocol": "MAVLink",
         "position_source": "REMOTEID"},
        {"detection_id": "D2", "callsign": "HOSTILE-2", "threat_level": "CRITICAL",
         "confidence_type": "rf_signature", "control_link_family": "OcuSync"},
    ]


def _plan():
    # A minimal engagement-plan shape: proposals joined by detection_id + an
    # excluded list. build_effector_recommendations consumes these read-only.
    return {
        "proposals": [
            {"detection_id": "D1", "rank": 1, "is_controller_candidate": True,
             "score_breakdown": {"controller_first_bonus": 30}},
            {"detection_id": "D2", "rank": 2, "is_controller_candidate": False,
             "score_breakdown": {"controller_first_bonus": 0}},
        ],
        "excluded": [],
        "summary": {"proposal_count": 2, "excluded_count": 0},
    }


class _FakeWsManager:
    def __init__(self, subscribed=True):
        self._subscribed = subscribed

    def has_tx_consumer(self, effect: str) -> bool:
        return self._subscribed


@pytest.fixture
def fake_env(monkeypatch):
    """Monkeypatch the read-only inputs + capture log_event. All availability
    flags default to 'clearable' (TX up, bridge subscribed, range auth enabled)
    so a recommendation can be produced; individual tests override as needed."""
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message,
                       "meta": meta or {}, "actor": actor})
        return {}

    async def _fake_contacts():
        return _contacts()

    async def _fake_plan():
        return _plan()

    monkeypatch.setattr(srv, "log_event", _log)
    monkeypatch.setattr(srv, "_sop_current_contacts", _fake_contacts)
    monkeypatch.setattr(srv, "_compute_engagement_plan", _fake_plan)
    monkeypatch.setattr(srv, "ws_manager", _FakeWsManager(subscribed=True))
    monkeypatch.setattr(srv, "_range_auth_status",
                        lambda effect: {"enabled": True, "effect": effect})
    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "_pending_mavlink_inject", {})
    monkeypatch.setattr(srv, "_pending_gnss_spoof", {})
    return events


# ==========================================================================
# Gate wiring -- both routes are commander-gated (introspection + helper 403)
# ==========================================================================
def _route_dep_calls(path: str, method: str):
    for route in srv.app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route {method} {path} not found")


def test_both_effector_routes_require_commander():
    for method, path in [("GET", "/api/effector/recommendations"),
                         ("POST", "/api/effector/recommendations/recompute")]:
        assert srv.require_commander in _route_dep_calls(path, method), \
            f"{method} {path} must be gated by require_commander"


def test_require_commander_refuses_operator():
    # The shared commander gate both routes depend on refuses an OPERATOR (403).
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


# ==========================================================================
# Commander accepted -- 200-shape payload from GET and POST
# ==========================================================================
def test_get_returns_proposed_recommendation_payload(fake_env):
    recs = asyncio.run(srv.get_effector_recommendations(user=COMMANDER))
    assert recs["status"] == PROPOSED
    assert recs["disclaimer"]
    assert "effector_availability_echo" in recs
    assert recs["summary"]["contacts_considered"] == 2
    assert len(recs["recommendations"]) == 2
    # Every recommendation carries the PROPOSED-requires-human posture.
    assert all(r["status"] == PROPOSED for r in recs["recommendations"])
    # The availability echo reflects the read-only snapshot (all clearable).
    echo = recs["effector_availability_echo"]
    assert echo["tx_halted"] is False
    assert echo["jam"]["bridge_up"] is True
    assert echo["gnss_spoof"]["maturity"] == "v1_placeholder"


def test_get_writes_no_audit_event(fake_env):
    events = fake_env
    asyncio.run(srv.get_effector_recommendations(user=COMMANDER))
    # GET is a safe method -- no mission-log entry (mirrors GET /engagement/plan).
    assert [e for e in events if e["kind"] == "EFFECTOR_RECOMMENDATION"] == []


def test_post_recompute_writes_exactly_one_audit_event(fake_env):
    events = fake_env
    recs = asyncio.run(srv.recompute_effector_recommendations(user=COMMANDER))
    assert recs["status"] == PROPOSED
    audits = [e for e in events if e["kind"] == "EFFECTOR_RECOMMENDATION"]
    assert len(audits) == 1
    assert audits[0]["actor"] == COMMANDER["email"]
    assert audits[0]["meta"]["summary"]["recommendation_count"] == 2
    # The audit meta echoes the read-only availability snapshot, never a mutation.
    assert audits[0]["meta"]["effector_availability_echo"]["tx_halted"] is False


def test_dedup_reflects_pending_mavlink_inject(fake_env, monkeypatch):
    # A detection already under an active MAVLink inject is flagged already_engaged.
    monkeypatch.setattr(srv, "_pending_mavlink_inject",
                        {"req-1": {"target_detection_id": "D1"}})
    recs = asyncio.run(srv.get_effector_recommendations(user=COMMANDER))
    by_id = {r["detection_id"]: r for r in recs["recommendations"]}
    assert by_id["D1"]["dedup_status"]["already_engaged"] is True
    assert by_id["D2"]["dedup_status"]["already_engaged"] is False
    assert recs["summary"]["already_engaged_count"] == 1


def test_tx_halted_snapshot_marks_effectors_unavailable(fake_env, monkeypatch):
    monkeypatch.setattr(srv, "_tx_halted", True)
    recs = asyncio.run(srv.get_effector_recommendations(user=COMMANDER))
    assert recs["effector_availability_echo"]["tx_halted"] is True
    # With the master TX halt set, nothing is currently clearable.
    assert all(r["recommended_effector"] is None for r in recs["recommendations"])
    assert recs["summary"]["recommendations_with_clearable_effector"] == 0


# ==========================================================================
# SAFETY -- governing invariant #1: no effector-recommendation path touches the
# TX spine (source introspection, mirroring test_sop_rules' no-TX-spine scan).
# ==========================================================================
_EFFECTOR_SYMBOLS = [
    "_effector_availability_snapshot",
    "_compute_effector_recommendations",
    "get_effector_recommendations",
    "recompute_effector_recommendations",
]

# Forbidden tokens: clearing the TX halt, minting/consuming arm/confirm tokens,
# or calling any deploy/transmit/bring-online path. Identical set to
# test_sop_rules._FORBIDDEN_TOKENS. Reading _tx_halted / _range_auth_status /
# has_tx_consumer is the SANCTIONED read-only availability snapshot and is NOT
# forbidden -- only the CLEAR form '_tx_halted = False' is.
_FORBIDDEN_TOKENS = [
    "_tx_halted = False", "_tx_halted=False",
    "_issue_arm_token", "_arm_tokens[",
    "_issue_jam_confirm_token", "_issue_gnss_spoof_confirm_token",
    "_issue_mavlink_sdr_inject_confirm_token", "_issue_iff_ff_ack",
    "payloads/deploy", "emergency_resume", "tx_bring_online",
    "mavlink/broadcast", "_consume_arm_token",
]


def test_no_effector_symbol_references_tx_spine():
    for name in _EFFECTOR_SYMBOLS:
        fn = getattr(srv, name)
        src = inspect.getsource(fn)
        for token in _FORBIDDEN_TOKENS:
            assert token not in src, \
                f"effector symbol {name} must not reference TX-spine token {token!r}"


def test_effector_recommendations_leave_tx_spine_untouched(fake_env):
    """Even a full recompute (which audits) must not clear the TX halt or mint
    an arm token."""
    halted_before = srv._tx_halted
    tokens_before = len(srv._arm_tokens)
    recs = asyncio.run(srv.recompute_effector_recommendations(user=COMMANDER))
    assert recs["status"] == PROPOSED
    assert srv._tx_halted is halted_before
    assert len(srv._arm_tokens) == tokens_before
