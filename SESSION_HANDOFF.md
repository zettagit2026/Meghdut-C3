# MEGHDUT C3 — Session Handoff

_Written 2026-08-14 to brief a fresh Claude Code session (this project moved to
the VS Code extension / desktop app). Read this + the last ~16 git commits to
get current._

## What this is
MEGHDUT C3 — a CEMA (Cyber-Electromagnetic Activities) counter-UAS platform for
the Indian Army (PMO Suraj / Army Cyber Range), authorized sandboxed defense R&D.
Stack: FastAPI + MongoDB + React (Docker Compose: mongo/backend/frontend/caddy) +
a fleet of systemd Python "field-bridge" services doing HackRF SDR / MAVLink /
Kismet / RF DSP. Repo: `github.com/zettagit2026/joydipdemo` (private).

## Hosts / access
- **Dev Mac** (here): canonical repo at `~/Desktop/Zettawise/CEMA/PMO Suraj/tool/joydipdemo`.
  NOTE the path gained a `CEMA/` parent recently — old sessions used a path without it.
- **Primary deploy host**: `172.16.16.196`, code at `/CEMA/joydipdemo`, SSH user `biswajit`,
  password via macOS Keychain: `security find-generic-password -a biswajit -s cema-primary-ssh -w`.
- Deploys are **scp + checksum via `scripts/deploy.sh`** (atomic, version-stamped, `--apply` to mutate;
  dry-run default). NOT git-pull on primary. `scripts/check_deploy_drift.sh` is read-only.
- **Rule: the Mac is code-only** — all docker/build/test/RF runs happen on primary, never locally.

## Current live state on primary (last verified 2026-08-14)
- 4 containers up (cema-backend/frontend/caddy/mongo); backend rebuilt with the latest code.
- 6 field-bridge/kismet services active (hackrf-rx, mavlink-sniffer, fpv-bridge, ml-classify-bridge,
  kismet, kismet-bridge). All NRestarts=0.
- **TX is HALTED** (`_tx_halted=True`, fail-closed, in-memory, auto-re-halts on every backend boot).
  Surfaced as `tx_halted` in `GET /api/health`. Clearing it = commander `POST /emergency/resume`
  (or the RESUME TX button). Do NOT clear it without explicit user go-ahead.
- Kismet BT/BLE scanning live (TP-Link UB500 as `hci1`).

## Safety architecture (do not weaken)
Every RF-transmit path (`/payloads/deploy`, `/mavlink/broadcast`, `/payloads/jam`,
`/payloads/gnss-spoof`) is gated: commander role → TX-halt check → effect+target-bound
single-use arm token → fire-time IFF friendly-fire interlock → backend range-auth lease →
bridge-side range-auth poll. Tamper-evident (append-time hash-chain) audit log with an
append-only anchor. All three offensive capabilities (B1 engagement planner, B7 RC-override
takeover) are human-in-the-loop, adversarially reviewed, and were built to this standard.

## OPEN ITEMS (what the next session should pick up)
1. **`git push`** — local is **6 commits ahead of origin/main** (no credential helper here; the
   user pushes manually). Commits `1c22fb3`..`fe5f04f`.
2. **SiK / RFD900 MAVLink radio** — still PHYSICALLY ABSENT on primary (`/dev/cema-sik-adapter`
   not present). `cema-mavlink-sniffer` idles gracefully (waiting-for-device, survives hot-plug now);
   `cema-rf-bridge` (the RC-override TX bridge) is staged **dormant** (disabled+stopped). When the
   radio is plugged in: follow `rf-bridge/ACTIVATION.md` (stop cema-mavlink-sniffer first — single
   serial port contention — then `systemctl enable --now cema-rf-bridge`). The user has said "both
   activated" before but the box shows otherwise — verify hardware presence, don't assume.
3. **Approval / audience matrix** — during TESTING everything is OPERATOR-ONLY (see
   `.claude/settings.local.json` autoMode env). The full multi-audience matrix (team /
   Army-PMO-Suraj / etc.) MUST be defined **before final deployment**.
4. **2 new parser units** (`cema-crsf-parser.service`, `cema-dronecan-parser.service`) — copied to
   primary but deliberately **inert** (not daemon-reloaded/enabled). Enabling is a separate reviewed
   decision; confirm neither has a TX path first.
5. **`verify-operator@meghaduta.mil`** — a real operator account a gate-test created on primary; keep
   or delete.
6. From the independent critic review (all 4 must-fix DONE + deployed): remaining should-improve items
   are Mongo retention/indexing TTL, per-container memory limits, breaking up the 4,668-line
   server.py monolith, and promoting the CI integration job to a required gate once green.

## Reference docs in the repo
- `SERVER_SPECIFICATION.md` + `MEGHDUT_C3_Server_Specification.docx` — migration hardware spec.
- `HARDWARE_PROCUREMENT.md` — procurement list (Bluetooth PROCURED; SiK radio pending).
- `rf-bridge/ACTIVATION.md` — SiK radio + rf-bridge activation runbook.
- `DOC_CORRECTIONS_MEMO.md` — known doc-vs-reality discrepancies (range, protocol count, etc.).
- `scripts/deploy.sh` / `scripts/check_deploy_drift.sh` — deploy + drift tooling.

## Working conventions established this session
- Route substantive work through division/OMC subagents (executor/critic/security-reviewer/verifier/SRE);
  build → adversarial review → deploy. Never enable a TX capability on a single agent's say-so.
- Staged, third-eye-verified deploys; stop-and-report on any instability (this caught a real
  mavlink_sniffer crash regression mid-deploy).
- Honesty rule: no fabricated data, no overstated capability — surface gaps plainly.
