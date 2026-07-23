#!/usr/bin/env python3
"""Convert raw IQ capture files into the exact spectrogram image format that
field-bridge/gamutrf_infer.py's ResNet18 classifier consumes.

This is dataset-staging tooling for the Drone RF Knowledge Base (backlog
B2) -- it does NOT fine-tune or run inference against the classifier. It
exists so that real raw-IQ drone captures (e.g. DroneSecurity's DJI
OcuSync 2.0 samples, or RFUAV's raw IQ if pulled from Hugging Face later)
can be turned into 256x256 jet-colormap spectrogram PNGs in the same
representation the existing checkpoint was trained on, ready for a future
fine-tuning pass.

Reuses gamutrf_infer.make_spectrogram_image() verbatim (same import, not a
reimplementation) so there is exactly one place that defines "how a
spectrogram is made" across inference and KB staging.

Usage:
    python convert_iq_to_spectrogram.py \
        --input /path/to/mini2_sm \
        --sample-rate 50e6 \
        --dtype complex64_interleaved_f32 \
        --label mini2 \
        --source dronesecurity \
        --nfft 1024 \
        --window-secs 0.1 \
        --outdir ./staged

Real caveats (see README.md in this directory):
  - Input sample rate need not match HackRF's ~20Msps cap -- spectrogram
    conversion is a fixed-resolution abstraction over whatever rate the
    file was actually captured at. This script does NOT resample; it
    records the true capture rate in the output manifest so provenance is
    never lost or implied to be HackRF-native.
  - This script does not label ground truth beyond the filename-derived
    tag the caller passes in. It does not invent classes.
"""
import argparse
import csv
import json
import os
import sys

import numpy as np

# Import the real, already-in-production spectrogram function instead of
# reimplementing it, so KB-staged images are guaranteed to match what the
# ResNet18 classifier actually expects.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from gamutrf_infer import make_spectrogram_image  # noqa: E402


DTYPE_LOADERS = {
    # DroneSecurity's droneid_receiver_offline.py loads raw samples this way:
    #   np.memmap(path, mode='r', dtype="<f").astype(np.float32).view(np.complex64)
    "complex64_interleaved_f32": lambda path: (
        np.fromfile(path, dtype="<f4").astype(np.float32).view(np.complex64)
    ),
    # Plain complex64 IQ (e.g. gr_complex / SigMF cf32 convention)
    "complex64": lambda path: np.fromfile(path, dtype=np.complex64),
}


def load_iq(path: str, dtype_key: str) -> np.ndarray:
    if dtype_key not in DTYPE_LOADERS:
        raise ValueError(f"Unknown --dtype {dtype_key!r}; choices: {list(DTYPE_LOADERS)}")
    return DTYPE_LOADERS[dtype_key](path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="Path to raw IQ capture file")
    ap.add_argument("--sample-rate", type=float, required=True,
                     help="TRUE capture sample rate in Hz (not resampled -- recorded as-is)")
    ap.add_argument("--dtype", default="complex64_interleaved_f32",
                     choices=list(DTYPE_LOADERS),
                     help="Raw IQ on-disk format")
    ap.add_argument("--label", required=True, help="Class/drone-model label for this file")
    ap.add_argument("--source", required=True,
                     help="Dataset provenance tag, e.g. dronesecurity, rfuav")
    ap.add_argument("--license", required=True,
                     help="Real source license, e.g. AGPLv3, Apache-2.0 -- recorded per-row so "
                          "provenance survives independent of this README if files move")
    ap.add_argument("--nfft", type=int, default=1024, help="STFT size (must match training config)")
    ap.add_argument("--window-secs", type=float, default=0.1,
                     help="Duration per spectrogram window, in seconds")
    ap.add_argument("--outdir", default=os.path.join(os.path.dirname(__file__), "staged"),
                     help="Output directory for staged PNGs + manifest")
    args = ap.parse_args()

    try:
        import matplotlib
        matplotlib.use("Agg")
        from torchvision.transforms.functional import to_pil_image
    except ImportError as e:
        print(f"Missing dependency: {e}. Install field-bridge/requirements.txt first.",
              file=sys.stderr)
        sys.exit(1)

    samples = load_iq(args.input, args.dtype)
    if len(samples) == 0:
        print(f"ERROR: {args.input} produced 0 samples with dtype {args.dtype}", file=sys.stderr)
        sys.exit(1)

    window_n = int(args.sample_rate * args.window_secs)
    if window_n <= 0 or window_n > len(samples):
        window_n = len(samples)

    out_dir = os.path.join(args.outdir, args.source, args.label)
    os.makedirs(out_dir, exist_ok=True)

    n_windows = max(1, len(samples) // window_n)
    manifest_rows = []
    for i in range(n_windows):
        window = samples[i * window_n:(i + 1) * window_n]
        if len(window) < args.nfft:
            break
        tensor = make_spectrogram_image(window, args.sample_rate, args.nfft)
        img = to_pil_image(tensor)
        fname = f"{os.path.basename(args.input)}_win{i:03d}.png"
        img.save(os.path.join(out_dir, fname))
        manifest_rows.append({
            "file": fname,
            "source": args.source,
            "license": args.license,
            "label": args.label,
            "orig_capture_file": os.path.abspath(args.input),
            "true_sample_rate_hz": args.sample_rate,
            "nfft": args.nfft,
            "window_secs": args.window_secs,
            "window_index": i,
        })

    manifest_path = os.path.join(out_dir, "manifest.csv")
    write_header = not os.path.exists(manifest_path)
    with open(manifest_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(manifest_rows[0].keys()))
        if write_header:
            writer.writeheader()
        writer.writerows(manifest_rows)

    print(json.dumps({
        "input": args.input,
        "windows_written": len(manifest_rows),
        "out_dir": out_dir,
        "manifest": manifest_path,
    }, indent=2))


if __name__ == "__main__":
    main()
