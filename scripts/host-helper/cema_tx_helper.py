#!/usr/bin/env python3
"""cema-tx-helper — minimal, hard-whitelisted host privilege helper.

PURPOSE
-------
Lets the (unprivileged, containerised) CEMA backend trigger the ONLY three
host-level systemctl operations it legitimately needs to bring the TX bridges
online / stand them down, WITHOUT giving the container the host's systemctl,
sudo, or Docker socket. It runs as root on the host, listens on a Unix-domain
socket that is bind-mounted into the backend container, and accepts exactly one
of three CONSTANT action keywords. It maps each keyword to a pre-baked,
hard-coded sequence of whitelisted (verb, unit) pairs. NOTHING about the command
run comes from the network beyond that single keyword — no unit name, no verb,
no arguments, no shell.

SECURITY BOUNDARY (READ BEFORE DEPLOY — this process runs as root)
-----------------------------------------------------------------
  * Input grammar is THREE keywords: "online" | "standdown" | "status".
    Anything else is rejected with an error and NO host action is taken.
  * The set of commands this daemon can EVER execute is the frozen
    _ALLOWED_COMMANDS whitelist below: `systemctl {start,stop,is-active}` on
    exactly {cema-rf-bridge, cema-jam-bridge, cema-mavlink-sniffer}. Units and
    verbs are compile-time constants in THIS file; they are never read from the
    request. Even a fully-compromised backend container cannot make this daemon
    run any other command, touch any other unit, or pass any argument.
  * subprocess is always invoked with an explicit argv list and shell=False, so
    there is no shell-injection surface even in principle.
  * The listener is AF_UNIX only — it is never exposed on any TCP port. Reach is
    limited to whoever can write the bind-mounted socket path on the host.

Install: see scripts/host-helper/README.md (systemd unit provided). Needs root.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from typing import Dict, List, Tuple

SOCK_PATH = os.environ.get("CEMA_TX_HELPER_SOCK", "/run/cema-tx-helper/control.sock")
# Socket file mode. Default 0o660: owner+group read/write. The backend container
# runs as root (see backend/Dockerfile — no USER directive), so root:root 0660
# is reachable from it. If the backend is ever run as a non-root uid, either
# widen this to 0o666 via env or add that uid to the socket's group at install
# time (documented in README.md).
SOCK_MODE = int(os.environ.get("CEMA_TX_HELPER_SOCK_MODE", "0o660"), 8)
# Per-systemctl-call timeout. The sniffer unit's ExecStartPre can wait on a udev
# symlink for up to ~30s on a cold SiK adapter; allow headroom.
CMD_TIMEOUT_S = float(os.environ.get("CEMA_TX_HELPER_CMD_TIMEOUT_S", "40"))
MAX_REQUEST_BYTES = 4096  # a request is a tiny one-line JSON object

# The three units this helper is allowed to touch — compile-time constants.
UNIT_RF_BRIDGE = "cema-rf-bridge"
UNIT_JAM_BRIDGE = "cema-jam-bridge"
UNIT_SNIFFER = "cema-mavlink-sniffer"
_UNITS = (UNIT_RF_BRIDGE, UNIT_JAM_BRIDGE, UNIT_SNIFFER)

# FROZEN command whitelist. A command is only ever executed if the exact
# (verb, unit) tuple appears here. Nothing outside this set can run.
_ALLOWED_COMMANDS: frozenset[Tuple[str, str]] = frozenset(
    [("start", u) for u in _UNITS]
    + [("stop", u) for u in _UNITS]
    + [("is-active", u) for u in _UNITS]
)

# Hard-coded action -> ordered mutation sequence. The keyword selects a
# pre-baked sequence; the request can never supply its own steps.
#   online:    hand the SiK to TX  (stop sniffer, start rf+jam bridges)
#   standdown: return the SiK to RX (stop rf+jam bridges, start sniffer)
#   status:    no mutation — report only
_ACTION_SEQUENCES: Dict[str, List[Tuple[str, str]]] = {
    "online": [
        ("stop", UNIT_SNIFFER),
        ("start", UNIT_RF_BRIDGE),
        ("start", UNIT_JAM_BRIDGE),
    ],
    "standdown": [
        ("stop", UNIT_RF_BRIDGE),
        ("stop", UNIT_JAM_BRIDGE),
        ("start", UNIT_SNIFFER),
    ],
    "status": [],
}

logging.basicConfig(
    level=logging.INFO,
    format="cema-tx-helper %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cema-tx-helper")


def _run_systemctl(verb: str, unit: str) -> Tuple[int, str]:
    """Execute ONE whitelisted `systemctl <verb> <unit>` with a fixed argv and
    shell=False. Refuses anything not in the frozen whitelist (belt-and-braces:
    the callers only ever pass whitelisted tuples, but this guarantees it)."""
    if (verb, unit) not in _ALLOWED_COMMANDS:
        raise ValueError(f"refusing non-whitelisted command: systemctl {verb} {unit}")
    try:
        proc = subprocess.run(
            ["systemctl", verb, unit],
            capture_output=True,
            text=True,
            timeout=CMD_TIMEOUT_S,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return (124, "timeout")
    out = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return (proc.returncode, out)


def _snapshot() -> Dict[str, object]:
    """`systemctl is-active` for the three units + derived sik_owner. is-active
    returns rc 0 and 'active' when running; non-zero otherwise (inactive/failed/
    unknown) — we report the raw state string honestly."""
    units: Dict[str, str] = {}
    active: Dict[str, bool] = {}
    for unit in _UNITS:
        rc, state = _run_systemctl("is-active", unit)
        state = state or ("active" if rc == 0 else "unknown")
        units[unit] = state
        active[unit] = rc == 0 and state == "active"
    if active[UNIT_RF_BRIDGE] or active[UNIT_JAM_BRIDGE]:
        sik_owner = "rf-bridge"
    elif active[UNIT_SNIFFER]:
        sik_owner = "sniffer"
    else:
        sik_owner = None
    return {
        "units": units,
        "bridges_online": bool(active[UNIT_RF_BRIDGE] or active[UNIT_JAM_BRIDGE]),
        "rf_bridge_active": active[UNIT_RF_BRIDGE],
        "jam_bridge_active": active[UNIT_JAM_BRIDGE],
        "sniffer_active": active[UNIT_SNIFFER],
        "sik_owner": sik_owner,
    }


def _handle_action(action: str) -> Dict[str, object]:
    if action not in _ACTION_SEQUENCES:
        return {"ok": False, "action": action, "error": f"unknown action {action!r}"}

    steps_run = []
    for verb, unit in _ACTION_SEQUENCES[action]:
        rc, out = _run_systemctl(verb, unit)
        steps_run.append({"verb": verb, "unit": unit, "rc": rc, "out": out})
        if rc != 0:
            # Surface the first failing step with a full snapshot for auditing;
            # do not continue a partially-applied handoff silently.
            snap = _snapshot()
            return {
                "ok": False,
                "action": action,
                "error": f"systemctl {verb} {unit} failed (rc={rc}): {out}",
                "steps": steps_run,
                **snap,
            }
    snap = _snapshot()
    log.info("action=%s applied ok sik_owner=%s bridges_online=%s",
             action, snap.get("sik_owner"), snap.get("bridges_online"))
    return {"ok": True, "action": action, "steps": steps_run, **snap}


def _serve_conn(conn: socket.socket) -> None:
    try:
        conn.settimeout(CMD_TIMEOUT_S + 5)
        chunks = []
        total = 0
        while True:
            b = conn.recv(1024)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > MAX_REQUEST_BYTES or b"\n" in b:
                break
        raw = b"".join(chunks).split(b"\n", 1)[0]
        try:
            req = json.loads(raw.decode("utf-8"))
            action = req.get("action") if isinstance(req, dict) else None
        except (ValueError, UnicodeDecodeError):
            action = None
        if not isinstance(action, str):
            resp = {"ok": False, "error": "malformed request: expected {\"action\": <keyword>}"}
        else:
            resp = _handle_action(action)
        conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except Exception as e:  # never let one bad client kill the daemon
        log.warning("connection handler error: %s", e)
        try:
            conn.sendall((json.dumps({"ok": False, "error": f"internal helper error: {e}"}) + "\n").encode("utf-8"))
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main() -> int:
    sock_dir = os.path.dirname(SOCK_PATH)
    os.makedirs(sock_dir, exist_ok=True)
    if os.path.exists(SOCK_PATH):
        os.unlink(SOCK_PATH)

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, SOCK_MODE)
    srv.listen(8)
    log.info("listening on %s (mode=%o); whitelist units=%s", SOCK_PATH, SOCK_MODE, ",".join(_UNITS))

    stop = threading.Event()

    def _shutdown(signum, _frame):
        log.info("received signal %s, shutting down", signum)
        stop.set()
        try:
            srv.close()
        except OSError:
            pass

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            # One short-lived thread per connection; requests are quick and
            # serialized by systemctl itself. Keeps a slow start/stop from
            # blocking a concurrent status read.
            threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()
    finally:
        try:
            if os.path.exists(SOCK_PATH):
                os.unlink(SOCK_PATH)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
