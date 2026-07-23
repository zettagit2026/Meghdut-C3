# Amplitude-Comparison Direction Finding (DF) -- Design Notes

Status as of 2026-07-23: **groundwork only. No bearing output exists or is
claimed anywhere in this codebase.** This file documents the approach the
user has directed for when hardware allows it, plus an honest split of
what's done vs. not.

## Why amplitude comparison, not interferometry

Interferometric (phase-comparison) DF needs a shared, phase-coherent clock
across receive channels -- typically a single SDR with multiple
phase-locked RX chains, or two units fed from a common reference clock/LO.
Two independent, free-running HackRF Ones have no such shared clock, so
their phase relationship is not usable for bearing estimation.

Amplitude-comparison DF sidesteps that entirely: each unit only needs to
report its own RSSI (already exactly what `hackrf_sweep`/`_one_sweep()` in
`hackrf_rx.py` produces per band, per bin). Two directional antennas
pointed in different fixed known directions, each on its own independent
HackRF, sweeping the same band at the same time -- compare their RSSI, and
the ratio/difference is a function of bearing relative to the antennas'
boresight directions. No shared clock, no synchronized sampling instant
required (coarse time alignment, e.g. both sweeps within the same ~second,
is enough given a stationary/slow-moving emitter). This is the standard,
much-more-achievable DF method for this hardware tier, and is why the user
specified it over interferometry.

## The math (two-element ratio-to-angle, coarse)

For two directional antennas with known, roughly-symmetric gain patterns
G1(theta), G2(theta) about their respective boresights (e.g. two antennas
squinted +/-45 deg off some reference heading), the measured power ratio:

    delta_dB(theta) = RSSI_1(theta) - RSSI_2(theta)
                     = [G1(theta) - G2(theta)] + (same-path-loss terms cancel)

Path loss to the emitter is identical for both antennas (co-located, or
close enough that differential path loss is negligible relative to antenna
gain differences) -- so it cancels in the subtraction, leaving delta_dB as
a function of bearing theta alone, dependent only on the two known antenna
patterns.

The classic simplification: if both antennas have the same gain pattern
shape (e.g. matched Yagi/patch/log-periodic antennas) just squinted apart
by a known boresight offset, delta_dB(theta) is often well-approximated
as monotonic and roughly linear (or a known trig function, e.g.
cosine-squared for many patterns) over their overlap region -- enough to
build either:
  (a) a closed-form inverse (if the pattern is a known analytic function,
      e.g. Gaussian-beam approx: delta_dB proportional to theta over the
      -3dB beamwidth region), or
  (b) an empirical lookup table / interpolation: physically rotate a
      test emitter (or the antenna pair) through known bearings, record
      delta_dB at each, then interpolate the inverse mapping at runtime.
      (b) is more robust in practice since it captures the antennas'
      REAL measured patterns (multipath, mounting effects, near-field
      coupling) rather than an idealized formula -- this is a standard
      calibration step in real amplitude-comparison DF systems and is
      the approach recommended here once real antennas exist to
      calibrate against.

This gives a COARSE bearing estimate, bounded by:
  - the antennas' beamwidth (the fatter the beam, the mushier the bearing --
    narrow directional antennas give tighter estimates, omnis give none),
  - ambiguity outside the two antennas' combined field of view (more
    antenna pairs / more sectors needed for full 360 deg coverage -- two
    antennas alone only usefully disambiguate bearing within their overlap
    region, not all around),
  - RSSI measurement noise/multipath fading, which is the same "coarse,
    single-antenna RSSI heuristic" caveat `hackrf_rx.py`'s existing
    `estimate_distance_m()` already documents for its own RSSI-to-distance
    heuristic -- amplitude DF inherits the same class of uncertainty.

## What hardware is still needed (NOT present today)

1. **Second HackRF physically passed through to this VM at the hypervisor
   level.** Acquired by the user, not yet passed through -- that step is
   the user's own task, outside what this codebase/session can do.
2. **Two or more directional antennas** (Yagi, log-periodic, patch, or
   similar) with **known, fixed, and ideally measured/calibrated**
   radiation patterns, mounted at **known relative boresight headings**.
   None confirmed on hand yet as of this writing.
3. Ideally, a **known physical separation and orientation** between the
   two antenna/HackRF pairs, documented and fixed (DF math above assumes
   this is stable, not something that shifts between runs).
4. A **calibration pass**: rotate a known test emitter (or the antenna
   rig) through known bearings and record real delta_dB(theta), to build
   the empirical inverse-mapping lookup table described above. Without
   this, any bearing number produced would be an unvalidated guess dressed
   up as a measurement -- do not skip it.

## What software groundwork this session's changes enable (DONE)

- `hackrf_device_lock.py`: per-device (per-serial) locking, so two
  physical HackRFs no longer serialize behind one shared mutex -- a
  correctness prerequisite for running two sweeps truly concurrently.
- `hackrf_rx.py`: `_one_sweep()`/`sweep_band()` can now be pinned to a
  specific serial via `HACKRF_RX_SERIAL`.
- `iq_capture.py`: already supported per-device `serial` (pre-existing,
  unused by callers before this session); now wired through from
  `ml_classify_bridge.py` and `droneid_decode_bridge.py` via
  `HACKRF_SERIAL`.
- `hackrf_config.py`: minimal serial-to-role config (`HACKRF_SERIAL_PRIMARY`
  / `HACKRF_SERIAL_SECONDARY` / `HACKRF_ROLE_MAP`), so an operator can
  assign "unit A -> RX sweep", "unit B -> DroneID bridge" independently.

## What is explicitly NOT built (do not claim otherwise)

- No code anywhere runs two `hackrf_sweep`/`hackrf_transfer` processes
  concurrently against two different serials and compares their output.
  Today's per-device locking makes that SAFE to build next, it does not
  itself do it.
- No RSSI-ratio-to-bearing conversion function exists.
- No antenna pattern data, calibration table, or lookup/interpolation
  code exists.
- No detection record anywhere carries a real `bearing_deg` -- every
  current detection in `hackrf_rx.py`/`ml_classify_bridge.py` posts
  `"bearing_deg": 0.0` as a hardcoded placeholder, not a DF output, and
  that has not changed in this session.

## Next concrete build step (once both antennas + passthrough exist)

A new bridge script, e.g. `df_amplitude_bridge.py`, that:
1. Uses `hackrf_config.get_role_serial("primary")` /
   `get_role_serial("secondary")` to resolve the two units' serials.
2. Runs `sweep_band(..., serial=serial_a)` and
   `sweep_band(..., serial=serial_b)` concurrently (two threads/processes;
   each now safely lock-scoped to its own device per this session's
   changes) against the same band/frequency.
3. Computes `delta_dB = peak_a - peak_b` for the gated band.
4. Looks up `delta_dB` in the calibrated table (see above) to produce a
   coarse bearing estimate, clearly flagged (mirroring the existing
   `"distance_estimated": true` pattern in `hackrf_rx.py`) as e.g.
   `"bearing_estimated": true` with a documented uncertainty/beamwidth,
   never posted as a precise value.
