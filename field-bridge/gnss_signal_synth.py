#!/usr/bin/env python3
"""GNSS L1 C/A signal synthesis — the DSP core of Task #103's GNSS-spoof
("soft-kill") capability. See field-bridge/GNSS_SPOOF_ARCHITECTURE.md and
field-bridge/GNSS_SIGNAL_SYNTH_HANDOFF.md for the full design/§7 task split.

=============================================================================
STATUS: REAL v1 GENERATOR (structurally correct GPS L1 C/A baseband IQ).
=============================================================================
This module is NO LONGER a stub. `synthesize_iq_file()` now generates a real,
structurally-correct GPS L1 C/A baseband IQ stream (Gold-code PRNs + a
well-formed 50 bps NAV bitstream, BPSK-modulated per satellite with per-SV
code-phase and Doppler offsets, summed to a composite complex IQ file in
HackRF's native interleaved-int8 format). The old all-zero placeholder
survives ONLY behind GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ=1 as an explicit
TEST-ONLY escape (see below) — the DEFAULT path produces a real signal and
the field must NEVER rely on the zero-IQ path.

-----------------------------------------------------------------------------
FIDELITY LEVEL ACHIEVED (read before trusting this against a real receiver)
-----------------------------------------------------------------------------
What is CORRECT / spec-faithful in v1:
  * C/A Gold codes: standard G1/G2 10-bit LFSRs with the IS-GPS-200 Table
    3-Ia per-PRN G2 tap-pair phase selection. Verified against the published
    first-10-chip reference for PRN 1 (octal 1440) and PRN 2 (octal 1620),
    and against the C/A balance property (512 ones / 511 zeros per 1023-chip
    period). A wrong tap produces a code that correlates with nothing — this
    is checked in field-bridge/test_gnss_signal_synth.py.
  * NAV framing: 5 subframes x 10 words x 30 bits @ 50 bps, 8-bit preamble
    (10001011), TLM + HOW words, incrementing TOW count and subframe IDs,
    and IS-GPS-200 Hamming(32,26) parity with the D29*/D30* trailing-bit
    convention (data bits transmitted as D_i = d_i XOR D30*). Parity is
    round-trip verified in the tests.
  * Modulation: (C/A chip) XOR (NAV bit) -> BPSK -> per-SV baseband carrier
    exp(j(2*pi*f_doppler*t + phi)), summed across satellites into one
    complex stream, packed as interleaved int8 exactly as
    hackrf_jam.build_noise_iq() does, so hackrf_jam.transmit_iq_file() can
    upconvert it to L1 (1575.42 MHz) with no change to that file.

What is SIMPLIFIED / NOT yet high-fidelity (a fabricated *static* scenario):
  * The NAV message carries a well-formed but PLACEHOLDER ephemeris/almanac
    (words 3-10 are structurally valid and parity-correct but do NOT encode a
    solved orbital constellation whose decoded satellite positions + the
    per-SV code phases below are mutually consistent with the fabricated
    receiver fix). i.e. v1 emits a *structurally valid* signal, NOT a
    guaranteed *navigation-solution-consistent* one.
  * Per-SV code-phase offsets are derived from a simplified geometric range
    model (fake-position ECEF projected onto fixed nominal line-of-sight unit
    vectors) so that the output DEPENDS on the fabricated position and is
    deterministic — but this is NOT the rigorous ephemeris<->pseudorange
    consistency a real fix requires.
  * Doppler is a fixed nominal per-SV spread, NOT computed from real orbital
    dynamics; there is no code/carrier Doppler *rate* (acceleration) over the
    burst, no ionospheric/tropospheric delay terms, and no real GPS system
    time alignment.

CONSEQUENCE / HONEST CLAIM BOUNDARY: this v1 produces a real, on-frequency,
structurally-correct L1 C/A signal that a receiver's acquisition stage can
detect/correlate on (right codes, right chip rate, plausible Doppler). It has
NOT been validated to force a position fix on any real receiver, and given
the simplifications above it very likely will NOT on its own — see
GNSS_SIGNAL_SYNTH_HANDOFF.md §4 (multi-PRN + jam-then-spoof capture) and the
"REMAINING FOR A REAL-RECEIVER TEST" list at the bottom of this file. Do NOT
describe this as working "against any GPS" until it is tested, on an
authorized range under the existing governed arming spine, against real
hardware. Nothing here transmits; this module only writes a file.

Reference technique (studied, reimplemented cleanly — NOT copied): the public
IS-GPS-200 interface spec and osqzss/gps-sdr-sim (MIT). This module contains
no networking/WS/HTTP code and is independently unit-testable without any
HackRF hardware or bridge/backend present.
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np

# =============================================================================
# Sample rate.
#
# DEFAULT IS 20 Msps AND THAT IS LOAD-BEARING, NOT A FREE PARAMETER: the field
# TX path is hackrf_jam.transmit_iq_file(), which invokes
# `hackrf_transfer -s <hackrf_jam.SAMPLE_RATE_HZ>` with SAMPLE_RATE_HZ hard-set
# to 20_000_000 in hackrf_jam.py. The bridge (gnss_spoof_bridge.py) calls
# synthesize_iq_file() with NO sample_rate argument, so this default is what
# the field actually generates at. If this default did not match the rate
# hackrf_transfer plays the file back at, the entire signal would be time-
# scaled -> every code rate and Doppler shifted by the ratio -> useless. A
# lower, L1-C/A-specific rate (e.g. 2.048 or 4 Msps, which the DSP would
# otherwise prefer for size/CPU) is only safe once the TX call is
# parameterized to pass that same rate to hackrf_transfer -- that change lives
# in hackrf_jam.py / gnss_spoof_bridge.py (a DIFFERENT workstream's files) and
# is flagged in this task's report, NOT made here.
# =============================================================================
SAMPLE_RATE_HZ = 20_000_000

# GPS L1 C/A physical constants (IS-GPS-200).
_L1_HZ = 1_575_420_000.0
_CA_CHIP_RATE_HZ = 1_023_000.0
_CA_CODE_LEN = 1023
_NAV_BIT_RATE_HZ = 50.0
_NAV_BITS_PER_SUBFRAME = 300
_NAV_SUBFRAMES = 5
_NAV_FRAME_BITS = _NAV_BITS_PER_SUBFRAME * _NAV_SUBFRAMES  # 1500
_C_LIGHT = 299_792_458.0

# WGS84 (for the simplified fake-position -> code-phase geometry only).
_WGS84_A = 6_378_137.0
_WGS84_E2 = 6.694379990141e-3

# Default satellite set to synthesize. 8 PRNs — a plausible "sky" of
# simultaneously-tracked SVs (see HANDOFF §4: a single PRN will not move a
# real multi-SV fix; multiple PRNs are the minimum credible attempt).
DEFAULT_PRNS: Tuple[int, ...] = (2, 4, 5, 9, 10, 12, 17, 23)

# IS-GPS-200 Table 3-Ia: per-PRN G2 phase-selector tap pair (1-indexed G2
# register stages whose XOR forms the delayed G2 output). PRN 1..32.
G2_TAPS: dict = {
    1: (2, 6), 2: (3, 7), 3: (4, 8), 4: (5, 9), 5: (1, 9), 6: (2, 10),
    7: (1, 8), 8: (2, 9), 9: (3, 10), 10: (2, 3), 11: (3, 4), 12: (5, 6),
    13: (6, 7), 14: (7, 8), 15: (8, 9), 16: (9, 10), 17: (1, 4), 18: (2, 5),
    19: (3, 6), 20: (4, 7), 21: (5, 8), 22: (6, 9), 23: (1, 3), 24: (4, 6),
    25: (5, 7), 26: (6, 8), 27: (7, 9), 28: (8, 10), 29: (1, 6), 30: (2, 7),
    31: (3, 8), 32: (4, 9),
}

# NAV preamble (IS-GPS-200): first 8 bits of every subframe's TLM word.
_NAV_PREAMBLE = (1, 0, 0, 0, 1, 0, 1, 1)  # 0x8B

# Set truthy to opt IN to a placeholder (silent, all-zero) IQ file instead of
# real synthesis. TEST-ONLY: lets the gate-chain integration tests exercise
# the full path (including a real hackrf_transfer invocation against a real,
# if content-free, IQ file) WITHOUT paying the CPU cost of real synthesis.
# NEVER set in production — see gnss_spoof_bridge.py's docstring. Retained
# (not deleted) purely as that explicit test escape; the default path is real.
_PLACEHOLDER_ENV = "GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ"

# Chunk size (complex samples) for streaming generation, so a 3s @ 20 Msps
# burst (60M samples, ~120 MB file) never materializes as one giant array.
_CHUNK_SAMPLES = 1 << 20  # ~1.05M samples/chunk


class GnssSynthNotImplemented(NotImplementedError):
    """Retained for import-compatibility with gnss_spoof_bridge.py (which does
    `from gnss_signal_synth import GnssSynthNotImplemented`). The real default
    path no longer raises this — v1 synthesis is implemented — but the symbol
    stays defined so the bridge's `except GnssSynthNotImplemented` import and
    handler remain valid (that branch is now simply not exercised on the
    default path)."""


# =============================================================================
# C/A Gold-code generation (IS-GPS-200).
# =============================================================================
def generate_ca_code(prn: int) -> np.ndarray:
    """Return the 1023-chip C/A Gold code for `prn` as an int8 array of {0,1}.

    Two 10-bit LFSRs (G1, G2) initialized to all-ones:
      * G1 feedback taps: stages 3 and 10;  output: stage 10.
      * G2 feedback taps: stages 2,3,6,8,9,10;  output: XOR of the two
        per-PRN phase-selector stages (G2_TAPS[prn]).
      * chip = G1_out XOR G2_selected_out.
    """
    if prn not in G2_TAPS:
        raise ValueError(f"unsupported PRN {prn!r} (valid PRNs: 1..32)")
    t1, t2 = G2_TAPS[prn]
    g1 = [1] * 10
    g2 = [1] * 10
    code = np.empty(_CA_CODE_LEN, dtype=np.int8)
    for i in range(_CA_CODE_LEN):
        g1_out = g1[9]
        g2_out = g2[t1 - 1] ^ g2[t2 - 1]
        code[i] = g1_out ^ g2_out
        fb1 = g1[2] ^ g1[9]
        fb2 = g2[1] ^ g2[2] ^ g2[5] ^ g2[7] ^ g2[8] ^ g2[9]
        g1 = [fb1] + g1[:9]
        g2 = [fb2] + g2[:9]
    return code


def ca_code_bipolar(prn: int) -> np.ndarray:
    """C/A code mapped to bipolar float32: logical 0 -> +1.0, logical 1 -> -1.0
    (standard BPSK convention). Length 1023."""
    return (1.0 - 2.0 * generate_ca_code(prn).astype(np.float32)).astype(np.float32)


# =============================================================================
# NAV message (50 bps) — structurally-valid framing + IS-GPS-200 parity.
# =============================================================================
# Parity source bit indices (1-indexed into the 24 transmitted data bits
# D1..D24), IS-GPS-200 Table 20-XIV / the standard Hamming(32,26) equations.
_PARITY_SETS: Tuple[Tuple[int, ...], ...] = (
    (1, 2, 3, 5, 6, 10, 11, 12, 13, 14, 17, 18, 20, 23),        # D25 (^ D29*)
    (2, 3, 4, 6, 7, 11, 12, 13, 14, 15, 18, 19, 21, 24),        # D26 (^ D30*)
    (1, 3, 4, 5, 7, 8, 12, 13, 14, 15, 16, 19, 20, 22),         # D27 (^ D29*)
    (2, 4, 5, 6, 8, 9, 13, 14, 15, 16, 17, 20, 21, 23),         # D28 (^ D30*)
    (1, 3, 5, 6, 7, 9, 10, 14, 15, 16, 17, 18, 21, 22, 24),     # D29 (^ D30*)
    (3, 5, 6, 8, 9, 10, 11, 13, 15, 19, 22, 23, 24),            # D30 (^ D29*)
)
# Which starred trailing bit prefixes each parity bit.
_PARITY_STAR = ("D29", "D30", "D29", "D30", "D30", "D29")


def _encode_word(data24: Sequence[int], d29star: int, d30star: int) -> Tuple[List[int], int, int]:
    """Encode one 30-bit NAV word from 24 raw source bits + the previous
    word's last two transmitted bits (D29*, D30*), per IS-GPS-200.

    Transmitted data bits D_i = d_i XOR D30*; parity D25..D30 computed from
    the transmitted D1..D24 and D29*/D30*. Returns (30 bits, D29, D30)."""
    if len(data24) != 24:
        raise ValueError("NAV word needs exactly 24 data bits")
    d = [int(b) & 1 for b in data24]
    tx = [di ^ d30star for di in d]  # D1..D24 as transmitted
    parity: List[int] = []
    for k, idxs in enumerate(_PARITY_SETS):
        acc = d29star if _PARITY_STAR[k] == "D29" else d30star
        for j in idxs:
            acc ^= tx[j - 1]
        parity.append(acc & 1)
    word = tx + parity
    return word, parity[4], parity[5]  # D29, D30


def _int_bits(value: int, nbits: int) -> List[int]:
    """value -> nbits MSB-first bit list (two's-complement-agnostic; caller
    supplies non-negative field values)."""
    value &= (1 << nbits) - 1
    return [(value >> (nbits - 1 - i)) & 1 for i in range(nbits)]


def build_nav_message(prn: int, tow_count: int = 100, week_number: int = 2200) -> np.ndarray:
    """Build one full, structurally-valid 1500-bit NAV frame (5 subframes)
    as an int8 array of transmitted {0,1} bits @ 50 bps.

    Framing (preamble/TLM/HOW/subframe-ID/TOW/parity) is spec-faithful and
    parity-correct. Words 3-10 carry PLACEHOLDER (deterministic, parity-valid)
    ephemeris/almanac payload — see module docstring's fidelity note: this is
    a well-formed static NAV message, NOT a solved orbital constellation."""
    bits: List[int] = []
    d29star = 0
    d30star = 0
    for sf in range(1, _NAV_SUBFRAMES + 1):
        # TOW in the HOW is the count of the NEXT subframe's start (6s units).
        tow_next = (tow_count + sf) % (1 << 17)
        for word_idx in range(1, 11):
            if word_idx == 1:
                # TLM: 8-bit preamble + 14-bit TLM message + 2 reserved/integrity.
                data = list(_NAV_PREAMBLE) + _int_bits(0x1234 & 0x3FFF, 14) + [0, 0]
            elif word_idx == 2:
                # HOW: 17-bit TOW count + alert(0) + anti-spoof(0) + 3-bit
                # subframe ID + 2 trailing bits (left 0; a full encoder would
                # solve these so D29,D30 == 0 — left explicit/simple here).
                data = _int_bits(tow_next, 17) + [0, 0] + _int_bits(sf, 3) + [0, 0]
            else:
                # Words 3-10: placeholder-but-parity-valid payload. Deterministic
                # in (prn, subframe, word) so output is reproducible; NOT a solved
                # ephemeris (see docstring). week_number is surfaced in SF1 word3.
                if sf == 1 and word_idx == 3:
                    data = _int_bits(week_number, 10) + _int_bits(0, 14)
                else:
                    seed = (prn * 131 + sf * 17 + word_idx) & 0xFFFFFF
                    data = _int_bits(seed, 24)
            word, d29star, d30star = _encode_word(data, d29star, d30star)
            bits.extend(word)
    arr = np.asarray(bits, dtype=np.int8)
    assert arr.size == _NAV_FRAME_BITS
    return arr


def verify_nav_parity(bits: Sequence[int]) -> bool:
    """Independently re-check IS-GPS-200 parity across a bit sequence whose
    length is a multiple of 30. Returns True iff every word's stored D25..D30
    match a fresh recomputation from its D1..D24 and the previous word's
    D29*/D30*. Used by the tests as an encode/decode round-trip."""
    b = [int(x) & 1 for x in bits]
    if len(b) % 30 != 0 or not b:
        return False
    d29star = 0
    d30star = 0
    for w in range(0, len(b), 30):
        tx = b[w:w + 24]
        stored = b[w + 24:w + 30]
        for k, idxs in enumerate(_PARITY_SETS):
            acc = d29star if _PARITY_STAR[k] == "D29" else d30star
            for j in idxs:
                acc ^= tx[j - 1]
            if (acc & 1) != stored[k]:
                return False
        d29star = stored[4]
        d30star = stored[5]
    return True


def nav_message_bipolar(prn: int, **kw) -> np.ndarray:
    """NAV frame mapped to bipolar float32: bit 0 -> +1.0, bit 1 -> -1.0."""
    return (1.0 - 2.0 * build_nav_message(prn, **kw).astype(np.float32)).astype(np.float32)


# =============================================================================
# Simplified fake-position geometry -> per-SV code-phase offsets.
# =============================================================================
def lla_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> Tuple[float, float, float]:
    """WGS84 geodetic lat/lon/alt -> ECEF (x, y, z) metres."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + alt_m) * sin_lat
    return x, y, z


# Fixed nominal line-of-sight unit vectors (one per satellite slot). NOT real
# satellite geometry — a deterministic spread so per-SV code phase depends on
# the fabricated position. Spherical-ish spread over the visible hemisphere.
def _nominal_los_unit(sat_index: int, n_sats: int) -> Tuple[float, float, float]:
    az = 2.0 * math.pi * (sat_index / max(1, n_sats))
    el = math.radians(15.0 + 60.0 * ((sat_index * 37) % max(1, n_sats)) / max(1, n_sats))
    ce = math.cos(el)
    return (ce * math.cos(az), ce * math.sin(az), math.sin(el))


def _code_phase_offset_chips(fake_ecef: Tuple[float, float, float], sat_index: int, n_sats: int) -> float:
    """Simplified per-SV code-phase offset (in chips) derived from the fake
    position: project the fake ECEF onto a fixed nominal LOS unit vector to
    get a pseudo-range, convert to chips mod 1023. Deterministic and position-
    dependent; NOT a rigorous ephemeris<->pseudorange solution (see docstring).
    """
    ux, uy, uz = _nominal_los_unit(sat_index, n_sats)
    # A nominal GPS range (~2.0e7 m) plus the fake-position projection, so the
    # offset moves with the fabricated fix but stays in a plausible regime.
    pseudo_range_m = 2.0e7 + (fake_ecef[0] * ux + fake_ecef[1] * uy + fake_ecef[2] * uz)
    chips = (pseudo_range_m / _C_LIGHT) * _CA_CHIP_RATE_HZ
    return float(chips % _CA_CODE_LEN)


def _nominal_doppler_hz(sat_index: int, n_sats: int) -> float:
    """Fixed nominal per-SV Doppler spread in [-4000, +4000] Hz. NOT from real
    orbital dynamics; constant over the burst (no Doppler rate). See docstring."""
    if n_sats <= 1:
        return 0.0
    frac = sat_index / (n_sats - 1)  # 0..1
    return -4000.0 + 8000.0 * frac


# =============================================================================
# Composite IQ synthesis.
# =============================================================================
def _resolve_sat_params(
    fake_lat: float,
    fake_lon: float,
    fake_alt_m: float,
    prns: Sequence[int],
    dopplers_hz: Optional[Sequence[float]],
    code_phase_offsets_chips: Optional[Sequence[float]],
) -> Tuple[List[float], List[float]]:
    n = len(prns)
    fake_ecef = lla_to_ecef(fake_lat, fake_lon, fake_alt_m)
    if dopplers_hz is None:
        dopplers = [_nominal_doppler_hz(i, n) for i in range(n)]
    else:
        if len(dopplers_hz) != n:
            raise ValueError("dopplers_hz length must match prns")
        dopplers = [float(d) for d in dopplers_hz]
    if code_phase_offsets_chips is None:
        offsets = [_code_phase_offset_chips(fake_ecef, i, n) for i in range(n)]
    else:
        if len(code_phase_offsets_chips) != n:
            raise ValueError("code_phase_offsets_chips length must match prns")
        offsets = [float(o) for o in code_phase_offsets_chips]
    return dopplers, offsets


def _iter_iq_chunks(
    fake_lat: float,
    fake_lon: float,
    fake_alt_m: float,
    duration_s: float,
    sample_rate: int,
    prns: Sequence[int],
    dopplers_hz: Optional[Sequence[float]],
    code_phase_offsets_chips: Optional[Sequence[float]],
) -> Iterator[bytes]:
    """Stream the composite interleaved-int8 IQ in chunks. Phase, code phase
    and NAV index are all computed from ABSOLUTE sample index, so chunk
    boundaries are seamless (no per-chunk accumulator drift)."""
    n_total = int(round(duration_s * sample_rate))
    if n_total <= 0:
        return
    prns = list(prns)
    n_sats = len(prns)
    if n_sats == 0:
        raise ValueError("need at least one PRN")

    ca = [ca_code_bipolar(p) for p in prns]
    nav = [nav_message_bipolar(p) for p in prns]
    nav_len = _NAV_FRAME_BITS
    dopplers, offsets = _resolve_sat_params(
        fake_lat, fake_lon, fake_alt_m, prns, dopplers_hz, code_phase_offsets_chips
    )

    # Per-SV amplitude: independent of N so dynamic range is stable. With
    # near-orthogonal codes the composite RMS ~= amp*sqrt(N)/sqrt(2) ~= 70,
    # leaving headroom under the int8 clip (matches hackrf_jam's clip posture).
    amp = 100.0 / math.sqrt(n_sats)

    fs = float(sample_rate)
    two_pi = 2.0 * math.pi
    n0 = 0
    while n0 < n_total:
        m = min(_CHUNK_SAMPLES, n_total - n0)
        t = (np.arange(n0, n0 + m, dtype=np.float64)) / fs  # seconds
        comp_r = np.zeros(m, dtype=np.float64)
        comp_i = np.zeros(m, dtype=np.float64)
        for s in range(n_sats):
            dop = dopplers[s]
            # Code Doppler scales the chip rate slightly (delta = f_dop/L1).
            code_rate = _CA_CHIP_RATE_HZ * (1.0 + dop / _L1_HZ)
            code_phase = t * code_rate + offsets[s]
            chip_idx = np.mod(np.floor(code_phase).astype(np.int64), _CA_CODE_LEN)
            chip = ca[s][chip_idx]
            nav_idx = np.mod(np.floor(t * _NAV_BIT_RATE_HZ).astype(np.int64), nav_len)
            navv = nav[s][nav_idx]
            sig = chip * navv  # +-1 BPSK symbol (code XOR nav)
            ph = two_pi * dop * t  # baseband carrier (HackRF LO handles L1)
            comp_r += amp * sig * np.cos(ph)
            comp_i += amp * sig * np.sin(ph)

        iq = np.empty(2 * m, dtype=np.int8)
        iq[0::2] = np.clip(np.rint(comp_r), -127, 127).astype(np.int8)
        iq[1::2] = np.clip(np.rint(comp_i), -127, 127).astype(np.int8)
        yield iq.tobytes()
        n0 += m


def synthesize_iq_samples(
    fake_lat: float,
    fake_lon: float,
    fake_alt_m: float,
    duration_s: float,
    sample_rate: int = SAMPLE_RATE_HZ,
    prns: Optional[Sequence[int]] = None,
    dopplers_hz: Optional[Sequence[float]] = None,
    code_phase_offsets_chips: Optional[Sequence[float]] = None,
) -> np.ndarray:
    """Return the full composite IQ as one interleaved int8 array (I,Q,I,Q...).

    In-memory convenience for tests/inspection — the field path streams to a
    file via synthesize_iq_file() instead. Keep durations/rates small here."""
    if prns is None:
        prns = DEFAULT_PRNS
    parts = list(_iter_iq_chunks(
        fake_lat, fake_lon, fake_alt_m, duration_s, sample_rate,
        prns, dopplers_hz, code_phase_offsets_chips,
    ))
    if not parts:
        return np.empty(0, dtype=np.int8)
    return np.frombuffer(b"".join(parts), dtype=np.int8).copy()


def synthesize_iq_file(
    true_lat: float,
    true_lon: float,
    true_alt_m: float,
    fake_lat: float,
    fake_lon: float,
    fake_alt_m: float,
    duration_s: float,
    sample_rate: int = SAMPLE_RATE_HZ,
    *,
    prns: Optional[Sequence[int]] = None,
    dopplers_hz: Optional[Sequence[float]] = None,
    code_phase_offsets_chips: Optional[Sequence[float]] = None,
) -> str:
    """Synthesize a real GPS L1 C/A baseband IQ file and return its path.

    Called by field-bridge/gnss_spoof_bridge.py as
    `synthesize_iq_file(true_lat, true_lon, true_alt_m, fake_lat, fake_lon,
    fake_alt_m, duration_s)` — i.e. positionally, with NO sample_rate. The
    default sample_rate therefore MUST match the rate hackrf_transfer plays
    the file back at (see the SAMPLE_RATE_HZ note at the top of this file).

    The `true_*` coordinates are accepted for signature/contract stability
    with the bridge (and future geodesic/relative-offset fidelity work) but
    are not required by the current fabricated-static-scenario generator,
    which encodes the `fake_*` position directly.

    Output format: interleaved int8 (1 byte I, 1 byte Q per complex sample) —
    byte-for-byte the same layout hackrf_jam.build_noise_iq() writes, so
    hackrf_jam.transmit_iq_file() consumes it unchanged.

    GNSS_SPOOF_ALLOW_PLACEHOLDER_IQ=1 (TEST-ONLY) writes a silent all-zero IQ
    file of the correct size instead of synthesizing — for gate-chain testing
    only; NEVER set in production (the default path is real synthesis).
    """
    if prns is None:
        prns = DEFAULT_PRNS

    n_total = int(round(duration_s * sample_rate))
    f = tempfile.NamedTemporaryFile(suffix=".iq", delete=False)
    try:
        if os.environ.get(_PLACEHOLDER_ENV, "").lower() in ("1", "true", "yes"):
            # TEST-ONLY placeholder: silent (all-zero) IQ, correct size only.
            chunk_n = min(max(n_total, 0), 1_000_000)
            zero_chunk = bytes(2 * chunk_n) if chunk_n else b""
            remaining = n_total
            while remaining > 0:
                take = min(remaining, chunk_n)
                f.write(zero_chunk[: 2 * take])
                remaining -= take
        else:
            for chunk in _iter_iq_chunks(
                fake_lat, fake_lon, fake_alt_m, duration_s, sample_rate,
                prns, dopplers_hz, code_phase_offsets_chips,
            ):
                f.write(chunk)
        f.flush()
    finally:
        f.close()
    return f.name


# =============================================================================
# REMAINING FOR A REAL-RECEIVER / DRONE TEST (later governed, authorized-range
# step — NOT done here, code+tests only):
#   1. Solved ephemeris: encode subframes 1-3 with orbital elements whose
#      decoded SV positions are mutually consistent with the per-SV code
#      phases and the fabricated fix (the osqzss/gps-sdr-sim core problem),
#      then decode-and-resolve to assert the fix reproduces fake_lat/lon/alt.
#   2. Real (RINEX/broadcast) ephemeris ingestion + true GPS system-time
#      alignment, so pseudoranges are physically coherent.
#   3. Doppler dynamics: per-SV Doppler + Doppler RATE over the burst from
#      real SV motion; code/carrier coherency.
#   4. Ionospheric/tropospheric delay terms; SV clock corrections.
#   5. Power/geometry calibration against a real receiver (and the jam-then-
#      spoof capture approach, HANDOFF §4) on an authorized range under the
#      existing arm/confirm/range-auth/tx_halt spine.
# =============================================================================
