# cema-tx-helper — host privilege helper for the GUI TX-bridge handoff

This is the **privileged host-side install artifact** that lets the operator
bring the TX bridges online / stand them down entirely from the console GUI,
instead of a human running `systemctl` on a shell. It must be installed on the
TX host (`.186`) **as root** during the coordinated deploy step. Flag it clearly
to the SRE performing the deploy: **this component runs as root.**

## What it does

The CEMA backend runs inside the `cema-backend` container and cannot run host
`systemctl`. This helper is a tiny root daemon on the host that:

1. Listens on a Unix-domain socket `/run/cema-tx-helper/control.sock`
   (bind-mounted into the backend container — see `docker-compose.yml`).
2. Accepts exactly **three constant action keywords**: `online`, `standdown`,
   `status`.
3. Maps each keyword to a **pre-baked, hard-coded** sequence of whitelisted
   `systemctl` calls on **exactly three units**:
   - `cema-mavlink-sniffer` (RX-only passive intercept; owns the SiK when idle)
   - `cema-rf-bridge` (MAVLink TX consumer)
   - `cema-jam-bridge` (RF barrage-jam TX consumer)

| action | sequence |
|-----------|----------------------------------------------------------------------|
| online | stop `cema-mavlink-sniffer` → start `cema-rf-bridge` → start `cema-jam-bridge` |
| standdown | stop `cema-rf-bridge` → stop `cema-jam-bridge` → start `cema-mavlink-sniffer` |
| status | `systemctl is-active` for the three units (no mutation) |

All three actions are **idempotent** (`systemctl start/stop` on an
already-in-that-state unit is a no-op success).

## Security boundary (why it cannot be abused)

- **The wire grammar is three keywords.** No unit name, no `systemctl` verb, no
  argument, and no shell string ever crosses the socket. The daemon selects a
  compile-time-constant sequence from the keyword; the request cannot supply its
  own steps. See `_ACTION_SEQUENCES` and `_ALLOWED_COMMANDS` in
  `cema_tx_helper.py`.
- **Frozen command whitelist.** A command runs only if the exact `(verb, unit)`
  tuple is in `_ALLOWED_COMMANDS` = `{start,stop,is-active}` × the three units
  above. Anything else raises before executing.
- **No shell.** `subprocess.run([...], shell=False)` with an explicit argv —
  no shell-injection surface even in principle.
- **AF_UNIX only.** Never bound to any TCP port. Reachable only by a process
  that can write the bind-mounted socket path.
- **Blast radius if the backend container is fully compromised:** the attacker
  can start/stop those three specific units (or read their status) — and
  nothing else. It is not a general `systemctl`/sudo/Docker-socket handoff.

This is deliberately the **tightest-privilege** mechanism available: a socket
daemon with a 3-keyword input grammar, rather than mounting the Docker socket,
granting broad sudo, or exposing host `systemctl`.

## Files this needs on the host

- `/CEMA/joydipdemo/scripts/host-helper/cema_tx_helper.py` — the daemon (ships
  in the repo; already present if the repo is checked out at `/CEMA/joydipdemo`).
- `/etc/systemd/system/cema-tx-helper.service` — copied from
  `cema-tx-helper.service` in this directory.
- `/run/cema-tx-helper/` — created automatically by systemd (`RuntimeDirectory`).
- The bind mount in `docker-compose.yml` on the `backend` service:
  `- /run/cema-tx-helper:/run/cema-tx-helper` (already added in the repo).

## Install (root, on the TX host `.186`)

```sh
# 1) Ensure the repo is checked out at /CEMA/joydipdemo (adjust the unit's
#    ExecStart path if your checkout differs).
sudo cp /CEMA/joydipdemo/scripts/host-helper/cema-tx-helper.service \
        /etc/systemd/system/cema-tx-helper.service

# 2) Grant this host user the ability to control ONLY these three units without
#    a password. This is what actually lets the root daemon's `systemctl` calls
#    succeed under systemd's default policy. (The daemon runs as root, so this
#    step is normally a no-op — root may already manage units — but keep the
#    unit's User=root as shipped rather than downgrading it.)

# 3) Reload + enable + start.
sudo systemctl daemon-reload
sudo systemctl enable --now cema-tx-helper.service
sudo systemctl status cema-tx-helper.service   # expect: active (running)

# 4) Recreate the backend container so the new bind mount takes effect.
#    NOTE: a backend restart re-halts TX (fail-closed _tx_halted default) and
#    drops the WS bridge consumers — this is EXPECTED. The commander re-arms
#    from the GUI afterward (Bring TX Online + Resume TX).
cd /CEMA/joydipdemo && docker compose up -d backend
```

### Non-root backend container

The backend container runs as **root** (see `backend/Dockerfile` — no `USER`),
so the default `0660 root:root` socket is reachable. If you ever switch the
backend to a non-root uid, either:

- set `Environment=CEMA_TX_HELPER_SOCK_MODE=0o666` in the unit, **or**
- create a shared group, `chown root:<group>` the socket (add
  `ExecStartPost=/bin/chgrp <group> /run/cema-tx-helper/control.sock`), and run
  the container with that supplementary gid.

## Verify

```sh
# From the host:
printf '{"action":"status"}\n' | sudo socat - UNIX-CONNECT:/run/cema-tx-helper/control.sock

# From inside the backend container (the path the app uses):
docker exec cema-backend python3 -c \
 "import asyncio, tx_bridge_control as t; print(asyncio.run(t.status()))"
```

Both should return a JSON snapshot with `units`, `bridges_online`, and
`sik_owner`.

## Uninstall

```sh
sudo systemctl disable --now cema-tx-helper.service
sudo rm /etc/systemd/system/cema-tx-helper.service
sudo systemctl daemon-reload
```
