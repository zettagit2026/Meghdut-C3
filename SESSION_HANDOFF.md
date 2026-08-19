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

## MIGRATION IN PROGRESS (new hardware — Lenovo ST550 — arriving 2026-08-20)
- Repo was **renamed on GitHub**: `joydipdemo` → **`Meghdut-C3`** (`github.com/zettagit2026/Meghdut-C3`).
  Local remote repointed via SSH alias `github-meghdut` (fresh deploy key `~/.ssh/meghdut_c3_ed25519`,
  write-scoped, authenticating). `main` fully synced at `88b3634` — nothing unpushed as of the rename.
  NOTE: local working dir is still named `joydipdemo/` (cosmetic; git unaffected).
- New box = **Lenovo ST550, pre-used rental** = Tier-A-class BRIDGE host (dual Xeon octa @ 2.10 GHz,
  32 GB ECC, 3×1.2 TB SAS HDD + RAID, USB-only, no GPU, PCIe-expandable). Two honest deviations vs spec:
  spinning SAS (not NVMe) and low single-thread clock (backend is single-thread-bound). B-upgradable later.
- **Two new planning docs drive tomorrow** (read both before cutover):
  - `MIGRATION_RUNBOOK.md` — ST550-specific cutover: provisioning, state migration (Mongo/Caddy CA/
    .env/udev/venv+model), deploy+rebuild, bring-up ordering (TX fail-closed, 6 dormant units stay inert),
    a PASS/FAIL validation gate, rollback (old host stays live until green), and open decisions.
  - `CAPABILITY_ROADMAP.md` — honest ledger (12 BUILT / 6 HW-gated / 2 PARTIAL / 4 DESIGNED / 1 NOT-STARTED)
    + phased plan. Key finds: RC-takeover/1.11 is BUILT (RFI draft is stale — reconcile up); compliance
    docs OVERCLAIM power/range/band (reconcile down); GNSS-spoof DSP is a stub; OB-06 FPGA intent (throughput
    vs deterministic-latency/SWaP-C) is the decisive open question. Demo target: user confirms post-migration.

## OPEN ITEMS (what the next session should pick up)
1. **`git push`** — after the rename, push to `github-meghdut` remote (no credential helper here; the
   user pushes manually). At handoff-update time local == origin at `88b3634`.
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
