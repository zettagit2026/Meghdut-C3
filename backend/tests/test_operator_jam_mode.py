"""Unit tests for the Operator Jam mode on the backend side (jam_mode routing
+ distinct audit + shared spine).

True unit tests (no requests/websockets/live BASE_URL, no running Mongo) — same
pattern as test_jam_bluetooth_band.py: importing backend/server.py only needs
the env vars SET (motor is lazy). deploy_jam is driven directly with its async
dependencies monkeypatched, so nothing transmits and no Mongo is touched.

Run: pytest backend/tests/test_operator_jam_mode.py -v
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


# --------------------------------------------------------------------------
# Model: jam_mode field
# --------------------------------------------------------------------------
def test_jam_mode_defaults_to_meghdut():
    body = srv.JamRequestBody(band="915", **TOKENS)
    assert body.jam_mode == "meghdut"


def test_jam_mode_accepts_operator():
    body = srv.JamRequestBody(band="915", jam_mode="operator", **TOKENS)
    assert body.jam_mode == "operator"


def test_jam_mode_rejects_bogus():
    with pytest.raises(Exception):
        srv.JamRequestBody(band="915", jam_mode="not_a_mode", **TOKENS)


def test_operator_jam_bands_subset_of_jam_presets():
    # Operator supports only its four band-fixed presets, all valid JAM bands.
    assert srv.OPERATOR_JAM_BANDS == {"433", "915", "2g4", "5g8"}
    for b in srv.OPERATOR_JAM_BANDS:
        assert b in srv.JAM_BAND_PRESETS_MHZ


# --------------------------------------------------------------------------
# Shared spine: tx_halt still 409 for operator mode
# --------------------------------------------------------------------------
def test_operator_mode_still_409_when_tx_halted(monkeypatch):
    # A mode=operator fire while EMERGENCY ABORT is in effect must be refused
    # with 409 exactly like meghdut mode — operator mode is NOT a bypass.
    monkeypatch.setattr(srv, "_tx_halted", True)
    body = srv.JamRequestBody(band="915", jam_mode="operator", **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 409


# --------------------------------------------------------------------------
# Routing + distinct audit (spine dependencies stubbed, nothing transmits)
# --------------------------------------------------------------------------
def _stub_spine(monkeypatch):
    """Neutralize the spine's real side effects (token consumption, range-auth,
    Mongo audit, WS broadcast) while capturing what deploy_jam emits."""
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


def test_operator_mode_routes_and_audits_distinctly(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="2g4", jam_mode="operator", **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))

    # Response carries the mode.
    assert resp["jam_mode"] == "operator"

    # The jam_request broadcast carries jam_mode so the operator bridge picks
    # it up (and the meghdut bridge ignores it).
    jam_reqs = [b for b in broadcasts if b.get("type") == "jam_request"]
    assert len(jam_reqs) == 1
    assert jam_reqs[0]["jam_mode"] == "operator"

    # Audit is distinct: a JAM event whose meta.jam_mode == "OPERATOR".
    jam_events = [e for e in events if e["kind"] == "JAM" and e["meta"].get("jam_mode")]
    assert jam_events, "expected a JAM audit event carrying meta.jam_mode"
    assert jam_events[0]["meta"]["jam_mode"] == "OPERATOR"


def test_meghdut_mode_audits_distinctly(monkeypatch):
    events, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", **TOKENS)  # default meghdut
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["jam_mode"] == "meghdut"
    jam_reqs = [b for b in broadcasts if b.get("type") == "jam_request"]
    assert jam_reqs[0]["jam_mode"] == "meghdut"
    jam_events = [e for e in events if e["kind"] == "JAM" and e["meta"].get("jam_mode")]
    assert jam_events[0]["meta"]["jam_mode"] == "MEGHDUT"


def test_operator_mode_rejects_unsupported_band(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="gps_l1", jam_mode="operator", **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_operator_mode_rejects_explicit_freq(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", jam_mode="operator", freq_mhz=1234.0, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400
