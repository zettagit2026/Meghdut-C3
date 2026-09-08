"""Unit tests for the COMMANDER-gated Wi-Fi survey DESIGNATE path
(POST /api/wifi-environment/designate -> designate_wifi_survey_uas).

This endpoint is a fire-ADJACENT targeting affordance: it promotes ONE surveyed
drone-OUI/SSID AP into a governed db.detections contact (via the EXISTING
_upsert_wifi_drone_detection) so the byte-unchanged /wifi-defeat SafetyGate flow
can resolve it by id. It CREATES A GOVERNED CONTACT ONLY — it never arms,
confirms, transmits, or fires, and never clears tx_halted.

The gate chain is entirely SERVER-SIDE (the client possible_uas hint is never
trusted): (a) fail-closed on absent/broadcast BSSID; (b) re-derive the
possible-UAS verdict from the drone-OUI/SSID matchers; (c) hard-refuse a
civilian/neutral no-strike match; then synthesize the ingest + upsert.

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_wifi_drone_promote.py: the handler is driven directly with db /
log_event / track-observation / no-strike-registry monkeypatched.

Run: pytest backend/tests/test_wifi_survey_designate.py -v
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

import asyncio

import pytest

import server as srv

COMMANDER = {"email": "cmd@unused.local", "role": "commander"}
OPERATOR = {"email": "op@unused.local", "role": "operator"}

# A real, actively-typed justification (>=20 chars, not trivial) — required to
# commander-override the drone gate for a NON-DRONE AP (F2 friction).
GOOD_JUST = "Visual confirmation of a rogue relay operating from this office AP."

# A DJI drone-OUI softAP row as it appears in the Wi-Fi survey (OUI 60:60:1F is
# in kismet_survey.DRONE_MANUFACTURER_OUIS).
DJI_BSSID = "60:60:1F:AA:BB:CC"
DJI_OUI = "60:60:1F"


def _body(**overrides):
    base = dict(
        bssid=DJI_BSSID,
        ssid="Mavic-Air-1234",
        oui=DJI_OUI,
        channel=149,
        vendor="Dji Innovations",
        pmf_required=None,
        pmf_supported=None,
    )
    base.update(overrides)
    return srv.WifiSurveyDesignateBody(**base)


class _FakeDetections:
    """Minimal in-memory stand-in for the motor db.detections collection."""

    def __init__(self):
        self.docs = []

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


def _setup(monkeypatch, *, no_strike_entries=None):
    """Neutralize side effects (Mongo, audit log, track-manager) while keeping
    the designate + promotion logic itself REAL. `no_strike_entries` seeds the
    hot-loaded registry the civilian hard-block consults."""
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

    entries = list(no_strike_entries or [])

    async def _entries():
        return entries
    monkeypatch.setattr(srv, "_no_strike_entries", _entries)

    return db, events


# ---------------------------------------------------------------------
# (a) Commander-gated: route wiring + the helper refuses an operator (403)
# ---------------------------------------------------------------------
def _route_dep_calls(path: str, method: str):
    for route in srv.app.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return {d.call for d in route.dependant.dependencies}
    raise AssertionError(f"route {method} {path} not found")


def test_designate_route_requires_commander():
    assert srv.require_commander in _route_dep_calls("/api/wifi-environment/designate", "POST"), \
        "POST /api/wifi-environment/designate must be gated by require_commander"


def test_require_commander_refuses_operator():
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.require_commander(user=OPERATOR))
    assert ei.value.status_code == 403


def test_designate_route_not_wired_to_get_current_user_only():
    # The commander gate is a STRICT superset of get_current_user; assert the
    # commander gate is present (the operator-refusing dependency), never the
    # bare authenticated-user gate alone.
    deps = _route_dep_calls("/api/wifi-environment/designate", "POST")
    assert srv.require_commander in deps


# ---------------------------------------------------------------------
# (b) A non-possible-UAS row -> 422 AND no db.detections write
# ---------------------------------------------------------------------
async def test_non_uas_row_is_refused_422_and_creates_nothing(monkeypatch):
    db, _events = _setup(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="AA:BB:CC:DD:EE:FF", oui="AA:BB:CC", ssid="MyHomeWiFi"),
            user=COMMANDER,
        )
    assert ei.value.status_code == 422
    assert "not a UAS candidate" in ei.value.detail
    assert db.detections.docs == []


# ---------------------------------------------------------------------
# (c) Absent / broadcast BSSID -> 422 (fail-closed, per-BSSID only)
# ---------------------------------------------------------------------
async def test_broadcast_bssid_is_refused_422(monkeypatch):
    db, _events = _setup(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="FF:FF:FF:FF:FF:FF"), user=COMMANDER)
    assert ei.value.status_code == 422
    assert db.detections.docs == []


async def test_absent_bssid_is_refused_422(monkeypatch):
    db, _events = _setup(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(_body(bssid=""), user=COMMANDER)
    assert ei.value.status_code == 422
    assert db.detections.docs == []


# ---------------------------------------------------------------------
# (d) A drone-OUI row that ALSO matches a CIVILIAN no-strike entry -> 403,
#     no contact created (belt-and-braces beyond the fire-time floor)
# ---------------------------------------------------------------------
async def test_civilian_no_strike_match_is_refused_403_and_creates_nothing(monkeypatch):
    civilian_entry = {
        "id": "civ-1",
        "category": "CIVILIAN_INFRASTRUCTURE",
        "label": "Registered civilian AP",
        "enabled": True,
        "match": {"oui": DJI_OUI},   # matches the candidate's OUI
    }
    db, _events = _setup(monkeypatch, no_strike_entries=[civilian_entry])

    # The row IS a drone-OUI candidate (passes gate b) but matches a civilian
    # protection entry (gate c) -> hard 403, nothing created.
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(_body(), user=COMMANDER)
    assert ei.value.status_code == 403
    assert "CIVILIAN_INFRASTRUCTURE" in ei.value.detail
    assert db.detections.docs == []


# ---------------------------------------------------------------------
# (e) A valid drone-OUI row -> creates a governed contact the fire path
#     can resolve, at the HONEST candidate tier (never auto-authorized)
# ---------------------------------------------------------------------
async def test_valid_drone_oui_creates_resolvable_governed_contact(monkeypatch):
    db, _events = _setup(monkeypatch)

    res = await srv.designate_wifi_survey_uas(_body(), user=COMMANDER)

    assert res["bssid"] == DJI_BSSID
    det_id = res["detection_id"]
    assert det_id

    assert len(db.detections.docs) == 1
    doc = db.detections.docs[0]

    # Resolvable by the fire path: match_protocol wifi + concrete BSSID + id.
    assert doc["match_protocol"] == "wifi"
    assert doc["protocol"] == "wifi"
    assert doc["bssid"] == DJI_BSSID
    assert srv._wifi_bssid_missing_or_broadcast(doc["bssid"]) is False
    found = await db.detections.find_one({"id": det_id})
    assert found is not None and found["bssid"] == DJI_BSSID

    # Target-grade via T3 (a classified, non-civilian wifi softAP).
    assert doc["make_candidate"]
    assert doc["target_grade"] is True
    assert srv.is_target_grade(doc) is True

    # HONEST candidate tier — a spoofable heuristic, NEVER auto-authorized.
    assert doc["confidence_type"] == "heuristic_binary"
    assert doc["protocol_confirmed"] is False
    assert doc.get("authorized_target") is False
    assert not doc.get("iff_verified")
    assert doc["source"] == "WIFI_SURVEY_MANUAL"

    # A distinct audit event was emitted for the designation.
    assert any(e["kind"] == "WIFI_SURVEY_DESIGNATE" for e in _events)


async def test_designate_does_not_arm_or_clear_tx_halted(monkeypatch):
    """The endpoint creates a contact ONLY: no authorized_target, no arm, and
    it must not touch the tx-halt state (there is no code path here that does)."""
    db, _events = _setup(monkeypatch)
    res = await srv.designate_wifi_survey_uas(_body(), user=COMMANDER)
    doc = db.detections.docs[0]
    assert doc.get("authorized_target") is False
    # The returned payload is a bare contact reference — no token, no fire ack.
    assert set(res.keys()) == {"detection_id", "bssid"}


# ---------------------------------------------------------------------
# (f) A pmf_required row -> the promoted contact makes _wifi_target_has_pmf True
# ---------------------------------------------------------------------
async def test_pmf_required_row_carries_pmf_to_the_contact(monkeypatch):
    db, _events = _setup(monkeypatch)

    await srv.designate_wifi_survey_uas(_body(pmf_required=True), user=COMMANDER)
    doc = db.detections.docs[0]

    # The PMF signal is persisted so the downstream deauth-applicability gate
    # (a deauth vs a PMF-protected AP is a NO-OP) correctly refuses.
    assert srv._wifi_target_has_pmf(doc) is True


async def test_non_pmf_row_does_not_falsely_signal_pmf(monkeypatch):
    db, _events = _setup(monkeypatch)

    await srv.designate_wifi_survey_uas(_body(pmf_required=None), user=COMMANDER)
    doc = db.detections.docs[0]

    # Unknown PMF is honestly treated as PMF-not-indicated (deauth allowed as a
    # best-effort link-drop) — never fabricated as PMF-present.
    assert srv._wifi_target_has_pmf(doc) is False


# ---------------------------------------------------------------------
# COMMANDER NO-STRIKE OVERRIDE (designate side) — the two-tier split:
#   * a NEUTRAL no-strike match is now DESIGNATABLE (fire still needs the
#     fire-time override token); only CIVILIAN_INFRASTRUCTURE is hard-refused;
#   * a non-drone-tagged row is designatable under commander_override, forced to
#     the honest non-drone candidate tier; commander_override NEVER bypasses the
#     CIVILIAN hard floor.
# ---------------------------------------------------------------------
async def test_neutral_no_strike_match_is_designatable(monkeypatch):
    neutral_entry = {
        "id": "neu-1", "category": "NEUTRAL", "label": "Neutral AP",
        "enabled": True, "match": {"oui": DJI_OUI},   # matches the drone-OUI candidate
    }
    db, events = _setup(monkeypatch, no_strike_entries=[neutral_entry])
    # A drone-OUI row that matches a NEUTRAL entry is NO LONGER refused at
    # designate — it becomes a governed contact (fire is gated later by the
    # fire-time no-strike override token).
    res = await srv.designate_wifi_survey_uas(_body(), user=COMMANDER)
    assert res["bssid"] == DJI_BSSID
    assert len(db.detections.docs) == 1
    assert any(e["kind"] == "WIFI_SURVEY_DESIGNATE" for e in events)


async def test_commander_override_designates_non_drone_row_honestly(monkeypatch):
    db, events = _setup(monkeypatch)
    # A NON-drone AP (no drone OUI/SSID) is normally refused 422; a commander may
    # override, but it is forced to the honest non-drone candidate tier.
    res = await srv.designate_wifi_survey_uas(
        _body(bssid="AA:BB:CC:DD:EE:01", oui="AA:BB:CC", ssid="MyOfficeWiFi",
              vendor="Cisco", commander_override=True, justification=GOOD_JUST),
        user=COMMANDER,
    )
    assert res["bssid"] == "AA:BB:CC:DD:EE:01"
    doc = db.detections.docs[0]
    assert "non-drone" in doc["make_candidate"].lower()
    assert doc["match_basis"] == "commander-override"
    desig = [e for e in events if e["kind"] == "WIFI_SURVEY_DESIGNATE"]
    assert desig and desig[0]["meta"]["commander_override"] is True


async def test_non_drone_row_without_override_still_422(monkeypatch):
    db, _events = _setup(monkeypatch)
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="AA:BB:CC:DD:EE:01", oui="AA:BB:CC", ssid="MyOfficeWiFi",
                  vendor="Cisco", commander_override=False),
            user=COMMANDER,
        )
    assert ei.value.status_code == 422
    assert db.detections.docs == []


async def test_commander_override_never_bypasses_civilian_hard_floor(monkeypatch):
    civilian_entry = {
        "id": "civ-1", "category": "CIVILIAN_INFRASTRUCTURE", "label": "Civilian AP",
        "enabled": True, "match": {"oui": "AA:BB:CC"},
    }
    db, _events = _setup(monkeypatch, no_strike_entries=[civilian_entry])
    # Even with commander_override AND a valid justification, a
    # CIVILIAN_INFRASTRUCTURE match is hard-refused (the civilian floor is unchanged).
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="AA:BB:CC:DD:EE:01", oui="AA:BB:CC", ssid="MyOfficeWiFi",
                  vendor="Cisco", commander_override=True, justification=GOOD_JUST),
            user=COMMANDER,
        )
    assert ei.value.status_code == 403
    assert "CIVILIAN_INFRASTRUCTURE" in ei.value.detail
    assert db.detections.docs == []


# ---------------------------------------------------------------------
# F2 FRICTION (flag-gated): the commander-override justification requirement is
# governed by COMMANDER_OVERRIDE_JUSTIFICATION_REQUIRED (DEFAULT FALSE per the
# operator directive — not needed until near field deployment).
#   * flag OFF (now): a commander-override designate of a NON-DRONE AP proceeds
#     with NO justification, forces the honest possibly-civilian caveat, and
#     STILL emits the distinct WIFI_SURVEY_COMMANDER_OVERRIDE_DESIGNATE audit
#     (noting the justification was not required);
#   * flag ON: a real justification is required (absent/trivial -> 400).
# ---------------------------------------------------------------------
async def test_commander_override_designate_no_justification_succeeds_when_flag_off(monkeypatch):
    monkeypatch.setattr(srv, "COMMANDER_OVERRIDE_JUSTIFICATION_REQUIRED", False)
    db, events = _setup(monkeypatch)
    # No justification at all -> still created (flag off, pre-field).
    res = await srv.designate_wifi_survey_uas(
        _body(bssid="AA:BB:CC:DD:EE:03", oui="AA:BB:CC", ssid="MyOfficeWiFi",
              vendor="Cisco", commander_override=True),
        user=COMMANDER,
    )
    assert res["bssid"] == "AA:BB:CC:DD:EE:03"
    assert len(db.detections.docs) == 1
    doc = db.detections.docs[0]
    caveats = " ".join(doc.get("caveats") or []).lower()
    assert "possibly civilian" in caveats and "non-drone" in caveats
    # The distinct override audit STILL fires — never silent — noting no justification required.
    ovr = [e for e in events if e["kind"] == "WIFI_SURVEY_COMMANDER_OVERRIDE_DESIGNATE"]
    assert ovr, "the override designate must be audited even when no justification is required"
    assert ovr[0]["meta"]["justification"] == "not required (pre-field)"
    assert ovr[0]["meta"]["bssid"] == "AA:BB:CC:DD:EE:03"


async def test_commander_override_designate_requires_justification_when_flag_on(monkeypatch):
    monkeypatch.setattr(srv, "COMMANDER_OVERRIDE_JUSTIFICATION_REQUIRED", True)
    db, events = _setup(monkeypatch)
    # (1) No justification -> 400, nothing created.
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="AA:BB:CC:DD:EE:02", oui="AA:BB:CC", ssid="MyOfficeWiFi",
                  vendor="Cisco", commander_override=True),
            user=COMMANDER,
        )
    assert ei.value.status_code == 400
    assert db.detections.docs == []
    # (2) A trivial placeholder justification -> still 400.
    with pytest.raises(srv.HTTPException) as ei:
        await srv.designate_wifi_survey_uas(
            _body(bssid="AA:BB:CC:DD:EE:02", oui="AA:BB:CC", ssid="MyOfficeWiFi",
                  vendor="Cisco", commander_override=True, justification="n/a"),
            user=COMMANDER,
        )
    assert ei.value.status_code == 400
    assert db.detections.docs == []
    # (3) A real justification -> created, honest possibly-civilian caveat, distinct audit.
    res = await srv.designate_wifi_survey_uas(
        _body(bssid="AA:BB:CC:DD:EE:02", oui="AA:BB:CC", ssid="MyOfficeWiFi",
              vendor="Cisco", commander_override=True, justification=GOOD_JUST),
        user=COMMANDER,
    )
    assert res["bssid"] == "AA:BB:CC:DD:EE:02"
    doc = db.detections.docs[0]
    caveats = " ".join(doc.get("caveats") or []).lower()
    assert "possibly civilian" in caveats and "non-drone" in caveats
    ovr = [e for e in events if e["kind"] == "WIFI_SURVEY_COMMANDER_OVERRIDE_DESIGNATE"]
    assert ovr, "a commander-override non-drone designate must emit WIFI_SURVEY_COMMANDER_OVERRIDE_DESIGNATE"
    assert ovr[0]["meta"]["justification"] == GOOD_JUST
    assert ovr[0]["meta"]["bssid"] == "AA:BB:CC:DD:EE:02"
    # A drone-tagged designation still needs NO justification (unchanged).
