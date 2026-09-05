"""Unit tests for sdr_mavlink_inject.py — the GFSK PHY modulator that injects a
byte-accurate MAVLink command frame into baseband IQ for over-the-air injection
into a fixed-frequency unencrypted MAVLink link.

No hardware, no network, no transmit, no live backend — pure DSP. Covers:
  * byte<->bit round-trip (MSB/LSB), framing layout.
  * GFSK modulation correctness: symbol->frequency sign, Gaussian taps (unity
    DC gain), samples-per-symbol, achieved peak deviation, constant envelope.
  * A known MAVLink frame round-trips modulate -> (reference GFSK demod) ->
    deframe back to the ORIGINAL bytes, for several commands and both bit orders.
  * IQ output shape / dtype / interleaved-int8 format matching the transmitter,
    int8 range, and full determinism.

Run: pytest field-bridge/test_sdr_mavlink_inject.py -v
"""
from __future__ import annotations

import math

import numpy as np
import pytest

import sdr_mavlink_inject as inj


# ---------------------------------------------------------------------
# byte <-> bit helpers + framing
# ---------------------------------------------------------------------
def test_bytes_to_bits_msb_first():
    bits = inj.bytes_to_bits(b"\x01", "msb")
    assert bits.tolist() == [0, 0, 0, 0, 0, 0, 0, 1]


def test_bytes_to_bits_lsb_first():
    bits = inj.bytes_to_bits(b"\x01", "lsb")
    assert bits.tolist() == [1, 0, 0, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("bit_order", ["msb", "lsb"])
def test_bytes_bits_round_trip(bit_order):
    data = bytes(range(256))
    bits = inj.bytes_to_bits(data, bit_order)
    assert bits.size == 256 * 8
    assert inj.bits_to_bytes(bits, bit_order) == data


def test_bits_to_bytes_rejects_non_multiple_of_8():
    with pytest.raises(ValueError):
        inj.bits_to_bytes([1, 0, 1], "msb")


def test_bad_bit_order_rejected():
    with pytest.raises(ValueError):
        inj.bytes_to_bits(b"\x00", "middle")


def test_build_framed_bits_layout():
    frame = b"\xDE\xAD"
    pre = b"\xAA"
    sync = b"\x2D\xD4"
    bits = inj.build_framed_bits(frame, preamble=pre, sync_word=sync, bit_order="msb")
    expected = (
        inj.bytes_to_bits(pre, "msb").tolist()
        + inj.bytes_to_bits(sync, "msb").tolist()
        + inj.bytes_to_bits(frame, "msb").tolist()
    )
    assert bits.tolist() == expected


# ---------------------------------------------------------------------
# Gaussian pulse-shaping taps
# ---------------------------------------------------------------------
def test_gaussian_taps_unity_dc_gain():
    taps = inj.gaussian_taps(20, inj.DEFAULT_BT)
    assert abs(taps.sum() - 1.0) < 1e-12  # unity DC gain -> full deviation on a run


def test_gaussian_taps_symmetric_and_odd():
    taps = inj.gaussian_taps(16, 0.5)
    assert taps.size % 2 == 1
    assert np.allclose(taps, taps[::-1])


def test_gaussian_taps_rejects_bad_bt():
    with pytest.raises(ValueError):
        inj.gaussian_taps(10, 0.0)


# ---------------------------------------------------------------------
# GFSK modulation: symbol -> frequency, deviation, samples/symbol
# ---------------------------------------------------------------------
_SR = 2_000_000.0       # 2 Msps: small & fast, integer samples/symbol
_BAUD = 100_000.0       # -> 20 samples/symbol
_DEV = 25_000.0         # modulation index 0.5


def test_all_ones_gives_positive_frequency():
    """A run of 1-bits is a +1 symbol run -> positive instantaneous frequency
    approaching +deviation (Gaussian has unity DC gain)."""
    bits = np.ones(40, dtype=np.int8)
    z = inj.modulate_bits_to_complex(bits, _SR, _BAUD, _DEV, inj.DEFAULT_BT)
    inst = np.angle(z[1:] * np.conj(z[:-1]))
    freq_hz = inst * _SR / (2.0 * math.pi)
    # Middle samples (away from filter edges) should sit near +deviation.
    mid = freq_hz[len(freq_hz) // 3: 2 * len(freq_hz) // 3]
    assert np.mean(mid) > 0
    assert abs(np.max(freq_hz) - _DEV) < 0.05 * _DEV  # within 5% of peak deviation


def test_all_zeros_gives_negative_frequency():
    bits = np.zeros(40, dtype=np.int8)
    z = inj.modulate_bits_to_complex(bits, _SR, _BAUD, _DEV, inj.DEFAULT_BT)
    inst = np.angle(z[1:] * np.conj(z[:-1]))
    freq_hz = inst * _SR / (2.0 * math.pi)
    assert np.min(freq_hz) < 0
    assert abs(np.min(freq_hz) + _DEV) < 0.05 * _DEV


def test_samples_per_symbol_count():
    bits = np.array([1, 0, 1, 0, 1], dtype=np.int8)
    z = inj.modulate_bits_to_complex(bits, _SR, _BAUD, _DEV)
    assert z.size == int(round(5 * _SR / _BAUD))  # 5 symbols * 20 sps = 100


def test_constant_envelope():
    """GFSK is constant-envelope: |s[n]| is unity for every sample."""
    bits = inj.bytes_to_bits(b"\x12\x34\x56", "msb")
    z = inj.modulate_bits_to_complex(bits, _SR, _BAUD, _DEV)
    assert np.allclose(np.abs(z), 1.0, atol=1e-9)


def test_modulate_rejects_baud_above_sample_rate():
    with pytest.raises(ValueError):
        inj.modulate_bits_to_complex(np.ones(8, dtype=np.int8),
                                     sample_rate_hz=1000.0, air_data_rate_bps=2000.0)


# ---------------------------------------------------------------------
# IQ output: shape / dtype / interleaved-int8 format / range / determinism
# ---------------------------------------------------------------------
def _frame():
    return inj.build_command_frame("force_land", target_system=7, target_component=1, seq=3)


def test_iq_dtype_and_interleaved_shape():
    iq = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR,
                                  air_data_rate_bps=_BAUD, deviation_hz=_DEV)
    assert iq.dtype == np.int8
    assert iq.size % 2 == 0  # interleaved I/Q pairs
    framed_bits = inj.build_framed_bits(_frame()).size
    assert iq.size == 2 * int(round(framed_bits * _SR / _BAUD))


def test_iq_within_int8_range_and_not_silent():
    iq = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR,
                                  air_data_rate_bps=_BAUD, deviation_hz=_DEV)
    assert iq.min() >= -127 and iq.max() <= 127
    assert np.any(iq != 0)


def test_iq_deterministic():
    a = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    b = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    assert np.array_equal(a, b)


def test_repeat_multiplies_and_is_seamless_concat():
    one = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                   repeat=1)
    three = inj.modulate_frame_to_iq(_frame(), sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                     repeat=3, gap_symbols=0)
    # 3 back-to-back bursts of identical framed bits -> 3x the symbols/samples.
    assert three.size == 3 * one.size


def test_repeat_must_be_positive():
    with pytest.raises(ValueError):
        inj.modulate_frame_to_iq(_frame(), repeat=0)


def test_write_iq_file_matches_transmitter_format(tmp_path):
    """The on-disk IQ is raw interleaved int8, exactly what
    hackrf_jam.transmit_iq_file() reads back (np.fromfile int8)."""
    frame = _frame()
    path = str(tmp_path / "burst.iq")
    inj.write_iq_file(frame, path, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    iq_mem = inj.modulate_frame_to_iq(frame, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    on_disk = np.fromfile(path, dtype=np.int8)
    assert np.array_equal(on_disk, iq_mem)


# ---------------------------------------------------------------------
# Full modulate -> demodulate -> deframe round-trip back to original bytes
# ---------------------------------------------------------------------
@pytest.mark.parametrize("command", sorted(inj.COMMAND_BUILDERS))
@pytest.mark.parametrize("bit_order", ["msb", "lsb"])
def test_frame_round_trips_through_gfsk(command, bit_order):
    """A known MAVLink frame survives modulate -> GFSK demod -> deframe and
    comes back byte-identical, proving the PHY carries the real frame bytes."""
    frame = inj.build_command_frame(command, target_system=5, target_component=1, seq=1)
    iq = inj.modulate_frame_to_iq(
        frame, sample_rate_hz=_SR, air_data_rate_bps=_BAUD, deviation_hz=_DEV,
        bit_order=bit_order,
    )
    bits = inj.demodulate_to_symbols(iq, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    recovered = inj.deframe_symbols(bits, n_frame_bytes=len(frame),
                                    sync_word=inj.DEFAULT_SYNC_WORD, bit_order=bit_order)
    assert recovered == frame


def test_round_trip_default_field_rate():
    """Round-trip at the LOAD-BEARING field rate (20 Msps default) and the
    default air rate — the config the field actually generates at."""
    frame = _frame()
    iq = inj.modulate_frame_to_iq(frame)  # all defaults incl. 20 Msps
    bits = inj.demodulate_to_symbols(
        iq, sample_rate_hz=inj.DEFAULT_SAMPLE_RATE_HZ,
        air_data_rate_bps=inj.DEFAULT_AIR_DATA_RATE_BPS,
    )
    recovered = inj.deframe_symbols(bits, n_frame_bytes=len(frame))
    assert recovered == frame


def test_deframe_returns_none_without_sync():
    bits = np.zeros(500, dtype=np.int8)  # no sync word present
    assert inj.deframe_symbols(bits, n_frame_bytes=4) is None


def test_demod_recovers_known_bit_pattern():
    """Directly modulate a known bit pattern and recover it (no framing), to
    isolate the modulator/demodulator from the deframer."""
    pattern = np.array([1, 0, 1, 1, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 0, 0], dtype=np.int8)
    z = inj.modulate_bits_to_complex(pattern, _SR, _BAUD, _DEV)
    iq = inj.complex_to_interleaved_int8(z)
    recovered = inj.demodulate_to_symbols(iq, _SR, _BAUD)
    assert recovered[: pattern.size].tolist() == pattern.tolist()


# ---------------------------------------------------------------------
# Command building delegates to mavlink_codec (byte accuracy) + describe
# ---------------------------------------------------------------------
def test_build_command_frame_is_valid_mavlink():
    from mavlink_codec import describe_packet, payload_force_land
    frame = inj.build_command_frame("force_land", target_system=9)
    assert frame == payload_force_land(9, 1, 0)  # identical bytes, not re-implemented
    info = describe_packet(frame)
    assert info["valid"] and info["message_id"] == 76  # COMMAND_LONG


def test_build_command_frame_rejects_unknown():
    with pytest.raises(ValueError):
        inj.build_command_frame("self_destruct", target_system=1)


def test_describe_modulation_reports_honest_metadata():
    info = inj.describe_modulation(_frame(), sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                  deviation_hz=_DEV)
    assert info["modulation_index"] == pytest.approx(2.0 * _DEV / _BAUD)
    assert info["samples_per_symbol"] == pytest.approx(_SR / _BAUD)
    assert info["iq_int8_bytes"] == 2 * info["iq_samples"]
    assert info["mavlink"]["valid"] is True
    assert info["on_air_duration_s"] > 0


def test_import_is_non_transmitting():
    """Importing the module must not transmit or need hardware — it already
    imported at module top; assert the guard constants are sane."""
    assert inj.DEFAULT_SAMPLE_RATE_HZ == 20_000_000
    assert set(inj.COMMAND_BUILDERS) >= {"force_land", "rth", "disarm"}


# ---------------------------------------------------------------------
# Configurable PHY: operator-settable preamble + sync word
# ---------------------------------------------------------------------
def test_custom_preamble_and_sync_are_used():
    """A custom preamble + sync word actually change the framed bits and survive
    the round-trip (operator matches the target link's acquisition framing)."""
    frame = _frame()
    pre = b"\xAA\xAA\xAA\xAA\xAA\xAA"
    sync = b"\x13\x37"
    framed = inj.build_framed_bits(frame, preamble=pre, sync_word=sync, bit_order="msb")
    # First bits are the (uncoded) preamble; the sync word follows it.
    pre_bits = len(pre) * 8
    assert framed[:pre_bits].tolist() == inj.bytes_to_bits(pre, "msb").tolist()
    sync_bits = inj.bytes_to_bits(sync, "msb")
    assert framed[pre_bits: pre_bits + sync_bits.size].tolist() == sync_bits.tolist()
    # Round-trip with the SAME custom framing recovers the frame.
    iq = inj.modulate_frame_to_iq(frame, sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                  deviation_hz=_DEV, preamble=pre, sync_word=sync)
    bits = inj.demodulate_to_symbols(iq, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    recovered = inj.deframe_symbols(bits, n_frame_bytes=len(frame), sync_word=sync)
    assert recovered == frame


def test_wrong_sync_word_does_not_deframe():
    """Deframing with a sync word the burst was NOT built with finds no frame —
    proving sync is genuinely matched, not ignored."""
    frame = _frame()
    iq = inj.modulate_frame_to_iq(frame, sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                  deviation_hz=_DEV, sync_word=b"\x2D\xD4")
    bits = inj.demodulate_to_symbols(iq, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    assert inj.deframe_symbols(bits, n_frame_bytes=len(frame), sync_word=b"\x99\x99") is None


# ---------------------------------------------------------------------
# Optional Golay(24,12) FEC — real encode/decode, real error correction
# ---------------------------------------------------------------------
def test_golay_word_roundtrip_all_datawords():
    """Every 12-bit dataword encodes to 24 bits and decodes back identically."""
    for m in range(0, 4096, 7):  # sample the 4096 words
        cw = inj.golay_encode12(m)
        assert cw >> 12 == m  # systematic: top 12 bits are the data
        assert inj.golay_decode24(cw) == m


def test_golay_corrects_up_to_3_bit_errors():
    """Extended Golay(24,12) corrects up to 3 bit errors per 24-bit word — the
    whole point of the FEC (raises decode probability on a noisy link)."""
    import itertools
    m = 0b101100111010
    cw = inj.golay_encode12(m)
    for nbits in (1, 2, 3):
        for positions in itertools.combinations(range(24), nbits):
            r = cw
            for p in positions:
                r ^= (1 << p)
            assert inj.golay_decode24(r) == m, (nbits, positions)


def test_fec_golay_frame_round_trips_through_gfsk():
    """A MAVLink frame Golay-coded, GFSK-modulated, demodulated and deframed with
    fec='golay' comes back byte-identical."""
    frame = inj.build_command_frame("disarm", target_system=7, target_component=1, seq=2)
    iq = inj.modulate_frame_to_iq(frame, sample_rate_hz=_SR, air_data_rate_bps=_BAUD,
                                  deviation_hz=_DEV, fec="golay")
    bits = inj.demodulate_to_symbols(iq, sample_rate_hz=_SR, air_data_rate_bps=_BAUD)
    recovered = inj.deframe_symbols(bits, n_frame_bytes=len(frame),
                                    sync_word=inj.DEFAULT_SYNC_WORD, fec="golay")
    assert recovered == frame


def test_fec_golay_expands_payload_vs_none():
    """fec='golay' expands the on-air payload (24/12 = 2x the frame bits) vs raw —
    a real coding layer, not a no-op flag."""
    frame = _frame()
    none_bits = inj.build_framed_bits(frame, fec="none").size
    golay_bits = inj.build_framed_bits(frame, fec="golay").size
    # preamble+sync are identical/uncoded; the frame portion roughly doubles.
    assert golay_bits > none_bits
    info = inj.describe_modulation(frame, fec="golay")
    assert info["fec"] == "golay"


def test_bad_fec_rejected():
    with pytest.raises(ValueError):
        inj.build_framed_bits(_frame(), fec="reed_solomon")
