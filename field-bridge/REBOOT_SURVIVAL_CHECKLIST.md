# Reboot Survival Checklist (Task #140)

**Why this exists**: the 2026-07-29 gap audit found no doc anywhere in this
project tracked "does this artifact/state survive a reboot?" as its own
checklist category. It took a real incident — the ml-classify-bridge
checkpoint (#133) — before the same class of gap was found again, twice more,
in FPV capture output (#137) and the emergency-abort TX-halt flag (#136).
Three incidents from the same root cause is a process gap, not three
unrelated bugs. This doc is the fix: a short, standing checklist for every
future field-bridge script, systemd unit, or backend safety state.

Not a treatise — for the full reasoning behind the TX-halt fix, see
[`backend/TX_HALT_PERSISTENCE_SCOPE.md`](../backend/TX_HALT_PERSISTENCE_SCOPE.md).

---

## 1. The checklist

Run through this for every **new** field-bridge script, systemd service, or
backend in-memory safety state before it ships:

1. **Persistent artifacts.** Does this component depend on a file/directory
   that must survive a reboot (model checkpoints, calibration data, captured
   evidence, logs someone will need in an after-action review)?
   - If yes: is the **default** path under the project directory (e.g.
     `field-bridge/...`), not `/tmp`, `/var/run`, or any tmpfs-backed path
     that a reboot wipes?
   - Is there an explicit env var override, and is it documented in the
     script's module docstring and the systemd unit's comments?

2. **ExecStart python matches real imports.** Does the systemd unit's
   `ExecStart=` point at the field-bridge venv's `.venv/bin/python3` (or
   equivalent dedicated venv), not `/usr/bin/python3` — verified by actually
   grepping the script's `import`/`from` lines for non-stdlib packages
   (`numpy`, `torch`, `requests`, etc.), not by "it currently happens to
   work on my machine"? A script with zero non-stdlib imports today can
   still gain one in a future change — re-check this whenever imports
   change, not just at initial creation.

3. **Liveness is actually monitored.** If this service dies silently, is
   that caught within minutes via `/api/health` and/or `preflight.sh`
   (`check_bridge_heartbeat` for log-freshness, or at minimum
   `report_tx_service`/`systemctl is-active` for TX-path services) — or
   would it only be discovered by accident, days later, when someone
   notices detections stopped?

4. **Safety-critical in-memory state defaults conservative.** Any new
   flag in the shape of `_tx_halted` / `_range_authorization` /
   `_arm_tokens` must default to the **safe/restrictive** state on every
   process start (halted, unauthorized, no valid token) — never the
   permissive one — regardless of what state it was in before the restart.
   See `TX_HALT_PERSISTENCE_SCOPE.md` §2-3 for why this project persists
   safety state as "fail-closed default," not "remember last value in
   Mongo."

---

## 2. Current status by service/state

| Component | Persistent artifact? | ExecStart uses venv python? | Liveness monitored? | Safe default on restart? |
|---|---|---|---|---|
| `ml_classify_bridge.py` (`cema-ml-classify-bridge.service`) | Yes — `.pt` checkpoint. **Fixed (#133)**: default path documented, `CEMA_ML_CHECKPOINT` override supported. | **Documented, not enforced** — unit file has an explicit comment warning to point `ExecStart` at the ML venv (`torch` import), but the checked-in `ExecStart=` still reads `/usr/bin/python3`. Deployer must edit per-host. | Yes — covered by `check_bridge_heartbeat` in `preflight.sh`. | N/A (no safety-gating state) |
| `fpv_video_bridge.py` (`cema-fpv-bridge.service`) | Yes — captured evidence. **Fixed (#137)**: default capture dir moved off `/tmp` onto a persistent path under `field-bridge/`, matching the #133 fix pattern; regression-tested in `test_fpv_capture_dir.py`. | **Not pinned (#138, pending)** — imports `numpy` (non-stdlib) but `ExecStart=/usr/bin/python3 -u fpv_video_bridge.py ...`, no venv comment/warning at all. | **Not covered (#139, pending)** — no `check_bridge_heartbeat` entry and no `report_tx_service` entry in `preflight.sh`. | N/A |
| `hackrf_rx.py` (`cema-hackrf-rx.service`) | No persistent artifact of its own (posts detections to backend). | **Not pinned (#138, pending)** — imports `numpy`, but `ExecStart=/usr/bin/python3 -u hackrf_rx.py ...` with no venv comment. | Yes — covered by `check_bridge_heartbeat`. | N/A |
| `mavlink_sniffer.py` (`cema-mavlink-sniffer.service`) | No persistent artifact. | OK as-is — only stdlib + `requests`; `/usr/bin/python3` is fine as long as `requests` is available system-wide (verify at deploy time). | **Partial (#139, pending)** — covered by `report_tx_service` (active/inactive only), **not** by `check_bridge_heartbeat` (no log-freshness check), so a hung-but-still-running process would not be caught. | N/A |
| `_tx_halted` (backend/server.py) | N/A — deliberately in-memory only. | N/A | Yes — surfaced in `/api/health` (`tx_halted` field) per the #136 fix. | **Fixed (#136)** — defaults `True` (TX-halted) on every process start; commander must explicitly `POST /api/emergency/resume`. See `TX_HALT_PERSISTENCE_SCOPE.md`. |
| `_range_authorization`, `_arm_tokens`, `_jam_confirm_tokens` (backend/server.py) | N/A — deliberately in-memory only. | N/A | Indirectly, via the endpoints that check them. | Already correct — always defaulted to the conservative state (no auth / no valid token) since before this audit; cited in `TX_HALT_PERSISTENCE_SCOPE.md` §2 as the pattern `_tx_halted` was fixed to match. |

**Pending work**: #138 (pin `ExecStart` to venv python for `cema-hackrf-rx.service`
and `cema-fpv-bridge.service`, verified against their `numpy` imports) and
#139 (add `check_bridge_heartbeat` coverage for `cema-mavlink-sniffer.service`
and `cema-fpv-bridge.service` in `preflight.sh`).

---

## 3. Adding a new service? Do this

1. Fill in one new row of the table above before merging.
2. Add the corresponding `check_bridge_heartbeat` (or at minimum
   `report_tx_service`) line to `preflight.sh` in the same change.
3. If the script has any non-stdlib import, set `ExecStart=` to the venv
   python and say so in a unit-file comment — don't leave it to the next
   deployer to discover via a crash loop.
4. If the script touches any safety-gating state, state explicitly in the
   PR description what the restart-time default is and why it's safe.
