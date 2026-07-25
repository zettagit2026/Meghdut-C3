"""Bistatic geometry: (bistatic_range, doppler) -> physical estimates.

Per PASSIVE_RADAR_ARCHITECTURE.md §2.3/§5 step 6: implements the
bistatic range/Doppler-to-speed relation the reference repo's own
README.md documents (171210ship/README.md):

    f_D = 2 * f_c * v / c

("the RADAR Doppler shift is f_D=2*fc*v/c since the wave reaches the
target twice upon reflection"), plus a placeholder bearing model.

HARDWARE-BLOCKED for real bearing accuracy (needs a real directional/
rotated surveillance antenna or an antenna array -- a single fixed Yagi
gives coarse bearing at best, from antenna boresight, not true angle-of-
arrival) -- task #57 (or a follow-on rotator/array task beyond #57's
stated scope). The math/interfaces here are designable and unit-testable
now with assumed/placeholder geometry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

SPEED_OF_LIGHT_MPS = 299792458.0


@dataclass
class ReceiverGeometry:
    """Assumed/config'd receiver + surveillance-antenna geometry. Real
    accuracy depends on a real deployment site survey (out of scope
    here)."""
    lat: float
    lon: float
    alt_m: float
    surveillance_antenna_boresight_deg: float  # compass bearing the fixed Yagi points at


@dataclass
class BistaticEstimate:
    bistatic_range_m: float
    radial_speed_mps: float
    bearing_deg: float
    bearing_is_boresight_only: bool  # True until a rotator/array exists (task #57+)
    bearing_accuracy_caveat: str


def doppler_to_speed_mps(doppler_hz: float, illuminator_center_freq_hz: float) -> float:
    """f_D = 2 * f_c * v / c  =>  v = f_D * c / (2 * f_c), matching
    171210ship/README.md's documented relation exactly."""
    if illuminator_center_freq_hz <= 0:
        raise ValueError("illuminator_center_freq_hz must be positive")
    return doppler_hz * SPEED_OF_LIGHT_MPS / (2.0 * illuminator_center_freq_hz)


def speed_to_doppler_hz(speed_mps: float, illuminator_center_freq_hz: float) -> float:
    """Inverse of doppler_to_speed_mps, provided for tests/tooling."""
    return 2.0 * illuminator_center_freq_hz * speed_mps / SPEED_OF_LIGHT_MPS


def estimate_bistatic_target(
    bistatic_range_m: float,
    doppler_hz: float,
    illuminator_center_freq_hz: float,
    receiver: ReceiverGeometry,
) -> BistaticEstimate:
    """Produces a BistaticEstimate from a single CAF peak (range_m,
    doppler_hz) plus the illuminator's carrier and assumed receiver
    geometry.

    Bearing is reported as the surveillance antenna's own boresight
    (coarse, "somewhere in this antenna's beamwidth" resolution) with an
    explicit caveat, per PASSIVE_RADAR_ARCHITECTURE.md §2.4/§4 -- true
    angle-of-arrival needs a rotator (per the reference repo's own 0MQ-
    gated azimuth-scan pattern) or an antenna array, both task #57+ scope.
    """
    speed = doppler_to_speed_mps(doppler_hz, illuminator_center_freq_hz)
    return BistaticEstimate(
        bistatic_range_m=bistatic_range_m,
        radial_speed_mps=speed,
        bearing_deg=receiver.surveillance_antenna_boresight_deg,
        bearing_is_boresight_only=True,
        bearing_accuracy_caveat=(
            "Bearing is the fixed surveillance antenna's boresight only "
            "(no rotator or antenna array present) -- true resolution is "
            "the antenna's beamwidth, not a precise angle-of-arrival. See "
            "PASSIVE_RADAR_ARCHITECTURE.md §2.4."
        ),
    )
