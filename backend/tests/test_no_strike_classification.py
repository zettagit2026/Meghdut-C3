"""Unit tests for the P2 CLASSIFICATION + DISPLAY-HONESTY consult
(no-strike-registry.md §2A/§3/§5): the pure helpers `is_target_grade` and
`_no_strike_confidence_stamps` in backend/server.py.

These are TRUE unit tests -- the two helpers are pure functions of a resolved
detection dict + the registry entries, so nothing here touches Mongo, the TX
spine, websockets, or a running server (same in-process style as
test_no_strike.py: importing server.py only needs the env vars set).

What is covered (the honesty invariants):
  * a civilian OUI known ONLY via the Wi-Fi fusion cross-ref (an RF-energy
    contact carries no OUI of its own) -> NON_THREAT + not target-grade +
    "Protected"/"Civilian infrastructure" display;
  * a direct civilian SSID match -> NON_THREAT;
  * a purely-heuristic no-DF contact -> NOT target-grade, capped LOW, honest
    "Unidentified RF emitter" label, ML "%" badged as a class-probability;
  * a DroneID-DECODED drone that ALSO matches a no-strike OUI -> CONFLICT:
    target-grade, NOT demoted, stamped `no_strike_conflict`, never `no_strike`
    (so the board keeps it on priority behind an Adjudicate badge);
  * `match_model`/`match_protocol` are NEVER touched on any path;
  * a friendly-own-force match labels "FRIENDLY (registry)" but defers to an
    existing IFF relabel;
  * the four target-grade corroboration tiers (T1..T4).

Run: pytest backend/tests/test_no_strike_classification.py -v
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


def _entry(eid, category="CIVILIAN_INFRASTRUCTURE", hard=True, label=None, **match):
    return {"id": eid, "category": category, "hard": hard,
            "label": label or eid, "match": match}


# A globally-administered consumer OUI (A8 -> 0x02 bit clear, so NOT randomized)
# and an ISP SSID prefix. Realistic values mirror test_no_strike.py's fixtures;
# the SHIPPED seed uses TEST-Org names, this is a test fixture only.
CIVILIAN_ENTRIES = [
    _entry("civ-oui", category="CIVILIAN_INFRASTRUCTURE", oui="A8:BB:CC",
           label="Consumer device (test)"),
    _entry("civ-ssid", category="CIVILIAN_INFRASTRUCTURE", ssid_prefix="BSNL",
           label="ISP BSNL (test)"),
    _entry("friendly", category="FRIENDLY_OWN_FORCE", hard=False,
           ssid_prefix="Zetta", label="Own force Zetta (test)"),
]


# ==========================================================================
# is_target_grade — the four corroboration tiers
# ==========================================================================
def test_target_grade_t1_protocol_decode():
    assert srv.is_target_grade({"protocol_confirmed": True}) is True
    assert srv.is_target_grade({"confidence_type": "protocol_verified"}) is True


def test_target_grade_t2_real_df_bearing():
    assert srv.is_target_grade(
        {"bearing_available": True, "bearing_estimated": False}) is True
    # an ESTIMATED bearing is not a real DF fix
    assert srv.is_target_grade(
        {"bearing_available": True, "bearing_estimated": True}) is False


def test_target_grade_t4_multidomain_fused():
    assert srv.is_target_grade({"confidence_type": "multidomain_fused"}) is True


def test_target_grade_t3_wifi_drone_softap_non_civilian():
    # classified wifi drone candidate (make_candidate + match_protocol wifi)
    assert srv.is_target_grade(
        {"match_protocol": "wifi", "make_candidate": "DJI FLIP (candidate)"}) is True
    # ...but NOT if it is itself a civilian no-strike match
    assert srv.is_target_grade({
        "match_protocol": "wifi", "make_candidate": "x",
        "no_strike": {"matched": True, "category": "CIVILIAN_INFRASTRUCTURE"},
    }) is False


def test_not_target_grade_bare_heuristic():
    assert srv.is_target_grade(
        {"confidence_type": "heuristic_binary", "bearing_available": False}) is False
    assert srv.is_target_grade({}) is False
    assert srv.is_target_grade("nope") is False


# ==========================================================================
# _no_strike_confidence_stamps — the consult (civilian / conflict / low-conf)
# ==========================================================================
def test_civilian_oui_via_fusion_demotes_to_non_threat():
    # An RF-energy contact: no ssid/oui/bssid of its own -- the civilian OUI is
    # known ONLY through the Kismet Wi-Fi fusion cross-ref.
    det = {
        "model": "DJI Mini (candidate)", "protocol": "2.4GHz",
        "match_model": "DJI Mini (candidate)", "match_protocol": "2.4GHz",
        "threat_level": "MEDIUM",
        "confidence_type": "wifi_attributed",
        "protocol_confirmed": False,
        "bearing_available": False,
        "wifi_fusion": {"matched_mac_oui": "A8:BB:CC", "matched_manuf": "Consumer",
                        "matched_ssid": "HomeAP"},
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["threat_level"] == "NON_THREAT"
    assert stamps["no_strike"]["matched"] is True
    assert stamps["no_strike"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert stamps["no_strike"]["entry_id"] == "civ-oui"
    assert stamps["target_grade"] is False
    assert stamps["model"].startswith("Protected —")
    assert stamps["protocol"] == "Civilian infrastructure"
    # never touch the immutable merge key
    assert "match_model" not in stamps and "match_protocol" not in stamps
    # NON_THREAT is OUTSIDE the hostile set -> ROE floor already fails closed
    assert "NON_THREAT" not in srv._HOSTILE_THREAT_LEVELS


def test_civilian_ssid_direct_match_demotes():
    det = {
        "model": "Unidentified 2.4GHz Emitter", "protocol": "2.4GHz",
        "match_model": "raw", "match_protocol": "2.4GHz",
        "threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
        "ssid": "BSNL_HOME_5G",
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["threat_level"] == "NON_THREAT"
    assert stamps["no_strike"]["entry_id"] == "civ-ssid"
    assert stamps["target_grade"] is False


def test_heuristic_no_df_not_target_grade_honest_label():
    det = {
        "model": "DJI Mini (candidate)", "protocol": "2.4GHz",
        "match_model": "DJI Mini (candidate)", "match_protocol": "2.4GHz",
        "threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
        "protocol_confirmed": False, "bearing_available": False,
        "ml_confidence": 0.76, "ml_label": "drone",
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["target_grade"] is False
    assert stamps["threat_level"] == "LOW"          # capped, never above LOW
    assert stamps["model"] == "Unidentified RF emitter — unconfirmed, no bearing"
    assert stamps["ml_probability_note"] == "ML class probability (no reject class)"
    assert "no_strike" not in stamps               # not a protected contact
    assert "no_strike_conflict" not in stamps
    assert "match_model" not in stamps


def test_droneid_decoded_conflict_not_demoted():
    # A CRC-verified DroneID decode whose OUI ALSO matches a civilian no-strike
    # entry. This is the honesty-critical case: NEVER demote/suppress a decoded
    # drone -- stamp a conflict for adjudication and keep it target-grade.
    det = {
        "model": "DJI DroneID (Mavic 3)", "protocol": "droneid",
        "match_model": "DJI DroneID (Mavic 3)", "match_protocol": "droneid",
        "threat_level": "HIGH", "confidence_type": "protocol_verified",
        "protocol_confirmed": True,
        "bssid": "A8:BB:CC:DD:EE:FF",   # OUI A8:BB:CC == the civilian entry
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike_conflict"]["registry_entry_id"] == "civ-oui"
    assert stamps["no_strike_conflict"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert stamps["no_strike_conflict"]["drone_basis"] == "protocol_verified"
    assert stamps["target_grade"] is True
    # NOT demoted: threat_level untouched, no demoting no_strike stamp, model kept
    assert "threat_level" not in stamps
    assert "no_strike" not in stamps
    assert "model" not in stamps
    # immutable merge key preserved
    assert "match_model" not in stamps and "match_protocol" not in stamps


def test_conflict_via_protocol_confirmed_flag_only():
    # protocol_confirmed True without confidence_type protocol_verified still
    # counts as a decode -> conflict, drone_basis names the flag.
    det = {
        "threat_level": "MEDIUM", "protocol_confirmed": True,
        "match_model": "m", "match_protocol": "wifi",
        "oui": "A8:BB:CC",
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike_conflict"]["drone_basis"] == "protocol_confirmed"
    assert "threat_level" not in stamps
    assert stamps["target_grade"] is True


def test_friendly_registry_labels_when_no_iff():
    det = {"ssid": "Zetta_Cmd_Post", "threat_level": "MEDIUM",
           "match_model": "m", "match_protocol": "wifi"}
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike"]["category"] == "FRIENDLY_OWN_FORCE"
    assert stamps["threat_level"] == "FRIENDLY (registry)"


def test_friendly_defers_to_existing_iff_relabel():
    det = {"ssid": "Zetta_Cmd_Post", "threat_level": "FRIENDLY (IFF verified)",
           "iff_verified": True, "match_model": "m", "match_protocol": "wifi"}
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike"]["category"] == "FRIENDLY_OWN_FORCE"
    # defers -- does NOT overwrite the IFF relabel
    assert "threat_level" not in stamps


def test_multidomain_fused_not_demoted_no_match():
    det = {
        "model": "DJI OcuSync", "protocol": "ocusync",
        "match_model": "DJI OcuSync", "match_protocol": "ocusync",
        "threat_level": "HIGH", "confidence_type": "multidomain_fused",
        "wifi_fusion": {"matched_manuf": "DJI"},
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["target_grade"] is True
    assert "threat_level" not in stamps          # not demoted
    assert "model" not in stamps                 # honest label not applied
    assert "no_strike" not in stamps


def test_wifi_drone_candidate_is_target_grade_not_demoted():
    # The _upsert_wifi_drone_detection shape: classified candidate, drone OUI,
    # non-civilian -> T3 target-grade, untouched.
    det = {
        "model": "Autel (candidate)", "protocol": "wifi",
        "match_model": "Autel (candidate)", "match_protocol": "wifi",
        "make_candidate": "Autel (candidate)", "match_basis": "oui",
        "threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
        "oui": "60:60:1F",   # a drone OUI, not in CIVILIAN_ENTRIES
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["target_grade"] is True
    assert "threat_level" not in stamps
    assert "no_strike" not in stamps
    assert "no_strike_conflict" not in stamps


def test_no_entries_is_a_plain_low_confidence_result():
    det = {"threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
           "match_model": "m", "match_protocol": "2.4GHz",
           "bearing_available": False}
    stamps = srv._no_strike_confidence_stamps(det, [])
    assert stamps["target_grade"] is False
    assert stamps["threat_level"] == "LOW"
    assert "no_strike" not in stamps


# ==========================================================================
# FIX A — ReDoS guard on vendor_regex (write-time reject + load-time precompile)
# No third-party dependency (sovereign/offline appliance); a catastrophic
# pattern must never reach the single-threaded event loop's matcher.
# ==========================================================================
def test_has_catastrophic_backtracking_detects_nested_quantifiers():
    for bad in ("(a+)+$", "(a*)*", "(a+)*", "(a*)+", "(.*)+", "(ab+)+",
                "((a+)+)+", "(a{1,9})+", ".*.*", ".+.+", "a(b|c+)+d"):
        assert no_strike.has_catastrophic_backtracking(bad) is True, bad


def test_has_catastrophic_backtracking_allows_normal_patterns():
    for ok in ("DJI", "Apple Inc", "DJI|Autel|Parrot", "Samsung.*Electronics",
               "^BSNL", "a+b+", "(abc)+", "(ab|cd)"):
        assert no_strike.has_catastrophic_backtracking(ok) is False, ok


def test_validate_match_rejects_catastrophic_vendor_regex():
    ok, reason = no_strike.validate_match({"vendor_regex": "(a+)+$"})
    assert ok is False
    assert "backtracking" in reason.lower()


def test_validate_match_rejects_overlength_vendor_regex():
    # over the SHORT 64-char cap -> rejected with a clear message (independent of
    # the pydantic Field bound, so any caller is covered).
    ok, reason = no_strike.validate_match({"vendor_regex": "A" * 100})
    assert ok is False
    assert "64" in reason


def test_validate_match_accepts_normal_vendor_regex():
    assert no_strike.validate_match({"vendor_regex": "DJI|Autel|Parrot"})[0] is True


def test_compile_vendor_regex_returns_pattern_or_none():
    import re as _re
    good = no_strike.compile_vendor_regex("Samsung")
    assert isinstance(good, _re.Pattern)
    assert good.search("Samsung Electronics")
    # a catastrophic shape, an over-length pattern, and a broken pattern all
    # compile to None (skipped by the matcher, never run).
    assert no_strike.compile_vendor_regex("(a+)+$") is None
    assert no_strike.compile_vendor_regex("A" * 100) is None
    assert no_strike.compile_vendor_regex("([a-z") is None
    assert no_strike.compile_vendor_regex("") is None


def test_matcher_uses_precompiled_pattern_when_present():
    # An entry carrying a precompiled pattern is matched via it (hot path never
    # re-compiles); a precompiled None (broken/dangerous at load) is skipped.
    import re as _re
    entries = [
        {"id": "skip", "category": "NEUTRAL", "label": "skip",
         "match": {"vendor_regex": "(a+)+$",
                   "_vendor_regex_compiled": None}},
        {"id": "hit", "category": "NEUTRAL", "label": "hit",
         "match": {"vendor_regex": "Samsung",
                   "_vendor_regex_compiled": _re.compile("Samsung")}},
    ]
    res = no_strike.match({"manuf": "Samsung Electronics"}, entries)
    assert res["matched"] is True
    assert res["entry_id"] == "hit"


# --- endpoint + hot-load-cache tests (minimal in-memory fake Mongo) ----------
class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *_a, **_k):
        return self

    async def to_list(self, _n):
        return [dict(d) for d in self._docs]


class _FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    @staticmethod
    def _strip(doc, projection):
        d = dict(doc)
        if projection and projection.get("_id") == 0:
            d.pop("_id", None)
        return d

    def _match(self, doc, flt):
        return all(doc.get(k) == v for k, v in (flt or {}).items())

    def find(self, flt=None, projection=None):
        return _FakeCursor(
            [self._strip(d, projection) for d in self.docs if self._match(d, flt)])

    async def find_one(self, flt=None, projection=None):
        for d in self.docs:
            if self._match(d, flt):
                return self._strip(d, projection)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class _FakeDB:
    def __init__(self, docs=None):
        self.no_strike_registry = _FakeCollection(docs)


@pytest.fixture
def fake_env(monkeypatch):
    db = _FakeDB()

    async def _log(*_a, **_k):
        return {}

    monkeypatch.setattr(srv, "db", db)
    monkeypatch.setattr(srv, "log_event", _log)
    monkeypatch.setattr(srv, "_no_strike_version", 0)
    monkeypatch.setattr(srv, "_no_strike_cache", {"version": None, "entries": []})
    return db


def test_create_rejects_catastrophic_vendor_regex_422(fake_env):
    body = srv.NoStrikeBody(
        category="NEUTRAL",
        match=srv.NoStrikeMatch(vendor_regex="(a+)+$"),
        label="redos")
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert ei.value.status_code == 422
    # nothing persisted
    assert len(fake_env.no_strike_registry.docs) == 0


def test_overlength_vendor_regex_rejected_at_post(fake_env):
    # the SHORT cap is enforced at the model bound (a real request -> 422); a
    # 100-char pattern can never be constructed into a body.
    with pytest.raises(Exception):
        srv.NoStrikeMatch(vendor_regex="A" * 100)


def test_create_accepts_normal_vendor_regex_and_persists(fake_env):
    body = srv.NoStrikeBody(
        category="NEUTRAL",
        match=srv.NoStrikeMatch(vendor_regex="DJI|Autel"),
        label="drone vendors")
    entry = asyncio.run(srv.create_no_strike(body, user=COMMANDER))
    assert entry["match"]["vendor_regex"] == "DJI|Autel"
    assert len(fake_env.no_strike_registry.docs) == 1


def test_hotload_precompiles_vendor_regex_and_matches(monkeypatch):
    # A stored good pattern is precompiled ONCE at cache-load; a stored dangerous
    # pattern (a legacy row that predates the write-time guard) is precompiled to
    # None so the matcher skips it and can never run it.
    import re as _re
    seed = [
        {"id": "good", "category": "NEUTRAL", "label": "good", "enabled": True,
         "match": {"vendor_regex": "Samsung"}},
        {"id": "legacy-redos", "category": "NEUTRAL", "label": "legacy",
         "enabled": True, "match": {"vendor_regex": "(a+)+$"}},
    ]
    db = _FakeDB(seed)
    monkeypatch.setattr(srv, "db", db)
    monkeypatch.setattr(srv, "_no_strike_version", 1)          # force a reload
    monkeypatch.setattr(srv, "_no_strike_cache", {"version": None, "entries": []})

    entries = asyncio.run(srv._no_strike_entries())
    by_id = {e["id"]: e for e in entries}
    assert isinstance(by_id["good"]["match"]["_vendor_regex_compiled"], _re.Pattern)
    assert by_id["legacy-redos"]["match"]["_vendor_regex_compiled"] is None

    # end-to-end: the good pattern matches via its precompiled object; the
    # dangerous one is skipped (no match, no hang).
    assert no_strike.match({"manuf": "Samsung Galaxy"}, entries)["entry_id"] == "good"
    assert no_strike.match({"manuf": "aaaaaaaaaaaaaaaaaaaaX"}, entries) == {"matched": False}


# ==========================================================================
# FIX B — widened conflict net: a target-grade (T1..T4) contact matching a
# CIVILIAN/NEUTRAL entry is a CONFLICT (stays on the board behind Adjudicate),
# NEVER laundered off to NON_THREAT. Only a NON-target-grade civilian match is
# demoted. is_target_grade is computed on the ORIGINAL corroboration, pre-demote.
# ==========================================================================
def test_t2_real_df_civilian_match_is_conflict_not_demoted():
    # A real (measured) DF bearing on a contact whose OUI ALSO matches a civilian
    # entry -- the operator's core "don't hide my target" case.
    det = {
        "model": "Unknown UAS", "protocol": "2.4GHz",
        "match_model": "Unknown UAS", "match_protocol": "2.4GHz",
        "threat_level": "HIGH",
        "protocol_confirmed": False,
        "bearing_available": True, "bearing_estimated": False,   # T2
        "oui": "A8:BB:CC",   # == the civilian entry
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike_conflict"]["registry_entry_id"] == "civ-oui"
    assert stamps["no_strike_conflict"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert stamps["no_strike_conflict"]["drone_basis"] == "df_bearing"
    assert stamps["target_grade"] is True
    # NOT demoted / not laundered off the board
    assert "threat_level" not in stamps
    assert "no_strike" not in stamps
    assert "model" not in stamps
    assert "match_model" not in stamps and "match_protocol" not in stamps


def test_t4_multidomain_fused_civilian_match_is_conflict():
    det = {
        "model": "DJI OcuSync", "protocol": "ocusync",
        "match_model": "DJI OcuSync", "match_protocol": "ocusync",
        "threat_level": "HIGH", "confidence_type": "multidomain_fused",  # T4
        "protocol_confirmed": False,
        "wifi_fusion": {"matched_mac_oui": "A8:BB:CC", "matched_manuf": "Consumer"},
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike_conflict"]["drone_basis"] == "multidomain_fused"
    assert stamps["target_grade"] is True
    assert "threat_level" not in stamps
    assert "no_strike" not in stamps


def test_heuristic_no_df_civilian_still_demoted_regression():
    # A genuine low-confidence civilian RF emitter (no corroboration tier) is
    # STILL demoted to NON_THREAT -- unchanged behaviour.
    det = {
        "model": "DJI Mini (candidate)", "protocol": "2.4GHz",
        "match_model": "DJI Mini (candidate)", "match_protocol": "2.4GHz",
        "threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
        "protocol_confirmed": False, "bearing_available": False,
        "wifi_fusion": {"matched_mac_oui": "A8:BB:CC", "matched_manuf": "Consumer"},
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["threat_level"] == "NON_THREAT"
    assert stamps["no_strike"]["category"] == "CIVILIAN_INFRASTRUCTURE"
    assert stamps["target_grade"] is False
    assert "no_strike_conflict" not in stamps


def test_protocol_decoded_civilian_still_conflict_regression():
    det = {
        "model": "DJI DroneID (Mavic 3)", "protocol": "droneid",
        "match_model": "DJI DroneID (Mavic 3)", "match_protocol": "droneid",
        "threat_level": "HIGH", "confidence_type": "protocol_verified",  # T1
        "protocol_confirmed": True,
        "bssid": "A8:BB:CC:DD:EE:FF",
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike_conflict"]["drone_basis"] == "protocol_verified"
    assert stamps["target_grade"] is True
    assert "threat_level" not in stamps
    assert "no_strike" not in stamps


def test_wifi_only_civilian_oui_is_demoted_not_conflict():
    # A wifi make_candidate contact whose OUI IS the civilian one is a genuine
    # civilian AP (T3 requires a NON-civilian vendor), so it is demoted, NOT a
    # conflict.
    det = {
        "model": "x (candidate)", "protocol": "wifi",
        "match_model": "x (candidate)", "match_protocol": "wifi",
        "make_candidate": "x (candidate)",
        "threat_level": "MEDIUM", "confidence_type": "heuristic_binary",
        "protocol_confirmed": False, "bearing_available": False,
        "oui": "A8:BB:CC",   # civilian
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["threat_level"] == "NON_THREAT"
    assert stamps["target_grade"] is False
    assert "no_strike_conflict" not in stamps


def test_friendly_decoded_drone_still_relabeled_friendly_regression():
    # FRIENDLY_OWN_FORCE is intentionally the IFF trust model: a contact you
    # registered as own-force is relabeled friendly even when decoded. NOT a bug,
    # and NOT changed by the widened civilian net.
    det = {
        "ssid": "Zetta_Recon_1", "protocol": "droneid",
        "match_model": "own drone", "match_protocol": "droneid",
        "threat_level": "HIGH", "confidence_type": "protocol_verified",
        "protocol_confirmed": True,
    }
    stamps = srv._no_strike_confidence_stamps(det, CIVILIAN_ENTRIES)
    assert stamps["no_strike"]["category"] == "FRIENDLY_OWN_FORCE"
    assert stamps["threat_level"] == "FRIENDLY (registry)"
    assert "no_strike_conflict" not in stamps
