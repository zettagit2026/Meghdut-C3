#!/usr/bin/env python3
"""LIVE FPV-OSD telemetry consumer: fpv_video_bridge frame -> OCR -> ingest.

RX-ONLY. Analog FPV video only. No transmit path.

=============================================================================
WHAT THIS IS
=============================================================================
This wires field-bridge/fpv_osd_ocr.py (the verified MAX7456 glyph-template
OSD reader) to the LIVE output of field-bridge/fpv_video_bridge.py:

  fpv_video_bridge.py captures a real HackRF IQ window, AM-envelope-demods it
  to a reconstructed analog video frame, and POSTs that PNG to the backend
  (/api/fpv/ingest -> served at /api/fpv/latest-frame.png).

  This service polls that latest reconstructed frame, runs
  fpv_osd_ocr.extract_osd_telemetry() over it, and POSTs the result
  (FpvOsdTelemetry.to_ingest_dict()) to /api/fpv/osd/ingest, plus a per-cycle
  /api/protocols/heartbeat.

This is the OVER-THE-AIR "MSP-class" telemetry path on an enemy airframe: we
read the craft's own battery / altitude / GPS / sats / RSSI off its analog
video downlink OSD -- NOT from a wire tap on its flight controller.

=============================================================================
HONEST STATUS -- READ THIS
=============================================================================
The OSD reader is real and self-tested against synthesized OSD frames. But the
UPSTREAM analog demod in fpv_video_bridge.py is itself UNTESTED against a live
analog FPV transmitter (see that script's docstring). So:

  * fpv_osd protocol shows LIVE only when a real reconstructed frame yields at
    least one legible OSD field.
  * A frame with no legible OSD (the expected result until a real analog FPV
    signal is captured AND the demod is validated) records a heartbeat only ->
    the board honestly shows READY, never fabricated telemetry.
  * DJI/HDZero/Walksnail DIGITAL video OSD is NOT decodable here -- analog only.

=============================================================================
CONFIG (env vars)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
FPV_OSD_POLL_INTERVAL_S    seconds between frame polls (default 5)
FPV_OSD_VIDEO_STANDARD     "NTSC" (default) or "PAL"
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
from typing import Dict, Optional

import numpy as np
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fpv_osd_ocr

BRIDGE_NAME = "fpv_osd_bridge"
PROTOCOL_ID = "fpv_osd"


def frame_from_png_bytes(png: bytes) -> np.ndarray:
    """Decode PNG bytes into a 2D uint8 grayscale frame -- the SAME format
    fpv_osd_ocr.extract_osd_telemetry() expects. Uses PIL (already a dependency
    of fpv_video_bridge.py, which SAVES the PNG via PIL)."""
    from PIL import Image
    img = Image.open(io.BytesIO(png)).convert("L")
    return np.asarray(img, dtype=np.uint8)


def telemetry_ingest_from_frame(frame: np.ndarray, *, video_standard: str = "NTSC") -> Dict:
    """Run the OSD reader over one demodulated frame and return the
    to_ingest_dict() payload for /api/fpv/osd/ingest. Fields that could not be
    read with sufficient confidence are already null (no fabrication)."""
    tele = fpv_osd_ocr.extract_osd_telemetry(frame, video_standard=video_standard)
    return tele.to_ingest_dict()


# ---------------------------------------------------------------------------
# Console auth (same convention as every other field-bridge script).
# ---------------------------------------------------------------------------
def login(console_url: str, email: str, password: str) -> str:
    r = requests.post(f"{console_url}/api/auth/login",
                      json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _reauth(console_url: str, headers: dict, email: str, password: str) -> None:
    try:
        headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
    except requests.RequestException as e:
        print(f"[{BRIDGE_NAME}] re-login failed ({e})", file=sys.stderr)


def _post(console_url: str, path: str, json_body: dict, headers: dict,
          email: str, password: str, timeout: float = 8) -> "requests.Response":
    headers.setdefault("X-Bridge-Name", BRIDGE_NAME)
    r = requests.post(f"{console_url}{path}", json=json_body, headers=headers, timeout=timeout)
    if r.status_code == 401:
        _reauth(console_url, headers, email, password)
        r = requests.post(f"{console_url}{path}", json=json_body, headers=headers, timeout=timeout)
    return r


def _get(console_url: str, path: str, headers: dict, email: str, password: str,
         timeout: float = 8) -> "requests.Response":
    r = requests.get(f"{console_url}{path}", headers=headers, timeout=timeout)
    if r.status_code == 401:
        _reauth(console_url, headers, email, password)
        r = requests.get(f"{console_url}{path}", headers=headers, timeout=timeout)
    return r


def poll_once(console_url: str, headers: dict, email: str, password: str,
              video_standard: str, last_frame_ts: Optional[str]) -> Optional[str]:
    """One cycle: fetch latest FPV frame metadata; if a NEW reconstructed frame
    exists, OCR it and ingest the telemetry. Always heartbeats. Returns the
    frame timestamp processed (to dedupe next cycle), or last_frame_ts unchanged."""
    processed_ts = last_frame_ts
    try:
        meta_r = _get(console_url, "/api/fpv/latest-frame", headers, email, password, timeout=6)
        meta = meta_r.json() if meta_r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        meta = {}

    frame_ts = meta.get("received_at")
    has_frame = bool(meta.get("available") and meta.get("has_frame"))
    is_new = has_frame and frame_ts is not None and frame_ts != last_frame_ts

    if is_new:
        try:
            png_r = _get(console_url, "/api/fpv/latest-frame.png", headers, email, password, timeout=8)
            if png_r.status_code == 200 and png_r.content:
                frame = frame_from_png_bytes(png_r.content)
                std = meta.get("video_standard") or video_standard
                body = telemetry_ingest_from_frame(frame, video_standard=std)
                r = _post(console_url, "/api/fpv/osd/ingest", body, headers, email, password)
                fields = {k: v for k, v in (body.get("telemetry") or {}).items() if v is not None}
                if r.status_code == 200:
                    print(f"[{BRIDGE_NAME}] OSD read on new frame ts={frame_ts}: "
                          f"{fields if fields else 'no legible OSD fields (READY)'}")
                    processed_ts = frame_ts
                else:
                    print(f"[{BRIDGE_NAME}] osd ingest HTTP {r.status_code}: {r.text[:200]}",
                          file=sys.stderr)
        except (requests.RequestException, OSError, ValueError) as e:
            print(f"[{BRIDGE_NAME}] frame OCR/ingest failed: {e}", file=sys.stderr)

    # Per-cycle heartbeat (READY when running even if no new frame / no OSD).
    try:
        _post(console_url, "/api/protocols/heartbeat",
              {"protocol": PROTOCOL_ID,
               "note": "awaiting new analog FPV frame" if not is_new else "processed frame"},
              headers, email, password, timeout=5)
    except requests.RequestException:
        pass
    return processed_ts


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("FPV_OSD_POLL_INTERVAL_S", "5.0")))
    ap.add_argument("--video-standard",
                     default=os.environ.get("FPV_OSD_VIDEO_STANDARD", "NTSC"))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[{BRIDGE_NAME}] logged in. Polling /api/fpv/latest-frame every "
          f"{args.interval_s}s, OCR standard={args.video_standard}. RX ONLY, analog only.")

    i = 0
    last_ts: Optional[str] = None
    while args.iterations == 0 or i < args.iterations:
        last_ts = poll_once(args.console_url, headers, args.email, args.password,
                            args.video_standard, last_ts)
        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
