#!/usr/bin/env python3
"""Real HackRF raw-IQ capture tool for CEMA cUAS RF fingerprinting/SEI work.

RECEIVE ONLY. This script never transmits. It has no TX code path, no
`-t`/transmit flag is ever passed to the underlying `hackrf_transfer`
invocation, and there is no synthetic/simulated-signal generation anywhere
in it -- every sample it writes to disk came from the HackRF's ADC, off a
real antenna, in real time. This is the same "no simulation, ever" rule
applied everywhere else in this codebase (see `hackrf_rx.py`).

--------------------------------------------------------------------------
WHY THIS EXISTS / THE GAP IT CLOSES
--------------------------------------------------------------------------
`hackrf_rx.py` in this same directory is energy/RSSI-only: it shells out to
`hackrf_sweep` and only ever sees peak power per bin, never the underlying
IQ waveform. That is enough for energy-detection + coarse RSSI-distance
heuristics, but it is a dead end for anything that needs to look at the
*signal itself* -- modulation classification, RF/emitter fingerprinting
(specific emitter identification), or any ML classifier trained on raw IQ
(e.g. the RFUAV / ORACLE-style pipelines referenced during this project's
planning). Those all need real complex baseband samples as input. This
script is step one of closing that gap: a clean, minimal tool that captures
N seconds of real IQ from the HackRF and writes it to disk as SigMF.

This script intentionally does NOT wire into the live detection pipeline
(no backend/server.py changes, no new ingest endpoint) -- that is separate,
future work once a classifier actually exists to consume these captures.

--------------------------------------------------------------------------
CAPTURE MECHANISM -- WHY `hackrf_transfer`, NOT GNU RADIO
--------------------------------------------------------------------------
Two ways to drive a HackRF for raw IQ capture were considered, following
FISSURE's (`/Users/biswajit/Desktop/Zettawise/PMO Suraj/tool/FISSURE`)
proven approach as the reference:

  1. GNU Radio, via `gr-osmosdr`'s `osmocom_source` block feeding a
     `blocks.file_sink` (this is exactly what FISSURE's own
     `Flow Graph Library/maint-3.10/IQ Flow Graphs/iq_recorder_hackrf.py`
     does: `osmosdr.source(args="numchan=1 hackrf=<serial>")` ->
     `blocks.skiphead` (drops the first ~200k transient samples) ->
     `blocks.head` (caps sample count) -> `blocks.file_sink`).
  2. `hackrf_transfer -r <file> -f <freq_hz> -s <rate_hz> -n <n_samples>`,
     the raw capture mode of the `hackrf_transfer` CLI that ships in the
     `hackrf` apt/brew package -- no GNU Radio involved at all.

This deployment's Python environment was checked directly (not assumed):
    $ python3 -c "import gnuradio"   -> ModuleNotFoundError: No module named 'gnuradio'
    $ python3 -c "import osmosdr"    -> ModuleNotFoundError: No module named 'osmosdr'
    $ python3 -c "import SoapySDR"   -> ModuleNotFoundError: No module named 'SoapySDR'
GNU Radio's Python bindings and gr-osmosdr are NOT importable here. The
"gnuradio symlinked into the tool/ reference directory" is FISSURE's own
bundled copy for its own use -- it is not on this deployment's Python path
and pulling in a full GNU Radio + gr-osmosdr + UHD/SoapySDR stack just to
record IQ would be a heavy, fragile new dependency chain for something
`hackrf_transfer` already does natively and reliably.

The `hackrf` apt package (already installed and confirmed working via
`hackrf_info`/`hackrf_sweep`) ships `hackrf_transfer`, which talks to the
HackRF over libusb directly and supports raw IQ recording out of the box
with `-r`. So: **this script wraps `hackrf_transfer` in a subprocess**,
matching the same "shell out to the vetted hackrf-tools CLI" pattern
`hackrf_rx.py` already uses for sweeps, rather than adding a brand-new
dependency family. This is simpler to deploy and lower-risk, and produces
byte-for-byte the same real ADC samples GNU Radio's osmocom_source would
have handed to a file_sink -- HackRF's firmware/USB framing is identical
either way; GNU Radio would just be a second layer of plumbing on top.

--------------------------------------------------------------------------
SAMPLE FORMAT
--------------------------------------------------------------------------
The HackRF's ADC is 8-bit. `hackrf_transfer -r` writes raw interleaved
signed 8-bit I/Q pairs (I0,Q0,I1,Q1,...), i.e. SigMF datatype `ci8`
(complex, signed 8-bit integer, no byte-swap). No format conversion is
done here -- what lands on disk is exactly what the HackRF produced.
Downstream consumers (e.g. numpy) can load it as:
    raw = np.fromfile(path, dtype=np.int8)
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)

--------------------------------------------------------------------------
SigMF OUTPUT -- WHY HAND-ROLLED METADATA, NOT THE `sigmf` PYPI PACKAGE
--------------------------------------------------------------------------
`pip show sigmf` / `import sigmf` were checked -- not installed in this
deployment's Python environment, and pulling in the `sigmf` package (which
itself pulls in jsonschema, six, etc.) is unnecessary weight for what is a
genuinely small, well-documented JSON schema (SigMF core namespace v1.0.0:
https://github.com/sigmf/SigMF). This script hand-writes the
`.sigmf-meta` JSON sidecar directly using only the standard library --
no new dependency, same reasoning as choosing `hackrf_transfer` over GNU
Radio above: less deployment risk for an equivalent, verifiable result.
The written metadata is spec-compliant core SigMF (global core:datatype /
core:sample_rate / core:version, one capture segment with core:frequency
/ core:datetime), so it will load fine in the real `sigmf` library or any
other SigMF-aware tool (this matters because RFUAV/ORACLE-style reference
pipelines analyzed for this project consume SigMF).

--------------------------------------------------------------------------
KNOWN CONSTRAINT: HackRF sample-rate ceiling
--------------------------------------------------------------------------
The HackRF One's practical continuous sample rate ceiling is ~20 Msps
(datasheet quotes up to 20 MHz baseband bandwidth; USB3 not required since
it uses USB2 High-Speed, which is the real bottleneck for sustained
streaming). This is a hard constraint relative to the USRP B2xx/X3xx-class
captures used in some of the reference RFUAV/ORACLE datasets, which can
run at 40-56 Msps+. Anything requested above ~20 Msps here is very likely
to overrun the HackRF's USB pipe and drop samples (hackrf_transfer will
print "stream_underrun"/"stream_overrun" to stderr if it happens) --
this script does not hide that: `hackrf_transfer` stderr is passed through
live, and the wrapper's SigMF metadata records exactly what sample rate
you asked for, not a "did it actually keep up" guarantee. Watch the
console output during capture for underrun/overrun warnings.

Usage
-----
    python3 iq_capture.py --freq 2412e6 --rate 20e6 --duration 10 \\
        --out capture.sigmf-data

Produces `capture.sigmf-data` (raw ci8 IQ) and `capture.sigmf-meta` (JSON
sidecar) alongside it.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys


def _hackrf_transfer_path() -> str:
    """Locate the hackrf_transfer binary, failing loudly if it's missing."""
    path = shutil.which("hackrf_transfer")
    if path is None:
        raise FileNotFoundError(
            "hackrf_transfer not found on PATH. Install the 'hackrf' package "
            "(Ubuntu: `sudo apt install hackrf`, macOS: `brew install hackrf`). "
            "This tool has no fallback signal source -- it will not run without "
            "real HackRF hardware and its CLI tools present."
        )
    return path


def build_sigmf_meta(
    *,
    sample_rate_hz: float,
    center_freq_hz: float,
    hw_description: str,
    capture_datetime: str,
    author: str,
    description: str,
    num_samples: int,
) -> dict:
    """Construct a minimal, spec-compliant SigMF core-namespace metadata dict.

    Schema reference: SigMF core v1.0.0 (https://github.com/sigmf/SigMF).
    Only core:* fields are used -- no vendor extension namespaces -- to keep
    this maximally portable across downstream tooling (RFUAV/ORACLE-style
    pipelines, the real `sigmf` Python library, sigmf-cli, etc).
    """
    return {
        "global": {
            "core:datatype": "ci8",  # signed 8-bit I, signed 8-bit Q, interleaved
            "core:sample_rate": sample_rate_hz,
            "core:version": "1.0.0",
            "core:hw": hw_description,
            "core:author": author,
            "core:description": description,
            "core:num_channels": 1,
            "core:sha512": None,  # filled in by caller after the data file is written
        },
        "captures": [
            {
                "core:sample_start": 0,
                "core:frequency": center_freq_hz,
                "core:datetime": capture_datetime,
            }
        ],
        "annotations": [
            {
                "core:sample_start": 0,
                "core:sample_count": num_samples,
                "core:comment": (
                    "Real RF capture via HackRF One / hackrf_transfer. "
                    "No synthetic/simulated samples anywhere in this file."
                ),
            }
        ],
    }


def _sha512_of_file(path: str) -> str:
    import hashlib

    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def capture_iq(
    *,
    center_freq_hz: float,
    sample_rate_hz: float,
    duration_s: float,
    out_path: str,
    lna_gain: int = 16,
    vga_gain: int = 20,
    amp_enable: bool = False,
    serial: str | None = None,
    author: str = "cema-field-bridge",
    description: str = "Real HackRF IQ capture (RX only)",
) -> str:
    """Capture `duration_s` seconds of REAL IQ from a real HackRF.

    RECEIVE ONLY: builds and runs a `hackrf_transfer -r ...` subprocess.
    `-t` (transmit) is never passed, and no code path in this function (or
    anywhere else in this file) writes to the HackRF -- only from it.

    Returns the path to the written `.sigmf-meta` sidecar file.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be > 0")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be > 0")
    if sample_rate_hz > 20e6:
        print(
            f"WARNING: requested sample rate {sample_rate_hz / 1e6:.2f} Msps exceeds "
            "the HackRF One's practical ~20 Msps ceiling. Expect USB "
            "stream_underrun/overrun warnings and dropped samples.",
            file=sys.stderr,
        )

    hackrf_transfer = _hackrf_transfer_path()

    num_samples = int(round(sample_rate_hz * duration_s))
    if num_samples <= 0:
        raise ValueError("computed num_samples <= 0; check rate/duration")

    if not out_path.endswith(".sigmf-data"):
        print(
            f"NOTE: out_path '{out_path}' does not end in .sigmf-data "
            "(SigMF convention) -- proceeding anyway, metadata sidecar will "
            "be derived from this same basename.",
            file=sys.stderr,
        )

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    cmd = [
        hackrf_transfer,
        "-r", out_path,          # RECEIVE to file. Never "-t" (transmit).
        "-f", str(int(center_freq_hz)),
        "-s", str(int(sample_rate_hz)),
        "-n", str(num_samples),
        "-l", str(lna_gain),     # RF/LNA gain stage, 0-40dB in 8dB steps
        "-g", str(vga_gain),     # baseband VGA gain, 0-62dB in 2dB steps
    ]
    if amp_enable:
        cmd += ["-a", "1"]       # front-end amp -- optional extra ~14dB gain
    if serial:
        cmd += ["-d", serial]

    capture_datetime = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )

    print(f"[iq_capture] running: {' '.join(cmd)}")
    print(
        f"[iq_capture] center_freq={center_freq_hz/1e6:.3f}MHz "
        f"sample_rate={sample_rate_hz/1e6:.3f}Msps duration={duration_s}s "
        f"-> {num_samples} samples ({num_samples * 2} bytes, ci8)"
    )

    result = subprocess.run(cmd, capture_output=False, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"hackrf_transfer exited with code {result.returncode}. "
            "Check that a HackRF is connected (hackrf_info) and not already "
            "in use by another process (only one process can hold the "
            "device at a time)."
        )
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(
            f"hackrf_transfer reported success but '{out_path}' is missing or "
            "empty -- treating this as a capture failure rather than silently "
            "producing an empty/synthetic file."
        )

    meta = build_sigmf_meta(
        sample_rate_hz=sample_rate_hz,
        center_freq_hz=center_freq_hz,
        hw_description="HackRF One (hackrf_transfer raw capture)",
        capture_datetime=capture_datetime,
        author=author,
        description=description,
        num_samples=num_samples,
    )
    meta["global"]["core:sha512"] = _sha512_of_file(out_path)

    if out_path.endswith(".sigmf-data"):
        meta_path = out_path[: -len(".sigmf-data")] + ".sigmf-meta"
    else:
        meta_path = out_path + ".sigmf-meta"

    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
        f.write("\n")

    actual_bytes = os.path.getsize(out_path)
    expected_bytes = num_samples * 2
    if actual_bytes != expected_bytes:
        print(
            f"WARNING: captured file is {actual_bytes} bytes, expected "
            f"{expected_bytes} ({num_samples} ci8 samples). This usually means "
            "hackrf_transfer hit a USB underrun/overrun and the capture is "
            "shorter/longer than requested -- see stderr above for "
            "stream_underrun/overrun messages. The sigmf-meta annotation's "
            "core:sample_count still reflects the REQUESTED count; recompute "
            "actual sample count from file size if this warning fired.",
            file=sys.stderr,
        )

    print(f"[iq_capture] wrote {out_path} ({actual_bytes} bytes) + {meta_path}")
    return meta_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Capture REAL raw IQ from a HackRF One (RX only, never transmits) "
            "and save it as SigMF (data + meta sidecar)."
        )
    )
    parser.add_argument("--freq", type=float, required=True,
                         help="Center frequency in Hz, e.g. 2412e6 for 2.412GHz")
    parser.add_argument("--rate", type=float, required=True,
                         help="Sample rate in Hz, e.g. 20e6. HackRF practical ceiling ~20e6.")
    parser.add_argument("--duration", type=float, required=True,
                         help="Capture duration in seconds")
    parser.add_argument("--out", type=str, required=True,
                         help="Output data file path, conventionally ending in .sigmf-data")
    parser.add_argument("--lna-gain", type=int, default=16,
                         help="RF/LNA gain, 0-40dB in 8dB steps (default 16)")
    parser.add_argument("--vga-gain", type=int, default=20,
                         help="Baseband VGA gain, 0-62dB in 2dB steps (default 20)")
    parser.add_argument("--amp", action="store_true",
                         help="Enable the HackRF's front-end amp (~+14dB, use with caution near strong signals)")
    parser.add_argument("--serial", type=str, default=None,
                         help="HackRF serial number, if multiple devices are attached")
    parser.add_argument("--author", type=str, default="cema-field-bridge",
                         help="SigMF core:author field")
    parser.add_argument("--description", type=str,
                         default="Real HackRF IQ capture (RX only)",
                         help="SigMF core:description field")
    args = parser.parse_args()

    try:
        meta_path = capture_iq(
            center_freq_hz=args.freq,
            sample_rate_hz=args.rate,
            duration_s=args.duration,
            out_path=args.out,
            lna_gain=args.lna_gain,
            vga_gain=args.vga_gain,
            amp_enable=args.amp,
            serial=args.serial,
            author=args.author,
            description=args.description,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Done. SigMF metadata: {meta_path}")


if __name__ == "__main__":
    main()
