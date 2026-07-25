# MEGHADUTA C³ — Army Demonstration Playbook

**Classification:** RESTRICTED — For MoD / Army Tech Team evaluation only.
**Build:** v0.9-eval · Zettawise
**Duration:** 8–10 minutes on stage · 2 hours setup

---

## 1. Deliverables (what the evaluators leave with)

| # | Item | Where |
|---|---|---|
| 1 | Live end-to-end demonstration on real hardware | On stage |
| 2 | Signed **Mission Report PDF** (SHA-256 hash-chained audit trail) | Mission Log → Export button |
| 3 | Source code (Docker-runnable) | `zettagit2026/joydipdemo` |
| 4 | Installation guide (`INSTALL.md`) | Repo root |
| 5 | RF-bridge deployment guides (`rf-bridge/README.md` — MAVLink TX/RX; `field-bridge/README.md` — HackRF detection) | Repo `rf-bridge/`, `field-bridge/` |
| 6 | Requirements-gap alignment note (this document) | `DEMO_PLAYBOOK.md` |

---

## 2. Hardware setup

**Ground station (operator side):**
| Item | Model / spec | Purpose |
|---|---|---|
| Laptop | Ubuntu 22.04, 16 GB RAM, Docker Desktop | Runs app + rf-bridge (MAVLink) + field-bridge (HackRF) |
| SDR | HackRF One + telescopic ANT500 | Wide-band RF detection |
| Optional PA | 370–2700 MHz LNA/PA | RX sensitivity + future GNSS spoof |
| Telemetry radio | SiK 915 MHz on `/dev/ttyUSB0` | MAVLink TX/RX to drone |
| Presenter laptop | Second screen mirroring Mission Planner | Shows drone-side reaction |

**Airborne (test drone):**
| Item | Notes |
|---|---|
| Custom quad | Pixhawk or CUAV FC, ArduPilot 4.5+ / PX4 1.14+ |
| SiK 915 MHz air module | Paired to ground module (same NET-ID + baud) |
| MAVLink over serial 1 | Standard 57600 baud |
| **Props physically removed** | For all kinetic payload demos |

**Range:**
| Item | Notes |
|---|---|
| Screened bench area | Minimum: 5×5 m indoor RF-quiet room |
| No third-party drones | Confirm nothing else on same SiK NET-ID within RF range |
| Fire extinguisher + kill-switch operator | Standard drone-safety kit |

---

## 3. Pre-flight T-30 min checklist

Run this in order, tick each box before the demo starts. If any check fails, **do not proceed** — resolve first.

```
[  ]  1. Backend docker stack UP
        $ docker compose ps → all 3 containers "Up (healthy)"

[  ]  2. HackRF + MAVLink bridge diagnostics pass
        (task #36: HackRF scanning/diagnostics moved to field-bridge/;
        rf-bridge/ now only runs the MAVLink TX/RX bridge)
        $ cd field-bridge && python hackrf_baseline_test.py
        $ cd rf-bridge && ./run.sh bridge   (confirm it logs in + connects)
        Expected: baseline test PASS, mavlink bridge connects without error

[  ]  3. UI accessible at http://drone-lab01:3000
        Login: operator@cema.mil / cema@2026
        Expected: Command Center loads

[  ]  4. System Health panel — all 4 rows show ● UP
        Backend API · MongoDB · HackRF RX · SiK Radio

[  ]  5. Header shows green "● HACKRF + SIK_RADIO LIVE" pill

[  ]  6. Waterfall header shows "● HACKRF LIVE" (green pill, not amber SIM)

[  ]  7. Drone powered on → appears in Active Contacts within 5 s
        SRC = SIK_RADIO (green badge) · CEMA = CAPTURE

[  ]  8. Mission Planner on presenter laptop shows drone sysid=1 + your GCS sysid=255

[  ]  9. Test PL-009 (GNSS DENIAL) — reversible smoke test
        Watch Mission Planner: GPS_RAW_INT stops streaming

[  ] 10. Emergency Abort button visible top-right of every page
        (Do NOT click it in the check — just verify it's there.)
```

If any box is unchecked at T-0, **abort the demo, do not improvise on stage**.

---

## 4. The 90-second live script

Read on stage. Each segment is timed.

### T-0:00 — Opening (30 s)
> "This is the MEGHADUTA C³ operator console. It implements the seven-stage
> Capture-through-Exploit pipeline from the requirements document, and
> it runs entirely on off-the-shelf hardware — a HackRF One SDR and a
> SiK-family telemetry radio. Everything I show is happening on real
> RF, on a real drone, in real time."

Point at:
- Green **HACKRF + SIK_RADIO LIVE** header pill
- Waterfall painting real 2.4 GHz spectrum
- System Health tile showing all subsystems UP

### T-0:30 — Detection (15 s)
> "As soon as the target drone powers on, its RF signature appears
> here — UAV-XXXX, protocol MAVLink v2, source SIK_RADIO, threat HIGH.
> No manual entry, no lookup — the sweep detected it on 915 MHz."

Hover the newly-appeared row. Point to the SRC badge (`SIK_RADIO` green).

### T-0:45 — CEMA analysis (20 s)
> "Signal Analysis walks the target through the seven CEMA stages —
> Capture, Analyze, Segregate, Demodulate, Decode, Decrypt, and finally
> Exploit. At the Decode stage we have positive protocol identification;
> at Exploit we're ready to craft interference."

Click **Signal Analysis** → select the drone → click **ADVANCE STAGE** four times fast. Each stage animates.

### T-1:05 — MAVLink craft (15 s)
> "The MAVLink Console lets me hand-craft any frame the target
> autopilot will accept. Here's a valid COMMAND_LONG carrying
> MAV_CMD_NAV_LAND. The hex bytes on the right are what actually
> hit the antenna — CRC-16 MCRF4XX, correct CRC_EXTRA seed, wire-valid."

Click **MAVLink Console** → default is already NAV_LAND. Point at hex-preview.

### T-1:20 — Payload deploy: reversible (15 s)
> "Payload PL-009 — GNSS Telemetry Denial — reversible. Watch Mission
> Planner on the second screen."

Click **Payload Library** → **PL-009 GNSS DENIAL** → **DEPLOY → TGT**.
Presenter laptop: GPS_RAW_INT rate drops to zero.

### T-1:35 — Payload deploy: kinetic (25 s)
> "PL-002 Return To Launch — flight-mode change."

**PL-002 → DEPLOY → TGT** — no safety gate (reversible mode change).
Drone switches to RTL on the video feed.

> "PL-005 Propeller Stop — kinetic. Props physically removed. You'll
> see the pre-flight safety gate — five mandatory checks — before
> the operator can even arm the payload."

**PL-005 → DEPLOY → TGT** → Safety Gate opens → tick 5 boxes → **ARM & FIRE** → **CONFIRM FIRE**.
Drone motors go to 0 % RPM.

### T-2:00 — Broadcast takedown (10 s)
> "PL-010 Broadcast Takedown — target system zero — hits every
> MAVLink-speaking drone on the SiK NET-ID at once. This is your
> swarm defeat."

**PL-010 → BROADCAST**. Safety gate → confirm. Kill Chain page updates instantly — every active contact flips to `NEUTRALIZED` (red pulse).

### T-2:10 — Kill Chain visualization (10 s)
> "Every engagement is tracked through the Detect → Track → Identify
> → Decide → Defeat kill chain, with per-target state and timestamps.
> Notice the color transition — cyan is active, green is complete,
> red is defeat."

Click **Kill Chain** → show the transition.

### T-2:20 — Mission Report (20 s)
> "Every RF packet emitted, every operator decision, every stage
> transition is written to a SHA-256 hash-chained audit log. One click
> generates a signed classified PDF — evidence-grade, ready for
> post-action review."

Click **Mission Log** → **EXPORT MISSION REPORT (PDF)** → PDF downloads.
Hold it up. Hand a printed copy to the ranking officer.

### T-2:40 — Close (10 s)
> "Everything you just saw runs on a $300 SDR and a $30 radio module,
> against a live drone. The architecture is designed to scale to
> operational hardware — RFSoC, GNSS spoofing, phased-array DoA — the
> gap analysis from the requirements document is our roadmap."

---

## 5. Safety & abort protocol

### Emergency Abort button (top-right of every page)

Click once → button flashes **CONFIRM ABORT** for 4 seconds.
Click again → all RF TX halted, ceasefire event logged, all listening bridges receive an abort signal.

**Use it if:**
- The drone is behaving unexpectedly (uncommanded motion).
- The audience raises a safety concern.
- Any physical safety perimeter is breached.
- The operator loses situational awareness for any reason.

### Kinetic payload safety gate

Payloads PL-003, PL-004, PL-005, PL-006, PL-007, PL-010 open a mandatory 5-item safety checklist. **Do not train the audience to click through it fast — read every line aloud once during the demo, then use muscle memory for the rest.** The evaluators are watching for exactly this discipline.

### Physical safety

- Props **off** for any PL-003/005/007 demo.
- Fire extinguisher within 3 m of the drone bench.
- Second operator on independent RC transmitter as manual kill-switch backup.
- **Never** run PL-006 (memory erase) unless you're prepared to re-calibrate the FC before the next flight.

---

## 6. Requirements-gap alignment

| Requirements-doc gap | This build's coverage |
|---|---|
| OB-01 Detection across 2.4/5.8/868/915/433 MHz | HackRF sweep 400–2500 MHz + heuristic band classifier |
| OB-02 Real MAVLink v1/v2 emission | Byte-accurate codec with CRC-16 MCRF4XX + CRC_EXTRA seeds |
| OB-03 Cyber-physical payload catalogue | 10 payloads: force-land, RTL, disarm, flight-term, prop-stop, memory-erase, reboot, RTH spoof, GNSS denial, broadcast takedown |
| OB-04 Multi-target concurrent CEMA pipeline | Per-target CEMA state, 60+ concurrent contacts tested |
| OB-05 Software architecture / rehostable | Docker Compose one-command deploy; runs on any x86-64 Linux/Windows/macOS |
| OB-06 Kill chain visualization | Detect → Track → Identify → Decide → Defeat page |
| OB-07 Audit trail | SHA-256 hash-chained mission log + evidence-grade PDF export |
| OB-08 Safety | Pre-flight safety gate on kinetic payloads + one-click emergency abort |
| **Open gaps** | GNSS spoofing engine, direction-finding, non-MAVLink protocols (DJI/ELRS), FPGA acceleration, phased-array DoA — items on the P0/P1 backlog |

---

## 7. Q&A anticipation — likely evaluator questions

| Question | Answer |
|---|---|
| "Does this work against DJI?" | Detection yes (HackRF sees OcuSync). Payloads no — DJI is proprietary encrypted. Roadmap: RemoteID decoder + GNSS spoof, both on P1. |
| "What's the effective range?" | Bounded by the SiK radio, ~1 km line-of-sight with stock antenna. With the 370–2700 MHz PA on the RX chain, HackRF detection extends to ~2 km. |
| "What if the drone changes NET-ID?" | Detection still works (HackRF is passive). Takedown fails — because MAVLink over SiK is NET-ID-gated. P1 mitigation: direct HackRF MAVLink emission bypasses NET-ID. |
| "Cost per unit?" | ~$400 hardware BOM + Linux laptop. Full stack is Docker-runnable in a hardened form factor (Nvidia AGX Orin recommended). |
| "Certification / airworthiness?" | This is an eval build. Operational deployment requires MoD RF-licensing clearance and range certification. |
| "Can it be networked to multiple ground stations?" | Yes — the WebSocket bus can fan out to N frontends. Multi-operator UI is P1. |

---

## 8. Failure recovery

| Symptom mid-demo | Recovery |
|---|---|
| UI blank | Refresh browser (Ctrl+Shift+R). If persists, `docker compose restart frontend`. |
| Backend 502 | `docker compose logs backend` → common cause: mongo not healthy. `docker compose restart mongo backend`. |
| Drone doesn't appear in contacts | Check SiK ground module LED (should be solid green). Re-plug USB. Check baud rate matches. |
| Payload deployed but no drone reaction | Confirm Mission Planner sees your GCS sysid on the same MAVLink bus. If sysid=255 is missing → SiK link is one-way (TX broken). |
| Emergency Abort clicked | Announce "ceasefire logged." Refresh page. Re-power drone. Show recovery to the audience — it's a *feature*, not a failure. |

---

## 9. Post-demo hand-off

1. Export the Mission Report PDF and hand printed + digital copies to:
   - The evaluating officer
   - The tech team lead
   - Your own file
2. Show the source repo: `github.com/zettagit2026/joydipdemo`
3. Offer to answer follow-up integration questions offline.
4. Do **not** leave the hardware unattended. Pack, seal, log.

---

**End of playbook.** RESTRICTED — NOT FOR OPERATIONAL USE.
