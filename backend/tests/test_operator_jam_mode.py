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
from datetime import datetime, timedelta, timezone
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


# --------------------------------------------------------------------------
# Commander directive: continuous (no auto-stop cap) + swept barrage
# --------------------------------------------------------------------------
def test_continuous_jam_forwards_null_duration(monkeypatch):
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", continuous=True, **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    # A continuous jam carries duration_s = None (runs until the operator stops
    # it) — NOT a capped number.
    assert resp["duration_s"] is None
    assert resp["continuous"] is True
    jam_req = [b for b in broadcasts if b.get("type") == "jam_request"][0]
    assert jam_req["duration_s"] is None
    assert jam_req["continuous"] is True


def test_long_bounded_duration_is_not_capped(monkeypatch):
    # NO artificial ceiling: a 3600s bounded request is forwarded verbatim,
    # not clamped to the old 10s JAM_MAX_DURATION_S.
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", duration_s=3600.0, **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["duration_s"] == 3600.0
    assert resp["duration_s"] > srv.JAM_MAX_DURATION_S  # the old cap is gone


def test_sweep_jam_forwards_band_edges(monkeypatch):
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2483.5,
                              continuous=True, **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["sweep"] is True
    assert resp["freq_start_mhz"] == 2400.0 and resp["freq_stop_mhz"] == 2483.5
    jam_req = [b for b in broadcasts if b.get("type") == "jam_request"][0]
    assert jam_req["sweep"] is True
    assert jam_req["freq_start_mhz"] == 2400.0 and jam_req["freq_stop_mhz"] == 2483.5


def test_sweep_requires_band_edges(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, **TOKENS)  # no start/stop
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


# --------------------------------------------------------------------------
# FREQUENCY-SCOPE sweep bounds (security hardening — NOT a timing/effectiveness
# cap). See backend/server.py: HACKRF_MIN/MAX_FREQ_MHZ + MAX_SWEEP_SPAN_MHZ.
# --------------------------------------------------------------------------
def test_sweep_bound_constants_are_generous_frequency_scope():
    # The span cap is deliberately GENEROUS (covers any single drone band incl.
    # the full 5.8GHz video band) — it blocks all-spectrum, not real jamming.
    assert srv.HACKRF_MIN_FREQ_MHZ == 1.0
    assert srv.HACKRF_MAX_FREQ_MHZ == 6000.0
    assert srv.MAX_SWEEP_SPAN_MHZ == 500.0


def test_sweep_rejects_below_hackrf_range(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=0.5, freq_stop_mhz=100.0,
                              continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_sweep_rejects_above_hackrf_range(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=5900.0, freq_stop_mhz=6100.0,
                              continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_sweep_rejects_all_spectrum_span(monkeypatch):
    # The blast-radius case the bound exists to stop: 2400 -> 6000 MHz (blankets
    # aviation/GNSS/cellular). Span 3600 MHz >> 500 MHz -> 400.
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=6000.0,
                              continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_sweep_rejects_span_just_over_cap(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2901.0,
                              continuous=True, **TOKENS)  # span 501 MHz
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_sweep_rejects_nonpositive_step(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2483.5,
                              step_mhz=0.0, continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_sweep_rejects_nonpositive_dwell(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2483.5,
                              dwell_ms=-1.0, continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_normal_drone_band_sweep_accepted(monkeypatch):
    # A real 2.4GHz ISM drone-band sweep (span 83.5 MHz, positive step/dwell) is
    # unaffected by the frequency-scope bounds — the whole point.
    _, broadcasts = _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=2400.0, freq_stop_mhz=2483.5,
                              step_mhz=20.0, dwell_ms=5.0, continuous=True, **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["sweep"] is True
    assert resp["freq_start_mhz"] == 2400.0 and resp["freq_stop_mhz"] == 2483.5


def test_full_5g8_video_band_sweep_accepted(monkeypatch):
    # The full 5.725-5.875GHz video band (span 150 MHz) is well within the cap.
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(sweep=True, freq_start_mhz=5725.0, freq_stop_mhz=5875.0,
                              continuous=True, **TOKENS)
    resp = asyncio.run(srv.deploy_jam(body, user=USER))
    assert resp["sweep"] is True


def test_operator_mode_rejects_sweep(monkeypatch):
    _stub_spine(monkeypatch)
    body = srv.JamRequestBody(band="915", jam_mode="operator", sweep=True,
                              freq_start_mhz=2400.0, freq_stop_mhz=2483.5, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 400


def test_continuous_jam_still_409_when_tx_halted(monkeypatch):
    # KILL-SWITCH PROOF at the backend layer: even a continuous jam is refused
    # with 409 while EMERGENCY ABORT (tx_halt) is in effect — the operator's
    # stop always wins, no matter the duration mode.
    monkeypatch.setattr(srv, "_tx_halted", True)
    body = srv.JamRequestBody(band="915", continuous=True, **TOKENS)
    with pytest.raises(srv.HTTPException) as ei:
        asyncio.run(srv.deploy_jam(body, user=USER))
    assert ei.value.status_code == 409


def test_continuous_active_jam_not_force_expired(monkeypatch):
    # A CONTINUOUS jam sits in JAM_ACTIVE indefinitely (no fixed duration) — the
    # lazy expiry must NOT crash on duration_s=None nor time it out.
    import asyncio as _aio
    srv._pending_jam.clear()
    old = datetime.now(timezone.utc) - timedelta(seconds=10_000)
    srv._pending_jam["rid-cont"] = {
        "ts": old, "status": "JAM_ACTIVE", "duration_s": None, "continuous": True,
        "freq_mhz": 915.0, "actor": "x",
    }

    async def _noop_broadcast(msg):
        return None
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _noop_broadcast)

    async def _noop_log(*a, **k):
        return {}
    monkeypatch.setattr(srv, "log_event", _noop_log)

    _aio.run(srv._expire_pending_jam())
    # Still active — never force-expired to TX_TIMEOUT on a duration it never had.
    assert srv._pending_jam["rid-cont"]["status"] == "JAM_ACTIVE"
    srv._pending_jam.clear()


# --------------------------------------------------------------------------
# FIX 4: a dropped TX bridge must not leave a phantom-active CONTINUOUS session
# reported "active" forever (status-honesty only; the TX already stopped).
# --------------------------------------------------------------------------
def test_lost_jam_bridge_marks_continuous_session_bridge_lost(monkeypatch):
    import asyncio as _aio
    srv._pending_jam.clear()
    srv._pending_jam["rid-cont"] = {
        "ts": datetime.now(timezone.utc), "status": "JAM_ACTIVE",
        "duration_s": None, "continuous": True, "freq_mhz": 915.0, "actor": "x",
    }

    async def _noop_broadcast(msg):
        return None
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _noop_broadcast)

    async def _noop_log(*a, **k):
        return {}
    monkeypatch.setattr(srv, "log_event", _noop_log)
    # No jam bridge remains subscribed after the drop.
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: False)

    _aio.run(srv._mark_lost_tx_bridge_sessions({"jam"}))
    # The phantom continuous session is now honestly closed — not "active" forever.
    assert srv._pending_jam["rid-cont"]["status"] == "BRIDGE_LOST"
    assert srv._pending_jam["rid-cont"]["terminal"] is True
    srv._pending_jam.clear()


def test_lost_bridge_does_not_touch_bounded_or_other_effect(monkeypatch):
    import asyncio as _aio
    srv._pending_jam.clear()
    # A BOUNDED jam self-expires via the normal timeout — do NOT relabel it here.
    srv._pending_jam["rid-bounded"] = {
        "ts": datetime.now(timezone.utc), "status": "JAM_ACTIVE",
        "duration_s": 30.0, "continuous": False, "freq_mhz": 915.0, "actor": "x",
    }

    async def _noop_broadcast(msg):
        return None
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _noop_broadcast)

    async def _noop_log(*a, **k):
        return {}
    monkeypatch.setattr(srv, "log_event", _noop_log)
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: False)

    # A dropped mavlink_sdr_inject bridge must not touch a jam session at all.
    _aio.run(srv._mark_lost_tx_bridge_sessions({"mavlink_sdr_inject"}))
    assert srv._pending_jam["rid-bounded"]["status"] == "JAM_ACTIVE"
    srv._pending_jam.clear()


def test_lost_bridge_noop_when_another_bridge_still_subscribed(monkeypatch):
    import asyncio as _aio
    srv._pending_jam.clear()
    srv._pending_jam["rid-cont"] = {
        "ts": datetime.now(timezone.utc), "status": "JAM_ACTIVE",
        "duration_s": None, "continuous": True, "freq_mhz": 915.0, "actor": "x",
    }

    async def _noop_broadcast(msg):
        return None
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _noop_broadcast)

    async def _noop_log(*a, **k):
        return {}
    monkeypatch.setattr(srv, "log_event", _noop_log)
    # Another jam bridge is STILL connected — the session is still being carried.
    monkeypatch.setattr(srv.ws_manager, "has_tx_consumer", lambda effect: True)

    _aio.run(srv._mark_lost_tx_bridge_sessions({"jam"}))
    assert srv._pending_jam["rid-cont"]["status"] == "JAM_ACTIVE"
    srv._pending_jam.clear()
