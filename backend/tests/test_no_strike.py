"""Unit tests for the No-strike / civilian-protection registry (P1 of
no-strike-registry.md): the pure matcher `backend/no_strike.py` + the
commander-gated CRUD + version-stamped hot-load in `backend/server.py`.

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
in-process pattern as test_zones.py: importing backend/server.py only needs the
env vars SET (motor is lazy). The endpoint coroutines are driven directly with
srv.db and srv.log_event monkeypatched to in-memory fakes, so nothing touches
Mongo and the hash-chained mission log is captured, not written.

What is covered:
  MATCHER (no_strike.match)
    * precedence: bssid beats ssid_exact beats ssid_prefix beats oui beats
      vendor_regex (first hit wins, most-specific-first);
    * ssid_prefix is case-insensitive;
    * an OUI match on a locally-administered (randomized) MAC sets
      randomized:True; a globally-administered MAC does not;
    * malformed input (non-dict identity, junk MAC, broken stored regex) is an
      honest non-match and NEVER raises;
    * is_locally_administered / normalize_mac helpers.
  CRUD (server.py)
    * the write routes (POST/PUT/DELETE) + GET are wired to require_commander
      (route-introspection: the 403 gate is proven at the route);
    * require_commander refuses an operator (403) and accepts a commander;
    * create persists + returns the entry, defaults `hard` True for
      CIVILIAN_INFRASTRUCTURE and False otherwise, and audits NO_STRIKE_CREATE;
    * create bumps the version and _no_strike_entries() reflects it with no
      restart (the hot-load path);
    * DELETE DISABLES (enabled:false), never removes, and audits NO_STRIKE_DISABLE;
    * an invalid match block (no key / bad regex / bad MAC) is rejected 422;
    * a create touches NO TX-spine state (honesty invariant).

Run: pytest backend/tests/test_no_strike.py -v
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

import no_strike
import server as srv

COMMANDER = {"email": "cmdr@unused.local", "role": "commander"}
OPERATOR = {"email": "op@unused.local", "role": "operator"}


# ==========================================================================
# PURE MATCHER — no_strike.py
# ==========================================================================
def _entry(eid, category="CIVILIAN_INFRASTRUCTURE", hard=True, **match):
    return {"id": eid, "category": category, "hard": hard,
            "label": eid, "match": match}


def test_normalize_mac_forms():
    assert no_strike.normalize_mac("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"
    assert no_strike.normalize_mac("aabbccddeeff") == "AA:BB:CC:DD:EE:FF"
    assert no_strike.normalize_mac("AA:BB:CC", 3) == "AA:BB:CC"
    # wrong length / junk -> honest None (never a guess)
    assert no_strike.normalize_mac("aabbcc", 6) is None
    assert no_strike.normalize_mac("zz:zz:zz:zz:zz:zz") is None
    assert no_strike.normalize_mac(None) is None
    assert no_strike.normalize_mac(12345) is None


def test_is_locally_administered_bit():
    # 0x02 bit of the first octet: AA=0b10101010 -> set (randomized);
    # A8=0b10101000 -> clear (globally administered).
    assert no_strike.is_locally_administered("AA:BB:CC:DD:EE:FF") is True
    assert no_strike.is_locally_administered("A8:BB:CC:DD:EE:FF") is False
    assert no_strike.is_locally_administered("AA:BB:CC") is True  # OUI form
    assert no_strike.is_locally_administered("garbage") is False


def test_precedence_bssid_beats_all():
    entries = [
        _entry("vr", vendor_regex="Apple"),
        _entry("oui", oui="A8:BB:CC"),
        _entry("pfx", ssid_prefix="BSNL"),
        _entry("exact", ssid_exact="BSNL_HOME"),
        _entry("bss", bssid="A8:BB:CC:DD:EE:FF"),
    ]
    ident = {"ssid": "BSNL_HOME", "bssid": "A8:BB:CC:DD:EE:FF", "manuf": "Apple Inc"}
    res = no_strike.match(ident, entries)
    assert res["matched"] is True
    assert res["basis"] == "bssid"
    assert res["entry_id"] == "bss"


def test_precedence_ssid_exact_beats_prefix_oui_vendor():
    entries = [
        _entry("vr", vendor_regex="Apple"),
        _entry("oui", oui="A8:BB:CC"),
        _entry("pfx", ssid_prefix="BSNL"),
        _entry("exact", ssid_exact="BSNL_HOME"),
    ]
    # No bssid on the contact -> ssid_exact is the most specific remaining.
    res = no_strike.match({"ssid": "BSNL_HOME", "manuf": "Apple"}, entries)
    assert res["basis"] == "ssid_exact"


def test_precedence_ssid_prefix_beats_oui_and_vendor():
    entries = [
        _entry("vr", vendor_regex="Apple"),
        _entry("oui", oui="A8:BB:CC"),
        _entry("pfx", ssid_prefix="BSNL"),
    ]
    res = no_strike.match({"ssid": "BSNL_GUEST_2", "manuf": "Apple"}, entries)
    assert res["basis"] == "ssid_prefix"


def test_precedence_oui_beats_vendor():
    entries = [
        _entry("vr", category="NEUTRAL", hard=False, vendor_regex="Apple"),
        _entry("oui", oui="A8:BB:CC"),
    ]
    res = no_strike.match({"oui": "A8:BB:CC", "manuf": "Apple"}, entries)
    assert res["basis"] == "oui"


def test_ssid_prefix_case_insensitive():
    entries = [_entry("pfx", ssid_prefix="Zetta")]
    for ssid in ("Zetta_Cmd", "zetta_cmd", "ZETTA_CMD", "zEtTa"):
        res = no_strike.match({"ssid": ssid}, entries)
        assert res["matched"] is True, ssid
        assert res["basis"] == "ssid_prefix"


def test_oui_on_randomized_mac_flags_unverified():
    entries = [_entry("oui", category="NEUTRAL", hard=False, oui="AA:BB:CC")]
    # locally-administered (AA) full BSSID whose OUI matches -> randomized True
    res = no_strike.match({"bssid": "AA:BB:CC:11:22:33"}, entries)
    assert res["matched"] is True
    assert res["basis"] == "oui"
    assert res["randomized"] is True


def test_oui_on_stable_mac_not_flagged():
    entries = [_entry("oui", oui="A8:BB:CC")]
    res = no_strike.match({"bssid": "A8:BB:CC:11:22:33"}, entries)
    assert res["matched"] is True
    assert res["randomized"] is False


def test_ssid_match_is_never_flagged_randomized():
    entries = [_entry("pfx", ssid_prefix="BSNL")]
    res = no_strike.match({"ssid": "BSNL_x", "bssid": "AA:BB:CC:DD:EE:FF"}, entries)
    assert res["basis"] == "ssid_prefix"
    assert res["randomized"] is False


def test_no_match_returns_unmatched():
    entries = [_entry("bss", bssid="A8:BB:CC:DD:EE:FF")]
    assert no_strike.match({"ssid": "unknown"}, entries) == {"matched": False}
    assert no_strike.match({}, entries) == {"matched": False}
    assert no_strike.match({"bssid": "11:22:33:44:55:66"}, entries) == {"matched": False}


def test_malformed_input_never_raises():
    entries = [_entry("bss", bssid="A8:BB:CC:DD:EE:FF")]
    # non-dict identity, junk MAC, None entries, broken stored regex — all
    # honest non-matches, no exception.
    assert no_strike.match("nope", entries) == {"matched": False}
    assert no_strike.match(None, entries) == {"matched": False}
    assert no_strike.match({"bssid": "zzzz"}, entries) == {"matched": False}
    assert no_strike.match({"ssid": "x"}, None) == {"matched": False}
    bad = [{"id": "b", "category": "NEUTRAL", "label": "b",
            "match": {"vendor_regex": "([a-z"}}]
    assert no_strike.match({"manuf": "abc"}, bad) == {"matched": False}


def test_vendor_regex_broken_entry_skipped_not_fatal():
    entries = [
        {"id": "bad", "category": "NEUTRAL", "label": "bad",
         "match": {"vendor_regex": "([a-z"}},
        {"id": "good", "category": "NEUTRAL", "label": "good",
         "match": {"vendor_regex": "Samsung"}},
    ]
    res = no_strike.match({"manuf": "Samsung Electronics"}, entries)
    assert res["matched"] is True
    assert res["entry_id"] == "good"


def test_validate_match_rules():
    assert no_strike.validate_match({"ssid_prefix": "Zetta"})[0] is True
    assert no_strike.validate_match({})[0] is False
    assert no_strike.validate_match({"bssid": "not-a-mac"})[0] is False
    assert no_strike.validate_match({"oui": "AABB"})[0] is False
    assert no_strike.validate_match({"vendor_regex": "([a-z"})[0] is False
    assert no_strike.validate_match("not-a-dict")[0] is False


# ==========================================================================
# In-memory fake Mongo (only the ops the no-strike endpoints use) — same shape
# as test_zones.py.
# ==========================================================================
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


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

    async def update_one(self, flt, update):
        for d in self.docs:
            if self._match(d, flt):
                d.update(update.get("$set", {}))
                return None
        return None


class _FakeDB:
    def __init__(self):
        self.no_strike_registry = _FakeCollection()


@pytest.fixture
def fake_env(monkeypatch):
    """Monkeypatch srv.db to an in-memory fake, reset the hot-load version/cache,
    and capture log_event calls."""
    db = _FakeDB()
    events = []

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message,
                       "meta": meta or {}, "actor": actor})
        return {}

    monkeypatch.setattr(srv, "db", db)
    monkeypatch.setattr(srv, "log_event", _log)
    monkeypatch.setattr(srv, "_no_strike_version", 0)
    monkeypatch.setattr(srv, "_no_strike_cache",
                        {"version": None, "entries": []})
    return db, events


# --------------------------------------------------------------------------
# Gate wiring — the 403 for an operator is proven at the route + the helper
# --------------------------------------------------------------------------
def _route_dep_calls(path: str, method: str):
    for route in srv.app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route {method} {path} not found")


def test_all_routes_require_commander():
    for method, path in [("GET", "/api/no-strike"),
                         ("POST", "/api/no-strike"),
                         ("PUT", "/api/no-strike/{entry_id}"),
                         ("DELETE", "/api/no-strike/{entry_id}")]:
        assert srv.require_commander in _route_dep_calls(path, method), \
            f"{method} {path} must be gated by require_commander"


def test_require_commander_refuses_operator():
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


def test_require_commander_accepts_commander():
    assert asyncio.run(srv.require_commander(user=COMMANDER)) is COMMANDER


# --------------------------------------------------------------------------
# POST /no-strike — commander create + persistence + hard default + audit
# --------------------------------------------------------------------------
def test_commander_create_persists_and_returns(fake_env):
    db, _events = fake_env
    body = srv.NoStrikeBody(
        category="CIVILIAN_INFRASTRUCTURE",
        match=srv.NoStrikeMatch(ssid_prefix="BSNL"),
        label="ISP BSNL")
    entry = asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert entry["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert entry["label"] == "ISP BSNL"
    assert entry["match"]["ssid_prefix"] == "BSNL"
    assert entry["enabled"] is True
    assert entry["created_by"] == COMMANDER["email"]
    assert "_id" not in entry
    assert len(db.no_strike_registry.docs) == 1


def test_hard_defaults_true_for_civilian_false_otherwise(fake_env):
    civ = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="CIVILIAN_INFRASTRUCTURE",
                         match=srv.NoStrikeMatch(ssid_prefix="BSNL"),
                         label="civ"),
        user=COMMANDER))
    neutral = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL",
                         match=srv.NoStrikeMatch(ssid_prefix="Cafe"),
                         label="neu"),
        user=COMMANDER))
    explicit = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL", hard=True,
                         match=srv.NoStrikeMatch(ssid_prefix="Gov"),
                         label="exp"),
        user=COMMANDER))
    assert civ["hard"] is True
    assert neutral["hard"] is False
    assert explicit["hard"] is True  # explicit override wins


def test_create_logs_audit(fake_env):
    _db, events = fake_env
    entry = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="FRIENDLY_OWN_FORCE",
                         match=srv.NoStrikeMatch(ssid_prefix="Zetta"),
                         label="own"),
        user=COMMANDER))
    creates = [e for e in events if e["kind"] == "NO_STRIKE_CREATE"]
    assert len(creates) == 1
    assert creates[0]["actor"] == COMMANDER["email"]
    assert creates[0]["meta"]["entry_id"] == entry["id"]
    assert creates[0]["meta"]["category"] == "FRIENDLY_OWN_FORCE"


def test_create_bumps_version_and_hotload_reflects_without_restart(fake_env):
    _db, _events = fake_env
    # cold cache: no entries
    assert asyncio.run(srv._no_strike_entries()) == []
    before = srv._no_strike_version
    entry = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="CIVILIAN_INFRASTRUCTURE",
                         match=srv.NoStrikeMatch(bssid="A8:BB:CC:DD:EE:FF"),
                         label="ap"),
        user=COMMANDER))
    assert srv._no_strike_version == before + 1
    # the hot-load accessor now returns the new entry — no process restart
    entries = asyncio.run(srv._no_strike_entries())
    assert [e["id"] for e in entries] == [entry["id"]]
    # and the pure matcher can consult it end-to-end
    verdict = no_strike.match({"bssid": "A8:BB:CC:DD:EE:FF"}, entries)
    assert verdict["matched"] is True
    assert verdict["entry_id"] == entry["id"]


def test_create_touches_no_tx_spine(fake_env):
    halted_before = srv._tx_halted
    tokens_before = len(srv._arm_tokens)
    asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="CIVILIAN_INFRASTRUCTURE",
                         match=srv.NoStrikeMatch(ssid_prefix="BSNL"),
                         label="x"),
        user=COMMANDER))
    assert srv._tx_halted is halted_before
    assert len(srv._arm_tokens) == tokens_before


# --------------------------------------------------------------------------
# Invalid match block -> 422
# --------------------------------------------------------------------------
def test_empty_match_block_rejected_422(fake_env):
    body = srv.NoStrikeBody(category="NEUTRAL",
                            match=srv.NoStrikeMatch(), label="empty")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_bad_regex_rejected_422(fake_env):
    body = srv.NoStrikeBody(category="NEUTRAL",
                            match=srv.NoStrikeMatch(vendor_regex="([a-z"),
                            label="badre")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_bad_mac_rejected_422(fake_env):
    body = srv.NoStrikeBody(category="CIVILIAN_INFRASTRUCTURE",
                            match=srv.NoStrikeMatch(bssid="not-a-mac"),
                            label="badmac")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert ei.value.status_code == 422


def test_bogus_category_rejected_by_model():
    with pytest.raises(Exception):
        srv.NoStrikeBody(category="NOPE",
                         match=srv.NoStrikeMatch(ssid_prefix="x"), label="l")


# --------------------------------------------------------------------------
# GET /no-strike
# --------------------------------------------------------------------------
def test_list_returns_created(fake_env):
    created = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL",
                         match=srv.NoStrikeMatch(ssid_prefix="Cafe"),
                         label="cafe"),
        user=COMMANDER))
    listing = asyncio.run(srv.list_no_strike(user=COMMANDER))
    assert listing["count"] == 1
    assert listing["entries"][0]["id"] == created["id"]


# --------------------------------------------------------------------------
# PUT /no-strike/{id}
# --------------------------------------------------------------------------
def test_commander_update_edits_and_audits(fake_env):
    _db, events = fake_env
    created = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL",
                         match=srv.NoStrikeMatch(ssid_prefix="Cafe"),
                         label="cafe"),
        user=COMMANDER))
    updated = asyncio.run(srv.update_no_strike(
        created["id"],
        srv.NoStrikeUpdateBody(label="Cafe-2", enabled=False),
        user=COMMANDER))
    assert updated["label"] == "Cafe-2"
    assert updated["enabled"] is False
    assert updated["category"] == "NEUTRAL"  # untouched preserved
    assert any(e["kind"] == "NO_STRIKE_UPDATE" for e in events)


def test_update_missing_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_no_strike(
            "no-such-id", srv.NoStrikeUpdateBody(label="Z"), user=COMMANDER))
    assert ei.value.status_code == 404


def test_update_invalid_match_rejected_422(fake_env):
    created = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL",
                         match=srv.NoStrikeMatch(ssid_prefix="Cafe"),
                         label="cafe"),
        user=COMMANDER))
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.update_no_strike(
            created["id"],
            srv.NoStrikeUpdateBody(match=srv.NoStrikeMatch(bssid="xx")),
            user=COMMANDER))
    assert ei.value.status_code == 422


def test_update_category_to_civilian_restores_hard_default(fake_env):
    created = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="NEUTRAL",
                         match=srv.NoStrikeMatch(ssid_prefix="Cafe"),
                         label="cafe"),
        user=COMMANDER))
    assert created["hard"] is False
    updated = asyncio.run(srv.update_no_strike(
        created["id"],
        srv.NoStrikeUpdateBody(category="CIVILIAN_INFRASTRUCTURE"),
        user=COMMANDER))
    assert updated["hard"] is True  # a civilian entry cannot silently lose hard


# --------------------------------------------------------------------------
# DELETE /no-strike/{id} — DISABLES, never removes
# --------------------------------------------------------------------------
def test_delete_disables_not_removes(fake_env):
    db, events = fake_env
    created = asyncio.run(srv.create_no_strike(
        srv.NoStrikeBody(category="CIVILIAN_INFRASTRUCTURE",
                         match=srv.NoStrikeMatch(ssid_prefix="BSNL"),
                         label="bsnl"),
        user=COMMANDER))
    res = asyncio.run(srv.delete_no_strike(created["id"], user=COMMANDER))
    assert res["disabled"] is True
    assert res["id"] == created["id"]
    # STILL present in the store (not hard-deleted) but disabled
    assert len(db.no_strike_registry.docs) == 1
    assert db.no_strike_registry.docs[0]["enabled"] is False
    assert any(e["kind"] == "NO_STRIKE_DISABLE" for e in events)
    # dropped from the hot-load cache (enabled:false filtered out)
    assert asyncio.run(srv._no_strike_entries()) == []


def test_delete_missing_404(fake_env):
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.delete_no_strike("no-such-id", user=COMMANDER))
    assert ei.value.status_code == 404
