"""Unit tests for geometry.py and illuminator_profile.py (task #43, C10).

Run: pytest field-bridge/passive_radar/test_geometry_and_illuminator.py -v
"""
import pytest

from passive_radar.geometry import (
    doppler_to_speed_mps,
    speed_to_doppler_hz,
    estimate_bistatic_target,
    ReceiverGeometry,
    SPEED_OF_LIGHT_MPS,
)
from passive_radar.illuminator_profile import (
    IlluminatorProfile,
    DVB_T2_PLACEHOLDER,
    FM_BROADCAST_PLACEHOLDER,
)


def test_doppler_to_speed_matches_readme_worked_example():
    # 171210ship/README.md: at fc=500 MHz, fD=+-200 Hz is +-60 m/s (+-216 km/h).
    speed = doppler_to_speed_mps(200.0, 500e6)
    assert speed == pytest.approx(60.0, rel=0.01)


def test_speed_to_doppler_is_inverse():
    fc = 578e6
    speed = 12.5
    fd = speed_to_doppler_hz(speed, fc)
    back = doppler_to_speed_mps(fd, fc)
    assert back == pytest.approx(speed)


def test_estimate_bistatic_target_flags_boresight_only_bearing():
    receiver = ReceiverGeometry(lat=0, lon=0, alt_m=0, surveillance_antenna_boresight_deg=270.0)
    result = estimate_bistatic_target(
        bistatic_range_m=1500.0, doppler_hz=100.0,
        illuminator_center_freq_hz=578e6, receiver=receiver,
    )
    assert result.bearing_deg == 270.0
    assert result.bearing_is_boresight_only is True
    assert "boresight" in result.bearing_accuracy_caveat.lower()
    assert result.radial_speed_mps == pytest.approx(doppler_to_speed_mps(100.0, 578e6))


def test_illuminator_profiles_are_not_only_dvb_t2():
    # Proves the abstraction isn't secretly DVB-T2-only, per
    # PASSIVE_RADAR_ARCHITECTURE.md §5 step 7.
    assert DVB_T2_PLACEHOLDER.name == "DVB-T2"
    assert FM_BROADCAST_PLACEHOLDER.name == "FM_BROADCAST"
    assert DVB_T2_PLACEHOLDER.center_freq_hz != FM_BROADCAST_PLACEHOLDER.center_freq_hz
    assert isinstance(DVB_T2_PLACEHOLDER, IlluminatorProfile)
    assert isinstance(FM_BROADCAST_PLACEHOLDER, IlluminatorProfile)
    # known_transmitter_locations intentionally empty pending site survey.
    assert DVB_T2_PLACEHOLDER.known_transmitter_locations == []
    assert FM_BROADCAST_PLACEHOLDER.known_transmitter_locations == []
