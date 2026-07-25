"""Direct Signal Interference (DSI) suppression via least-squares projection.

Direct port of goship.m's `dsi_suppression` block (verified against
~/Desktop/zettagit/passive_radar/171210ship/goship.m lines ~99-116),
credited there to "W. Feng (X'ian, China)":

    Index1=-9;Index2=+9;
    num_range_shift=(Index2-Index1+1);
    X1=zeros(nt,num_range_shift);
    for kk=Index1:Index2
        te=kk+abs(Index1)+1;
        if kk<=0
            X1(:,te)=[ref(0-kk+1:end);zeros(0-kk,1)];
        else
            X1(:,te)=[zeros(kk-1,1);ref(1:end-kk+1)];
        end
    end
    mes=mes-X1*(pinv(X1)*mes);   % Least Square optimization

Builds a small bank of range-shifted copies of the reference signal
(shift = Index1..Index2 samples) and removes their least-squares-optimal
projection from the surveillance channel. This kills the strong direct-
path breakthrough (and any static/near-zero-Doppler clutter) that would
otherwise swamp the cross-ambiguity map and hide real (weak, moving)
targets in its sidelobes.

This is pure math -- no illuminator- or hardware-specific assumptions
(PASSIVE_RADAR_ARCHITECTURE.md §3): it operates on any two complex
baseband streams regardless of what emitter they came from.
"""
from __future__ import annotations

import numpy as np


def _shift_matrix(ref: np.ndarray, index1: int, index2: int) -> np.ndarray:
    """Builds the X1 range-shift bank exactly as goship.m does, translated
    from Octave's 1-indexed slicing to numpy's 0-indexed slicing.

    Octave `kk<=0` branch: X1(:,te) = [ref(0-kk+1:end); zeros(0-kk,1)]
      -- i.e. ref shifted LEFT (earlier) by |kk| samples, zero-padded at
      the tail. In 0-indexed numpy terms: take ref[-kk:] then pad -kk
      zeros at the end.
    Octave `kk>0` branch: X1(:,te) = [zeros(kk-1,1); ref(1:end-kk+1)]
      -- ref shifted RIGHT (later) by (kk-1) samples via (kk-1) leading
      zeros, keeping ref[0:end-kk+1] after them. Translated: pad (kk-1)
      zeros at the head, then ref[: n-kk+1].
    """
    nt = len(ref)
    num_range_shift = index2 - index1 + 1
    x1 = np.zeros((nt, num_range_shift), dtype=np.complex128)
    for kk in range(index1, index2 + 1):
        te = kk - index1  # 0-indexed column, matches Octave's kk+abs(Index1)+1 (1-indexed)
        if kk <= 0:
            shift = -kk
            col = np.concatenate([ref[shift:], np.zeros(shift, dtype=ref.dtype)])
        else:
            shift = kk - 1
            tail_len = nt - kk + 1
            col = np.concatenate([np.zeros(shift, dtype=ref.dtype), ref[:tail_len]])
        # Guard against off-by-one length drift for edge kk values.
        if len(col) != nt:
            col = np.resize(col, nt)
        x1[:, te] = col
    return x1


def suppress_dsi(
    ref: np.ndarray, surv: np.ndarray, index1: int = -9, index2: int = 9
) -> np.ndarray:
    """Removes the least-squares-optimal projection of range-shifted
    copies of `ref` from `surv`, matching goship.m's
    `mes = mes - X1*(pinv(X1)*mes)`.

    Returns the DSI-suppressed surveillance channel (same length as
    input). `ref` and `surv` must already be aligned (see alignment.py)
    and the same length.
    """
    ref = np.asarray(ref, dtype=np.complex128)
    surv = np.asarray(surv, dtype=np.complex128)
    if len(ref) != len(surv):
        n = min(len(ref), len(surv))
        ref = ref[:n]
        surv = surv[:n]
    x1 = _shift_matrix(ref, index1, index2)
    projection_coeffs = np.linalg.pinv(x1) @ surv
    suppressed = surv - x1 @ projection_coeffs
    return suppressed.astype(np.complex64)


def dsi_suppression_gain_db(ref: np.ndarray, surv: np.ndarray, **kwargs) -> float:
    """Convenience metric for tests/validation: how many dB of zero-lag
    (direct-path) energy were removed by suppress_dsi(). Matches the
    reference repo's own qualitative before/after framing (README.md:
    DSI removal makes a previously-hidden target visible by suppressing
    the dominant near-zero-lag clutter)."""
    suppressed = suppress_dsi(ref, surv, **kwargs)
    before = float(np.sum(np.abs(surv) ** 2))
    after = float(np.sum(np.abs(suppressed) ** 2))
    if after <= 0 or before <= 0:
        return float("inf")
    return 10.0 * np.log10(before / after)
