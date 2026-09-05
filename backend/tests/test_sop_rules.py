"""Unit tests for the SOP rules engine, rule-alerts feed, C2 mode and the
background eval loop (Zone/SOP engine -- Phase B+C).

Same in-process pattern as test_zones.py: importing backend/server.py only needs
the env vars SET (motor is lazy). The endpoint coroutines and the eval loop are
driven directly with srv.db, srv.log_event and the position globals monkeypatched
to in-memory fakes, so nothing touches Mongo/ws and the hash-chained mission log
is captured, not written.

What is covered (per .omc/plans/zone-sop-engine.md, Phase B+C):
  * every WRITE (POST/PUT/DELETE /sop/rules, POST /c2/mode) is gated by
    require_commander (route introspection + helper 403); reads are get_current_user;
  * a rule whose action.type is a fire/engage/deploy value is REJECTED at 422
    (create AND validate) -- the governing "no auto-fire" invariant at the boundary;
  * hot-apply: create/edit a rule -> the loop's version-stamped cache reloads ->
    a subsequent evaluate reflects it, with NO redeploy;
  * POST /sop/rules/validate previews matches with NO persistence / NO side effects;
  * two-lane honesty: a spatial rule fires for a positioned RemoteID contact but
    does NOT fire for a position-less HackRF detection;
  * MANUAL suppresses auto-emission (evaluate only); AUTO emits (persist + push);
  * rule-alerts feed + acknowledge; c2 mode toggle is audited;
  * SAFETY: no SOP endpoint/loop path clears _tx_halted, mints an arm token, or
    calls a deploy endpoint (source introspection + a live AUTO tick assertion).

Run: pytest backend/tests/test_sop_rules.py -v
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

# A square zone around (lon 77.0..77.1, lat 28.0..28.1).
SQUARE_RING = [[77.0, 28.0], [77.1, 28.0], [77.1, 28.1], [77.0, 28.1], [77.0, 28.0]]
VALID_POLYGON = {"type": "Polygon", "coordinates": [SQUARE_RING]}
INSIDE_LON, INSIDE_LAT = 77.05, 28.05
OUTSIDE_LON, OUTSIDE_LAT = 10.0, 10.0


# --------------------------------------------------------------------------
# In-memory fake Mongo (adds upsert to test_zones' fake, for db.system_state)
# --------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


class _FakeDeleteResult:
    def __init__(self, deleted_count):
        self.deleted_count = deleted_count


class _FakeCollection:
    def __init__(self):
        self.docs = []

    @staticmethod
    def _strip(doc, projection):
        d = dict(doc)
        if projection and projection.get("_id") == 0:
            d.pop("_id", None)
        return d

    def _match(self, doc, flt):
        return all(doc.get(k) == v for k, v in (flt or {}).items())

    def find(self, flt=None, projection=None):
        matched = [self._strip(d, projection) for d in self.docs if self._match(d, flt)]
        return _FakeCursor(matched)

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if self._match(d, flt):
                return self._strip(d, projection)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return None

    async def update_one(self, flt, update, upsert=False):
        for d in self.docs:
            if self._match(d, flt):
                d.update(update.get("$set", {}))
                return None
        if upsert:
            new_doc = dict(flt)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)
        return None

    async def delete_one(self, flt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, flt)]
        return _FakeDeleteResult(before - len(self.docs))


class _FakeDB:
    def __init__(self):
        self.zones = _FakeCollection()
        self.sop_rules = _FakeCollection()
        self.rule_alerts = _FakeCollection()
        self.detections = _FakeCollection()
        self.system_state = _FakeCollection()


@pytest.fixture
def fake_env(monkeypatch):
    """Monkeypatch srv.db + the position globals + capture log_event; reset the
    module-level SOP caches so tests don't leak version/dedup state into each
    other."""
    db = _FakeDB()
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message,
                       "meta": meta or {}, "actor": actor})
        return {}

    monkeypatch.setattr(srv, "db", db)
    monkeypatch.setattr(srv, "log_event", _log)
    monkeypatch.setattr(srv, "_last_remoteid_decode", None)
    monkeypatch.setattr(srv, "_last_adsb_decode", None)
    # Reset hot-apply cache + de-dup table between tests.
    srv._sop_config_version = 0
    srv._sop_cache = {"version": None, "zones": [], "rules": []}
    srv._sop_recent_emits.clear()
    return db, events


# --------------------------------------------------------------------------
# Helpers to build rule bodies
# --------------------------------------------------------------------------
def _rule_body(**over):
    action = srv.SopAction(**over.pop("action", {"type": "ALERT", "severity": "WARNING",
                                                 "message_template": "hit {callsign}"}))
    conditions = srv.SopConditions(**over.pop("conditions", {}))
    return srv.SopRuleBody(name=over.pop("name", "R1"),
                           enabled=over.pop("enabled", True),
                           priority=over.pop("priority", 0),
                           zone_id=over.pop("zone_id", None),
                           conditions=conditions, action=action, **over)


def _active_detection(det_id="D1", source="HACKRF", protocol="OcuSync",
                      center_freq_ghz=2.44, **extra):
    doc = {"id": det_id, "status": "ACTIVE", "source": source, "protocol": protocol,
           "center_freq_ghz": center_freq_ghz, "callsign": f"CONTACT-{det_id}",
           "threat_level": "MEDIUM", "ml_confidence": None, "confidence_type": None}
    doc.update(extra)
    return doc


# ==========================================================================
# Gate wiring -- every WRITE is require_commander; reads are get_current_user
# ==========================================================================
def _route_dep_calls(path: str, method: str):
    for route in srv.app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route {method} {path} not found")


def test_rule_write_routes_require_commander():
    for method, path in [("POST", "/api/sop/rules"),
                         ("PUT", "/api/sop/rules/{rule_id}"),
                         ("DELETE", "/api/sop/rules/{rule_id}"),
                         ("POST", "/api/c2/mode")]:
        assert srv.require_commander in _route_dep_calls(path, method), \
            f"{method} {path} must be gated by require_commander"


def test_rule_read_routes_require_authenticated_user_only():
    for method, path in [("GET", "/api/sop/rules"),
                         ("POST", "/api/sop/rules/validate"),
                         ("GET", "/api/sop/alerts"),
                         ("POST", "/api/sop/alerts/{alert_id}/ack"),
                         ("GET", "/api/c2/mode")]:
        calls = _route_dep_calls(path, method)
        assert srv.get_current_user in calls, f"{method} {path} must require auth"
        assert srv.require_commander not in calls, f"{method} {path} must NOT be commander-gated"


def test_require_commander_refuses_operator_on_writes():
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


# ==========================================================================
# Fire/engage/deploy action is rejected at 422 (create AND validate)
# ==========================================================================
@pytest.mark.parametrize("bad_type", ["ENGAGE", "FIRE", "DEPLOY", "jam", "TRANSMIT"])
def test_fire_action_rejected_422_on_create(fake_env, bad_type):
    body = _rule_body(action={"type": bad_type, "severity": "CRITICAL"})
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_sop_rule(body, user=COMMANDER))
    assert ei.value.status_code == 422
    # nothing persisted
    assert srv.db.sop_rules.docs == []


def test_fire_action_rejected_422_on_validate(fake_env):
    body = _rule_body(action={"type": "ENGAGE", "severity": "CRITICAL"})
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.validate_sop_rule(body, user=OPERATOR))
    assert ei.value.status_code == 422


def test_allowed_action_types_are_exactly_alert_cue_set():
    # Guard the invariant at the type level: the permitted set has no
    # fire/engage/deploy member and equals the engine's authoritative set.
    assert srv.SOP_ALLOWED_ACTION_TYPES == frozenset(
        {"ALERT", "ANNUNCIATE", "PRIORITIZE", "CUE_RECOMMENDATION"})
    for banned in ("ENGAGE", "FIRE", "DEPLOY", "JAM", "TRANSMIT"):
        assert banned not in srv.SOP_ALLOWED_ACTION_TYPES


# ==========================================================================
# Commander create/list/update/delete + audit
# ==========================================================================
def test_commander_create_persists_and_audits(fake_env):
    db, events = fake_env
    body = _rule_body(name="Alpha", priority=5,
                      action={"type": "CUE_RECOMMENDATION", "severity": "CRITICAL",
                              "recommended_effect": "jam", "message_template": "cue {callsign}"})
    rule = asyncio.run(srv.create_sop_rule(body, user=COMMANDER))
    assert rule["name"] == "Alpha"
    assert rule["action"]["type"] == "CUE_RECOMMENDATION"
    assert rule["action"]["recommended_effect"] == "jam"
    assert rule["version"] == 1
    assert rule["created_by"] == COMMANDER["email"]
    assert "_id" not in rule
    assert len(db.sop_rules.docs) == 1
    creates = [e for e in events if e["kind"] == "SOP_RULE_CREATE"]
    assert len(creates) == 1 and creates[0]["actor"] == COMMANDER["email"]
    assert creates[0]["meta"]["rule_id"] == rule["id"]


def test_list_update_delete_flow(fake_env):
    db, events = fake_env
    created = asyncio.run(srv.create_sop_rule(_rule_body(name="Bravo"), user=COMMANDER))
    listing = asyncio.run(srv.list_sop_rules(user=OPERATOR))
    assert listing["count"] == 1 and listing["rules"][0]["id"] == created["id"]

    upd = srv.SopRuleUpdateBody(name="Bravo-2", priority=9, enabled=False)
    updated = asyncio.run(srv.update_sop_rule(created["id"], upd, user=COMMANDER))
    assert updated["name"] == "Bravo-2" and updated["priority"] == 9
    assert updated["enabled"] is False
    assert updated["version"] == 2  # bumped
    assert any(e["kind"] == "SOP_RULE_UPDATE" for e in events)

    res = asyncio.run(srv.delete_sop_rule(created["id"], user=COMMANDER))
    assert res["deleted"] is True
    assert db.sop_rules.docs == []
    assert any(e["kind"] == "SOP_RULE_DELETE" for e in events)


def test_update_and_delete_missing_rule_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_sop_rule("nope", srv.SopRuleUpdateBody(name="X"), user=COMMANDER))
    assert ei.value.status_code == 404
    with pytest.raises(srv.HTTPException) as ei2:
        asyncio.run(srv.delete_sop_rule("nope", user=COMMANDER))
    assert ei2.value.status_code == 404


def test_update_with_fire_action_rejected_422(fake_env):
    created = asyncio.run(srv.create_sop_rule(_rule_body(name="C"), user=COMMANDER))
    bad = srv.SopRuleUpdateBody(action=srv.SopAction(type="ENGAGE"))
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_sop_rule(created["id"], bad, user=COMMANDER))
    assert ei.value.status_code == 422


# ==========================================================================
# Hot-apply: create/edit -> version-stamped cache reloads -> reflected
# ==========================================================================
def test_hot_apply_cache_reloads_on_create_and_edit(fake_env):
    async def scenario():
        # Initial reload: no rules yet.
        zones, rules = await srv._sop_reload_config_if_stale()
        assert rules == []

        created = await srv.create_sop_rule(
            _rule_body(name="Hot", conditions={"protocol_in": ["OcuSync"]},
                       action={"type": "ALERT", "severity": "INFO", "message_template": "x"}),
            user=COMMANDER)
        # A CRUD bumped the version -> next reload must pick the new rule up.
        zones, rules = await srv._sop_reload_config_if_stale()
        assert len(rules) == 1 and rules[0]["conditions"]["protocol_in"] == ["OcuSync"]

        # Edit the rule's predicate; the cache must reflect the edit next reload.
        await srv.update_sop_rule(
            created["id"],
            srv.SopRuleUpdateBody(conditions=srv.SopConditions(protocol_in=["WiFi"])),
            user=COMMANDER)
        zones, rules = await srv._sop_reload_config_if_stale()
        assert rules[0]["conditions"]["protocol_in"] == ["WiFi"]

    asyncio.run(scenario())


def test_hot_apply_reflected_in_evaluate(fake_env):
    """End-to-end hot-apply: an ACTIVE OcuSync detection does not match until a
    matching rule is created; after the create (no redeploy) the very next tick
    matches it."""
    async def scenario():
        srv.db.detections.docs.append(_active_detection(protocol="OcuSync"))
        # AUTO so a match would emit.
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "AUTO"})

        summary = await srv._sop_eval_tick()
        assert summary["firings"] == 0  # no rules yet

        await srv.create_sop_rule(
            _rule_body(name="P", conditions={"protocol_in": ["OcuSync"]},
                       action={"type": "ALERT", "severity": "INFO", "message_template": "m"}),
            user=COMMANDER)
        summary2 = await srv._sop_eval_tick()
        assert summary2["firings"] >= 1
        assert summary2["emitted"] >= 1

    asyncio.run(scenario())


# ==========================================================================
# validate previews matches with NO persistence / NO side effects
# ==========================================================================
def test_validate_previews_without_persistence(fake_env):
    async def scenario():
        srv.db.detections.docs.append(_active_detection(protocol="OcuSync"))
        body = _rule_body(name="Prev", conditions={"protocol_in": ["OcuSync"]},
                          action={"type": "ALERT", "severity": "INFO",
                                  "message_template": "would hit {callsign}"})
        res = await srv.validate_sop_rule(body, user=OPERATOR)
        assert res["ok"] is True
        assert res["would_match_count"] == 1
        assert res["matches"][0]["message"] == "would hit CONTACT-D1"
        # NO side effects: no rule persisted, no alert emitted, version untouched.
        assert srv.db.sop_rules.docs == []
        assert srv.db.rule_alerts.docs == []

    before_version = srv._sop_config_version
    asyncio.run(scenario())
    assert srv._sop_config_version == before_version


# ==========================================================================
# Two-lane honesty: spatial rule fires for positioned RemoteID, NOT for a
# position-less HackRF detection
# ==========================================================================
def test_spatial_rule_two_lane_honesty(fake_env, monkeypatch):
    async def scenario():
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "AUTO"})
        zone = await srv.create_zone(
            srv.ZoneBody(name="Z", zone_type="ALERT", polygon=VALID_POLYGON),
            user=COMMANDER)
        # A purely spatial rule: inside the zone, no other predicate.
        await srv.create_sop_rule(
            _rule_body(name="Spatial", zone_id=zone["id"],
                       conditions={"zone_membership": "inside"},
                       action={"type": "ANNUNCIATE", "severity": "WARNING",
                               "message_template": "in-zone {position_source}"}),
            user=COMMANDER)

        # A position-less HackRF detection (must NOT match the spatial rule).
        srv.db.detections.docs.append(_active_detection(det_id="HRF", source="HACKRF"))
        # A positioned RemoteID broadcast INSIDE the zone (must match).
        monkeypatch.setattr(srv, "_last_remoteid_decode",
                            {"uas_id": "SN-1", "latitude_deg": INSIDE_LAT,
                             "longitude_deg": INSIDE_LON})

        summary = await srv._sop_eval_tick()
        alerts = srv.db.rule_alerts.docs
        assert len(alerts) == 1, f"exactly the RemoteID contact should fire, got {alerts}"
        assert alerts[0]["contact_ref"]["position_source"] == "REMOTEID"
        assert alerts[0]["detection_id"] is None  # not the HackRF detection
        assert summary["emitted"] == 1

    asyncio.run(scenario())


def test_remoteid_outside_zone_does_not_fire(fake_env, monkeypatch):
    async def scenario():
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "AUTO"})
        zone = await srv.create_zone(
            srv.ZoneBody(name="Z", zone_type="ALERT", polygon=VALID_POLYGON),
            user=COMMANDER)
        await srv.create_sop_rule(
            _rule_body(name="Spatial", zone_id=zone["id"],
                       conditions={"zone_membership": "inside"},
                       action={"type": "ANNUNCIATE", "severity": "WARNING",
                               "message_template": "in"}),
            user=COMMANDER)
        monkeypatch.setattr(srv, "_last_remoteid_decode",
                            {"uas_id": "SN-2", "latitude_deg": OUTSIDE_LAT,
                             "longitude_deg": OUTSIDE_LON})
        await srv._sop_eval_tick()
        assert srv.db.rule_alerts.docs == []

    asyncio.run(scenario())


def test_droneid_detection_is_spatial_lane_eligible(fake_env):
    """A DroneID detection carrying a real drone_lat/drone_lon is evaluated by a
    spatial rule (the position-forwarding honesty enabler)."""
    async def scenario():
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "AUTO"})
        zone = await srv.create_zone(
            srv.ZoneBody(name="Z", zone_type="ALERT", polygon=VALID_POLYGON),
            user=COMMANDER)
        await srv.create_sop_rule(
            _rule_body(name="Spatial", zone_id=zone["id"],
                       conditions={"zone_membership": "inside"},
                       action={"type": "ALERT", "severity": "INFO",
                               "message_template": "in"}),
            user=COMMANDER)
        srv.db.detections.docs.append(_active_detection(
            det_id="DID", protocol="OcuSync",
            drone_lat=INSIDE_LAT, drone_lon=INSIDE_LON))
        await srv._sop_eval_tick()
        alerts = srv.db.rule_alerts.docs
        assert len(alerts) == 1
        assert alerts[0]["contact_ref"]["position_source"] == "DRONEID"
        assert alerts[0]["detection_id"] == "DID"

    asyncio.run(scenario())


# ==========================================================================
# MANUAL suppresses auto-emission; AUTO emits
# ==========================================================================
def test_manual_mode_suppresses_emission_auto_emits(fake_env):
    async def scenario():
        srv.db.detections.docs.append(_active_detection(protocol="OcuSync"))
        await srv.create_sop_rule(
            _rule_body(name="M", conditions={"protocol_in": ["OcuSync"]},
                       action={"type": "ALERT", "severity": "INFO", "message_template": "m"}),
            user=COMMANDER)

        # MANUAL: still evaluates (firings > 0) but emits nothing.
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "MANUAL"})
        manual = await srv._sop_eval_tick()
        assert manual["mode"] == "MANUAL"
        assert manual["firings"] >= 1
        assert manual["emitted"] == 0
        assert srv.db.rule_alerts.docs == []

        # Flip to AUTO: same rule now emits.
        await srv.db.system_state.update_one({"_id": "c2"}, {"$set": {"mode": "AUTO"}})
        auto = await srv._sop_eval_tick()
        assert auto["mode"] == "AUTO"
        assert auto["emitted"] >= 1
        assert len(srv.db.rule_alerts.docs) >= 1
        emitted_alert = srv.db.rule_alerts.docs[0]
        assert emitted_alert["action_type"] == "ALERT"
        assert emitted_alert["message"] == "m"
        assert emitted_alert["acknowledged_by"] is None

    asyncio.run(scenario())


def test_default_mode_is_manual_when_singleton_absent(fake_env):
    # No system_state doc at all -> fail-safe MANUAL (no auto-emission).
    async def scenario():
        srv.db.detections.docs.append(_active_detection(protocol="OcuSync"))
        await srv.create_sop_rule(
            _rule_body(name="D", conditions={"protocol_in": ["OcuSync"]},
                       action={"type": "ALERT", "severity": "INFO", "message_template": "m"}),
            user=COMMANDER)
        summary = await srv._sop_eval_tick()
        assert summary["mode"] == "MANUAL"
        assert summary["emitted"] == 0
        assert srv.db.rule_alerts.docs == []

    asyncio.run(scenario())


# ==========================================================================
# rule-alerts feed + acknowledge
# ==========================================================================
def test_alerts_feed_and_ack(fake_env):
    async def scenario():
        srv.db.rule_alerts.docs.append({
            "id": "A1", "ts": "2026-09-05T00:00:00Z", "rule_id": "R", "rule_name": "n",
            "action_type": "ALERT", "severity": "WARNING", "message": "m",
            "rank_boost": None, "cue": None,
            "acknowledged_by": None, "acknowledged_at": None,
        })
        feed = await srv.list_sop_alerts(user=OPERATOR)
        assert feed["count"] == 1 and feed["alerts"][0]["id"] == "A1"

        acked = await srv.ack_sop_alert("A1", user=OPERATOR)
        assert acked["acknowledged_by"] == OPERATOR["email"]
        assert acked["acknowledged_at"] is not None

    asyncio.run(scenario())


def test_ack_missing_alert_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.ack_sop_alert("nope", user=OPERATOR))
    assert ei.value.status_code == 404


def test_alerts_feed_orders_by_rank_boost(fake_env):
    async def scenario():
        srv.db.rule_alerts.docs.extend([
            {"id": "low", "ts": "2026-09-05T00:00:02Z", "rank_boost": 0},
            {"id": "high", "ts": "2026-09-05T00:00:01Z", "rank_boost": 50},
        ])
        feed = await srv.list_sop_alerts(user=OPERATOR)
        assert feed["alerts"][0]["id"] == "high"  # boosted surfaces first

    asyncio.run(scenario())


# ==========================================================================
# C2 mode toggle audited
# ==========================================================================
def test_c2_mode_get_default_and_toggle_audited(fake_env):
    db, events = fake_env

    async def scenario():
        current = await srv.get_c2_mode(user=OPERATOR)
        assert current["mode"] == "MANUAL" and current["default"] is True

        res = await srv.set_c2_mode(srv.C2ModeBody(mode="AUTO"), user=COMMANDER)
        assert res["mode"] == "AUTO" and res["updated_by"] == COMMANDER["email"]

        after = await srv.get_c2_mode(user=OPERATOR)
        assert after["mode"] == "AUTO" and after["default"] is False

    asyncio.run(scenario())
    changes = [e for e in events if e["kind"] == "C2_MODE_CHANGE"]
    assert len(changes) == 1 and changes[0]["meta"]["mode"] == "AUTO"
    assert changes[0]["actor"] == COMMANDER["email"]


def test_c2_mode_bad_value_rejected_422(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.set_c2_mode(srv.C2ModeBody(mode="AUTOFIRE"), user=COMMANDER))
    assert ei.value.status_code == 422


# ==========================================================================
# SAFETY -- governing invariant #1: no SOP path touches the TX spine
# ==========================================================================
_SOP_SYMBOLS = [
    "create_sop_rule", "update_sop_rule", "delete_sop_rule", "validate_sop_rule",
    "list_sop_rules", "list_sop_alerts", "ack_sop_alert",
    "get_c2_mode", "set_c2_mode", "_get_c2_mode",
    "_sop_eval_loop", "_sop_eval_tick", "_sop_emit_firing",
    "_sop_current_contacts", "_sop_contact_from_detection", "_sop_positioned_contacts",
    "_sop_reload_config_if_stale", "_validate_sop_action", "_bump_sop_version",
    "_sop_apply_threat_enrichment", "_sop_contact_ref", "_sop_dedup_key",
]

# Forbidden tokens: clearing the TX halt, minting arm/confirm tokens, or calling
# any deploy/transmit/bring-online path.
_FORBIDDEN_TOKENS = [
    "_tx_halted = False", "_tx_halted=False",
    "_issue_arm_token", "_arm_tokens[",
    "_issue_jam_confirm_token", "_issue_gnss_spoof_confirm_token",
    "_issue_mavlink_sdr_inject_confirm_token", "_issue_iff_ff_ack",
    "payloads/deploy", "emergency_resume", "tx_bring_online",
    "mavlink/broadcast", "_consume_arm_token",
]


def test_no_sop_symbol_references_tx_spine():
    for name in _SOP_SYMBOLS:
        fn = getattr(srv, name)
        src = inspect.getsource(fn)
        for token in _FORBIDDEN_TOKENS:
            assert token not in src, \
                f"SOP symbol {name} must not reference TX-spine token {token!r}"


def test_auto_tick_emits_but_leaves_tx_spine_untouched(fake_env):
    """Even a full AUTO tick that persists+pushes firings must not clear the TX
    halt or mint an arm token."""
    async def scenario():
        await srv.db.system_state.insert_one({"_id": "c2", "mode": "AUTO"})
        srv.db.detections.docs.append(_active_detection(protocol="OcuSync"))
        await srv.create_sop_rule(
            _rule_body(name="S", conditions={"protocol_in": ["OcuSync"]},
                       action={"type": "CUE_RECOMMENDATION", "severity": "CRITICAL",
                               "recommended_effect": "jam", "message_template": "cue"}),
            user=COMMANDER)
        return await srv._sop_eval_tick()

    halted_before = srv._tx_halted
    tokens_before = len(srv._arm_tokens)
    summary = asyncio.run(scenario())
    assert summary["emitted"] >= 1
    # The strongest emitted action is a PROPOSED cue, never an effect.
    alert = srv.db.rule_alerts.docs[0]
    assert alert["action_type"] == "CUE_RECOMMENDATION"
    assert alert["cue"]["status"] == "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"
    # TX spine untouched.
    assert srv._tx_halted is halted_before
    assert len(srv._arm_tokens) == tokens_before


# ==========================================================================
# DroneID position-forwarding fix on DetectionIngestBody
# ==========================================================================
def test_detection_ingest_body_accepts_droneid_position():
    body = srv.DetectionIngestBody(center_freq_ghz=2.44, drone_lat=INSIDE_LAT,
                                   drone_lon=INSIDE_LON, app_lat=1.0, app_lon=2.0)
    assert body.drone_lat == INSIDE_LAT and body.drone_lon == INSIDE_LON
    assert body.app_lat == 1.0 and body.app_lon == 2.0
    # Absent by default -> no fabricated position for other sources.
    plain = srv.DetectionIngestBody(center_freq_ghz=2.44)
    assert plain.drone_lat is None and plain.drone_lon is None
