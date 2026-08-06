#!/usr/bin/env python3
"""Amplitude-comparison (RSSI-ratio) direction finding (DF) for MEGHDUT C3.

=============================================================================
HARDWARE DEPENDENCY (read first)
=============================================================================
This module is the real bearing-estimation math the codebase has been missing
(see field-bridge/DIRECTION_FINDING_NOTES.md for the design rationale the user
directed). It is HARDWARE-GATED for field use: producing a *real* bearing
requires **2 or more matched directional antennas**, each pointed at a known,
fixed boresight heading, each fed to its own receiver (independent HackRF per
DIRECTION_FINDING_NOTES.md), measuring RSSI of the SAME emission at the SAME
time. As of this writing NONE of that hardware is present/passed-through
(task #20 procurement + hypervisor passthrough are the user's own steps, not
something this session can do).

Therefore this module is deliberately structured so it is:
  * fully USABLE AND TESTABLE NOW against synthetic RSSI vectors (pure Python,
    stdlib `math` only -- runs on the Mac dev copy), and
  * a CLEAN SUBSTITUTION POINT later: when the antenna array exists, wire each
    antenna's real measured RSSI (already produced per-band/per-bin by
    hackrf_rx.py's `_one_sweep()`/`sweep_band()`) into `AntennaMeasurement`
    records and call `estimate_bearing()` -- no algorithm rewrite needed.

Honesty contract (this project's standing rule -- no fake data, ever):
  * With fewer than 2 antennas (the current single-antenna reality),
    `estimate_bearing()` returns `available=False` / `bearing_deg=None` with an
    explicit "DF unavailable -- requires multi-antenna array" status. It NEVER
    fabricates a confident 0 deg (or any angle) from single-antenna data.
  * Even with >=2 antennas, when the geometry is ill-conditioned (the emitter
    is outside the antennas' beam-overlap region, i.e. the estimate would be an
    extrapolation past a beam edge) the result is flagged low-quality /
    ambiguous with an honest uncertainty, never a crisp value.

=============================================================================
THE MATH (two-element amplitude-comparison monopulse, closed form)
=============================================================================
Two co-located directional antennas with the same gain-pattern shape, squinted
to known boresights b1, b2 (deg). Path loss to the emitter is common to both
(co-located) and cancels in the dB difference, so:

    delta_dB(theta) = RSSI_1 - RSSI_2 = G(theta; b1) - G(theta; b2)

is a function of the bearing `theta` and the (known) antenna patterns alone.

Antenna-pattern model (SWAPPABLE -- see `gaussian_beam_gain_db`): we use the
standard sectored-antenna main-lobe approximation (quadratic-in-dB, a.k.a. the
3GPP TR 36.814 / TR 38.901 sector-antenna model):

    G(theta; b) = -min( 12 * ((theta - b) / BW_3dB)^2 , A_max )   [dB]

Within the beam-overlap region the A_max clamp is inactive, so the difference
of two such patterns is:

    delta_dB = (12 / BW^2) * (b1 - b2) * (2*theta - (b1 + b2))

which inverts to a CLOSED FORM for the bearing:

    theta = (b1 + b2)/2  +  delta_dB * BW^2 / (24 * (b1 - b2))

The crossover (delta_dB = 0, equal received power) lands exactly at the boresight
midpoint (b1+b2)/2 -- the classic equal-signal DF null, which is also the
best-conditioned (most precise) point. Precision degrades toward the beam edges
where gain is low (poor SNR) and the quadratic model flattens.

This pattern model is an IDEALIZATION. Real antennas have asymmetric lobes,
side lobes, mounting/multipath effects. The recommended production path
(DIRECTION_FINDING_NOTES.md sec. "The math", option (b)) is to REPLACE
`gaussian_beam_gain_db` / the closed-form inverse with an EMPIRICAL calibration
table measured by rotating a test emitter through known bearings. The pattern
model here is intentionally a single, clearly-labelled, swappable function so
that substitution is a one-function change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple


# --- Tunable defaults (documented, not magic numbers) -----------------------

# Default antenna -3 dB beamwidth (deg). A placeholder until real antennas are
# measured -- typical directional patch/Yagi/log-periodic panels used at this
# tier run ~45-90 deg. MUST be replaced with the antennas' measured beamwidth.
DEFAULT_BEAMWIDTH_DEG = 65.0

# 3GPP sector-model front-to-back / max attenuation clamp (dB). Beyond this the
# pattern is treated as flat (deep null / back lobe) -- only affects gains far
# off boresight, outside the usable overlap region.
DEFAULT_MAX_ATTEN_DB = 25.0

# Minimum antennas for any bearing at all. Amplitude-comparison DF needs a
# ratio -> at least two receive channels. Below this: honestly unavailable.
MIN_ANTENNAS_FOR_DF = 2

# Status strings surfaced to the backend/operator (kept as constants so the
# frontend/backend can match on them, and so "unavailable" is one canonical
# phrase everywhere rather than drifting per call site).
STATUS_OK = "ok"
STATUS_UNAVAILABLE_SINGLE = (
    "DF unavailable -- requires multi-antenna array (>=2 directional antennas)"
)
STATUS_AMBIGUOUS_OVERLAP = (
    "bearing ambiguous -- emitter outside antenna beam-overlap region"
)
STATUS_DEGENERATE_GEOMETRY = (
    "DF unavailable -- antenna boresights coincide (no angular baseline)"
)


# --- Antenna pattern model (SWAPPABLE) --------------------------------------

def gaussian_beam_gain_db(theta_deg: float, boresight_deg: float,
                          beamwidth_deg: float = DEFAULT_BEAMWIDTH_DEG,
                          max_atten_db: float = DEFAULT_MAX_ATTEN_DB) -> float:
    """Sectored-antenna main-lobe gain (dB, relative to boresight peak = 0 dB).

    Standard 3GPP quadratic-in-dB model:
        G(theta) = -min( 12 * (delta/BW_3dB)^2, A_max )
    where delta is the angular offset from boresight (wrap-aware, +/-180 deg).

    This is the ONE function to replace with a measured/interpolated real
    pattern once antennas are calibrated (see module docstring). Keeping it
    pure and side-effect-free makes that substitution trivial and testable.
    """
    delta = _wrap180(theta_deg - boresight_deg)
    atten = 12.0 * (delta / beamwidth_deg) ** 2
    return -min(atten, max_atten_db)


# --- Data types -------------------------------------------------------------

@dataclass(frozen=True)
class AntennaMeasurement:
    """One receive channel's report for a single emission.

    boresight_deg: fixed, KNOWN azimuth this antenna points at (0 = North,
        clockwise, matching the compass convention used across this codebase's
        bearing_deg fields and backend/server.py::_bearing_compass).
    rssi_dbm: measured received power for the emission on this channel. In the
        field this comes straight from hackrf_rx.py's per-band peak RSSI for
        the antenna's own HackRF; in tests it comes from a synthetic emitter.
    beamwidth_deg: this antenna's -3 dB beamwidth (measured value once known).
    """
    boresight_deg: float
    rssi_dbm: float
    beamwidth_deg: float = DEFAULT_BEAMWIDTH_DEG


@dataclass(frozen=True)
class DFResult:
    """Outcome of a bearing estimate. `available` is the single source of truth:
    when False, `bearing_deg` is None and `status` says why -- callers must NOT
    invent an angle in that case."""
    available: bool
    bearing_deg: Optional[float]          # 0-360, 0=North; None when unavailable
    quality: float                        # 0.0 (useless) .. 1.0 (best-conditioned)
    uncertainty_deg: Optional[float]      # 1-sigma-ish coarse angular spread
    status: str
    # Diagnostics (which pair was used, the measured ratio) -- useful for logs
    # and for the future empirical-calibration path; never presented as truth.
    used_boresights: Optional[Tuple[float, float]] = None
    delta_db: Optional[float] = None
    ambiguous: bool = False

    def to_ingest_fields(self) -> dict:
        """Detection-ingest payload fragment. Emits an HONEST bearing state:
        a real (estimated) bearing only when available, else an explicit
        unavailable marker -- never a fake 0.0. Mirrors the existing
        `distance_estimated` honesty flag pattern in hackrf_rx.py."""
        if not self.available:
            return {
                "bearing_deg": None,
                "bearing_available": False,
                "bearing_estimated": False,
                "bearing_status": self.status,
            }
        return {
            "bearing_deg": round(self.bearing_deg, 1),
            "bearing_available": True,
            "bearing_estimated": True,   # coarse amplitude-comparison estimate
            "bearing_uncertainty_deg": (round(self.uncertainty_deg, 1)
                                        if self.uncertainty_deg is not None else None),
            "bearing_quality": round(self.quality, 3),
            "bearing_status": self.status if not self.ambiguous else STATUS_AMBIGUOUS_OVERLAP,
        }


# --- Core estimator ---------------------------------------------------------

def estimate_bearing(
    measurements: Sequence[AntennaMeasurement],
    pattern_model: Callable[..., float] = gaussian_beam_gain_db,
) -> DFResult:
    """Estimate bearing to an emitter from >=2 antennas' RSSI of the same signal.

    Algorithm:
      1. Guard: need >= MIN_ANTENNAS_FOR_DF channels, else honestly unavailable.
      2. Select the strongest-pair: the two channels with the highest RSSI (the
         emitter is, by construction of amplitude-comparison DF, in the overlap
         of the two beams currently seeing it best). For N=2 that's just the
         two. For N>2 arranged around the compass this picks the sector.
      3. Invert the two-element ratio -> bearing via the closed form derived
         from `pattern_model` (documented in the module docstring for the
         default Gaussian/3GPP model).
      4. Quality/uncertainty: best at the equal-signal crossover (balanced
         RSSI), degrading toward beam edges; flagged AMBIGUOUS and low-quality
         if the solution extrapolates outside the two boresights (past a beam
         edge, where the ratio no longer well-constrains the angle).

    `pattern_model` is accepted for API symmetry / documentation; the closed-form
    inverse below is specific to the quadratic-in-dB default. A different pattern
    (or an empirical calibration table) should provide its own inverse -- see the
    module docstring's substitution note.
    """
    if len(measurements) < MIN_ANTENNAS_FOR_DF:
        return DFResult(
            available=False, bearing_deg=None, quality=0.0,
            uncertainty_deg=None, status=STATUS_UNAVAILABLE_SINGLE,
        )

    # Strongest pair by RSSI.
    ranked = sorted(measurements, key=lambda m: m.rssi_dbm, reverse=True)
    a, b = ranked[0], ranked[1]

    d = _wrap180(a.boresight_deg - b.boresight_deg)  # signed baseline, a rel. b
    if abs(d) < 1e-6:
        # Coincident boresights: no angular baseline -> ratio carries no
        # directional information. Honestly unavailable rather than dividing by ~0.
        return DFResult(
            available=False, bearing_deg=None, quality=0.0,
            uncertainty_deg=None, status=STATUS_DEGENERATE_GEOMETRY,
            used_boresights=(a.boresight_deg, b.boresight_deg),
        )

    bw = 0.5 * (a.beamwidth_deg + b.beamwidth_deg)  # avg beamwidth for the pair
    delta_db = a.rssi_dbm - b.rssi_dbm

    # Closed-form inverse of the quadratic-in-dB model, worked in a local frame
    # centred on antenna b (b at 0, a at d) to stay wrap-safe:
    #   delta_db = (12/BW^2) * d * (2*t - d)   =>   t = d/2 + delta_db*BW^2/(24*d)
    t = d / 2.0 + (delta_db * bw * bw) / (24.0 * d)
    bearing = _wrap360(b.boresight_deg + t)

    # Offset of the estimate from the crossover (equal-signal) midpoint, and the
    # half-span of the overlap (boresight separation / 2). Inside the overlap ->
    # interpolation (trustworthy); outside -> extrapolation past a beam edge.
    theta_from_mid = t - d / 2.0
    half_span = abs(d) / 2.0
    edge_ratio = abs(theta_from_mid) / half_span if half_span > 0 else float("inf")

    ambiguous = edge_ratio > 1.0

    # Quality: 1.0 at the crossover, smoothly to ~0 at a boresight (beam edge),
    # clamped to a small floor when extrapolating beyond (ambiguous). Honest,
    # coarse -- documented as a conditioning heuristic, not a calibrated PDF.
    if ambiguous:
        quality = max(0.0, 0.15 / edge_ratio)
    else:
        quality = math.cos(min(edge_ratio, 1.0) * (math.pi / 2.0))

    # Coarse 1-sigma-ish angular uncertainty: scales with beamwidth, widening
    # as the solution moves off the crossover toward (and past) the beam edge.
    # A tenth of the beamwidth at the crossover, growing with edge_ratio.
    uncertainty = 0.1 * bw * (1.0 + 3.0 * min(edge_ratio, 3.0))

    return DFResult(
        available=True,
        bearing_deg=bearing,
        quality=quality,
        uncertainty_deg=uncertainty,
        status=STATUS_OK if not ambiguous else STATUS_AMBIGUOUS_OVERLAP,
        used_boresights=(a.boresight_deg, b.boresight_deg),
        delta_db=delta_db,
        ambiguous=ambiguous,
    )


# --- Synthetic-emitter helper (for tests + calibration bring-up) ------------

def synthesize_measurements(
    true_bearing_deg: float,
    boresights: Sequence[float],
    tx_power_dbm: float = -40.0,
    beamwidth_deg: float = DEFAULT_BEAMWIDTH_DEG,
    pattern_model: Callable[..., float] = gaussian_beam_gain_db,
    noise_db: float = 0.0,
    rng: Optional["object"] = None,
) -> List[AntennaMeasurement]:
    """Build the RSSI vector a set of antennas WOULD see for an emitter at a
    known bearing, using the same pattern model the estimator inverts. Lets the
    algorithm be exercised end-to-end NOW without hardware, and is exactly how a
    calibration harness would replay recorded patterns later.

    noise_db: optional +/- uniform RSSI jitter (needs `rng` = random.Random) to
    check estimator robustness; 0.0 (default) = noiseless ground truth.
    """
    out: List[AntennaMeasurement] = []
    for b in boresights:
        gain = pattern_model(true_bearing_deg, b, beamwidth_deg)
        rssi = tx_power_dbm + gain
        if noise_db and rng is not None:
            rssi += rng.uniform(-noise_db, noise_db)
        out.append(AntennaMeasurement(boresight_deg=b, rssi_dbm=rssi,
                                      beamwidth_deg=beamwidth_deg))
    return out


# --- angle helpers ----------------------------------------------------------

def _wrap180(a: float) -> float:
    """Wrap to (-180, 180]."""
    return (a + 180.0) % 360.0 - 180.0


def _wrap360(a: float) -> float:
    """Wrap to [0, 360)."""
    return a % 360.0
