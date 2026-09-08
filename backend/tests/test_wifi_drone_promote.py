"""Unit tests for Wi-Fi drone detection promotion (_upsert_wifi_drone_detection
via POST /api/wifi-drone/ingest).

Closes the Wi-Fi-drone detection loop: a CLASSIFIED Wi-Fi drone candidate (e.g.
the DJI FLOW video softAP) that carries a concrete softAP BSSID must become a
TARGETABLE db.detections contact — one that shows in /detections and is resolvable
by the governed Wi-Fi-defeat flow via its REAL BSSID — with HONEST candidate
confidence (never inflated, never auto-authorized to engage).

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_wifi_defeat_endpoint.py: importing backend/server.py only needs
the env vars SET (motor is lazy). The handler is driven directly with db /
log_event / track-observation monkeypatched, so nothing persists to a real Mongo.

Run: pytest backend/tests/test_wifi_drone_promote.py -v
"""
from __future__ import annotations

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

USER = {"email": "op@unused.local", "role": "operator"}

# The exact payload field-bridge/wifi_drone_bridge.py POSTs for a matched DJI
# FLOW video softAP (SSID "FLOW_261916" on BSSID B4:C2:E0:26:19:16, channel 6).
FLOW_BSSID = "B4:C2:E0:26:19:16"
FLOW_MAKE = "DJI (FLOW video softAP, candidate)"


def _flow_body(**overrides):
    base = dict(
        ssid="FLOW_261916",
        oui="B4:C2:E0",
        manuf="Bouffalo Lab",
        make_candidate=FLOW_MAKE,
        match_basis="ssid",
        channel=6,
        signal_dbm=-52.0,
        source_mac=FLOW_BSSID,
        bssid=FLOW_BSSID,
        source="WIFI_DRONE_KISMET",
        caveats=["SSID and MAC OUI are BOTH spoofable -- candidate, not a serial"],
    )
    base.update(overrides)
    return srv.WifiDroneIngestBody(**base)


class _FakeDetections:
    """Minimal in-memory stand-in for the motor db.detections collection —
    supports the find_one / insert_one / update_one used by the promotion path."""

    def __init__(self):
        self.docs = []  # list of stored documents

    def _match(self, doc, query):
        return all(doc.get(k) == v for k, v in query.items())

    async def find_one(self, query, projection=None):
        for d in self.docs:
            if self._match(d, query):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return object()

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._match(d, query):
                d.update(update.get("$set", {}))
                return object()
        if upsert:
            self.docs.append({**query, **update.get("$set", {})})
        return object()


class _FakeDB:
    def __init__(self):
        self.detections = _FakeDetections()


def _setup(monkeypatch):
    """Neutralize side effects (Mongo, audit log, track-manager) while keeping the
    promotion logic itself REAL."""
    db = _FakeDB()
    events = []
    monkeypatch.setattr(srv, "db", db)

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "meta": meta or {}})
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _observe(det, actor):
        return None
    monkeypatch.setattr(srv, "_observe_track_for_detection", _observe)

    # Neutralize the P2 no-strike registry read (a real Mongo collection in
    # prod, irrelevant to the promotion logic under test): return no entries so
    # the classification consult still runs and stamps target_grade, but no
    # fake no_strike_registry collection is needed here.
    async def _no_entries():
        return []
    monkeypatch.setattr(srv, "_no_strike_entries", _no_entries)

    return db, events


# ---------------------------------------------------------------------
# Model: the ingest body accepts bssid / softap_bssid (backward-compatible)
# ---------------------------------------------------------------------
def test_ingest_body_accepts_bssid_and_softap_bssid():
    b = srv.WifiDroneIngestBody(bssid=FLOW_BSSID, softap_bssid=FLOW_BSSID, channel=6)
    assert b.bssid == FLOW_BSSID
    assert b.softap_bssid == FLOW_BSSID
    # Backward-compatible: everything optional, an empty body still validates.
    assert srv.WifiDroneIngestBody().bssid is None


def test_channel_to_ghz_is_honest_spec_mapping():
    assert srv._wifi_channel_to_ghz(6) == 2.437     # 2.4 GHz ch 6
    assert srv._wifi_channel_to_ghz(1) == 2.412
    assert srv._wifi_channel_to_ghz(None) is None   # never guessed
    assert srv._wifi_channel_to_ghz(999) is None    # out of plan


# ---------------------------------------------------------------------
# A classified FLOW candidate becomes ONE ACTIVE targetable contact
# ---------------------------------------------------------------------
async def test_flow_candidate_creates_one_active_targetable_contact(monkeypatch):
    db, _events = _setup(monkeypatch)

    res = await srv.wifi_drone_ingest(_flow_body(), user=USER)

    assert res["targetable"] is True
    det_id = res["detection_id"]
    assert det_id

    # Exactly ONE detection row created.
    assert len(db.detections.docs) == 1
    doc = db.detections.docs[0]

    assert doc["status"] == "ACTIVE"
    assert doc["bssid"] == FLOW_BSSID
    assert doc["softap_bssid"] == FLOW_BSSID
    assert doc["channel"] == 6
    assert doc["protocol"] == "wifi"
    assert doc["model"] == FLOW_MAKE
    assert doc["center_freq_ghz"] == 2.437
    assert doc["source"] == "WIFI_DRONE_KISMET"

    # Resolvable by id (the wifi-defeat flow does find_one({"id": target_id})).
    found = await db.detections.find_one({"id": det_id})
    assert found is not None
    assert found["bssid"] == FLOW_BSSID


async def test_contact_resolves_by_real_bssid_the_wifi_defeat_way(monkeypatch):
    """Mirror deploy_wifi_defeat's exact BSSID resolution + fail-closed guard: the
    promoted contact must yield a concrete (non-broadcast) softAP BSSID."""
    db, _events = _setup(monkeypatch)
    res = await srv.wifi_drone_ingest(_flow_body(), user=USER)
    doc = await db.detections.find_one({"id": res["detection_id"]})

    target_bssid = (doc.get("bssid") or doc.get("softap_bssid")
                    or doc.get("target_bssid"))
    assert target_bssid == FLOW_BSSID
    # The endpoint's fail-closed broadcast/absent guard must PASS (not refuse).
    assert srv._wifi_bssid_missing_or_broadcast(target_bssid) is False


# ---------------------------------------------------------------------
# HONESTY: candidate confidence tier, never inflated / never auto-authorized
# ---------------------------------------------------------------------
async def test_contact_carries_honest_candidate_confidence(monkeypatch):
    db, _events = _setup(monkeypatch)
    await srv.wifi_drone_ingest(_flow_body(), user=USER)
    doc = db.detections.docs[0]

    # Honest candidate tier — a spoofable SSID/OUI heuristic, NOT a decode.
    assert doc["confidence_type"] == "heuristic_binary"
    assert doc["confidence_type"] != "protocol_verified"
    assert doc["protocol_confirmed"] is False
    assert doc["ml_label"] is None

    # Not inflated: threat MEDIUM, not HIGH/CRITICAL.
    assert doc["threat_level"] == "MEDIUM"

    # Engagement authorization stays a SEPARATE fire-time step — never auto-set.
    assert doc["authorized_target"] is False
    assert not doc.get("iff_verified")

    # It carries the caveats so the record self-documents spoofability.
    assert doc["caveats"]


# ---------------------------------------------------------------------
# Re-ingest upserts the SAME row (dedupe by BSSID), no duplicate, no gate reset
# ---------------------------------------------------------------------
async def test_reingest_upserts_no_duplicate(monkeypatch):
    db, _events = _setup(monkeypatch)

    res1 = await srv.wifi_drone_ingest(_flow_body(), user=USER)
    res2 = await srv.wifi_drone_ingest(_flow_body(signal_dbm=-40.0), user=USER)

    # Same contact, still exactly ONE row.
    assert res1["detection_id"] == res2["detection_id"]
    assert len(db.detections.docs) == 1
    # Liveness/signal refreshed on the merge.
    assert db.detections.docs[0]["rssi_dbm"] == -40.0


async def test_reingest_does_not_reset_engagement_authorization(monkeypatch):
    """A stream of beacons must NEVER silently clear an operator's fire-time gate:
    once authorized_target is set (or the record is IFF-classified), re-ingest
    must leave it, threat_level, and iff state untouched."""
    db, _events = _setup(monkeypatch)
    res = await srv.wifi_drone_ingest(_flow_body(), user=USER)

    # Simulate an operator authorization + an IFF-friendly downgrade landing.
    doc = db.detections.docs[0]
    doc["authorized_target"] = True
    doc["iff_verified"] = True
    doc["threat_level"] = "FRIENDLY (IFF verified)"
    doc["status"] = "LOST"  # also simulate a prior stale expiry

    await srv.wifi_drone_ingest(_flow_body(), user=USER)

    assert len(db.detections.docs) == 1
    merged = db.detections.docs[0]
    assert merged["id"] == res["detection_id"]
    # Re-activated (targetable again) ...
    assert merged["status"] == "ACTIVE"
    # ... but the fire-time gates are PRESERVED, not reset by the beacon.
    assert merged["authorized_target"] is True
    assert merged["iff_verified"] is True
    assert merged["threat_level"] == "FRIENDLY (IFF verified)"


# ---------------------------------------------------------------------
# A non-classified / generic / BSSID-less wifi ingest is NOT promoted
# ---------------------------------------------------------------------
async def test_generic_softap_does_not_create_targetable_contact(monkeypatch):
    """A bare generic ^DIRECT- softAP (make_candidate None) is a weak signal — it
    refreshes the latest-only board but must NOT spawn a targetable contact."""
    db, _events = _setup(monkeypatch)

    res = await srv.wifi_drone_ingest(
        _flow_body(make_candidate=None, ssid="DIRECT-a1-HP", oui=None,
                   manuf=None, match_basis="ssid(generic softAP)"),
        user=USER,
    )

    assert res["targetable"] is False
    assert res["detection_id"] is None
    assert db.detections.docs == []
    # Latest-only board still updated (backward-compatible behavior preserved).
    assert srv._last_wifi_drone is not None


async def test_candidate_without_concrete_bssid_is_not_promoted(monkeypatch):
    """A classified candidate with NO concrete BSSID (or a broadcast BSSID) must
    fail closed — a targetable contact requires a specific softAP BSSID."""
    db, _events = _setup(monkeypatch)

    # No bssid / softap_bssid / source_mac at all.
    res_none = await srv.wifi_drone_ingest(
        _flow_body(bssid=None, softap_bssid=None, source_mac=None), user=USER)
    assert res_none["targetable"] is False
    assert res_none["detection_id"] is None

    # Broadcast BSSID is refused too.
    res_bcast = await srv.wifi_drone_ingest(
        _flow_body(bssid="FF:FF:FF:FF:FF:FF", softap_bssid=None,
                   source_mac=None), user=USER)
    assert res_bcast["targetable"] is False

    assert db.detections.docs == []
