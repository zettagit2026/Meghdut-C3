# JAM SUSTAINED-POWER / ANTI-FADE — build contract (MEGHDUT C3)

Design pass 2026-09-07 (commander field obs: ~11 dB fade during continuous barrage). Architect pass complete. TX-path, safety-critical: each phase gets independent security-review + verifier.

## Root cause (confirmed in code)
`hackrf_jam.py` keys the HackRF's on-board TX RF amp (`-a 1`) at max IF VGA (`-x 47`) on a 100%-duty continuous `-R` loop (`:375-376, 552, 707, 875-885`). The unheatsinked on-board amp heat-soaks over tens of seconds; gain droops (negative tempco + compression of high-PAPR noise). **This is a hardware thermal limit — NO software register makes a bare HackRF hold full continuous power.** Honest ceiling: even cold a HackRF radiates only ~+10–15 dBm — intrinsically weak for a battlefield jammer; the fade takes it ~+13→+2 dBm.

## Trade space (operator picks with eyes open)
| Option | Flatness | Power | Needs | Honest limit |
|---|---|---|---|---|
| Bare HackRF max (`-a 1 -x 47`, TODAY) | **fades ~11 dB** | low peak | nothing | the defect — denies inconsistently |
| **Flat mode** (`-a 0`/reduced VGA) | **flat, zero fade** | lower | SOFTWARE ONLY | trades peak for steadiness; still weak bare radio |
| Open-loop gain schedule | flat-ish (modeled) | ~cold peak | sw + 1-time bench curve | feed-forward only; ignores run/ambient/VSWR; hits 47 ceiling |
| Closed-loop gain-hold | flat within shrinking envelope | held to setpoint | **forward-power sensor** | holds only until VGA=47, then fades; blind w/o sensor |
| **External PA + flat drive** | **flat** | **materially higher ERP** | **PA hardware** | THE REAL FIX; adds procurement + thermal/VSWR eng |

UI truth to print: *"A bare HackRF cannot hold full power continuously — it will fade. Choose Flat for steady low denial, or attach the external PA for flat high power. Gain-Hold needs the power sensor and only holds until it runs out of headroom."*

## (A) HARDWARE — the real fix
HackRF `…930c` = a cool, stable low-level EXCITER (`-a 0`, fixed modest VGA ~20–30 dB) driving an **external continuous-duty-rated PA** (heatsink+fan) → the PA holds power flat AND higher. PA spec: cover 2.400–2.4835 + 5.725–5.875 GHz (opt sub-GHz 433/915), 100% CW-rated, HackRF drive ≥3–6 dB below PA P1dB (linear), isolator/VSWR protection, in-line directional coupler on output (feeds the sensor below), PA-enable bound to the live TX process (fail-to-OFF on every stop). Software = a new `external_pa` drive profile. Partial standalone: heatsink+fan on the bare HackRF reduces (≠ eliminates) droop.

## (B) SOFTWARE — build now
- **3c FLAT MODE (software-only, no sensor, no PA — the immediate win):** `-a 0`/reduced fixed VGA → lower peak, ZERO fade. Correct default when no PA/coupler present.
- **3a POWER-vs-TIME TELEMETRY:** the RX HackRF `…a063` CANNOT honestly self-measure TX power (co-site saturation + self-blinding in the jammed band — separate units so no lock contention, but the physics blocks it). Honest sensor = **directional coupler → RF log-power detector (AD8317/AD8318) → the Arduino already in the kit** (project has 3 RISH meters + Arduino); one-time bench cal volts→dBm. Surface power-vs-time + "−N dB from start" on the existing `jam_ack` telemetry path; UI must say "power not measured — estimated" when no sensor.
- **3b-open-loop FEED-FORWARD SCHEDULE (software-only):** bench-characterize droop once → invert to a time-based `-x` ramp per chunk/dwell. Corrects the repeatable curve only; modeled-not-measured; capped at 47.
- **3b-closed-loop GAIN-HOLD (needs the sensor):** slow PI raises `-x` to hold measured P→setpoint (start VGA<47 for headroom); hard-clamp ≤`MAX_TX_VGA_GAIN=47`; on VGA=47 report "at ceiling, decaying" (honest); fail-SAFE on sensor loss (freeze/flat, NEVER ramp blind). Insertion: the SWEEP path already relaunches per dwell → gain-hold fits there with zero new gaps; single-center gapless `-R` can't change `-x` live → either accept brief retune gaps or steer to sweep/flat for that case.

## Safety spine preserved (map)
Anti-fade = a new gain/profile behavior INSIDE the existing gated primitives, carried as a REQUEST PARAMETER through the same `deploy_jam → jam_request → _do_transmit` path — NEVER a new endpoint/authz path. Preserve: TX device-pin `…930c` (`-d`), fail-closed `_tx_pinning_error`, per-frame kill-switch (`_stop_requested`/`make_tx_halt_check` polled between every chunk/dwell), range-auth lease (0.5s TTL fail-closed), sweep frequency-scope bound, VGA ceiling 47 hard-clamp. NEW: PA-enable actuator bound to TX lifecycle, fails to OFF on stop/abort/lease-expiry.

## Phasing + gates (each: independent security-review + verifier)
- **P0 Bench characterize** `P_out(t)` droop on real `…930c` (measure only). 
- **P1 FLAT mode + `external_pa` profile** (software-only, immediate win; profile selector threaded through the gated path). 
- **P2 Forward-power telemetry** (coupler+AD8318+Arduino; receive/measure only). 
- **P3 Open-loop schedule** (per-dwell `-x` ramp, capped 47). 
- **P4 Closed-loop gain-hold** (sweep path first; PI+clamp+anti-windup+sensor-loss fail-safe+ceiling-honest) — HIGHEST RISK (feedback loop around a live TX): mandatory TX-path security-review + on-hardware verifier evidence that abort/tx_halt/lease-expiry kill it within one poll. 
- **P5 External PA integration** (PA-enable lifecycle fails-OFF; VSWR/thermal; `external_pa` primary fielded config).

## Bottom line for the operator
The fade is a bare-HackRF thermal limit. **Flat mode** (zero fade, lower power) is a software-only win buildable today. **Guaranteed no-fade at higher power needs the external PA.** Honest telemetry + closed-loop need a forward-power coupler+detector (Arduino-read). Refs: `hackrf_jam.py:170,311-321,369-378,545-554,700-716,869-887,65-116,187-224,610-631`; `jam_bridge.py:277-316,319-370,487-497`; `range_auth_lease.py:90-111`; `hackrf_rx.py:59,962-1015`; `server.py:7188-7290`; `ANTENNA_ARRAY_EXCITER_SCOPE.md`.
