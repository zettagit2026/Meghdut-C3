"""Unit tests for the Zone CRUD endpoints (Zone/SOP engine — Phase A).

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
in-process pattern as test_operator_jam_mode.py: importing backend/server.py only
needs the env vars SET (motor is lazy). The endpoint coroutines are driven
directly with srv.db and srv.log_event monkeypatched to in-memory fakes, so
nothing touches Mongo and the hash-chained mission log is captured, not written.

What is covered:
  * POST /zones refuses an OPERATOR (require_commander -> 403) and accepts a
    COMMANDER (persists the zone + returns it);
  * the write routes (POST/PUT/DELETE) are actually wired to require_commander
    and GET is wired to get_current_user (route-introspection, so the 403 gate
    is proven at the route, not only at the helper);
  * a degenerate / out-of-range polygon is rejected with 422;
  * GET /zones lists a created zone;
  * PUT edits a zone (commander) and 404s on a missing id; operator is refused;
  * DELETE removes a zone (commander) and 404s on a missing id;
  * a ZONE_CREATE audit event is logged with the right actor/meta;
  * honesty: creating a zone touches NO TX-spine state (no _tx_halted change,
    no arm token minted).

Run: pytest backend/tests/test_zones.py -v
"""
from __future__ import annotations

import asyncio
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

# A valid closed square ring ([lon, lat] pairs) and a matching GeoJSON Polygon.
SQUARE_RING = [[77.0, 28.0], [77.1, 28.0], [77.1, 28.1], [77.0, 28.1], [77.0, 28.0]]
VALID_POLYGON = {"type": "Polygon", "coordinates": [SQUARE_RING]}


# --------------------------------------------------------------------------
# In-memory fake Mongo collection (only the ops the zone endpoints use)
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
        self.docs = []  # list of dicts (no _id — same as inserted copies here)

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

    async def update_one(self, flt, update):
        for d in self.docs:
            if self._match(d, flt):
                d.update(update.get("$set", {}))
                return None
        return None

    async def delete_one(self, flt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, flt)]
        return _FakeDeleteResult(before - len(self.docs))


class _FakeDB:
    def __init__(self):
        self.zones = _FakeCollection()


@pytest.fixture
def fake_env(monkeypatch):
    """Monkeypatch srv.db to an in-memory fake and capture log_event calls."""
    db = _FakeDB()
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message,
                       "meta": meta or {}, "actor": actor})
        return {}

    monkeypatch.setattr(srv, "db", db)
    monkeypatch.setattr(srv, "log_event", _log)
    return db, events


# --------------------------------------------------------------------------
# Gate wiring — the 403 for an operator is proven at the route + the helper
# --------------------------------------------------------------------------
def _route_dep_calls(path: str, method: str):
    for route in srv.app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route {method} {path} not found")


def test_write_routes_require_commander():
    for method, path in [("POST", "/api/zones"),
                         ("PUT", "/api/zones/{zone_id}"),
                         ("DELETE", "/api/zones/{zone_id}")]:
        assert srv.require_commander in _route_dep_calls(path, method), \
            f"{method} {path} must be gated by require_commander"


def test_list_route_requires_authenticated_user_only():
    calls = _route_dep_calls("/api/zones", "GET")
    assert srv.get_current_user in calls
    assert srv.require_commander not in calls


def test_require_commander_refuses_operator():
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


def test_require_commander_accepts_commander():
    assert asyncio.run(srv.require_commander(user=COMMANDER)) is COMMANDER


# --------------------------------------------------------------------------
# POST /zones — commander create + persistence + audit
# --------------------------------------------------------------------------
def test_commander_create_persists_and_returns_zone(fake_env):
    db, events = fake_env
    body = srv.ZoneBody(name="Alpha", zone_type="ALERT", polygon=VALID_POLYGON,
                        priority=5, notes="north gate")
    zone = asyncio.run(srv.create_zone(body, user=COMMANDER))

    assert zone["name"] == "Alpha"
    assert zone["zone_type"] == "ALERT"
    assert zone["priority"] == 5
    assert zone["enabled"] is True
    assert zone["polygon"] == VALID_POLYGON
    assert zone["created_by"] == COMMANDER["email"]
    assert zone["updated_by"] == COMMANDER["email"]
    assert zone["created_at"] and zone["updated_at"]
    assert "_id" not in zone
    # persisted
    assert len(db.zones.docs) == 1
    assert db.zones.docs[0]["id"] == zone["id"]


def test_create_logs_zone_create_audit(fake_env):
    _db, events = fake_env
    body = srv.ZoneBody(name="Bravo", zone_type="DETECTION", polygon=VALID_POLYGON)
    zone = asyncio.run(srv.create_zone(body, user=COMMANDER))

    creates = [e for e in events if e["kind"] == "ZONE_CREATE"]
    assert len(creates) == 1
    assert creates[0]["actor"] == COMMANDER["email"]
    assert creates[0]["meta"]["zone_id"] == zone["id"]
    assert creates[0]["meta"]["zone_type"] == "DETECTION"


def test_create_touches_no_tx_spine(fake_env):
    # Honesty invariant: a zone write can never arm/key/halt TX. Prove that
    # creating a zone leaves _tx_halted unchanged and mints no arm token.
    halted_before = srv._tx_halted
    tokens_before = len(srv._arm_tokens)
    body = srv.ZoneBody(name="Charlie", zone_type="TRACKING", polygon=VALID_POLYGON)
    asyncio.run(srv.create_zone(body, user=COMMANDER))
    assert srv._tx_halted is halted_before
    assert len(srv._arm_tokens) == tokens_before


# --------------------------------------------------------------------------
# Invalid geometry -> 422
# --------------------------------------------------------------------------
def test_degenerate_polygon_rejected_422(fake_env):
    # Two-distinct-vertex ring: cannot form a polygon.
    bad = {"type": "Polygon", "coordinates": [[[77.0, 28.0], [77.1, 28.0], [77.0, 28.0]]]}
    body = srv.ZoneBody(name="Bad", zone_type="ALERT", polygon=bad)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_zone(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_out_of_range_lat_rejected_422(fake_env):
    bad = {"type": "Polygon", "coordinates": [
        [[77.0, 28.0], [77.1, 28.0], [77.1, 999.0], [77.0, 28.0]]]}
    body = srv.ZoneBody(name="Bad2", zone_type="ALERT", polygon=bad)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_zone(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_non_polygon_type_rejected_422(fake_env):
    bad = {"type": "LineString", "coordinates": [SQUARE_RING]}
    body = srv.ZoneBody(name="Bad3", zone_type="ALERT", polygon=bad)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_zone(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_bogus_zone_type_rejected_by_model():
    with pytest.raises(Exception):
        srv.ZoneBody(name="X", zone_type="NOPE", polygon=VALID_POLYGON)


# --------------------------------------------------------------------------
# GET /zones
# --------------------------------------------------------------------------
def test_list_zones_returns_created(fake_env):
    body = srv.ZoneBody(name="Delta", zone_type="MITIGATION", polygon=VALID_POLYGON)
    created = asyncio.run(srv.create_zone(body, user=COMMANDER))
    listing = asyncio.run(srv.list_zones(user=OPERATOR))
    assert listing["count"] == 1
    assert listing["zones"][0]["id"] == created["id"]
    assert "_id" not in listing["zones"][0]


# --------------------------------------------------------------------------
# PUT /zones/{id}
# --------------------------------------------------------------------------
def test_commander_update_edits_fields_and_audits(fake_env):
    _db, events = fake_env
    created = asyncio.run(srv.create_zone(
        srv.ZoneBody(name="Echo", zone_type="ALERT", polygon=VALID_POLYGON),
        user=COMMANDER))
    upd = srv.ZoneUpdateBody(name="Echo-2", enabled=False, priority=9)
    updated = asyncio.run(srv.update_zone(created["id"], upd, user=COMMANDER))

    assert updated["name"] == "Echo-2"
    assert updated["enabled"] is False
    assert updated["priority"] == 9
    assert updated["updated_by"] == COMMANDER["email"]
    assert updated["zone_type"] == "ALERT"  # untouched field preserved
    assert any(e["kind"] == "ZONE_UPDATE" for e in events)


def test_update_missing_zone_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_zone("no-such-id",
                                    srv.ZoneUpdateBody(name="Z"), user=COMMANDER))
    assert ei.value.status_code == 404


def test_update_invalid_polygon_rejected_422(fake_env):
    created = asyncio.run(srv.create_zone(
        srv.ZoneBody(name="Foxtrot", zone_type="ALERT", polygon=VALID_POLYGON),
        user=COMMANDER))
    bad = {"type": "Polygon", "coordinates": [[[1.0, 1.0], [2.0, 2.0]]]}
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_zone(created["id"],
                                    srv.ZoneUpdateBody(polygon=bad), user=COMMANDER))
    assert ei.value.status_code == 422


def test_update_route_gated_operator_refused():
    # Operator refusal is enforced by the route's require_commander dependency
    # (proven in test_write_routes_require_commander) and the helper below.
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


# --------------------------------------------------------------------------
# DELETE /zones/{id}
# --------------------------------------------------------------------------
def test_commander_delete_removes_and_audits(fake_env):
    db, events = fake_env
    created = asyncio.run(srv.create_zone(
        srv.ZoneBody(name="Golf", zone_type="CLUTTER", polygon=VALID_POLYGON),
        user=COMMANDER))
    res = asyncio.run(srv.delete_zone(created["id"], user=COMMANDER))
    assert res["deleted"] is True
    assert res["id"] == created["id"]
    assert db.zones.docs == []
    assert any(e["kind"] == "ZONE_DELETE" for e in events)


def test_delete_missing_zone_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.delete_zone("no-such-id", user=COMMANDER))
    assert ei.value.status_code == 404
