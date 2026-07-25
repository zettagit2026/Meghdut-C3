"""Inter-channel delay estimation and alignment.

Direct port of goship.m's USB-bus delay correction (verified against
~/Desktop/zettagit/passive_radar/171210ship/goship.m lines ~36-55):

    xc=abs(xcorr(ref,mes));   % measure time offsets between RTL-SDR receivers
    [val,pos]=max(xc);
    pos=length(ref)-pos      % position max wrt cross-correlation origin
    if (pos>0)
        mes=mes(pos:end); ref=ref(1:end-pos);
    else
        ref=ref(-pos+1:end); mes=mes(1:end+pos);
    end

This is a ONE-TIME-PER-RUN calibration step (distinct from the per-block
CAF in caf.py) that exists because two independently-clocked USB dongles
have no shared sample clock/trigger -- it is a workaround for consumer
RTL-SDR hardware, not an inherent requirement of passive radar (real
hardware sync, task #57, would remove the *need* for it, though the
software technique itself remains generically useful/portable).
"""
from __future__ import annotations

from typing import Tuple

import numpy as np


def estimate_delay_samples(ref: np.ndarray, surv: np.ndarray) -> int:
    """Estimate the integer sample delay of `surv` relative to `ref` via
    full cross-correlation, mirroring goship.m's `xcorr(ref,mes)` +
    `pos=length(ref)-pos` convention.

    Positive return value means `surv` lags `ref` by that many samples
    (surv's useful content starts `delay` samples later than ref's);
    negative means `surv` leads `ref`.
    """
    ref = np.asarray(ref)
    surv = np.asarray(surv)
    n = len(ref)
    if n == 0 or len(surv) == 0:
        return 0
    # np.correlate 'full' mode gives lags from -(n-1) to +(n-1) for equal
    # length inputs, matching Octave/MATLAB's xcorr(ref, mes) semantics
    # (lag k means mes shifted right by k aligns with ref).
    full = np.correlate(ref, surv, mode="full")
    pos_idx = int(np.argmax(np.abs(full)))
    # goship.m's "pos = length(ref) - pos" where `pos` (1-indexed in Octave)
    # is the argmax index of xcorr(ref,mes). np.correlate's zero-lag index
    # is (n-1) for equal-length inputs; convert to the same lag convention.
    delay = pos_idx - (n - 1)
    return -delay


def align_channels(
    ref: np.ndarray, surv: np.ndarray, max_search: int = None
) -> Tuple[np.ndarray, np.ndarray, int]:
    """Estimate the inter-channel delay and return (ref_aligned,
    surv_aligned, delay_samples) sliced to a common, aligned length,
    directly mirroring goship.m's post-`pos` slicing:

        if (pos>0): mes=mes(pos:end); ref=ref(1:end-pos)
        else:       ref=ref(-pos+1:end); mes=mes(1:end+pos)

    `max_search` optionally truncates the correlation search window for
    performance on long blocks (goship.m uses the full first buffer; for
    very large blocks callers may want to bound this).
    """
    ref = np.asarray(ref)
    surv = np.asarray(surv)
    if max_search is not None:
        n = min(len(ref), len(surv), max_search)
        pos = estimate_delay_samples(ref[:n], surv[:n])
    else:
        pos = estimate_delay_samples(ref, surv)

    if pos > 0:
        surv_a = surv[pos:]
        ref_a = ref[: len(ref) - pos] if pos <= len(ref) else ref[0:0]
    elif pos < 0:
        ref_a = ref[-pos:]
        surv_a = surv[: len(surv) + pos] if -pos <= len(surv) else surv[0:0]
    else:
        ref_a, surv_a = ref, surv

    m = min(len(ref_a), len(surv_a))
    return ref_a[:m], surv_a[:m], pos
