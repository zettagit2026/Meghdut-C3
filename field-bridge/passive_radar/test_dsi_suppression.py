"""Unit tests for dsi_suppression.py's least-squares DSI removal (task #43, C10).

Run: pytest field-bridge/passive_radar/test_dsi_suppression.py -v
"""
import numpy as np

from passive_radar.dsi_suppression import suppress_dsi, dsi_suppression_gain_db
from passive_radar.channel_source import SyntheticDualChannelSource


def test_suppress_dsi_reduces_direct_path_energy():
    # Strong direct-path breakthrough (attenuation 1.0) dominating a much
    # weaker moving target (attenuation 0.01), matching the reference
    # repo's own qualitative "DSI swamps weak targets" framing.
    source = SyntheticDualChannelSource(
        sample_rate_hz=2.048e6,
        targets=[(120, 60.0, 0.01)],
        direct_path_gain=1.0,
        seed=7,
    )
    ref, surv = source.read_block(20000)
    gain_db = dsi_suppression_gain_db(ref, surv)
    # Suppressing the direct-path/near-zero-lag energy should show a
    # measurable positive dB reduction.
    assert gain_db > 3.0


def test_suppress_dsi_preserves_length():
    source = SyntheticDualChannelSource(sample_rate_hz=2.048e6, seed=3)
    ref, surv = source.read_block(5000)
    suppressed = suppress_dsi(ref, surv)
    assert len(suppressed) == min(len(ref), len(surv))


def test_suppress_dsi_leaves_pure_noise_roughly_unchanged():
    # With no structured direct-path component shared between ref/surv,
    # LS-projection shouldn't dramatically change total energy (no shared
    # structure to project out beyond noise-fitting).
    rng = np.random.default_rng(11)
    n = 5000
    ref = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    surv = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    suppressed = suppress_dsi(ref, surv)
    before = np.sum(np.abs(surv) ** 2)
    after = np.sum(np.abs(suppressed) ** 2)
    # Independent noise: projection removes only a small (~19/n fraction)
    # sliver of energy, not the dramatic reduction seen with real DSI.
    assert after > 0.5 * before
