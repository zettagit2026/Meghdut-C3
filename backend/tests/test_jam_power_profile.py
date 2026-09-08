"""Unit tests for the jam POWER PROFILE (anti-fade) end-to-end wiring on the
backend side: JamRequestBody.profile validation at the API boundary, the
profile threading into the WS jam dispatch payload + fire-log/audit, the
tx_gain 0..47 clamp precondition, and proof the profile is PARAM-ONLY (adds no
gate bypass — the tx_halt gate still refuses regardless of profile).

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_operator_jam_mode.py / test_jam_bluetooth_band.py: importing
backend/server.py only needs the env vars SET (motor is lazy). deploy_jam is
driven directly with its async dependencies monkeypatched, so nothing transmits
and no Mongo is touched.

Run: pytest backend/tests/test_jam_power_profile.py -v
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

USER = {"email": "cmdr@unused.local", "role": "commander"}
TOKENS = {"arm_token": "dummy-arm-token", "jam_confirm_token": "dummy-jam-confirm-token-00000000"}

VALID_PROFILES = ("max", "flat", "external_pa")


# --------------------------------------------------------------------------
# Model: profile field validated at the API boundary (not the bridge)
# --------------------------------------------------------------------------
def test_profile_defaults_to_max():
    body = srv.JamRequestBody(band="915", **TOKENS)
    assert body.profile == "max"


def test_profile_accepts_all_three_valid_values():
    for p in VALID_PROFILES:
        body = srv.JamRequestBody(band="915", profile=p, **TOKENS)
        assert body.profile == p


def test_profile_rejects_bogus_value():
    # A typo'd/unknown profile MUST be rejected at the API boundary (pydantic
    # ValidationError -> FastAPI 422), NOT silently normalized to MAX. The
    # backend does not rely on the bridge's defensive _normalize_jam_profile.
    with pytest.raises(Exception):
        srv.JamRequestBody(band="915", profile="not_a_profile", **TOKENS)


def test_profile_rejects_uppercase_typo():
    # An operator who typed the label (FLAT) rather than the value (flat) must
    # get an error here, not a silent power change — the pattern is strict-lower.
    with pytest.raises(Exception):
        srv.JamRequestBody(band="915", profile="FLAT", **TOKENS)


# --------------------------------------------------------------------------
# Model: tx_gain clamped to the HackRF IF-VGA range [0,47] at the boundary,
# so the field-bridge clamp's precondition always holds.
# --------------------------------------------------------------------------
def test_tx_gain_accepts_in_range():
    for g in (0, 20, 47):
        body = srv.JamRequestBody(band="915", tx_gain=g, **TOKENS)
        assert body.tx_gain == g


def test_tx_gain_rejects_above_ceiling():
    with pytest.raises(Exception):
        srv.JamRequestBody(band="915", tx_gain=48, **TOKENS)


def test_tx_gain_rejects_negative():
    with pytest.raises(Exception):
        srv.JamRequestBody(band="915", tx_gain=-1, **TOKENS)


# --------------------------------------------------------------------------
# Spine stub (identical to test_operator_jam_mode.py): neutralize real side
# effects while capturing what deploy_jam emits. Nothing transmits.
# --------------------------------------------------------------------------
def _stub_spine(monkeypatch):
    events = []
    broadcasts = []

    monkeypatch.setattr(srv, "_tx_halted", False)
    monkeypatch.setattr(srv, "_consume_arm_token", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_consume_jam_confirm_token", lambda *a, **k: None)

    async def _range_ok(effect, actor):
        return None
    monkeypatch.setattr(srv, "_require_range_authorized", _range_ok)

    async def _log(kind, message, meta=None, actor=None):
        events.append({"kind": kind, "message": message, "meta": meta or {}, "actor": actor})
        return {}
    monkeypatch.setattr(srv, "log_event", _log)

    async def _broadcast(msg):
        broadcasts.append(msg)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _broadcast)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)

    return events, broadcasts


# --------------------------------------------------------------------------
# Threading: a valid profile reaches the WS jam dispatch payload + response +
# audit meta (records WHICH power mode actually radiated).
# --------------------------------------------------------------------------
def test_profile_reaches_bridge_dispatch_payload(monkeypatch):
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", profile="flat", **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))

    # Response surfaces the profile.
    assert resp["profile"] == "flat"

    # The jam_request broadcast (the dict jam_bridge reads as data["profile"])
    # carries the profile so the field bridge receives it.
    jam_reqs = [b for b in broadcasts if b.get("type") == "jam_request"]
    assert len(jam_reqs) == 1
    assert jam_reqs[0]["profile"] == "flat"


def test_default_profile_reaches_dispatch_as_max(monkeypatch):
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", **TOKENS)  # default
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["profile"] == "max"
    jam_req = [b for b in broadcasts if b.get("type") == "jam_request"][0]
    assert jam_req["profile"] == "max"


def test_each_valid_profile_threads_through(monkeypatch):
    for p in VALID_PROFILES:
        _, broadcasts = _stub_spine(monkeypatch)
        body = srv.JamRequestBody(band="915", profile=p, **TOKENS)
        resp = asyncio.run(srv.deploy_jam(body, user=USER))
        assert resp["profile"] == p
        jam_req = [b for b in broadcasts if b.get("type") == "jam_request"][0]
        assert jam_req["profile"] == p


def test_profile_recorded_in_fire_log_audit(monkeypatch):
    events, _ = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", profile="external_pa", **TOKENS)
    asyncio.run(srv.deploy_jam(body, user=USER))

    # The JAM fire-log/audit entry records the profile in meta (which power
    # mode radiated) AND in the human-readable message.
    jam_events = [e for e in events if e["kind"] == "JAM" and "profile" in e["meta"]]
    assert jam_events, "expected a JAM audit event carrying meta.profile"
    assert jam_events[0]["meta"]["profile"] == "external_pa"
    assert "EXTERNAL_PA" in jam_events[0]["message"]


def test_tx_gain_forwarded_verbatim_within_range(monkeypatch):
    # The clamped-at-boundary gain is forwarded verbatim to the bridge dispatch
    # (the bridge clamp precondition holds because the value is already 0..47).
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", tx_gain=47, **TOKENS)
    asyncio.run(srv.deploy_jam(body, user=USER))
    jam_req = [b for b in broadcasts if b.get("type") == "jam_request"][0]
    assert jam_req["tx_gain"] == 47


# --------------------------------------------------------------------------
# PARAM-ONLY: the profile adds NO gate bypass. The tx_halt gate still refuses
# a fire regardless of which profile was requested.
# --------------------------------------------------------------------------
def test_profile_does_not_bypass_tx_halt(monkeypatch):
    monkeypatch.setattr(srv, "_tx_halted", True)
    for p in VALID_PROFILES:
        body = srv.JamRequestBody(band="915", profile=p, **TOKENS)
        with pytest.raises(srv.HTTPException) as ei:
            asyncio.run(srv.deploy_jam(body, user=USER))
        assert ei.value.status_code == 409
