#!/usr/bin/env python3
"""Real-IQ analog FPV video capture -> AM envelope demod -> snapshot bridge.

RECEIVE ONLY. No transmission happens anywhere in this script.

=============================================================================
SCOPE -- READ THIS BEFORE TRUSTING ANY OUTPUT
=============================================================================
This targets ANALOG FPV video: the AM-modulated NTSC/PAL composite video
signal that hobbyist FPV video transmitters (Boscam/Eachine/TBS "Raceband"/
"Fatshark" class hardware) put out on the 5.8GHz ISM band (and, less
commonly today, 2.4GHz). This is a well-documented, openly-modulated signal
type -- there is nothing proprietary or encrypted about it, unlike DJI's
OcuSync digital video link.

DJI DIGITAL VIDEO IS EXPLICITLY OUT OF SCOPE AND IS **NOT** DECODED HERE.
DJI OcuSync video is a proprietary, compressed, (on newer models) encrypted
digital stream -- this script cannot and does not attempt to decode DJI
video content. The only thing this script (or hackrf_rx.py's energy
sweep) can ever say about a DJI transmitter is "there is video-carrier-like
RF energy in this band" -- a presence/energy observation, nothing more.
DJI protocol/telemetry (DroneID) is handled by the separate, independent
droneid_decode_bridge.py in this same directory -- unrelated to this file.

=============================================================================
WHAT WAS ACTUALLY VERIFIED, AND WHAT WAS NOT (same honesty standard as
droneid_decode_bridge.py -- read this before trusting it)
=============================================================================
NOT VERIFIED against a live analog FPV transmitter. As of this writing, no
real analog FPV video transmitter was available in this session to test
against. Everything below the capture step (AM envelope detection, scanline
reconstruction) is UNTESTED against a live signal -- it has only been
exercised against synthetic/silent captures (i.e. whatever noise floor the
HackRF's ADC produces with no real video carrier present) to confirm the
code runs end-to-end without crashing, NOT that it correctly reconstructs a
real video frame. Treat any "image" this produces as a diagnostic energy/
envelope visualization until it has been validated against a real,
transmitting analog FPV VTX.

VERIFIED / real:
  - The IQ capture step (capture_iq() from iq_capture.py) is the same
    already-deployed, real hackrf_transfer wrapper (RX only) used by every
    other bridge in this directory -- genuinely captures real ADC samples
    off the antenna, no simulation.
  - The channel frequency table below is the standard, publicly documented
    5.8GHz analog FPV "Raceband"/Fatshark/Boscam channel plan used across
    the hobbyist FPV industry (not something invented for this task). No
    project-specific reference file with these frequencies was found in
    this repo or in ~/Desktop/zettagit -- see the tool-search note below.
  - AM envelope detection (|IQ|, i.e. sqrt(I^2+Q^2)) is textbook-correct AM
    demodulation for an amplitude-modulated carrier and needs no external
    library -- this part of the *math* is not in question. What IS unverified
    is whether it recovers anything resembling real NTSC/PAL video structure
    (line sync pulses, frame sync) from a live signal, because no live
    signal was available to check that against.

NOT VERIFIED / honest limitations:
  - No real NTSC/PAL line-sync or frame-sync detection is implemented. This
    does NOT lock onto horizontal sync pulses to correctly align scanlines
    (that requires a much more careful sync-pulse-detection pass than could
    be built and validated in this timeframe). The "image" is a NAIVE
    reshape of the AM envelope into fixed-width rows -- if the real line
    rate doesn't evenly match the assumed row width, the reconstructed image
    will show correct GRAYSCALE ENERGY CONTENT but will NOT be a properly
    synced picture (expect diagonal tearing/rolling, the classic symptom of
    unsynced analog video). This is disclosed here, not hidden.
  - This is a snapshot pipeline (capture N seconds, demod, produce ONE
    image), not continuous live video. A HackRF + this Python/numpy pipeline
    is not a real-time video decoder; producing smooth continuous FPV video
    would need a proper NTSC sync/deinterlace pipeline (e.g. a real GNU
    Radio flowgraph with an actual "restore video sync" block chain, or a
    dedicated device like an EasyCap/analog-to-digital capture card) -- that
    is explicitly a stretch goal, not delivered here. Re-running this script
    produces a new snapshot each time; it does not stream.
  - hackrf_rx.py's sweep/energy-detection is the ONLY continuously-running,
    validated-with-real-hardware piece of the existing pipeline. This script
    reuses that same real capture step but adds a NEW, unvalidated demod
    stage on top.

=============================================================================
TOOL-SEARCH DONE BEFORE WRITING THIS (per project skill-sourcing order)
=============================================================================
1. ~/Desktop/Zettawise/PMO Suraj/tool/ -- checked `urh` (Universal Radio
   Hacker, at tool/urh) for any bundled FPV/analog-video/NTSC demod
   plugin or signal profile: none found (grep for fpv/analog/ntsc/5.8 in
   its source turned up nothing relevant -- only unrelated colormap-name
   false positives). Checked the `gnuradio` symlink (-> zettagit/gnuradio,
   FISSURE's bundled copy): it ships gr-analog's generic `am_demod_cf`
   block (gr-analog/python/analog/am_demod.py) -- a general AM-envelope
   block, NOT an NTSC/video-specific demod chain, and no ready-made
   "FPV video receiver" flowgraph was found anywhere under it.
2. ~/Desktop/zettagit/ -- searched all directories for FPV/analog-video SDR
   receiver projects (GamutRF, RFUAV, DroneRF, RFClassification, FISSURE,
   etc. were all already catalogued this session for other purposes): none
   of them target analog FPV video reconstruction -- they are all RF
   fingerprinting/classification/protocol-decode projects, not video
   pipelines.
3. Conclusion: nothing off-the-shelf does "HackRF IQ -> reconstructed FPV
   video frame" in this environment. This script is the from-scratch
   fallback the task anticipated, built directly against numpy (no GNU
   Radio runtime dependency -- this deployment's Python environment does
   not have `gnuradio`/`osmosdr` importable, confirmed the same way
   iq_capture.py already documented for its own dependency choice).

=============================================================================
5.8GHz ANALOG FPV CHANNEL TABLE (standard "Raceband" plan)
=============================================================================
Publicly documented, industry-standard channel frequencies used by
Fatshark/Boscam/ImmersionRC/TBS-class analog VTX/VRX hardware. Not invented
for this task -- this is the same channel plan printed on the back of
virtually every analog FPV goggle/receiver sold in the last decade.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from typing import Optional

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from iq_capture import capture_iq  # noqa: E402  (real hackrf_transfer wrapper, RX only)

try:
    from PIL import Image
    _HAVE_PIL = True
except ImportError:
    _HAVE_PIL = False

try:
    import requests
    _HAVE_REQUESTS = True
except ImportError:
    _HAVE_REQUESTS = False


def login(console_url: str, email: str, password: str) -> str:
    """Log in against this project's own backend and return a fresh bearer
    token. Identical to the login() implemented in hackrf_rx.py /
    ml_classify_bridge.py / droneid_decode_bridge.py / mavlink_sniffer.py --
    duplicated here on purpose (no shared auth module exists in this
    codebase yet; every sibling bridge duplicates this same small function
    rather than import one another), so this stays consistent with the
    established convention instead of inventing a new pattern."""
    r = requests.post(f"{console_url}/api/auth/login", json={"email": email, "password": password}, timeout=10)
    r.raise_for_status()
    return r.json()["token"]


def _reauth_once(console_url: str, headers: dict, email: Optional[str], password: Optional[str]) -> bool:
    """Shared retry-on-401 recovery step, used by both the multipart POST
    (push_to_backend) and GET (run_poll_mode's status poll) call sites below.

    2026-07-24 FIX: this bridge previously logged in exactly once at startup
    and cached that token forever (see the old --email help text: "A single
    login at startup is sufficient ... none of the sibling long-running
    bridges re-login/refresh mid-run either"). That reasoning was wrong for
    any bridge meant to run indefinitely under systemd Restart=always: the
    backend's JWT TTL is 12h (create_access_token() in backend/server.py),
    so any run past 12h uptime silently and PERMANENTLY 401s on every push/
    poll call until manually restarted -- this is the exact bug that hit
    this bridge (and hackrf_rx.py/ml_classify_bridge.py) in production.

    Mutates `headers` in place with a fresh token and returns True if a
    retry should be attempted. Returns False (no retry) if no email/password
    were supplied to re-login with -- e.g. when the operator passed a static
    --token/CEMA_TOKEN override with no credentials behind it, in which case
    an expired token is a real, unrecoverable auth problem here, not
    something this bridge can fix on its own."""
    if not email or not password:
        print("[fpv_video_bridge] 401 from backend but no --email/--password "
              "(CEMA_EMAIL/CEMA_PASSWORD) available to re-authenticate -- "
              "a static --token/CEMA_TOKEN override cannot self-refresh. "
              "Fix credentials/token manually.", file=sys.stderr)
        return False
    print("[fpv_video_bridge] 401 from backend -- token expired, re-authenticating "
          f"as {email}", file=sys.stderr)
    try:
        headers["Authorization"] = f"Bearer {login(console_url, email, password)}"
        return True
    except requests.RequestException as e:
        print(f"[fpv_video_bridge] re-login failed: {e}", file=sys.stderr)
        return False


# --- Standard analog FPV channel tables (public, industry-standard) --------
# 5.8GHz "Raceband" (RB1-RB8), the most common analog-FPV plan today.
FPV_5800_RACEBAND_MHZ = {
    "R1": 5658, "R2": 5695, "R3": 5732, "R4": 5769,
    "R5": 5806, "R6": 5843, "R7": 5880, "R8": 5917,
}
# Boscam/Fatshark "Band A" (legacy, still common on older gear).
FPV_5800_BANDA_MHZ = {
    "A1": 5865, "A2": 5845, "A3": 5825, "A4": 5805,
    "A5": 5785, "A6": 5765, "A7": 5745, "A8": 5725,
}
# 2.4GHz analog FPV is rare today (mostly superseded by 5.8GHz + digital
# links) but still exists on some legacy/budget gear; channels sit inside
# the same 2.4GHz ISM band already swept by hackrf_rx.py.
FPV_2400_MHZ = {"F1": 2414, "F2": 2432, "F3": 2450, "F4": 2468}

ALL_FPV_CHANNELS_MHZ = {
    **{f"5800_{k}": v for k, v in FPV_5800_RACEBAND_MHZ.items()},
    **{f"BANDA_{k}": v for k, v in FPV_5800_BANDA_MHZ.items()},
    **{f"2400_{k}": v for k, v in FPV_2400_MHZ.items()},
}

# Analog video AM carriers are wideband-ish (NTSC ~4.2MHz video BW + audio
# subcarrier); this sample rate gives enough bandwidth to capture one
# channel's carrier with margin, while staying well under HackRF's ~20Msps
# practical ceiling (see iq_capture.py's own documented constraint).
DEFAULT_SAMPLE_RATE_HZ = 10_000_000.0
DEFAULT_DURATION_S = 2.0

# Naive scanline reconstruction row width. NOT derived from real NTSC line
# timing (that would require real sync-pulse detection, not implemented --
# see module docstring). This is a fixed, arbitrary width chosen only to
# produce a viewable 2D image out of the 1D envelope; it does not claim to
# be "one video line's worth of samples".
_ROW_WIDTH = 720

# 2026-07-29 FIX (task #137): default capture output dir used to be
# /tmp/fpv_capture -- ephemeral storage, wiped on every host reboot. Same
# silent-evidence-loss pattern already found and fixed for the ML classifier
# checkpoint (task #133, see ml_classify_bridge.py's CEMA_ML_CHECKPOINT).
# Any operator-triggered FPV capture (sigmf-data/meta + PNG) written under
# /tmp would vanish on reboot with no warning. Default now lives under the
# project directory instead, with an explicit env var override so operators
# can point it elsewhere (e.g. a larger/mounted evidence volume) without
# editing code -- same override convention as CEMA_ML_CHECKPOINT/
# FPV_FORWARD_URL/CEMA_API_URL.
DEFAULT_FPV_CAPTURE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fpv_captures"
)


def am_envelope_demod(iq: np.ndarray) -> np.ndarray:
    """Textbook AM envelope detection: |I + jQ|.

    This is standard, uncontroversial AM demodulation math -- no library
    dependency needed. What is NOT validated is whether the result of this
    on a live analog FPV signal actually looks like video (see module
    docstring "NOT VERIFIED" section).
    """
    return np.abs(iq)


def load_ci8_iq(sigmf_data_path: str) -> np.ndarray:
    """Load HackRF raw ci8 (signed 8-bit interleaved I/Q) samples, same
    convention iq_capture.py documents for its own output."""
    raw = np.fromfile(sigmf_data_path, dtype=np.int8)
    if raw.size % 2 != 0:
        raw = raw[:-1]
    i = raw[0::2].astype(np.float32)
    q = raw[1::2].astype(np.float32)
    return i + 1j * q


def envelope_to_image(envelope: np.ndarray, row_width: int = _ROW_WIDTH):
    """Naive reshape of the 1D AM envelope into a 2D grayscale image.

    HONEST LIMITATION (see module docstring): this does NOT perform real
    NTSC/PAL horizontal-sync detection, so rows are not guaranteed to align
    with actual video scanlines. Expect diagonal tearing/rolling on a real
    signal rather than a properly synced picture. This is a diagnostic
    envelope visualization, not a validated video decoder.
    """
    if not _HAVE_PIL:
        raise RuntimeError("Pillow (PIL) not installed -- cannot render image")

    n = envelope.size
    n_rows = max(1, n // row_width)
    trimmed = envelope[: n_rows * row_width]
    grid = trimmed.reshape(n_rows, row_width)

    # Normalize to 0-255 for a viewable grayscale image.
    lo, hi = np.percentile(grid, 1), np.percentile(grid, 99)
    if hi <= lo:
        hi = lo + 1.0
    norm = np.clip((grid - lo) / (hi - lo), 0.0, 1.0)
    img_u8 = (norm * 255).astype(np.uint8)
    return Image.fromarray(img_u8, mode="L")


def capture_and_demod(
    *,
    channel_name: str,
    center_freq_hz: float,
    sample_rate_hz: float = DEFAULT_SAMPLE_RATE_HZ,
    duration_s: float = DEFAULT_DURATION_S,
    out_dir: str = DEFAULT_FPV_CAPTURE_DIR,
    serial: Optional[str] = None,
) -> dict:
    """Real capture (hackrf_transfer, RX only) + AM envelope demod + PNG.

    Returns a dict describing what was produced, INCLUDING an explicit
    `validated_against_live_signal: False` flag so downstream consumers
    (backend endpoint, frontend panel) can render an honest caveat rather
    than silently implying this is confirmed-working video decode.
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = os.path.join(out_dir, f"fpv_{channel_name}_{ts}")
    sigmf_data = base + ".sigmf-data"

    print(f"[fpv_video_bridge] capturing real IQ: channel={channel_name} "
          f"freq={center_freq_hz/1e6:.3f}MHz rate={sample_rate_hz/1e6:.2f}Msps "
          f"duration={duration_s}s", file=sys.stderr)

    meta_path = capture_iq(
        center_freq_hz=center_freq_hz,
        sample_rate_hz=sample_rate_hz,
        duration_s=duration_s,
        out_path=sigmf_data,
        serial=serial,
        description=f"Real HackRF IQ capture for analog FPV video attempt, channel {channel_name}",
    )

    iq = load_ci8_iq(sigmf_data)
    n_samples = iq.size
    if n_samples == 0:
        raise RuntimeError("capture produced zero samples -- check HackRF connection")

    envelope = am_envelope_demod(iq)
    peak_power_est = float(20 * np.log10(np.max(envelope) + 1e-6))
    mean_power_est = float(20 * np.log10(np.mean(envelope) + 1e-6))

    png_path = base + ".png"
    image_produced = False
    if _HAVE_PIL:
        try:
            img = envelope_to_image(envelope)
            img.save(png_path)
            image_produced = True
        except Exception as e:
            print(f"[fpv_video_bridge] image reconstruction failed: {e}", file=sys.stderr)
    else:
        print("[fpv_video_bridge] Pillow not installed, skipping image reconstruction "
              "(energy stats only)", file=sys.stderr)

    result = {
        "channel": channel_name,
        "center_freq_hz": center_freq_hz,
        "sample_rate_hz": sample_rate_hz,
        "duration_s": duration_s,
        "n_samples": n_samples,
        "sigmf_meta_path": meta_path,
        "sigmf_data_path": sigmf_data,
        "png_path": png_path if image_produced else None,
        "peak_envelope_dbfs_like": round(peak_power_est, 2),
        "mean_envelope_dbfs_like": round(mean_power_est, 2),
        "captured_at": ts,
        # Explicit, load-bearing honesty flags -- consumers must not drop these.
        "source": "HACKRF_REAL_IQ",
        "demod_method": "am_envelope_naive_reshape",
        "validated_against_live_signal": False,
        "dji_digital_video_decoded": False,
        "note": (
            "Snapshot only, not continuous live video. AM envelope + naive "
            "scanline reshape -- no real NTSC/PAL sync-pulse detection. "
            "UNTESTED against a live analog FPV transmitter. If this "
            "channel actually carries DJI digital (OcuSync) video, only "
            "RF energy presence is observed here -- DJI video content is "
            "never decoded."
        ),
    }
    return result


def forward_result(result: dict, forward_url: str, png_path: Optional[str]) -> None:
    """Push a captured frame to an external destination via HTTP POST.

    Simple, deliberately minimal: multipart POST of the PNG (if produced)
    plus the JSON metadata as form fields. This is the "external forward"
    destination side of the dual-destination requirement -- chosen over a
    full RTSP/UDP video server because that is realistically out of scope
    for this timeline (see module docstring); HTTP POST of a snapshot frame
    is honestly achievable and testable in the time available.
    """
    if not _HAVE_REQUESTS:
        print("[fpv_video_bridge] `requests` not installed -- cannot forward", file=sys.stderr)
        return
    try:
        files = {}
        if png_path and os.path.isfile(png_path):
            files["frame"] = ("frame.png", open(png_path, "rb"), "image/png")
        resp = requests.post(forward_url, data={"metadata": json.dumps(result)},
                              files=files or None, timeout=10)
        print(f"[fpv_video_bridge] forwarded to {forward_url}: HTTP {resp.status_code}",
              file=sys.stderr)
    except Exception as e:
        print(f"[fpv_video_bridge] forward to {forward_url} failed: {e}", file=sys.stderr)


def push_to_backend(result: dict, console_url: str, headers: dict, png_path: Optional[str],
                     email: Optional[str] = None, password: Optional[str] = None) -> None:
    """POST the capture result (and PNG if produced) to this project's own
    backend so /api/fpv/latest-frame can serve it to the in-tool dashboard
    panel. Mirrors the ingest pattern droneid_decode_bridge.py already uses
    against /api/detections/ingest (same console_url/headers convention).

    Retries ONCE on a 401 (expired JWT) via _reauth_once -- see that
    function's docstring for why a single login-at-startup was not enough
    for a bridge meant to run forever."""
    if not _HAVE_REQUESTS:
        print("[fpv_video_bridge] `requests` not installed -- cannot push to backend", file=sys.stderr)
        return
    url = console_url.rstrip("/") + "/api/fpv/ingest"
    # Identifies this bridge in the backend's ingest_health tracking (task
    # #74) -- see GET /api/health's ingest_sources / preflight.sh.
    headers.setdefault("X-Bridge-Name", "fpv_video_bridge")

    def _open_files() -> dict:
        # Re-opened fresh on every attempt: a previously-opened file handle
        # would already be exhausted/at EOF if we tried to reuse it on retry.
        if png_path and os.path.isfile(png_path):
            return {"frame": ("frame.png", open(png_path, "rb"), "image/png")}
        return {}

    try:
        resp = requests.post(url, data={"metadata": json.dumps(result)},
                              files=_open_files() or None, headers=headers, timeout=10)
        if resp.status_code == 401 and _reauth_once(console_url, headers, email, password):
            resp = requests.post(url, data={"metadata": json.dumps(result)},
                                  files=_open_files() or None, headers=headers, timeout=10)
            if resp.status_code == 401:
                print("[fpv_video_bridge] still 401 after re-authenticating -- real auth "
                      "problem, not just an expired token.", file=sys.stderr)
        print(f"[fpv_video_bridge] pushed to backend {console_url}: HTTP {resp.status_code}",
              file=sys.stderr)
    except Exception as e:
        print(f"[fpv_video_bridge] push to backend failed: {e}", file=sys.stderr)


def run_poll_mode(
    *,
    channel_name: str,
    freq_hz: float,
    sample_rate_hz: float,
    duration_s: float,
    out_dir: str,
    serial: Optional[str],
    console_url: Optional[str],
    headers: dict,
    forward_url: Optional[str],
    poll_interval_s: float,
    email: Optional[str] = None,
    password: Optional[str] = None,
) -> None:
    """GUI-trigger poll loop. Polls the backend's
    GET /api/fpv/capture-request/status; when the operator clicks
    'CAPTURE NOW' in the dashboard (POST /api/fpv/capture-request), this
    performs exactly ONE real capture_and_demod() + push_to_backend()
    cycle -- the SAME functions the one-shot CLI path uses, not a
    reimplementation -- then resumes polling. Never invents a request:
    if the backend says pending=False, nothing happens.
    """
    if not _HAVE_REQUESTS:
        print("[fpv_video_bridge] `requests` not installed -- --poll mode requires it, "
              "cannot poll backend", file=sys.stderr)
        sys.exit(1)
    if not console_url:
        print("[fpv_video_bridge] --poll requires --console-url/CEMA_API_URL "
              "(nothing to poll)", file=sys.stderr)
        sys.exit(2)

    status_url = console_url.rstrip("/") + "/api/fpv/capture-request/status"
    print(f"[fpv_video_bridge] poll mode: watching {status_url} every "
          f"{poll_interval_s}s for operator-triggered capture requests "
          f"(GUI-only trigger, no SSH/CLI needed by the operator)", file=sys.stderr)

    while True:
        try:
            resp = requests.get(status_url, headers=headers, timeout=10)
            if resp.status_code == 401 and _reauth_once(console_url, headers, email, password):
                resp = requests.get(status_url, headers=headers, timeout=10)
                if resp.status_code == 401:
                    print("[fpv_video_bridge] still 401 polling capture-request status "
                          "after re-authenticating -- real auth problem, not just an "
                          "expired token.", file=sys.stderr)
            if resp.status_code == 200 and resp.json().get("pending"):
                requested = resp.json()
                req_channel = requested.get("channel") or channel_name
                req_freq_hz = (ALL_FPV_CHANNELS_MHZ[req_channel] * 1e6
                               if req_channel in ALL_FPV_CHANNELS_MHZ else freq_hz)
                print(f"[fpv_video_bridge] capture request received (channel="
                      f"{req_channel}) -- running one real capture+demod+ingest "
                      f"cycle", file=sys.stderr)
                try:
                    result = capture_and_demod(
                        channel_name=req_channel,
                        center_freq_hz=req_freq_hz,
                        sample_rate_hz=sample_rate_hz,
                        duration_s=duration_s,
                        out_dir=out_dir,
                        serial=serial,
                    )
                    print(json.dumps(result, indent=2))
                    if forward_url:
                        forward_result(result, forward_url, result.get("png_path"))
                    push_to_backend(result, console_url, headers, result.get("png_path"),
                                     email=email, password=password)
                except Exception as e:
                    print(f"[fpv_video_bridge] requested capture/demod cycle failed: {e}",
                          file=sys.stderr)
            elif resp.status_code != 200:
                print(f"[fpv_video_bridge] capture-request poll got HTTP "
                      f"{resp.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"[fpv_video_bridge] capture-request poll failed: {e}", file=sys.stderr)

        time.sleep(poll_interval_s)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--channel", default="5800_R1",
                    help=f"one of {sorted(ALL_FPV_CHANNELS_MHZ.keys())} or a raw freq in MHz")
    ap.add_argument("--rate", type=float, default=DEFAULT_SAMPLE_RATE_HZ)
    ap.add_argument("--duration", type=float, default=DEFAULT_DURATION_S)
    ap.add_argument("--out-dir", default=os.environ.get("FPV_CAPTURE_DIR", DEFAULT_FPV_CAPTURE_DIR),
                    help="directory for capture output (sigmf-data/meta + PNG). "
                         "Persistent by default (env: FPV_CAPTURE_DIR) -- NOT /tmp, "
                         "which is wiped on reboot and would silently lose operator-"
                         "triggered capture evidence (same fix as CEMA_ML_CHECKPOINT, "
                         "task #133/#137).")
    ap.add_argument("--serial", default=os.environ.get("HACKRF_SERIAL_PRIMARY"))
    ap.add_argument("--forward-url", default=os.environ.get("FPV_FORWARD_URL"),
                    help="external HTTP POST destination for captured frames "
                         "(env: FPV_FORWARD_URL)")
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"),
                    help="this project's own backend, to populate /api/fpv/latest-frame "
                         "(env: CEMA_API_URL)")
    ap.add_argument("--token", default=os.environ.get("CEMA_TOKEN"),
                    help="bearer token for --console-url, if auth required. Optional "
                         "fallback/override for manual debug use -- if set, used directly "
                         "and login via --email/--password is skipped. Normally leave this "
                         "unset and let --email/--password (or CEMA_EMAIL/CEMA_PASSWORD) "
                         "obtain a fresh token instead, matching the other bridges (a "
                         "static pre-obtained token would eventually expire with no way to "
                         "refresh it).")
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"),
                    help="operator login email (env: CEMA_EMAIL) -- used to obtain a fresh "
                         "bearer token via POST /api/auth/login, same as hackrf_rx.py/"
                         "ml_classify_bridge.py/droneid_decode_bridge.py/mavlink_sniffer.py. "
                         "Ignored if --token/CEMA_TOKEN is explicitly supplied.")
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"),
                    help="operator login password (env: CEMA_PASSWORD). Ignored if "
                         "--token/CEMA_TOKEN is explicitly supplied.")
    ap.add_argument("--loop", action="store_true",
                    help="repeat capture indefinitely (still one snapshot per iteration, "
                         "NOT continuous streaming -- see module docstring)")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="seconds between captures when --loop is set")
    ap.add_argument("--poll", action="store_true",
                    help="GUI-trigger mode: instead of capturing immediately/on a fixed "
                         "--interval, poll the backend's "
                         "GET /api/fpv/capture-request/status every --poll-interval "
                         "seconds; when the operator has clicked 'CAPTURE NOW' in the "
                         "dashboard (POST /api/fpv/capture-request), perform ONE real "
                         "capture_and_demod()+ingest cycle, then resume polling. This is "
                         "the whole point of this mode: the operator never needs SSH/CLI "
                         "access to trigger a capture -- requires --console-url (or "
                         "CEMA_API_URL) to be set.")
    ap.add_argument("--poll-interval", type=float, default=3.0,
                    help="seconds between capture-request polls when --poll is set")
    args = ap.parse_args()

    if args.channel in ALL_FPV_CHANNELS_MHZ:
        freq_hz = ALL_FPV_CHANNELS_MHZ[args.channel] * 1e6
    else:
        try:
            freq_hz = float(args.channel) * 1e6
        except ValueError:
            print(f"Unknown channel '{args.channel}'. Known: "
                  f"{sorted(ALL_FPV_CHANNELS_MHZ.keys())}", file=sys.stderr)
            sys.exit(2)

    # Auth: an explicitly-supplied --token/CEMA_TOKEN wins (manual debug override,
    # used as-is, no login performed). Otherwise, if CEMA_EMAIL/CEMA_PASSWORD are
    # set, log in now and obtain a fresh token ourselves -- same login() flow and
    # same env var names as hackrf_rx.py/ml_classify_bridge.py/
    # droneid_decode_bridge.py/mavlink_sniffer.py, so the existing field-bridge/.env
    # (which already has these set for the other bridges) works here unchanged.
    #
    # 2026-07-24 CORRECTION: this comment used to claim "a single login at
    # startup is sufficient ... none of the sibling long-running bridges
    # re-login/refresh mid-run either." That was wrong -- the backend's JWT
    # is only valid for 12h (create_access_token() in backend/server.py),
    # and this bridge (like its siblings) runs forever under systemd
    # Restart=always. A token cached once at startup WILL expire mid-run,
    # and every push_to_backend()/run_poll_mode() status-poll call after
    # that would silently and PERMANENTLY 401 until someone noticed and
    # restarted the service by hand. push_to_backend() and run_poll_mode()
    # now retry ONCE via _reauth_once() on a 401, re-logging in with
    # args.email/args.password (if available) and updating `headers` in
    # place -- see _reauth_once()'s docstring.
    token = args.token
    if not token and args.email and args.password:
        if not args.console_url:
            print("[fpv_video_bridge] --email/--password given but no "
                  "--console-url/CEMA_API_URL to log in against", file=sys.stderr)
            sys.exit(2)
        if not _HAVE_REQUESTS:
            print("[fpv_video_bridge] `requests` not installed -- cannot log in",
                  file=sys.stderr)
            sys.exit(1)
        token = login(args.console_url, args.email, args.password)
        print(f"[fpv_video_bridge] logged in as {args.email}", file=sys.stderr)
    if not token:
        print("[fpv_video_bridge] no auth available -- supply --token/CEMA_TOKEN "
              "or --email+--password/CEMA_EMAIL+CEMA_PASSWORD. Refusing to proceed "
              "unauthenticated (matches hackrf_rx.py/ml_classify_bridge.py/"
              "droneid_decode_bridge.py/mavlink_sniffer.py's hard-fail convention).",
              file=sys.stderr)
        sys.exit(2)
    headers = {"Authorization": f"Bearer {token}"}

    if args.poll:
        run_poll_mode(
            channel_name=args.channel,
            freq_hz=freq_hz,
            sample_rate_hz=args.rate,
            duration_s=args.duration,
            out_dir=args.out_dir,
            serial=args.serial,
            console_url=args.console_url,
            headers=headers,
            forward_url=args.forward_url,
            poll_interval_s=args.poll_interval,
            email=args.email,
            password=args.password,
        )
        return

    while True:
        try:
            result = capture_and_demod(
                channel_name=args.channel,
                center_freq_hz=freq_hz,
                sample_rate_hz=args.rate,
                duration_s=args.duration,
                out_dir=args.out_dir,
                serial=args.serial,
            )
            print(json.dumps(result, indent=2))

            if args.forward_url:
                forward_result(result, args.forward_url, result.get("png_path"))
            if args.console_url:
                push_to_backend(result, args.console_url, headers, result.get("png_path"),
                                 email=args.email, password=args.password)
        except Exception as e:
            print(f"[fpv_video_bridge] capture/demod cycle failed: {e}", file=sys.stderr)

        if not args.loop:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
