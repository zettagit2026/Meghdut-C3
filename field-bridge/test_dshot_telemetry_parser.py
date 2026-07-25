#!/usr/bin/env python3
"""Unit tests for dshot_telemetry_parser.py (task #115).

Covers the same ground as this module's embedded self_test() (which also
runs standalone via `python3 dshot_telemetry_parser.py --self-test`), plus a
couple of extra edge cases exercised here under pytest so they run as part
of the standard field-bridge suite.

NOTE ON SCOPE: bidirectional DSHOT is a wired FC<->ESC motor-control-bus
protocol with NO RF-detection relevance to this project (see the module
docstring's HONESTY DETERMINATION section) -- these tests only exercise
protocol-decode correctness, not any live serial/RF path (there isn't one).

Run: pytest field-bridge/test_dshot_telemetry_parser.py -v
"""
import pytest

from dshot_telemetry_parser import (
    DecodedTelemetry,
    TelemetryType,
    GCR_ENCODE_TABLE,
    GCR_DECODE_TABLE,
    ERPM_ZERO_SENTINEL,
    decode_telemetry_payload,
    edt_current_a,
    edt_temperature_c,
    edt_voltage_v,
    erpm_from_period,
    gcr_decode_frame,
    gcr_decode_nibble,
    gcr_encode_nibble,
    self_test,
)


def test_embedded_self_test_passes():
    # The module's own self-test is the primary correctness gate; run it
    # here too so pytest fails loudly if it ever regresses.
    self_test()


def test_gcr_table_is_bijective_and_round_trips():
    assert len(GCR_ENCODE_TABLE) == 16
    assert len(set(GCR_ENCODE_TABLE.values())) == 16
    assert len(GCR_DECODE_TABLE) == 16
    for nibble in range(16):
        code = gcr_encode_nibble(nibble)
        assert gcr_decode_nibble(code) == nibble


def test_gcr_encode_rejects_out_of_range():
    with pytest.raises(ValueError):
        gcr_encode_nibble(16)
    with pytest.raises(ValueError):
        gcr_encode_nibble(-1)


def test_gcr_decode_rejects_invalid_codeword():
    # 0b00000 is not in the 16-entry valid codeword set.
    assert gcr_decode_nibble(0b00000) is None


def test_gcr_decode_frame_range_check():
    with pytest.raises(ValueError):
        gcr_decode_frame(-1)
    with pytest.raises(ValueError):
        gcr_decode_frame(1 << 22)


def test_erpm_zero_sentinel():
    assert erpm_from_period(ERPM_ZERO_SENTINEL) == 0


def test_erpm_period_math_exact():
    # exponent=0, mantissa=1 -> period=1 -> erpm = 600,000,000 exactly
    assert erpm_from_period(1) == 600_000_000
    # exponent=3, mantissa=1 -> period=8 -> erpm = 75,000,000 exactly
    assert erpm_from_period((3 << 9) | 1) == 75_000_000


def test_erpm_from_period_range_check():
    with pytest.raises(ValueError):
        erpm_from_period(-1)
    with pytest.raises(ValueError):
        erpm_from_period(0x1000)


def test_decode_telemetry_payload_erpm_fallback_when_tag_zero():
    result = decode_telemetry_payload(0x0AB, edt_enabled=True)
    assert isinstance(result, DecodedTelemetry)
    assert result.telemetry_type == TelemetryType.ERPM
    assert result.erpm is not None
    assert result.edt_value8 is None


def test_decode_telemetry_payload_edt_temperature():
    result = decode_telemetry_payload(0x1FF, edt_enabled=True)
    assert result.telemetry_type == TelemetryType.TEMPERATURE
    assert result.edt_value8 == 0xFF
    assert edt_temperature_c(result.edt_value8) == 255


def test_decode_telemetry_payload_edt_voltage():
    result = decode_telemetry_payload(0x350, edt_enabled=True)
    assert result.telemetry_type == TelemetryType.VOLTAGE
    assert edt_voltage_v(result.edt_value8) == pytest.approx(20.0)


def test_decode_telemetry_payload_edt_current():
    result = decode_telemetry_payload(0x50F, edt_enabled=True)
    assert result.telemetry_type == TelemetryType.CURRENT
    assert edt_current_a(result.edt_value8) == 0x0F


def test_decode_telemetry_payload_edt_disabled_forces_erpm():
    result = decode_telemetry_payload(0x350, edt_enabled=False)
    assert result.telemetry_type == TelemetryType.ERPM


def test_decode_telemetry_payload_range_check():
    with pytest.raises(ValueError):
        decode_telemetry_payload(-1)
    with pytest.raises(ValueError):
        decode_telemetry_payload(0x1000)
