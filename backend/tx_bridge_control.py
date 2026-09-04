"""Container -> host privilege bridge for the TX-bridge SiK handoff.

WHY THIS EXISTS
---------------
The backend runs inside the `cema-backend` Docker container. Bringing the TX
bridges online (or standing them down) is a HOST-level `systemctl` operation on
three units that own the SiK serial radio:

    cema-mavlink-sniffer   (RX-only passive intercept; owns the SiK when idle)
    cema-rf-bridge         (MAVLink TX consumer; must own the SiK to transmit)
    cema-jam-bridge        (RF barrage-jam TX consumer)

A container cannot (and must not) be handed the host's `systemctl`/Docker
socket wholesale — that would be root-equivalent on the host. Instead the
backend speaks to a TINY, HARD-WHITELISTED root helper daemon on the host over
a Unix-domain socket bind-mounted into the container. See
`scripts/host-helper/cema_tx_helper.py` for the daemon and
`scripts/host-helper/README.md` for the exact security boundary and install
steps (the daemon needs root to install/run and is flagged for the privileged
deploy step).

SECURITY BOUNDARY (client side)
-------------------------------
This module NEVER sends a unit name, a `systemctl` verb, or any shell string
over the wire. It sends exactly one of three constant *action keywords*
("online" | "standdown" | "status"). The daemon maps that keyword to a
pre-baked, hard-coded sequence of whitelisted (verb, unit) pairs. Even a fully
compromised backend container can therefore only ever start/stop those three
specific units (or read their status) — nothing else. There is no code path
here that can be coaxed into running an arbitrary command on the host.

The action keyword is validated against ACTIONS here too (defense in depth), so
a bad value fails fast in-process rather than travelling to the daemon.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict

# Path to the bind-mounted Unix-domain control socket. Overridable via env for
# non-standard deploys, but the default matches docker-compose.yml's bind mount
# and scripts/host-helper/cema-tx-helper.service.
TX_HELPER_SOCK = os.environ.get("CEMA_TX_HELPER_SOCK", "/run/cema-tx-helper/control.sock")

# How long to wait for the daemon to complete a request. A systemctl start/stop
# of these units is sub-second in practice; the sniffer's ExecStartPre can wait
# on a udev symlink for up to ~30s on a cold adapter, so we allow generous
# headroom while still failing fast enough that the operator gets a definite
# answer rather than a spinner that never resolves.
TX_HELPER_TIMEOUT_S = float(os.environ.get("CEMA_TX_HELPER_TIMEOUT_S", "45"))

# The ONLY action keywords this client will ever send. Mirrors the daemon's own
# hard-coded action table; kept here purely as an in-process guard so an
# unexpected value never reaches the socket.
ACTIONS = ("online", "standdown", "status")


class TxHelperUnavailable(RuntimeError):
    """The host helper socket is absent or unreachable (daemon not installed /
    not running / bind mount missing). Distinct from TxHelperError so callers
    can surface an honest 'host helper not available' rather than pretending the
    handoff failed for an operational reason."""


class TxHelperError(RuntimeError):
    """The daemon accepted the request but reported a failure (e.g. a systemctl
    call exited non-zero). Carries the daemon's structured payload for auditing."""

    def __init__(self, message: str, payload: Dict[str, Any] | None = None):
        super().__init__(message)
        self.payload = payload or {}


async def _request(action: str) -> Dict[str, Any]:
    if action not in ACTIONS:
        # In-process whitelist: never even open the socket for an unknown action.
        raise ValueError(f"tx_bridge_control: refusing unknown action {action!r}")

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path=TX_HELPER_SOCK),
            timeout=TX_HELPER_TIMEOUT_S,
        )
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        raise TxHelperUnavailable(
            f"TX bridge control helper is not reachable at {TX_HELPER_SOCK} "
            f"({type(e).__name__}). The cema-tx-helper daemon must be installed "
            f"and running on the host (see scripts/host-helper/README.md)."
        ) from e

    try:
        writer.write((json.dumps({"action": action}) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=TX_HELPER_TIMEOUT_S)
    except (asyncio.TimeoutError, OSError) as e:
        raise TxHelperUnavailable(
            f"TX bridge control helper did not respond within "
            f"{TX_HELPER_TIMEOUT_S}s ({type(e).__name__})."
        ) from e
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass

    if not raw:
        raise TxHelperUnavailable("TX bridge control helper closed the connection with no response.")

    try:
        resp = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise TxHelperError(f"TX bridge control helper returned a malformed response: {e}")

    if not isinstance(resp, dict) or not resp.get("ok"):
        msg = (resp or {}).get("error") if isinstance(resp, dict) else None
        raise TxHelperError(msg or "TX bridge control helper reported a failure", resp if isinstance(resp, dict) else None)

    return resp


async def bring_online() -> Dict[str, Any]:
    """Perform the SiK handoff to TRANSMIT: stop the passive sniffer, then start
    the rf-bridge and jam-bridge TX consumers. Idempotent (systemctl start/stop
    on an already-in-that-state unit is a no-op success). Returns the daemon's
    resulting unit-status snapshot."""
    return await _request("online")


async def stand_down() -> Dict[str, Any]:
    """Reverse of bring_online(): stop the two TX bridges and return the SiK to
    the passive RX sniffer. Idempotent. Returns the resulting unit-status snapshot."""
    return await _request("standdown")


async def status() -> Dict[str, Any]:
    """Read-only: return the daemon's current `systemctl is-active` snapshot of
    the three units plus the derived sik_owner. Never mutates host state."""
    return await _request("status")
