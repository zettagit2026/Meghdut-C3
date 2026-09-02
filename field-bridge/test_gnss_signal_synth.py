"""Unit tests for gnss_signal_synth.py — the real v1 GPS L1 C/A baseband IQ
generator (Task #103, "Task B" DSP core).

No hardware, no network, no live backend — pure DSP. Covers:
  * C/A Gold-code correctness vs published IS-GPS-200 reference chips + the
    512/511 balance property + per-PRN distinctness.
  * NAV framing (preamble, subframe IDs, length) + IS-GPS-200 parity round-trip.
  * IQ output shape / dtype / sample count / format, determinism, and a
    despreading correlation check proving the composite really carries the
    C/A code (not noise, not silence).

Run: pytest field-bridge/test_gnss_signal_synth.py -v
"""
from __future__ import annotations

import os

import numpy as np
import pytest

import gnss_signal_synth as synth


# ---------------------------------------------------------------------
# C/A Gold-code generation
# ---------------------------------------------------------------------
def test_ca_code_length_and_binary():
    code = synth.generate_ca_code(1)
    assert code.shape == (1023,)
    assert set(np.unique(code).tolist()) <= {0, 1}


def test_ca_code_prn1_first10_matches_isgps200_reference():
    """PRN 1 first 10 chips = 1100100000 (IS-GPS-200 Table 3-Ia octal 1440)."""
    first10 = synth.generate_ca_code(1)[:10].tolist()
    assert first10 == [1, 1, 0, 0, 1, 0, 0, 0, 0, 0]
    assert int("".join(str(b) for b in first10), 2) == 0o1440


def test_ca_code_prn2_first10_matches_isgps200_reference():
    """PRN 2 first 10 chips = 1110010000 (IS-GPS-200 Table 3-Ia octal 1620)."""
    first10 = synth.generate_ca_code(2)[:10].tolist()
    assert first10 == [1, 1, 1, 0, 0, 1, 0, 0, 0, 0]
    assert int("".join(str(b) for b in first10), 2) == 0o1620


@pytest.mark.parametrize("prn", [1, 2, 5, 12, 23, 32])
def test_ca_code_balance_property(prn):
    """A valid C/A Gold code has exactly 512 ones and 511 zeros per period."""
    code = synth.generate_ca_code(prn)
    ones = int(code.sum())
    assert ones == 512
    assert code.size - ones == 511


def test_ca_codes_are_distinct_per_prn():
    c1 = synth.generate_ca_code(1)
    c2 = synth.generate_ca_code(2)
    assert not np.array_equal(c1, c2)
    # Balanced Gold codes cross-correlate low: agreement far from all/none.
    agree = int((c1 == c2).sum())
    assert 400 < agree < 620


def test_ca_code_rejects_bad_prn():
    with pytest.raises(ValueError):
        synth.generate_ca_code(99)


def test_ca_code_bipolar_mapping():
    code = synth.generate_ca_code(5)
    bip = synth.ca_code_bipolar(5)
    assert bip.dtype == np.float32
    # logical 0 -> +1, logical 1 -> -1
    assert np.all(bip[code == 0] == 1.0)
    assert np.all(bip[code == 1] == -1.0)


# ---------------------------------------------------------------------
# NAV message framing + parity
# ---------------------------------------------------------------------
def test_nav_message_length_and_binary():
    nav = synth.build_nav_message(1)
    assert nav.shape == (1500,)
    assert set(np.unique(nav).tolist()) <= {0, 1}


def test_nav_parity_round_trip_valid():
    """Every 30-bit word's IS-GPS-200 parity must independently verify."""
    nav = synth.build_nav_message(7)
    assert synth.verify_nav_parity(nav.tolist()) is True


def test_nav_parity_detects_bit_flip():
    nav = synth.build_nav_message(7).tolist()
    nav[123] ^= 1  # corrupt one bit
    assert synth.verify_nav_parity(nav) is False


def test_nav_verify_rejects_bad_length():
    assert synth.verify_nav_parity([1, 0, 1]) is False
    assert synth.verify_nav_parity([]) is False


def test_nav_first_subframe_preamble_is_0x8b():
    """At frame start D30*=0, so the transmitted preamble is the raw 0x8B."""
    nav = synth.build_nav_message(3)
    assert nav[:8].tolist() == [1, 0, 0, 0, 1, 0, 1, 1]


def test_nav_every_subframe_preamble_is_0x8b_or_complement():
    """Later subframes may transmit the preamble inverted (D30*=1) — receivers
    search for the preamble AND its complement; both are valid framing."""
    nav = synth.build_nav_message(3).tolist()
    preamble = [1, 0, 0, 0, 1, 0, 1, 1]
    complement = [b ^ 1 for b in preamble]
    for sf in range(5):
        start = sf * 300
        got = nav[start:start + 8]
        assert got == preamble or got == complement


def test_nav_subframe_ids_increment_1_to_5():
    """HOW word (word 2) carries the 3-bit subframe ID in bits 20-22 (0-indexed
    19..21) of the transmitted word. Recover it accounting for D30* inversion."""
    nav = synth.build_nav_message(1).tolist()
    for sf in range(5):
        word2 = nav[sf * 300 + 30: sf * 300 + 60]
        d30star_prev = nav[sf * 300 + 29]  # D30 of word 1 in this subframe
        sf_bits = [b ^ d30star_prev for b in word2[19:22]]
        sf_id = (sf_bits[0] << 2) | (sf_bits[1] << 1) | sf_bits[2]
        assert sf_id == sf + 1


def test_nav_codes_differ_per_prn():
    assert not np.array_equal(synth.build_nav_message(1), synth.build_nav_message(2))


# ---------------------------------------------------------------------
# lla_to_ecef sanity
# ---------------------------------------------------------------------
def test_lla_to_ecef_equator_prime_meridian():
    x, y, z = synth.lla_to_ecef(0.0, 0.0, 0.0)
    assert abs(x - synth._WGS84_A) < 1.0
    assert abs(y) < 1e-6
    assert abs(z) < 1e-6


def test_lla_to_ecef_north_pole():
    x, y, z = synth.lla_to_ecef(90.0, 0.0, 0.0)
    b = synth._WGS84_A * np.sqrt(1.0 - synth._WGS84_E2)  # polar semi-minor axis
    assert abs(x) < 1e-3
    assert abs(y) < 1e-3
    assert abs(z - b) < 1.0


# ---------------------------------------------------------------------
# IQ output: shape / dtype / count / format / determinism
# ---------------------------------------------------------------------
_SR = 2_046_000  # 2 samples/chip — small & fast, integer samples/chip


def test_iq_samples_shape_and_dtype():
    duration = 0.003
    iq = synth.synthesize_iq_samples(28.6, 77.2, 200.0, duration, sample_rate=_SR,
                                     prns=[1, 2, 3])
    assert iq.dtype == np.int8
    assert iq.size == 2 * int(round(duration * _SR))  # interleaved I/Q


def test_iq_samples_not_silent():
    iq = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.003, sample_rate=_SR,
                                     prns=[1, 2, 3])
    assert np.any(iq != 0)
    assert int(np.abs(iq.astype(np.int16)).max()) > 10


def test_iq_samples_within_int8_range():
    iq = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.003, sample_rate=_SR,
                                     prns=synth.DEFAULT_PRNS)
    assert iq.min() >= -127 and iq.max() <= 127


def test_iq_deterministic_same_inputs_same_bytes():
    a = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.003, sample_rate=_SR, prns=[1, 2])
    b = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.003, sample_rate=_SR, prns=[1, 2])
    assert np.array_equal(a, b)


def test_iq_depends_on_fake_position():
    a = synth.synthesize_iq_samples(28.60, 77.20, 200.0, 0.003, sample_rate=_SR, prns=[1, 2, 3])
    b = synth.synthesize_iq_samples(28.90, 77.90, 200.0, 0.003, sample_rate=_SR, prns=[1, 2, 3])
    assert not np.array_equal(a, b)


def test_zero_duration_yields_empty():
    iq = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.0, sample_rate=_SR, prns=[1])
    assert iq.size == 0


def test_iq_chunk_boundary_is_seamless():
    """Generating across a forced chunk boundary must equal a single-shot run
    (phase/code/nav are computed from absolute sample index)."""
    orig = synth._CHUNK_SAMPLES
    try:
        full = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.004, sample_rate=_SR, prns=[1, 2])
        synth._CHUNK_SAMPLES = 512  # force many small chunks
        chunked = synth.synthesize_iq_samples(28.6, 77.2, 200.0, 0.004, sample_rate=_SR, prns=[1, 2])
    finally:
        synth._CHUNK_SAMPLES = orig
    assert np.array_equal(full, chunked)


def test_single_sat_despreads_to_code_peak():
    """A single-PRN, zero-Doppler, zero-offset signal at 1 sample/chip must
    despread: correlating the real part of one 1023-sample code period against
    the known bipolar C/A code yields a sharp peak >> off-peak — proving the
    composite genuinely carries the Gold code."""
    fs = int(synth._CA_CHIP_RATE_HZ)  # 1 sample per chip
    iq = synth.synthesize_iq_samples(
        28.6, 77.2, 200.0, 1023.0 / fs, sample_rate=fs,
        prns=[5], dopplers_hz=[0.0], code_phase_offsets_chips=[0.0],
    )
    i = iq[0::2].astype(np.float64)[:1023]
    code = synth.ca_code_bipolar(5).astype(np.float64)
    corr = np.array([np.dot(i, np.roll(code, k)) for k in range(1023)])
    peak = np.max(np.abs(corr))
    peak_k = int(np.argmax(np.abs(corr)))
    off = np.abs(np.delete(corr, peak_k))
    assert peak > 8.0 * off.max()  # sharp, unambiguous correlation peak


def test_nav_bit_flips_carrier_phase_sign_present():
    """Sanity: with a real NAV bitstream the despread symbol is code XOR nav;
    the correlation peak sign at code offset 0 equals +/- nav[0]."""
    fs = int(synth._CA_CHIP_RATE_HZ)
    iq = synth.synthesize_iq_samples(
        0.0, 0.0, 0.0, 1023.0 / fs, sample_rate=fs,
        prns=[5], dopplers_hz=[0.0], code_phase_offsets_chips=[0.0],
    )
    i = iq[0::2].astype(np.float64)[:1023]
    code = synth.ca_code_bipolar(5).astype(np.float64)
    corr0 = np.dot(i, code)
    nav0 = float(synth.nav_message_bipolar(5)[0])
    assert np.sign(corr0) == np.sign(nav0)


# ---------------------------------------------------------------------
# File output + placeholder escape hatch
# ---------------------------------------------------------------------
def test_synthesize_iq_file_default_is_real_and_correct_size():
    """DEFAULT path (no env var) now produces a REAL signal of correct size —
    NOT a NotImplemented raise (that old stub behavior is intentionally gone)."""
    os.environ.pop(synth._PLACEHOLDER_ENV, None)
    duration, sr = 0.002, _SR
    path = synth.synthesize_iq_file(28.6, 77.2, 200.0, 28.62, 77.21, 200.0,
                                    duration, sample_rate=sr, prns=[1, 2, 3])
    try:
        assert os.path.getsize(path) == 2 * int(round(duration * sr))
        data = np.fromfile(path, dtype=np.int8)
        assert np.any(data != 0)  # real signal, not silence
    finally:
        os.unlink(path)


def test_synthesize_iq_file_placeholder_mode_is_silent(monkeypatch):
    """The TEST-ONLY escape still writes correct-size all-zero IQ."""
    monkeypatch.setenv(synth._PLACEHOLDER_ENV, "1")
    duration, sr = 0.5, 1000
    path = synth.synthesize_iq_file(28.6, 77.2, 200.0, 28.62, 77.21, 200.0,
                                    duration, sample_rate=sr)
    try:
        assert os.path.getsize(path) == 2 * int(round(duration * sr))
        data = np.fromfile(path, dtype=np.int8)
        assert np.all(data == 0)  # placeholder is silent
    finally:
        os.unlink(path)


def test_default_sample_rate_matches_field_tx_rate():
    """Guard the load-bearing coupling: the default generation rate MUST equal
    the rate hackrf_transfer plays the file back at (hackrf_jam.SAMPLE_RATE_HZ),
    or the field signal would be time-scaled. Kept as an explicit regression."""
    assert synth.SAMPLE_RATE_HZ == 20_000_000
