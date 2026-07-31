#!/usr/bin/env python3
"""Unit tests for crsf_parser.py's CRSFCadenceTracker (task #96).

These tests verify the TIMING-CONSISTENCY MATH against synthetic,
deterministic frame-timestamp sequences built directly from Betaflight's own
reference cadence constants (CRSF_TIME_BETWEEN_FRAMES_US,
CRSF_CYCLETIME_US, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US,
CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US -- see crsf_parser.py's module-level
comment block for verified betaflight source line citations). They do NOT
claim to prove real-world spoofing/replay detection -- consistent with this
project's standing rule against fabricated ground truth, and with
CRSFCadenceTracker's own documented honest limitations (timing consistency
only, not content authenticity; one reference implementation, not every
legitimate CRSF/ELRS configuration).

Run: pytest field-bridge/test_crsf_cadence.py -v
"""
import pytest

import crsf_parser
from crsf_parser import (
    CRSFCadenceTracker,
    CRSFSerialBridge,
    CRSF_CYCLETIME_US,
    CRSF_FRAMETYPE_BATTERY_SENSOR,
    CRSF_FRAMETYPE_LINK_STATISTICS,
    CRSF_FRAMETYPE_RC_CHANNELS_PACKED,
    CRSF_LINK_STATUS_UPDATE_TIMEOUT_US,
    CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US,
    CRSF_TIME_BETWEEN_FRAMES_US,
)


def _feed(tracker: CRSFCadenceTracker, frame_type: int, interval_us: float, count: int,
          start_t: float = 0.0) -> None:
    """Feed `count` synthetic frames of `frame_type` spaced exactly
    `interval_us` microseconds apart, starting at `start_t` seconds.
    """
    t = start_t
    for _ in range(count):
        tracker.record(frame_type, t)
        t += interval_us / 1_000_000.0


# =============================================================================
# insufficient_data
# =============================================================================

def test_insufficient_data_before_min_samples():
    tracker = CRSFCadenceTracker(min_samples=5)
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, CRSF_TIME_BETWEEN_FRAMES_US, count=3)
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "insufficient_data"
    assert verdict["sample_count"] == 2  # 3 frames -> 2 intervals


def test_insufficient_data_for_untouched_bucket():
    tracker = CRSFCadenceTracker()
    verdict = tracker.classify("link_statistics")
    assert verdict["status"] == "insufficient_data"
    assert verdict["sample_count"] == 0


# =============================================================================
# cadence_consistent -- reference-cadence synthetic sequences
# =============================================================================

def test_rc_channels_at_reference_150hz_is_consistent():
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, CRSF_TIME_BETWEEN_FRAMES_US, count=15)
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "cadence_consistent"
    assert abs(verdict["median_interval_us"] - CRSF_TIME_BETWEEN_FRAMES_US) < 1.0


def test_rc_channels_at_common_lower_rate_is_still_consistent():
    # A real, common, still-legitimate RC rate (e.g. 50Hz = 20000us) well
    # inside the [6.667ms, 250ms] reference band -- must not false-positive.
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, 20000.0, count=15)
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "cadence_consistent"


def test_link_statistics_at_reference_4hz_is_consistent():
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_LINK_STATISTICS, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US, count=10)
    verdict = tracker.classify("link_statistics")
    assert verdict["status"] == "cadence_consistent"


def test_telemetry_other_at_reference_cycletime_is_consistent():
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_BATTERY_SENSOR, CRSF_CYCLETIME_US, count=10)
    verdict = tracker.classify("telemetry_other")
    assert verdict["status"] == "cadence_consistent"


def test_normal_jitter_does_not_false_positive():
    # +/-15% jitter around the 150Hz reference rate -- realistic UART/
    # scheduler jitter, must classify as consistent (tolerance band exists
    # precisely to absorb this).
    tracker = CRSFCadenceTracker()
    base = CRSF_TIME_BETWEEN_FRAMES_US
    t = 0.0
    jitters = [1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 1.12, 0.88, 1.0, 1.03,
               0.97, 1.08, 0.92, 1.0, 1.06]
    for j in jitters:
        tracker.record(CRSF_FRAMETYPE_RC_CHANNELS_PACKED, t)
        t += (base * j) / 1_000_000.0
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "cadence_consistent"


# =============================================================================
# cadence_anomalous -- wildly different intervals
# =============================================================================

def test_rc_channels_wildly_faster_than_150hz_is_anomalous():
    # 10x faster than the fastest documented RC rate -- not achievable by a
    # spec-conformant Betaflight-class transmitter.
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, CRSF_TIME_BETWEEN_FRAMES_US / 10.0, count=15)
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "cadence_anomalous"


def test_rc_channels_wildly_slower_than_4hz_is_anomalous():
    # 10x slower than the slowest documented periodic cadence.
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US * 10.0, count=15)
    verdict = tracker.classify("rc_channels")
    assert verdict["status"] == "cadence_anomalous"


def test_link_statistics_faster_than_plausible_is_anomalous():
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_LINK_STATISTICS, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US / 50.0, count=10)
    verdict = tracker.classify("link_statistics")
    assert verdict["status"] == "cadence_anomalous"


def test_telemetry_other_tighter_than_enforced_floor_is_anomalous():
    # Well below CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US's 20ms floor -- not
    # achievable by a spec-conformant scheduler.
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_BATTERY_SENSOR, CRSF_TELEMETRY_FRAME_INTERVAL_MAX_US / 20.0, count=10)
    verdict = tracker.classify("telemetry_other")
    assert verdict["status"] == "cadence_anomalous"


# =============================================================================
# bucketing / independence between frame types
# =============================================================================

def test_buckets_are_tracked_independently():
    tracker = CRSFCadenceTracker()
    _feed(tracker, CRSF_FRAMETYPE_RC_CHANNELS_PACKED, CRSF_TIME_BETWEEN_FRAMES_US, count=10)
    _feed(tracker, CRSF_FRAMETYPE_LINK_STATISTICS, CRSF_LINK_STATUS_UPDATE_TIMEOUT_US / 50.0, count=10)
    rc_verdict = tracker.classify("rc_channels")
    ls_verdict = tracker.classify("link_statistics")
    assert rc_verdict["status"] == "cadence_consistent"
    assert ls_verdict["status"] == "cadence_anomalous"


def test_unknown_frame_type_is_not_tracked():
    tracker = CRSFCadenceTracker()
    tracker.record(0x7F, 0.0)  # not a frame type this tracker has a reference cadence for
    tracker.record(0x7F, 1.0)
    for bucket in ("rc_channels", "link_statistics", "telemetry_other"):
        assert tracker.classify(bucket)["status"] == "insufficient_data"


def test_classify_all_returns_all_three_buckets():
    tracker = CRSFCadenceTracker()
    result = tracker.classify_all()
    assert set(result.keys()) == {"rc_channels", "link_statistics", "telemetry_other"}
    assert all(v["status"] == "insufficient_data" for v in result.values())


def test_out_of_order_timestamp_is_ignored_not_corrupting():
    tracker = CRSFCadenceTracker()
    tracker.record(CRSF_FRAMETYPE_RC_CHANNELS_PACKED, 1.0)
    tracker.record(CRSF_FRAMETYPE_RC_CHANNELS_PACKED, 0.5)  # goes backwards -- must be ignored
    verdict = tracker.classify("rc_channels")
    assert verdict["sample_count"] == 0
    assert verdict["status"] == "insufficient_data"


# -----------------------------------------------------------------------
# TASK #151: run_forever() idle-loop liveness heartbeat (same pattern as
# mavlink_sniffer.py's IDLE_HEARTBEAT_INTERVAL_S, task #139). Verifies the
# "still listening" print fires on the fixed cadence when the serial link
# is genuinely idle (empty reads), and not more often than that cadence.
# -----------------------------------------------------------------------

class _StopLoop(BaseException):
    """Sentinel used to break run_forever()'s `while True` after a fixed
    number of iterations, so the test doesn't hang. Deliberately derives
    from BaseException (like KeyboardInterrupt), NOT Exception -- the read
    loop's `except Exception` (for real serial I/O errors) would otherwise
    swallow this sentinel too and loop forever instead of propagating out."""


class _FakeIdleSerial:
    """Fake serial.Serial-like object whose .read() always returns an empty
    chunk (genuinely idle link), and raises _StopLoop once the caller has
    exhausted the number of reads the test wants to observe."""

    def __init__(self, max_reads: int):
        self.max_reads = max_reads
        self.n = 0

    def read(self, size):
        self.n += 1
        if self.n > self.max_reads:
            raise _StopLoop()
        return b""


def test_run_forever_prints_idle_heartbeat_on_cadence(monkeypatch, capsys):
    bridge = CRSFSerialBridge("http://console.example", {}, "/dev/fake-crsf", 420000,
                               email="e@example.com", password="pw")
    fake_serial = _FakeIdleSerial(max_reads=3)
    monkeypatch.setattr(bridge, "_open_serial", lambda: fake_serial)

    # Fake clock: three idle reads at t=1000, t=1061, t=1062 (61s then 1s
    # apart) so exactly two of the three should cross the 60s cadence.
    fake_times = iter([1000.0, 1061.0, 1062.0])
    monkeypatch.setattr(crsf_parser.time, "time", lambda: next(fake_times))

    with pytest.raises(_StopLoop):
        bridge.run_forever()

    out = capsys.readouterr().out
    heartbeat_lines = [line for line in out.splitlines() if "[heartbeat]" in line]
    assert len(heartbeat_lines) == 2
    assert "still listening" in heartbeat_lines[0]
    assert "/dev/fake-crsf" in heartbeat_lines[0]
