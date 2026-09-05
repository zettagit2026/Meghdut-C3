"""Pure-Python tests for the completed PL-008 / PL-005 payload builders in
backend/mavlink_codec.py. No env/Mongo/RF needed — these assert byte-accurate
MAVLink content of the frames the deploy path transmits.

  * PL-008 RTH HOME-SPOOF: builds DO_SET_HOME carrying the OPERATOR coordinates,
    THEN emits the NAV_RETURN_TO_LAUNCH trigger (spoof home -> RTH to the false
    coords). Both frames + coords are asserted.
  * PL-005 PROPELLER STOP: iterates DO_MOTOR_TEST (throttle=0) across ALL motors
    1..motor_count (not just #1), clamped to the real airframe range [1, 8].

Run: pytest backend/tests/test_payload_builders.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mavlink_codec as mc  # noqa: E402

DO_SET_HOME = mc.MAV_CMD["DO_SET_HOME"]              # 179
NAV_RTL = mc.MAV_CMD["NAV_RETURN_TO_LAUNCH"]         # 20
DO_MOTOR_TEST = mc.MAV_CMD["DO_MOTOR_TEST"]          # 209


# ---- PL-008 RTH HOME-SPOOF ----------------------------------------------
def test_pl008_emits_do_set_home_with_coords_then_rth():
    blob = mc.payload_rth_spoof_home(7, 1, seq=0, lat=12.3456, lon=-56.789, alt=42.0)
    frames = mc.iter_frames(blob)
    assert len(frames) == 2, "PL-008 must emit DO_SET_HOME + the RTH trigger"

    set_home = mc.decode_command_long(frames[0])
    assert set_home["command"] == DO_SET_HOME
    assert set_home["target_system"] == 7 and set_home["target_component"] == 1
    assert set_home["param1"] == 0.0  # use SPECIFIED (spoofed) location, not current
    assert abs(set_home["param5"] - 12.3456) < 1e-3   # lat
    assert abs(set_home["param6"] - (-56.789)) < 1e-3  # lon
    assert abs(set_home["param7"] - 42.0) < 1e-3       # alt

    rth = mc.decode_command_long(frames[1])
    assert rth["command"] == NAV_RTL, "spoof home must be FOLLOWED by the RTH trigger"
    assert rth["target_system"] == 7


def test_pl008_zero_coords_default():
    blob = mc.payload_rth_spoof_home(3, 1)
    frames = mc.iter_frames(blob)
    assert len(frames) == 2
    d = mc.decode_command_long(frames[0])
    assert d["command"] == DO_SET_HOME and d["param5"] == 0.0 and d["param6"] == 0.0


# ---- PL-005 PROPELLER STOP ----------------------------------------------
def test_pl005_iterates_all_motors():
    blob = mc.payload_propeller_stop(9, 1, seq=0, motor_count=6)
    frames = mc.iter_frames(blob)
    assert len(frames) == 6, "one DO_MOTOR_TEST per motor"
    motors = []
    for f in frames:
        d = mc.decode_command_long(f)
        assert d["command"] == DO_MOTOR_TEST
        assert d["target_system"] == 9
        assert d["param3"] == 0.0  # throttle 0 => stop
        motors.append(int(d["param1"]))
    assert motors == [1, 2, 3, 4, 5, 6], "must address motors 1..N, not just #1"


def test_pl005_default_is_quadrotor():
    frames = mc.iter_frames(mc.payload_propeller_stop(1, 1))
    assert len(frames) == 4  # default motor_count=4


def test_pl005_clamped_to_real_airframe_range():
    # Above 8 clamps to 8 (max realistic multirotor); below 1 clamps to 1.
    assert len(mc.iter_frames(mc.payload_propeller_stop(1, 1, motor_count=99))) == 8
    assert len(mc.iter_frames(mc.payload_propeller_stop(1, 1, motor_count=0))) == 1
    assert len(mc.iter_frames(mc.payload_propeller_stop(1, 1, motor_count=-5))) == 1


# ---- frame splitter sanity ----------------------------------------------
def test_iter_frames_roundtrips_single_and_multi():
    single = mc.payload_force_land(5)
    assert mc.iter_frames(single) == [single]
    multi = mc.payload_propeller_stop(5, 1, motor_count=3)
    parts = mc.iter_frames(multi)
    assert len(parts) == 3 and b"".join(parts) == multi


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
