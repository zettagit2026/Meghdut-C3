"""Unit tests for channel_source.py (task #43, C10): DualChannelSource ABC,
Synthetic/RecordedFile implementations, and the DualRTLSDRSource stub.

Run: pytest field-bridge/passive_radar/test_channel_source.py -v
"""
import os
import struct
import tempfile

import numpy as np
import pytest

from passive_radar.channel_source import (
    SyntheticDualChannelSource,
    RecordedFileDualChannelSource,
    DualRTLSDRSource,
)


def test_synthetic_source_sample_rate_and_shape():
    src = SyntheticDualChannelSource(sample_rate_hz=1e6)
    assert src.sample_rate_hz == 1e6
    ref, surv = src.read_block(1000)
    assert ref.shape == (1000,)
    assert surv.shape == (1000,)
    assert ref.dtype == np.complex64
    assert surv.dtype == np.complex64


def test_synthetic_source_deterministic_with_seed():
    src1 = SyntheticDualChannelSource(sample_rate_hz=1e6, seed=99)
    src2 = SyntheticDualChannelSource(sample_rate_hz=1e6, seed=99)
    ref1, surv1 = src1.read_block(500)
    ref2, surv2 = src2.read_block(500)
    np.testing.assert_array_equal(ref1, ref2)
    np.testing.assert_array_equal(surv1, surv2)


def test_synthetic_source_stop_iteration_when_exhausted():
    src = SyntheticDualChannelSource(sample_rate_hz=1e6, total_samples=100)
    src.read_block(100)
    with pytest.raises(StopIteration):
        src.read_block(10)


def test_recorded_file_source_split_files_roundtrip():
    n = 200
    rng = np.random.default_rng(0)
    ref_iq = (rng.integers(-100, 100, size=2 * n)).astype(np.int8)
    surv_iq = (rng.integers(-100, 100, size=2 * n)).astype(np.int8)

    with tempfile.TemporaryDirectory() as d:
        ref_path = os.path.join(d, "ref.bin")
        surv_path = os.path.join(d, "surv.bin")
        ref_iq.tofile(ref_path)
        surv_iq.tofile(surv_path)

        src = RecordedFileDualChannelSource(
            sample_rate_hz=2.048e6, ref_path=ref_path, surv_path=surv_path, dtype="int8"
        )
        ref, surv = src.read_block(n)
        expected_ref = (ref_iq[0::2].astype(np.float32) + 1j * ref_iq[1::2].astype(np.float32))
        expected_surv = (surv_iq[0::2].astype(np.float32) + 1j * surv_iq[1::2].astype(np.float32))
        np.testing.assert_allclose(ref, expected_ref.astype(np.complex64))
        np.testing.assert_allclose(surv, expected_surv.astype(np.complex64))
        src.close()


def test_recorded_file_source_interleaved_roundtrip():
    n = 50
    rng = np.random.default_rng(1)
    interleaved = rng.integers(-100, 100, size=4 * n).astype(np.int8)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "interleaved.bin")
        interleaved.tofile(path)
        src = RecordedFileDualChannelSource(
            sample_rate_hz=2.048e6, interleaved_path=path, dtype="int8"
        )
        ref, surv = src.read_block(n)
        expected_ref = interleaved[0::4].astype(np.float32) + 1j * interleaved[1::4].astype(np.float32)
        expected_surv = interleaved[2::4].astype(np.float32) + 1j * interleaved[3::4].astype(np.float32)
        np.testing.assert_allclose(ref, expected_ref.astype(np.complex64))
        np.testing.assert_allclose(surv, expected_surv.astype(np.complex64))
        src.close()


def test_recorded_file_source_stop_iteration_on_exhaustion():
    n = 10
    rng = np.random.default_rng(2)
    ref_iq = rng.integers(-100, 100, size=2 * n).astype(np.int8)
    surv_iq = rng.integers(-100, 100, size=2 * n).astype(np.int8)
    with tempfile.TemporaryDirectory() as d:
        ref_path = os.path.join(d, "ref.bin")
        surv_path = os.path.join(d, "surv.bin")
        ref_iq.tofile(ref_path)
        surv_iq.tofile(surv_path)
        src = RecordedFileDualChannelSource(
            sample_rate_hz=2.048e6, ref_path=ref_path, surv_path=surv_path, dtype="int8"
        )
        src.read_block(n)
        with pytest.raises(StopIteration):
            src.read_block(n)
        src.close()


def test_dual_rtlsdr_source_is_stubbed_not_implemented():
    with pytest.raises(NotImplementedError, match="task #57"):
        DualRTLSDRSource()
