#!/usr/bin/env python3
"""Pure-Python tests for the sustained RC-override maneuver-takeover primitive.

Covers the SAFETY GUARANTEES (bounded duration, immediate abort, honest
FHSS-not-applicable refusal) and the byte-accuracy of the RC_CHANNELS_OVERRIDE
/ MANUAL_CONTROL frames. No Mongo/Docker/RF needed — a fake send sink and a
fake clock make the bounded/abort behavior deterministic.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import mavlink_codec as mc  # noqa: E402
from mavlink_takeover import (  # noqa: E402
    MAX_DURATION_S,
    link_is_overridable,
    run_sustained_takeover,
)


# ---- fake deterministic clock -------------------------------------------
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt


# ---- frame construction / round-trip ------------------------------------
def test_rc_override_frame_roundtrips_and_is_controlled_descent():
    frame = mc.payload_maneuver_takeover(7, 1, seq=0)
    info = mc.describe_packet(frame)
    assert info["valid"] and info["message_id"] == 70
    dec = mc.decode_rc_channels_override(frame)
    assert dec["target_system"] == 7 and dec["target_component"] == 1
    # roll/pitch/yaw neutral, throttle below mid => controlled descent
    assert dec["chan_raw"][0] == mc.RC_CENTER_US
    assert dec["chan_raw"][1] == mc.RC_CENTER_US
    assert dec["chan_raw"][2] == mc.RC_DESCENT_THROTTLE_US
    assert dec["chan_raw"][2] < mc.RC_CENTER_US
    assert dec["chan_raw"][3] == mc.RC_CENTER_US


def test_manual_control_frame_roundtrips():
    payload = mc.build_manual_control_payload(3, x=100, y=-200, z=500, r=0)
    frame = mc.build_packet_v2(69, payload, sequence=1)
    dec = mc.decode_manual_control(frame)
    assert dec["target"] == 3 and dec["z"] == 500 and dec["y"] == -200


# ---- honesty: encrypted/FHSS link => not applicable, transmit nothing ----
def test_encrypted_link_refused_no_transmit():
    for proto in ("ELRS", "DJI OcuSync", "Spektrum DSMX", "CRSF/Crossfire", "FrSky ACCST"):
        sent = []
        res = run_sustained_takeover(
            send_frame=sent.append, target_system=5, target_protocol=proto,
            duration_s=5.0, rc_rate_hz=20.0,
        )
        assert res.not_applicable and not res.ok, (proto, res)
        assert sent == [], (proto, sent)


def test_legacy_link_is_overridable():
    assert link_is_overridable("MAVLink-SiK")
    assert link_is_overridable("ArduPilot")
    # F-3: unknown/empty now fails CLOSED (was True before the fix).
    assert not link_is_overridable(None)
    assert not link_is_overridable("ELRS 2.4")


# ---- bounded duration: hard cap enforced regardless of caller ------------
def test_duration_hard_capped():
    clock = FakeClock()
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=5, target_protocol="MAVLink",
        duration_s=10_000.0,  # absurd request
        rc_rate_hz=10.0, now=clock.now, sleep=clock.sleep,
    )
    assert res.ok and not res.stopped_early
    assert res.elapsed_s <= MAX_DURATION_S + 1e-6, res.elapsed_s
    # at 10 Hz for 30 s => ~300 frames, definitely bounded well under an
    # unbounded stream.
    assert res.frames_sent <= MAX_DURATION_S * 10 + 1, res.frames_sent
    assert res.frames_sent > 0


def test_normal_bounded_window_frame_count():
    clock = FakeClock()
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=5, target_protocol="MAVLink",
        duration_s=2.0, rc_rate_hz=20.0, release_frames=3, now=clock.now, sleep=clock.sleep,
    )
    assert res.ok and not res.stopped_early
    # 2 s * 20 Hz ~= 40 frames
    assert 38 <= res.frames_sent <= 41, res.frames_sent
    # F-5: normal completion appends a neutral release burst on top of the
    # descent frames.
    assert res.release_frames_sent == 3, res.release_frames_sent
    assert len(sent) == res.frames_sent + res.release_frames_sent


# ---- F-5: normal completion emits neutral release frames -----------------
def test_normal_completion_emits_release_frames():
    import mavlink_codec as mc
    clock = FakeClock()
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=9, target_component=1,
        target_protocol="MAVLink", duration_s=1.0, rc_rate_hz=10.0,
        release_frames=3, now=clock.now, sleep=clock.sleep,
    )
    assert res.ok and not res.stopped_early
    assert res.release_frames_sent == 3
    # The last 3 frames must be RC_CHANNELS_OVERRIDE with ALL channels released
    # (0 == "do not override this channel"), returning control to the craft.
    for frame in sent[-3:]:
        dec = mc.decode_rc_channels_override(frame)
        assert dec["target_system"] == 9
        assert all(c == mc.RC_OVERRIDE_RELEASE for c in dec["chan_raw"][:4]), dec


# ---- F-5: abort does NOT transmit any release frame after tx-halt ---------
def test_abort_emits_no_release_frames():
    clock = FakeClock()
    sent = []
    halted = {"v": False}
    def send(frame):
        sent.append(frame)
        if len(sent) == 4:
            halted["v"] = True
    res = run_sustained_takeover(
        send_frame=send, target_system=5, target_protocol="MAVLink",
        duration_s=30.0, rc_rate_hz=20.0, release_frames=3,
        tx_halted=lambda: halted["v"], now=clock.now, sleep=clock.sleep,
    )
    assert res.stopped_early and res.ok, res
    assert res.frames_sent == 4, res.frames_sent
    # No release burst after an abort — nothing is transmitted post-halt.
    assert res.release_frames_sent == 0, res.release_frames_sent
    assert len(sent) == 4, len(sent)


# ---- F-3: unknown protocol fails closed unless legacy-attested -----------
def test_unknown_protocol_fails_closed_without_attestation():
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=5, target_protocol="SomeMysteryLink",
        duration_s=5.0, rc_rate_hz=20.0,
    )
    assert res.not_applicable and not res.ok, res
    assert sent == [], sent


def test_unknown_protocol_allowed_with_legacy_attestation():
    clock = FakeClock()
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=5, target_protocol="SomeMysteryLink",
        target_link_legacy_mavlink=True,
        duration_s=1.0, rc_rate_hz=10.0, now=clock.now, sleep=clock.sleep,
    )
    assert res.ok and not res.not_applicable, res
    assert res.frames_sent > 0


def test_empty_protocol_fails_closed():
    for proto in (None, ""):
        sent = []
        res = run_sustained_takeover(
            send_frame=sent.append, target_system=5, target_protocol=proto,
            duration_s=5.0, rc_rate_hz=20.0,
        )
        assert res.not_applicable and not res.ok and sent == [], (proto, res)


# ---- F-6: target_system 0 (broadcast) / invalid is refused ---------------
def test_target_system_zero_refused_no_transmit():
    for tsys in (0, -1):
        sent = []
        res = run_sustained_takeover(
            send_frame=sent.append, target_system=tsys, target_protocol="MAVLink",
            duration_s=5.0, rc_rate_hz=20.0,
        )
        assert res.not_applicable and not res.ok, (tsys, res)
        assert sent == [], (tsys, sent)


# ---- F-3: link_is_overridable fail-closed semantics ----------------------
def test_link_is_overridable_fail_closed():
    # recognized legacy MAVLink => overridable
    assert link_is_overridable("MAVLink-SiK")
    # encrypted => never, even if attested
    assert not link_is_overridable("ELRS 2.4")
    assert not link_is_overridable("ELRS 2.4", legacy_attested=True)
    # unknown/empty => fail closed unless attested
    assert not link_is_overridable(None)
    assert not link_is_overridable("MysteryLink")
    assert link_is_overridable("MysteryLink", legacy_attested=True)


# ---- immediate abort: tx_halted mid-stream terminates the takeover -------
def test_tx_halt_mid_stream_aborts_immediately():
    clock = FakeClock()
    sent = []
    halted = {"v": False}
    # Assert abort after 5 frames have gone out.
    def send(frame):
        sent.append(frame)
        if len(sent) == 5:
            halted["v"] = True
    res = run_sustained_takeover(
        send_frame=send, target_system=5, target_protocol="MAVLink",
        duration_s=30.0, rc_rate_hz=20.0,
        tx_halted=lambda: halted["v"],
        now=clock.now, sleep=clock.sleep,
    )
    assert res.stopped_early and res.ok, res
    # It must stop essentially immediately — not run the full 30 s window.
    assert res.frames_sent == 5, res.frames_sent
    assert res.elapsed_s < 1.0, res.elapsed_s


def test_stop_event_before_start_sends_nothing():
    ev = threading.Event()
    ev.set()
    sent = []
    res = run_sustained_takeover(
        send_frame=sent.append, target_system=5, target_protocol="MAVLink",
        duration_s=5.0, rc_rate_hz=20.0, stop_event=ev,
    )
    assert res.stopped_early and res.frames_sent == 0 and sent == []


def test_stop_event_mid_stream_aborts():
    clock = FakeClock()
    ev = threading.Event()
    sent = []
    def send(frame):
        sent.append(frame)
        if len(sent) == 3:
            ev.set()
    res = run_sustained_takeover(
        send_frame=send, target_system=5, target_protocol="MAVLink",
        duration_s=30.0, rc_rate_hz=50.0, stop_event=ev,
        now=clock.now, sleep=clock.sleep,
    )
    assert res.stopped_early and res.frames_sent == 3, res


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
