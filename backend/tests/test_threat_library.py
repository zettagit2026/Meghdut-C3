"""Pure unit tests for backend/threat_library.py -- the RFI Sec 4.2.12 inbuilt
threat library + matching/classification engine + offline/online import-merge.

No FastAPI, no Mongo, no network: imports the pure module directly (same
convention as test_protocol_status.py) so the confidence tiers, the
no-fabricated-exact-model guarantee, the schema validation, and the versioned
merge are all testable in-process.
"""
import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import threat_library as tl  # noqa: E402


@pytest.fixture()
def lib():
    # Always the seed (never a stray persisted active library) for determinism.
    return tl.load_library(tl.DEFAULT_LIBRARY_PATH)


# --------------------------------------------------------------------------
# Seed library integrity
# --------------------------------------------------------------------------
def test_seed_loads_and_validates(lib):
    tl.validate_library(lib)  # must not raise
    assert lib["schema"] == tl.SCHEMA_ID
    assert isinstance(lib["version"], str)
    assert len(lib["entries"]) >= 15


def test_seed_entries_are_well_formed(lib):
    ids = set()
    for e in lib["entries"]:
        for k in ("id", "make", "model", "class", "threat_level"):
            assert e.get(k), f"{e.get('id')} missing {k}"
        assert e["class"] in tl.CLASS_VOCAB
        assert e["threat_level"] in tl.THREAT_LEVELS
        assert e["id"] not in ids, "duplicate id"
        ids.add(e["id"])
        # honesty: every entry must carry a data_source provenance tag
        assert e.get("data_source"), f"{e['id']} missing data_source provenance"


def test_military_entries_are_class_level_placeholders(lib):
    mil = [e for e in lib["entries"] if e["class"] in
           ("loitering-munition-class", "ISR-class", "VTOL")]
    assert mil, "expected at least one military-class placeholder"
    for e in mil:
        # class-level placeholder: NO fabricated specific emitter parameters
        sig = e["signatures"]
        assert sig.get("control_band_centers_ghz") is None
        assert sig.get("occupied_bw_mhz_range") is None
        assert e["data_source"] == "class-level-open-source"


# --------------------------------------------------------------------------
# Tier 1 -- broadcast decode -> EXACT make/model/serial, HIGH
# --------------------------------------------------------------------------
def test_droneid_broadcast_exact_high(lib):
    r = tl.match_detection({"droneid": {"make": "DJI", "model": "Mavic 3"}}, lib)
    assert r["match_basis"] == tl.BASIS_BROADCAST
    assert r["confidence"] == tl.CONF_HIGH
    assert r["exact_model_confirmed"] is True
    assert r["best"]["make"] == "DJI"
    assert "Mavic 3" in r["best"]["model"]
    assert r["threat_level"] == "HIGH"


def test_droneid_serial_is_carried(lib):
    r = tl.match_detection(
        {"droneid": {"make": "DJI", "model": "Air 3", "serial": "SN-ABC-999"}}, lib)
    assert r["best"]["exact_model_confirmed"] is True
    assert r["best"]["serial"] == "SN-ABC-999"


def test_remoteid_serial_broadcast_exact_high(lib):
    r = tl.match_detection(
        {"remoteid": {"uas_id": "1581F5FKD230100XXXXX", "make": "DJI"}}, lib)
    assert r["match_basis"] == tl.BASIS_BROADCAST
    assert r["confidence"] == tl.CONF_HIGH
    assert r["exact_model_confirmed"] is True
    assert r["best"]["serial"] == "1581F5FKD230100XXXXX"


# --------------------------------------------------------------------------
# Tier 2 -- distinctive Wi-Fi identity -> MEDIUM (spoofable, not exact)
# --------------------------------------------------------------------------
def test_wifi_ssid_identity_medium(lib):
    r = tl.match_detection({"wifi": {"ssid": "ANAFI-654321"}}, lib)
    assert r["match_basis"] == tl.BASIS_WIFI_IDENTITY
    assert r["confidence"] == tl.CONF_MEDIUM
    assert r["best"]["make"] == "Parrot"
    # a beacon is spoofable -> never promoted to exact-confirmed
    assert r["exact_model_confirmed"] is False


# --------------------------------------------------------------------------
# Tier 3 -- RF-signature-only -> ranked CLASS/FAMILY candidates, NEVER exact
# --------------------------------------------------------------------------
def test_rf_signature_returns_class_candidates_never_exact(lib):
    r = tl.match_detection(
        {"bands": ["2.4GHz", "5.8GHz"], "occupied_bw_mhz": 20,
         "control_link_family": "DJI OcuSync", "fhss": True}, lib)
    assert r["match_basis"] == tl.BASIS_RF_SIGNATURE
    assert r["confidence"] in (tl.CONF_LOW, tl.CONF_MEDIUM)
    assert r["confidence"] != tl.CONF_HIGH
    assert len(r["candidates"]) >= 1
    # THE core honesty guarantee: NO signature-only candidate may claim an exact
    # model, and every one must be flagged class/family level.
    for c in r["candidates"]:
        assert c["exact_model_confirmed"] is False
        assert c["identification_level"] == "class_family"
        assert c["match_basis"] == tl.BASIS_RF_SIGNATURE


def test_rf_signature_analog_fpv_family(lib):
    r = tl.match_detection(
        {"bands": ["5.8GHz"], "video_type": "analog",
         "control_link_family": "Analog-FPV"}, lib)
    assert r["match_basis"] == tl.BASIS_RF_SIGNATURE
    assert r["exact_model_confirmed"] is False
    assert "FPV" in r["best"]["model"] or r["best"]["class"] == "FPV"


def test_rf_signature_ambiguous_ocusync_stays_low(lib):
    # Multiple DJI models share the OcuSync 2.4/5.8 signature -> the engine must
    # NOT pick a confident single model; it stays LOW with several candidates.
    r = tl.match_detection(
        {"bands": ["2.4GHz", "5.8GHz"], "control_link_family": "DJI OcuSync"}, lib)
    dji = [c for c in r["candidates"] if c["make"] == "DJI"]
    assert len(dji) >= 2
    assert r["confidence"] == tl.CONF_LOW


# --------------------------------------------------------------------------
# Tier 4 -- honest fail (feeds RFI 4.2.10 low-false-alarm)
# --------------------------------------------------------------------------
def test_unknown_when_no_signature(lib):
    r = tl.match_detection({}, lib)
    assert r["match_basis"] == tl.BASIS_NONE
    assert r["confidence"] is None
    assert r["threat_level"] == "UNKNOWN"
    assert r["candidates"] == []
    assert r["best"] is None


def test_unmatched_band_is_honest_fail_not_guess(lib):
    # A band with nothing in the library (and no family/bw) must not fabricate.
    r = tl.match_detection({"band": "1.2GHz"}, lib)
    assert r["match_basis"] == tl.BASIS_NONE
    assert r["best"] is None


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def test_classify_threat_level_from_best(lib):
    r = tl.match_detection({"droneid": {"make": "DJI", "model": "Mini 4 Pro"}}, lib)
    assert r["threat_level"] == "MEDIUM"  # sub-250g class
    assert tl.classify_threat_level(None) == "UNKNOWN"


# --------------------------------------------------------------------------
# Import / merge / versioning (Sec 4.2.12.3 / 4.2.12.4)
# --------------------------------------------------------------------------
def test_import_adds_new_entry_and_bumps_revision(lib):
    incoming = {
        "schema": tl.SCHEMA_ID,
        "version": "1.1.0",
        "source": "test-import",
        "entries": [{
            "id": "test-new-quad", "make": "TestCorp", "model": "TQ-1",
            "class": "multirotor", "threat_level": "LOW", "data_source": "public-spec",
        }],
    }
    merged, audit = tl.merge_library(lib, incoming)
    assert audit["added"] == ["test-new-quad"]
    assert audit["updated"] == []
    assert merged["revision"] == lib["revision"] + 1
    assert merged["version"] == "1.1.0"
    assert tl._lookup_by_id(merged, "test-new-quad") is not None
    # base is not mutated
    assert tl._lookup_by_id(lib, "test-new-quad") is None


def test_import_updates_existing_entry(lib):
    existing = copy.deepcopy(tl._lookup_by_id(lib, "dji-mavic-3"))
    existing["threat_level"] = "CRITICAL"
    incoming = {"schema": tl.SCHEMA_ID, "entries": [existing]}
    merged, audit = tl.merge_library(lib, incoming)
    assert audit["updated"] == ["dji-mavic-3"]
    assert audit["added"] == []
    assert tl._lookup_by_id(merged, "dji-mavic-3")["threat_level"] == "CRITICAL"


def test_import_records_history_audit(lib):
    incoming = {"schema": tl.SCHEMA_ID, "entries": [{
        "id": "hist-1", "make": "X", "model": "Y", "class": "FPV",
        "threat_level": "HIGH", "data_source": "public-spec"}]}
    merged, _ = tl.merge_library(lib, incoming)
    assert len(merged["import_history"]) == len(lib.get("import_history", [])) + 1
    last = merged["import_history"][-1]
    assert last["added_count"] == 1 and last["to_revision"] == merged["revision"]


def test_import_rejects_malformed():
    with pytest.raises(tl.ThreatLibraryError):
        tl.validate_library({"entries": "not-a-list"})
    with pytest.raises(tl.ThreatLibraryError):
        tl.validate_library({"entries": [{"id": "x"}]})  # missing required fields
    with pytest.raises(tl.ThreatLibraryError):
        tl.validate_library({"entries": [{
            "id": "x", "make": "m", "model": "n",
            "class": "not-a-real-class", "threat_level": "HIGH"}]})
    with pytest.raises(tl.ThreatLibraryError):
        tl.validate_library({"entries": [{
            "id": "x", "make": "m", "model": "n",
            "class": "FPV", "threat_level": "SEVERE"}]})  # bad level


def test_import_rejects_duplicate_ids():
    dup = {"entries": [
        {"id": "d", "make": "a", "model": "b", "class": "FPV", "threat_level": "LOW"},
        {"id": "d", "make": "a", "model": "b", "class": "FPV", "threat_level": "LOW"},
    ]}
    with pytest.raises(tl.ThreatLibraryError):
        tl.validate_library(dup)


def test_merge_rejects_malformed_incoming(lib):
    with pytest.raises(tl.ThreatLibraryError):
        tl.merge_library(lib, {"entries": [{"id": "bad"}]})


def test_summary_shape(lib):
    s = tl.library_summary(lib)
    assert s["entry_count"] == len(lib["entries"])
    assert s["version"] == lib["version"]
    assert sum(s["by_class"].values()) == s["entry_count"]
    assert sum(s["by_threat_level"].values()) == s["entry_count"]
