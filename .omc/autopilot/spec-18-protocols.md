# Autopilot spec — 18 drone-protocol coverage: deploy + expose + test

## Goal
Make all 18 real drone-protocol parser/decoder modules (code already exists) **visible on the Protocol Library board with honest status, deployed to .186, and tested** — with truthful classification (over-the-air operational vs forensic wire-tap bench-only). No fake airborne capability. No new hardware claims.

## The 18 (from the reconciliation, file:line-verified)
DEPLOYED over-the-air operational (DONE): 1 control_link (LIVE), 2 remoteid (READY), 3 droneid (READY), 4 fpv_osd (READY).
OTA-CAPABLE, code exists, NOT wired: 5 flysky_afhds, 6 frsky_accst, 7 spektrum_dsm (RF control-link recognizers), 8 adsb (1090 MHz passive RX), 9 parrot_arsdk (Wi-Fi network protocol).
FORENSIC wire-tap (bench-only by physics — need UART/CAN tap): 10 crsf (unit inert), 11 dronecan (unit inert), 12 ltm (unit exists), 13 msp, 14 canopen (needs CAN HW), 15 sik_mavlink_wire (break-glass CLI), 16 dshot, 17 frsky_smartport, 18 graupner_hott.

## Requirements
1. Protocol status board (backend/protocol_status.py) enumerates ALL 18 with correct class; status derived from real heartbeat+decode recency; NEVER hardcode LIVE. Forensic entries clearly labeled bench/wire, never "airborne".
2. Wire the 5 OTA-capable recognizers (flysky/frsky_accst/spektrum/adsb/parrot) into the detection plane: a bridge that heartbeats + emits real recognitions when present, a systemd unit, using EXISTING RX/Kismet/SDR feeds (no new HW), and PROVEN not to starve the primary detection radio (cema-hackrf-rx must stay active + detections keep incrementing). Decide per-protocol: standalone bridge vs fold into control_link_classifier.
3. Forensic parsers: expose as FORENSIC board entries; deploy units where a unit is meaningful (crsf/dronecan/ltm exist) as bench self-tests against synthetic/sample frames that report honest status; the rest are library entries shown FORENSIC. NEVER presented as over-the-air detection.
4. Frontend Protocol Library shows all 18 grouped OPERATIONAL vs FORENSIC with LIVE/READY/OFFLINE + FORENSIC badges.
5. Each parser retains/gains unit tests; deploy runs them; on-box each unit installs + reports honest status.
6. Honest labeling everywhere — no fake capability, no overclaim. Jam remains the universal defeat; these are detection/ID only.
7. tx_halt untouched (detection-side, no TX). .196 never contacted. Mac = code only; deploy via SRE to .186.

## Constraints / sequencing
- server.py + protocol_status.py edits must be sequenced AFTER the in-flight SDR-MAVLink executor (server.py collision). Planning is read-only and runs concurrently.
- Every shipped unit: independent third-eye verifier (never executor self-verify alone).
- Deploy runbook lesson: backend baked into image → `docker compose build backend && up -d --no-deps backend` (never plain --force-recreate).

## Phases
0/1 Expansion+Planning: this spec + architect execution plan (in flight).
2 Execution: build bridges/units/status/frontend per plan (after MAVLink clears server.py).
3 QA: tests + npm build green.
4 Validation: security-review + verifier + architect sign-off.
5 Deploy+test: SRE to .186, honest per-protocol status, non-starvation evidence; final read-only third-eye.
