"""Unit tests for the GUI TX-bridge SiK-handoff endpoints (POST /api/tx/online,
POST /api/tx/standdown) and their commander-gating + audit + fail-closed
guarantees.

Context: bringing the TX bridges online used to require a human running
`systemctl` on the transmit host's shell — an out-of-GUI step a fielded operator
cannot perform. server.py now exposes two commander-gated, audited endpoints
that trigger the hard-whitelisted cema-tx-helper host daemon
(tx_bridge_control.py) to perform the handoff. These tests assert:

  * both endpoints are commander-gated (require_commander is in each route's
    dependency tree),
  * a successful handoff is written to the hash-chained mission log with the
    acting commander as `actor`,
  * a handoff NEVER clears the master TX-halt (fail-closed _tx_halted preserved),
  * a helper-unavailable / helper-error condition audits the failed attempt and
    raises the right HTTP status (never a raw shell instruction),
  * the derived tx_subsystem status block reflects observable bridge/SiK state.

True unit tests (no live server/Mongo/host socket, same style as
tests/test_gnss_spoof_geodesic.py / tests/test_audit_chain.py): importing
server.py only needs the env vars SET (motor's client is lazy and never
connects), and the host helper + DB are mocked.

Run: pytest backend/tests/test_tx_bridge_control.py -v
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
from fastapi import HTTPException

import server as srv
import tx_bridge_control


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _collect_dependency_calls(dependant):
    """Recursively collect every dependency callable in a route's dependant
    tree, so we can assert require_commander gates the route."""
    calls = []
    for sub in dependant.dependencies:
        calls.append(sub.call)
        calls.extend(_collect_dependency_calls(sub))
    return calls


def _route_for(path: str):
    for r in srv.app.routes:
        if getattr(r, "path", None) == path and "POST" in getattr(r, "methods", set()):
            return r
    raise AssertionError(f"route not found: POST {path}")


class _AsyncCapture:
    """Minimal async stand-in that records call args (avoids a hard dependency
    on unittest.mock.AsyncMock semantics)."""
    def __init__(self, ret=None):
        self.calls = []
        self.ret = ret

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret


COMMANDER = {"id": "u-cmd", "email": "cmd@meghaduta.mil", "role": "commander"}


# ---------------------------------------------------------------------
# Commander-gating (route dependency introspection)
# ---------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/tx/online", "/api/tx/standdown"])
def test_endpoint_is_commander_gated(path):
    route = _route_for(path)
    calls = _collect_dependency_calls(route.dependant)
    assert srv.require_commander in calls, (
        f"POST {path} must depend on require_commander (commander-gated); "
        f"found deps: {[getattr(c, '__name__', c) for c in calls]}"
    )


@pytest.mark.asyncio
async def test_require_commander_rejects_operator():
    # Direct proof the gate itself 403s a non-commander (the DI wall the routes
    # sit behind).
    with pytest.raises(HTTPException) as ei:
        await srv.require_commander({"id": "u-op", "email": "op@x", "role": "operator"})
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------
# Audit + fail-closed on a successful handoff
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_online_audits_and_preserves_tx_halt(monkeypatch):
    log = _AsyncCapture()
    monkeypatch.setattr(srv, "log_event", log)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _AsyncCapture())
    monkeypatch.setattr(srv, "_tx_subsystem_status", _AsyncCapture(ret={"bridges_online": True}))
    monkeypatch.setattr(
        srv.tx_bridge_control, "bring_online",
        _AsyncCapture(ret={"units": {"cema-rf-bridge": "active"},
                           "sik_owner": "rf-bridge", "bridges_online": True}),
    )

    # Master TX-halt is ON going in; a bridge handoff must NEVER clear it.
    monkeypatch.setattr(srv, "_tx_halted", True, raising=False)

    out = await srv.tx_bring_online(COMMANDER)

    assert out["ok"] is True and out["action"] == "online"
    assert out["host_sik_owner"] == "rf-bridge"
    # Fail-closed preserved: the handoff did not touch the halt.
    assert srv._tx_halted is True
    # Audited to the mission log with the acting commander as actor.
    assert len(log.calls) == 1
    args, kwargs = log.calls[0]
    assert args[0] == "TX_BRIDGE_CONTROL"
    assert kwargs.get("actor") == COMMANDER["email"]
    assert kwargs.get("meta", {}).get("outcome") == "ok"


@pytest.mark.asyncio
async def test_standdown_audits(monkeypatch):
    log = _AsyncCapture()
    monkeypatch.setattr(srv, "log_event", log)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _AsyncCapture())
    monkeypatch.setattr(srv, "_tx_subsystem_status", _AsyncCapture(ret={"bridges_online": False}))
    monkeypatch.setattr(
        srv.tx_bridge_control, "stand_down",
        _AsyncCapture(ret={"units": {"cema-mavlink-sniffer": "active"},
                           "sik_owner": "sniffer", "bridges_online": False}),
    )

    out = await srv.tx_stand_down(COMMANDER)
    assert out["ok"] is True and out["action"] == "standdown"
    assert out["host_sik_owner"] == "sniffer"
    args, kwargs = log.calls[0]
    assert args[0] == "TX_BRIDGE_CONTROL"
    assert kwargs.get("actor") == COMMANDER["email"]


# ---------------------------------------------------------------------
# Helper unavailable / error: audit the attempt, raise a clean HTTP status,
# never leak a raw shell instruction.
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_helper_unavailable_audits_and_503(monkeypatch):
    log = _AsyncCapture()
    monkeypatch.setattr(srv, "log_event", log)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _AsyncCapture())

    async def _boom():
        raise tx_bridge_control.TxHelperUnavailable("socket missing")
    monkeypatch.setattr(srv.tx_bridge_control, "bring_online", _boom)

    with pytest.raises(HTTPException) as ei:
        await srv.tx_bring_online(COMMANDER)
    assert ei.value.status_code == 503
    # A failed attempt is still audited, attributed to the commander.
    args, kwargs = log.calls[0]
    assert args[0] == "TX_BRIDGE_CONTROL"
    assert kwargs.get("actor") == COMMANDER["email"]
    assert kwargs.get("meta", {}).get("outcome") == "helper_unavailable"


@pytest.mark.asyncio
async def test_helper_error_audits_and_502(monkeypatch):
    log = _AsyncCapture()
    monkeypatch.setattr(srv, "log_event", log)
    monkeypatch.setattr(srv.ws_manager, "broadcast_json", _AsyncCapture())

    async def _boom():
        raise tx_bridge_control.TxHelperError("systemctl start failed")
    monkeypatch.setattr(srv.tx_bridge_control, "stand_down", _boom)

    with pytest.raises(HTTPException) as ei:
        await srv.tx_stand_down(COMMANDER)
    assert ei.value.status_code == 502
    assert log.calls[0][1].get("meta", {}).get("outcome") == "helper_error"


# ---------------------------------------------------------------------
# tx_subsystem derivation (observable state -> plain-language block)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tx_subsystem_bridges_online_owner_rf_bridge(monkeypatch):
    monkeypatch.setattr(srv, "_expire_range_authorization", _AsyncCapture())
    monkeypatch.setattr(srv.ws_manager, "tx_consumers", lambda: ["mavlink", "jam"])
    monkeypatch.setattr(srv.db, "detections", _FakeColl(count=0))
    monkeypatch.setattr(srv, "_tx_halted", True, raising=False)

    block = await srv._tx_subsystem_status()
    assert block["bridges_online"] is True
    assert block["sik_owner"] == "rf-bridge"
    assert block["tx_halted"] is True
    assert set(block["range_auth"].keys()) == set(srv.RANGE_AUTH_EFFECTS)


@pytest.mark.asyncio
async def test_tx_subsystem_sniffer_when_offline_but_recent_rx(monkeypatch):
    monkeypatch.setattr(srv, "_expire_range_authorization", _AsyncCapture())
    monkeypatch.setattr(srv.ws_manager, "tx_consumers", lambda: [])
    monkeypatch.setattr(srv.db, "detections", _FakeColl(count=3))
    monkeypatch.setattr(srv, "_tx_halted", False, raising=False)

    block = await srv._tx_subsystem_status()
    assert block["bridges_online"] is False
    assert block["sik_link_up"] is True
    assert block["sik_owner"] == "sniffer"


@pytest.mark.asyncio
async def test_tx_subsystem_unknown_owner_when_idle(monkeypatch):
    monkeypatch.setattr(srv, "_expire_range_authorization", _AsyncCapture())
    monkeypatch.setattr(srv.ws_manager, "tx_consumers", lambda: [])
    monkeypatch.setattr(srv.db, "detections", _FakeColl(count=0))

    block = await srv._tx_subsystem_status()
    assert block["bridges_online"] is False
    assert block["sik_link_up"] is False
    assert block["sik_owner"] is None


class _FakeColl:
    """Stand-in for a motor collection whose count_documents returns a fixed
    count (the only method _tx_subsystem_status calls on db.detections)."""
    def __init__(self, count: int):
        self._count = count

    async def count_documents(self, *_a, **_k):
        return self._count
