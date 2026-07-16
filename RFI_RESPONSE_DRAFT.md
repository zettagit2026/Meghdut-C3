# Response to B/50529/CEMA/SURAJ/09/01 dt 01 Sep 2025
## Procurement of Cyber & Electromagnetic Activities (CEMA) Enabled Counter Drone System

*Draft technical response — review and edit before submission/demo.*

**1.1 — Malicious code insertion into flight control system via RF: exists?**
Yes, for MAVLink-based flight controllers (ArduPilot/PX4 — the open protocol
used by the large majority of commercial, hobbyist, and many military-adjacent
UAS platforms). Vanilla MAVLink v1/v2 has no mandatory authentication —
MAVLink 2's optional packet signing is frequently left disabled by
integrators — so a correctly-formed, byte-accurate packet delivered over the
matching RF link (matching frequency, modulation, and pairing/session state)
is accepted by the flight controller identically to a legitimate GCS command.
For closed proprietary links (e.g. DJI OcuSync), insertion requires
protocol-specific reverse engineering of that vendor's link layer and is
handled as a separate, narrower capability (see 1.13).

**1.2 — Frequency range for code insertion**
Demonstrated range for this build: 915MHz ISM (SiK telemetry, matches Indian
license-exempt band allocation) and 433MHz where regional SiK variants use
it. Extensible to 2.4GHz/5.8GHz control links with additional RF front-end
and protocol-specific work (see architecture note, 1.15).

**1.3 — Can the system manipulate flight path, sensor readings, or commands?**
Yes, for MAVLink targets: COMMAND_LONG injection can trigger RTH, forced
landing, disarm, flight termination, motor test/stop, mode changes; DO_SET_HOME
injection can spoof the recorded home position, corrupting subsequent RTH
navigation; SET_MESSAGE_INTERVAL manipulation can suppress specific telemetry
streams (e.g. GPS_RAW_INT) at the link layer to degrade the operator's
situational awareness without necessarily controlling the airframe.

**1.4 — Communication disruption between UAS and GCS, temporary/permanent malfunction**
Yes. Band-limited RF interference at the target link's frequency (SiK 915MHz
or DJI 2.4/5.8GHz) is demonstrated to force link loss, which most flight
controllers resolve via a configured failsafe (RTH/land/hold) — i.e.
"temporary" in the sense that the airframe recovers via its own failsafe
logic, not permanent airframe damage. Permanent damage would require either
inducing an uncontrolled attitude/motor state during a vulnerable flight
phase, or protocol-level commands (flight termination) where those exist and
are unauthenticated.

**1.5 — On-board data corruption via CEMA attack**
Partial: demonstrated at the telemetry/parameter layer (PARAM_SET,
PREFLIGHT_STORAGE for persistent parameter/mission/log reset) for MAVLink
targets with unauthenticated links. Does not extend to arbitrary onboard
filesystem/firmware corruption without physical or supply-chain access.

**1.6 — Physical damage via uncontrolled response (e.g. rotor overspeed)**
Demonstrated as a capability class via DO_MOTOR_TEST / actuator command
injection on MAVLink targets in a controlled test rig — this is exactly the
kind of test that must run in a supervised range (as at STEAG), never in
open-air testing, given the physical damage/safety risk it is explicitly
designed to probe.

**1.7 — Altered data leading to incorrect situational awareness at GCS**
Yes — this is architecturally the easiest and lowest-risk class: spoofed
telemetry (position, battery, attitude) or suppressed telemetry (message
interval manipulation) degrades the GCS operator's picture without any
airframe-side effect, and is the safest capability to demonstrate live.

**1.8 — Protocols influenced simultaneously**
Current build: MAVLink v1/v2 over any transport carrying it (SiK serial,
Wi-Fi/UDP telemetry bridges, USB companion links). Simultaneous multi-band
operation (SiK + 2.4GHz + 5.8GHz concurrently) requires either multiple
SDR/radio front-ends running in parallel or a wideband SDR with sufficient
instantaneous bandwidth — architecturally straightforward, a hardware
provisioning question more than a software one.

**1.9 — Mode of malicious code injection: broadcast vs sequential**
Both are supported by the packet model: `target_system=0` broadcasts to all
listening systems on the link (see `broadcast_takedown()`); targeted
injection addresses a specific `target_system`/`target_component`. Band
coverage (sequential per-channel vs. simultaneous) is a hardware/front-end
question per 1.8.

**1.10 — Effectiveness against autonomous-mode drones**
Reduced but not eliminated: autonomous (pre-programmed mission) drones with
no active GCS link are harder to influence via command injection mid-mission
since there's less to intercept, but RF disruption (1.4) still forces the
platform's own lost-link failsafe behavior, and any periodic
telemetry/command polling window remains an injection opportunity.

**1.11 — Feasibility of taking control for manoeuvring**
Yes for MAVLink targets with unauthenticated/unsigned links — full RC-style
manoeuvring requires either MANUAL_CONTROL/RC_CHANNELS_OVERRIDE message
injection at a sustained rate (out-competing the legitimate GCS) or session
hijack of the existing link; both are demonstrable capability extensions of
the current COMMAND_LONG injection base, roadmapped but not yet built in this
codebase (see delivery timeline, 1.17).

**1.12 — Unintended manoeuvre or crash induction**
Yes, as a subset of 1.6/1.11 — demonstrated via forced mode changes and
actuator commands in supervised test conditions only.

**1.13 — Effectiveness against encrypted communication channels**
Materially reduced. Proprietary encrypted/authenticated links (DJI
OcuSync 2.0/3.0, and any MAVLink deployment with signing enabled) require
either a cryptographic weakness in that specific protocol or a
downgrade/jamming-then-reacquire attack rather than direct injection. Current
build's answer for encrypted targets is detection + disruption (1.4), not
injection/hijack — this is stated plainly rather than overclaimed.

**1.14 — Power output required for effective CEMA attack at 20km GCS-UAS range**
Link-budget dependent on target link's receiver sensitivity, antenna gain,
and path loss; not yet characterized against a live DJI/MAVLink target at
that range in this build. Recommend this be scoped as a field-trial
deliverable at STEAG with calibrated equipment rather than a desk estimate,
to give the evaluators a real number instead of a guess.

**1.15 — Proposed system architecture (block diagram)**
```
 [HackRF One RX] --sweep/detect--> [Backend API: /spectrum/ingest,
                                     /detections/ingest]
                                          |
 [SiK 915MHz radio] --serial (pyserial)--> [mavlink_codec.py: byte-accurate
                                             MAVLink v1/v2 packet builder]
                                          |
 [HackRF One TX] --band-limited RF-->  [target link / target receiver]
                                          |
                        [Operator Console: React frontend, WebSocket live
                         feed, kill-chain visualization, audit log, JWT auth]
```
Detection and injection/disruption paths are decoupled — the console works
purely from ingested data (real or simulated) so the same UI/kill-chain
demonstrates both a fully safe passive-detection posture and a live-fire
posture, depending on which field-bridge scripts are run.

**1.16 — Variants by attack type / range**
Proposed tiering: (a) Detect-only variant (HackRF RX, no TX hardware,
zero regulatory footprint) — lowest cost, fastest to field; (b) Detect +
Disrupt variant (adds TX for jamming/link-denial); (c) Detect + Disrupt +
Inject variant (adds SiK/serial injection against MAVLink-protocol targets)
— highest capability, requires the RF authorization/test-range process this
demo itself is operating under.

**1.17 — Expected delivery timelines post-contract**
To be filled in with actual production timeline — not estimated here.

**1.18 — Recommended detection method**
Passive RF energy detection + spectral fingerprinting (hop pattern,
occupied bandwidth, burst timing) across 915MHz/2.4GHz/5.8GHz, as
implemented in `field-bridge/hackrf_rx.py`, feeding a persisted detection
log and live console view.

**1.19 — Indigenous content in software/hardware**
Software (packet crafting, detection logic, console, kill-chain
orchestration): fully authored in this codebase, no foreign proprietary
dependency. Hardware: HackRF One and SiK radios are open-hardware designs;
sourcing/manufacture location to be confirmed against DoT/MoD indigenization
requirements separately from this technical response.

**1.20 — Product support details**
To be filled in per your firm's actual support/SLA model — not estimated here.

**1.21 — Recommended category**
To be filled in against the Army's own capability categories — not
estimated here.

**1.22 — OEM status**
To be filled in factually per your firm's actual position — not estimated here.

---
*Note: items 1.14, 1.17, 1.19 (hardware sourcing), 1.20, 1.21, 1.22 need your
input with real business/programme facts before submission — I've left them
as placeholders rather than guessing on your behalf.*
