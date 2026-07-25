#!/usr/bin/env python3
"""Passive bistatic radar CAF/Doppler bridge for CEMA cUAS (task #43, C10).

RECEIVE ONLY. No transmission happens anywhere in this script -- passive
radar exploits an illuminator of opportunity (broadcast TV/FM/cellular)
that is already transmitting; this script only listens on two channels
and computes a cross-ambiguity function against them.

Wires field-bridge/passive_radar/'s DualChannelSource -> alignment ->
dsi_suppression -> caf -> detector -> geometry pipeline and posts
detections to /api/detections/ingest, analogous to hackrf_rx.py's role
for the RSSI-heuristic pipeline. See field-bridge/PASSIVE_RADAR_ARCHITECTURE.md
for the full design spec this implements.

SOFTWARE-ONLY / UNWIRED TO LIVE HARDWARE: `--source synthetic` and
`--source recorded-file` are fully functional today with zero hardware.
`--source rtlsdr-dual` exists on the CLI surface (so the eventual hardware
integration point is visible today) but raises NotImplementedError --
DualRTLSDRSource's real implementation is task #57's scope, not this
script's. No RTL-SDR hardware exists on this deployment yet.

Run examples:
    python3 passive_radar_bridge.py --source synthetic --illuminator fm \\
        --console-url http://localhost:8000 --once
    python3 passive_radar_bridge.py --source recorded-file \\
        --ref-file ch1.sigmf-data --surv-file ch2.sigmf-data \\
        --illuminator dvb-t2 --console-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import numpy as np
import requests

# Reused, not duplicated -- same convention ml_classify_bridge.py/
# droneid_decode_bridge.py already follow for auth+ingest helpers.
from hackrf_rx import login, _post_with_reauth

from passive_radar.channel_source import (
    DualChannelSource,
    SyntheticDualChannelSource,
    RecordedFileDualChannelSource,
    DualRTLSDRSource,
)
from passive_radar.alignment import align_channels
from passive_radar.dsi_suppression import suppress_dsi
from passive_radar.caf import compute_caf
from passive_radar.detector import topk_peaks
from passive_radar.geometry import estimate_bistatic_target, ReceiverGeometry
from passive_radar.illuminator_profile import (
    IlluminatorProfile,
    DVB_T2_PLACEHOLDER,
    FM_BROADCAST_PLACEHOLDER,
)

BRIDGE_NAME = "passive_radar_bridge"

ILLUMINATOR_CHOICES = {
    "dvb-t2": DVB_T2_PLACEHOLDER,
    "fm": FM_BROADCAST_PLACEHOLDER,
}

DEFAULT_BLOCK_SAMPLES = 1_048_576  # ~0.5s at 2.048 MHz, per goship.m's N=fs convention
DEFAULT_MAX_LAG = 512              # goship.m's dN
DEFAULT_DOPPLER_HZ = np.arange(-200, 201, 4)  # goship.m's freq=[-200:4:200]


def build_source(args) -> DualChannelSource:
    if args.source == "synthetic":
        return SyntheticDualChannelSource(sample_rate_hz=args.sample_rate_hz)
    if args.source == "recorded-file":
        if not args.ref_file or not args.surv_file:
            raise SystemExit("--source recorded-file requires --ref-file and --surv-file")
        return RecordedFileDualChannelSource(
            sample_rate_hz=args.sample_rate_hz,
            ref_path=args.ref_file,
            surv_path=args.surv_file,
            dtype=args.dtype,
            skip_samples=args.skip_samples,
        )
    if args.source == "rtlsdr-dual":
        # Documented-but-NotImplementedError, per
        # PASSIVE_RADAR_ARCHITECTURE.md §5 step 8/10 -- visible on the CLI
        # surface today, not silently omitted; real implementation is
        # task #57's scope.
        return DualRTLSDRSource()
    raise SystemExit(f"unknown --source {args.source!r}")


def process_block(
    ref: np.ndarray,
    surv: np.ndarray,
    sample_rate_hz: float,
    max_lag: int = DEFAULT_MAX_LAG,
    doppler_hz=DEFAULT_DOPPLER_HZ,
    dsi_enabled: bool = True,
    min_snr_db: float = 10.0,
):
    """Runs alignment -> (optional) DSI suppression -> CAF -> detector for
    a single block. Returns the list of passive_radar.detector.Detection.
    """
    ref_a, surv_a, _delay = align_channels(ref, surv)
    if dsi_enabled and len(ref_a) > 32:
        surv_a = suppress_dsi(ref_a, surv_a)
    caf_result = compute_caf(ref_a, surv_a, sample_rate_hz, doppler_hz, max_lag)
    return topk_peaks(caf_result, k=5, min_snr_db=min_snr_db)


def detection_to_ingest_body(
    detection,
    illuminator: IlluminatorProfile,
    receiver: ReceiverGeometry,
) -> dict:
    """Maps a passive_radar Detection to the DetectionIngestBody field
    conventions documented in PASSIVE_RADAR_ARCHITECTURE.md §4 (cross-
    checked against backend/server.py's DetectionIngestBody, which accepts
    `source` as a free-form str with no enum constraint at the Pydantic
    level -- confirmed, no backend schema change is required for this)."""
    estimate = estimate_bistatic_target(
        bistatic_range_m=detection.range_m,
        doppler_hz=detection.doppler_hz,
        illuminator_center_freq_hz=illuminator.center_freq_hz,
        receiver=receiver,
    )
    return {
        "source": "PASSIVE_RADAR",
        "distance_m": abs(estimate.bistatic_range_m),
        # Genuine time-of-flight-derived range from the CAF peak lag, NOT
        # an RSSI path-loss guess -- distance_estimated=False per the
        # existing field's own doc comment and PASSIVE_RADAR_ARCHITECTURE.md §4.
        "distance_estimated": False,
        "bearing_deg": estimate.bearing_deg,
        "speed_ms": abs(estimate.radial_speed_mps),
        "confidence_type": "bistatic_radar_detection",
        "protocol_confirmed": False,
        "rssi_dbm": detection.snr_db,  # repurposed as CAF peak SNR, per §4
        "snr_db": detection.snr_db,
        "model": "passive-bistatic-radar-caf-v1",
        "band": illuminator.name,
    }


def run_once(args, headers: dict) -> int:
    illuminator = ILLUMINATOR_CHOICES[args.illuminator]
    receiver = ReceiverGeometry(
        lat=args.receiver_lat,
        lon=args.receiver_lon,
        alt_m=args.receiver_alt_m,
        surveillance_antenna_boresight_deg=args.antenna_boresight_deg,
    )
    posted = 0
    try:
        source = build_source(args)
        with source:
            block_num = 0
            while True:
                try:
                    ref, surv = source.read_block(args.block_samples)
                except StopIteration:
                    print("[passive_radar] source exhausted", file=sys.stderr)
                    break
                block_num += 1
                detections = process_block(
                    ref, surv, source.sample_rate_hz,
                    max_lag=args.max_lag, dsi_enabled=not args.no_dsi,
                    min_snr_db=args.min_snr_db,
                )
                for det in detections:
                    body = detection_to_ingest_body(det, illuminator, receiver)
                    print(f"[passive_radar] block {block_num}: range={det.range_m:.1f}m "
                          f"doppler={det.doppler_hz:.1f}Hz snr={det.snr_db:.1f}dB", file=sys.stderr)
                    if args.console_url:
                        r = _post_with_reauth(
                            args.console_url, "/api/detections/ingest", body, headers,
                            args.email, args.password, bridge_name=BRIDGE_NAME,
                        )
                        if r.status_code >= 300:
                            print(f"[passive_radar] ingest failed: {r.status_code} {r.text}",
                                  file=sys.stderr)
                        else:
                            posted += 1
                if args.once:
                    break
    except NotImplementedError as e:
        print(f"[passive_radar] {e}", file=sys.stderr)
        return 1
    return 0 if posted or args.once else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["synthetic", "recorded-file", "rtlsdr-dual"], default="synthetic")
    ap.add_argument("--ref-file", default=None, help="reference-channel I/Q file (recorded-file source)")
    ap.add_argument("--surv-file", default=None, help="surveillance-channel I/Q file (recorded-file source)")
    ap.add_argument("--dtype", choices=["int8", "int16", "int32", "float"], default="int8",
                     help="recorded-file sample dtype, matches goship.m's `datatype`")
    ap.add_argument("--skip-samples", type=int, default=0,
                     help="samples to skip at file start, matches goship.m's fseek(f,1008*N)")
    ap.add_argument("--sample-rate-hz", type=float, default=2.048e6)
    ap.add_argument("--block-samples", type=int, default=DEFAULT_BLOCK_SAMPLES)
    ap.add_argument("--max-lag", type=int, default=DEFAULT_MAX_LAG)
    ap.add_argument("--min-snr-db", type=float, default=10.0)
    ap.add_argument("--no-dsi", action="store_true", help="disable DSI suppression")
    ap.add_argument("--illuminator", choices=list(ILLUMINATOR_CHOICES), default="fm")
    ap.add_argument("--receiver-lat", type=float, default=0.0)
    ap.add_argument("--receiver-lon", type=float, default=0.0)
    ap.add_argument("--receiver-alt-m", type=float, default=0.0)
    ap.add_argument("--antenna-boresight-deg", type=float, default=0.0)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--once", action="store_true", help="process a single block and exit")
    args = ap.parse_args()

    headers = {}
    if args.console_url:
        missing = [n for n, v in (("--email/CEMA_EMAIL", args.email),
                                   ("--password/CEMA_PASSWORD", args.password)) if not v]
        if missing:
            raise SystemExit(f"--console-url given but missing: {', '.join(missing)}")
        headers["Authorization"] = f"Bearer {login(args.console_url, args.email, args.password)}"

    sys.exit(run_once(args, headers))


if __name__ == "__main__":
    main()
