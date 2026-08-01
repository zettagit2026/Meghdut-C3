#!/usr/bin/env python3
"""GamutRF -> CEMA console ingest adapter (RX only, no transmission).

WHAT THIS IS
=============================================================================
This is a SEPARATE, ADDITIONAL bridge -- a parallel build alongside the
existing hackrf_rx.py / ml_classify_bridge.py bridges, NOT a replacement for
either. It does not touch, import, or modify hackrf_rx.py, ml_classify_bridge.py,
gamutrf_infer.py, iq_capture.py, or hackrf_device_lock.py.

It subscribes to the MQTT topic GamutRF's own `gamutrf-scan` container
publishes real-time inference results to (`gamutrf/inference`, on the
`mqtt` broker defined in GamutRF's orchestrator.yml, port 1883) and maps
each real inference message GamutRF produces onto this project's detection
schema, then POSTs it to /api/detections/ingest using the same
env-var-only credential pattern as ml_classify_bridge.py.

WHY THE ENERGY GATE IS RE-DERIVED HERE INSTEAD OF TRUSTING GAMUTRF'S OWN
SQUELCH/CONFIDENCE THRESHOLD
=============================================================================
GamutRF's own orchestrator.yml config already applies its own squelch
(`--iq_inference_squelch_db=-50`) and a flat inference-confidence floor
(`--inference_min_confidence=0.25`) before it ever publishes to
`gamutrf/inference`. Those are real, working gates -- but they are GLOBAL
(one squelch dB value, one confidence floor) across every band GamutRF
scans, whereas this project has already done SITE-SPECIFIC, PER-BAND
empirical calibration of noise floor (see hackrf_rx.py, calibrated
2026-07-16 via hackrf_baseline_test.py):

    BAND_NOISE_FLOOR_DBM = {"SiK-915": -50.0, "DJI-2G4": -58.0, "DJI-5G8": -57.0}
    DETECT_THRESHOLD_DB = 15.0

and ml_classify_bridge.py's own hard empirical finding that this checkpoint
(resnet18_leesburg_split_0.02_1_current.pt) is a CLOSED-WORLD 3-class model
with no idle/noise/"none of the above" class -- it will emit a confident
answer even on pure noise-floor energy, so a flat confidence floor alone
(GamutRF's --inference_min_confidence=0.25) is not sufficient corroboration
that real signal energy is actually present at this site.

So: this adapter takes GamutRF's own reported rssi/power for the message's
band (mapped from GamutRF's freq_center to the nearest of our three known
bands) and re-applies OUR already-tuned, site-calibrated gate
(peak_dBm > BAND_NOISE_FLOOR_DBM[band] + DETECT_THRESHOLD_DB) as `ml_gated`,
for consistency with the empirically-validated approach used everywhere
else in this project, rather than trusting GamutRF's own gate values
verbatim. GamutRF's own inference is still fully reused (ml_label /
ml_confidence come directly from its message) -- only the pass/fail GATE
decision is re-derived from our own constants. If a message's reported
power/rssi field is missing (some GamutRF message variants omit it), this
adapter conservatively falls back to GamutRF's own `--iq_inference_squelch_db`
having already gated it in (i.e. treats it as gated=True, since the message
would not have been published at all otherwise) -- see `_gate()` below.

MESSAGE SCHEMA (best-effort; document precisely once real live messages are
captured against the actual deployment -- see field-bridge/README or
inline TODO below)
=============================================================================
GamutRF's `gamutrf-scan` (gamutrf/scan.py / gamutrf/sigfinder.py in the
GamutRF source tree) publishes JSON on `gamutrf/inference` with fields that,
per GamutRF's own source/docs, include at minimum: a center frequency
(Hz), a label/class name and confidence from the configured Torchserve
model, and (when available) a signal power/rssi estimate for the detection.
Exact key names can vary by GamutRF version/build. This adapter is
deliberately defensive about key names (see `_extract_fields()`) and logs
the raw message on any parse miss so field names can be corrected here
without guessing blind.

CREDENTIALS / CONFIG -- same convention as ml_classify_bridge.py
=============================================================================
No secrets hardcoded here. All via env vars (systemd EnvironmentFile=):
  CEMA_API_URL              backend base URL (e.g. http://localhost:8001)
  CEMA_EMAIL                 operator login email
  CEMA_PASSWORD               operator login password
  GAMUTRF_MQTT_HOST           MQTT broker host (default: localhost)
  GAMUTRF_MQTT_PORT           MQTT broker port (default: 1883, per orchestrator.yml)
  GAMUTRF_MQTT_TOPIC          inference topic to subscribe to (default: gamutrf/inference)
  GAMUTRF_INGEST_MIN_INTERVAL_S  minimum seconds between POSTs per band, to
                              avoid flooding /api/detections/ingest if GamutRF
                              publishes at high rate (default: 2.0)
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional, Tuple

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Reused verbatim from the live, site-calibrated energy-detection bridge --
# NOT duplicated/re-derived. See module docstring above for why we re-apply
# these instead of trusting GamutRF's own squelch/confidence floor alone.
from hackrf_rx import (
    BANDS_MHZ,
    BAND_NOISE_FLOOR_DBM,
    DETECT_THRESHOLD_DB,
    estimate_distance_m,
    login,
    _post_with_reauth,  # shared 401-retry-once helper -- see hackrf_rx.py for rationale
)

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("[gamutrf_backend_adapter] FATAL: paho-mqtt is required "
          "(pip install paho-mqtt) and was not found.", file=sys.stderr)
    raise

DEFAULT_MQTT_HOST = os.environ.get("GAMUTRF_MQTT_HOST", "localhost")
DEFAULT_MQTT_PORT = int(os.environ.get("GAMUTRF_MQTT_PORT", "1883"))
DEFAULT_MQTT_TOPIC = os.environ.get("GAMUTRF_MQTT_TOPIC", "gamutrf/inference")
MIN_INGEST_INTERVAL_S = float(os.environ.get("GAMUTRF_INGEST_MIN_INTERVAL_S", "2.0"))

# Map our known bands (name, low_mhz, high_mhz, human label) to a helper that
# finds which of them a given center frequency (Hz) falls in, so GamutRF's
# broadband scan output can be attributed to one of our three bands of
# interest. Anything outside all three known bands is ignored (out of scope
# for this project's detections).
_BAND_LOOKUP = [(name, low, high, label) for (name, low, high, label) in BANDS_MHZ]


def _band_for_freq(freq_hz: float) -> Optional[Tuple[str, int, int, str]]:
    freq_mhz = freq_hz / 1e6
    for name, low, high, label in _BAND_LOOKUP:
        if low <= freq_mhz <= high:
            return name, low, high, label
    return None


def _extract_fields(payload: dict) -> Optional[dict]:
    """Defensively pull the fields we need out of a GamutRF inference message.
    Key names are not 100% pinned down without live messages from this
    deployment's actual GamutRF build -- try the documented/likely candidates
    and fall back gracefully. Returns None if a center frequency can't be
    found at all (nothing we can attribute to a band without it)."""
    freq_hz = None
    for k in ("freq_center", "frequency", "freq", "center_freq"):
        if k in payload:
            try:
                freq_hz = float(payload[k])
                break
            except (TypeError, ValueError):
                pass
    if freq_hz is None:
        return None

    label = None
    for k in ("label", "class", "pred", "prediction", "top_class"):
        if k in payload:
            label = str(payload[k])
            break

    confidence = None
    for k in ("confidence", "score", "prob", "probability"):
        if k in payload:
            try:
                confidence = float(payload[k])
                break
            except (TypeError, ValueError):
                pass

    power_dbm = None
    for k in ("rssi", "power", "db", "signal_power", "peak_db"):
        if k in payload:
            try:
                power_dbm = float(payload[k])
                break
            except (TypeError, ValueError):
                pass

    return {
        "freq_hz": freq_hz,
        "label": label or "unknown",
        "confidence": confidence if confidence is not None else 0.0,
        "power_dbm": power_dbm,
    }


def _gate(band_name: str, power_dbm: Optional[float]) -> bool:
    """Re-derive ml_gated from our own site-calibrated constants (see module
    docstring). If GamutRF didn't report a power/rssi field, this message
    already passed GamutRF's own --iq_inference_squelch_db gate to be
    published at all, so we conservatively treat it as gated=True rather
    than silently dropping every message that lacks a power field."""
    if power_dbm is None:
        return True
    floor = BAND_NOISE_FLOOR_DBM.get(band_name)
    if floor is None:
        return True
    return power_dbm > floor + DETECT_THRESHOLD_DB


class Adapter:
    def __init__(self, console_url: str, email: str, password: str):
        self.console_url = console_url
        self.email = email
        self.password = password
        self.token = login(console_url, email, password)
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self._last_ingest_by_band: dict = {}

    def _should_throttle(self, band_name: str) -> bool:
        now = time.time()
        last = self._last_ingest_by_band.get(band_name, 0.0)
        if now - last < MIN_INGEST_INTERVAL_S:
            return True
        self._last_ingest_by_band[band_name] = now
        return False

    def handle_message(self, raw_payload: bytes) -> None:
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"[gamutrf_backend_adapter] unparseable message, skipping: {e}",
                  file=sys.stderr)
            return

        fields = _extract_fields(payload)
        if fields is None:
            print(f"[gamutrf_backend_adapter] message missing a recognizable "
                  f"center-frequency field, skipping. raw={payload}", file=sys.stderr)
            return

        band = _band_for_freq(fields["freq_hz"])
        if band is None:
            return  # outside our three bands of interest -- not in scope
        band_name, low_mhz, high_mhz, label_human = band

        if self._should_throttle(band_name):
            return

        ml_gated = _gate(band_name, fields["power_dbm"])
        center_mhz = fields["freq_hz"] / 1e6
        peak_dbm = fields["power_dbm"] if fields["power_dbm"] is not None \
            else BAND_NOISE_FLOOR_DBM.get(band_name, -60.0) + DETECT_THRESHOLD_DB
        floor = BAND_NOISE_FLOOR_DBM.get(band_name, -60.0)
        est_distance_m = estimate_distance_m(band_name, peak_dbm)

        det = {
            "model": "DJI Mini (candidate)" if "DJI" in band_name else "MAVLink craft (candidate)",
            "protocol": "OcuSync/Wi-Fi" if "DJI" in band_name else "SiK/MAVLink",
            "threat_level": "MEDIUM",
            "center_freq_ghz": center_mhz / 1000.0,
            "bandwidth_mhz": high_mhz - low_mhz,
            "rssi_dbm": peak_dbm,
            "snr_db": peak_dbm - floor,
            "bearing_deg": 0.0,
            "distance_m": round(est_distance_m, 1),
            "distance_estimated": True,
            "source": "SIK_RADIO" if band_name == "SiK-915" else "HACKRF",
            "ml_label": fields["label"],
            "ml_confidence": round(fields["confidence"], 4),
            "ml_gated": ml_gated,
        }

        if not ml_gated:
            print(f"[gamutrf_backend_adapter] [{label_human}] GamutRF inference "
                  f"'{fields['label']}' ({fields['confidence']:.4f}) NOT re-gated in "
                  f"(power {peak_dbm:.1f} dBm at/below site floor+threshold) -- "
                  f"posting with ml_gated=False for visibility, not suppressing.")

        try:
            _post_with_reauth(self.console_url, "/api/detections/ingest", det,
                               self.headers, self.email, self.password, timeout=10,
                               bridge_name="gamutrf_backend_adapter")
            print(f"[gamutrf_backend_adapter] [{label_human}] ingested: "
                  f"{fields['label']} conf={fields['confidence']:.4f} gated={ml_gated}")
        except requests.RequestException as e:
            print(f"[gamutrf_backend_adapter] [{label_human}] ingest failed: {e}",
                  file=sys.stderr)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--mqtt-host", default=DEFAULT_MQTT_HOST)
    ap.add_argument("--mqtt-port", type=int, default=DEFAULT_MQTT_PORT)
    ap.add_argument("--mqtt-topic", default=DEFAULT_MQTT_TOPIC)
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)} "
                  f"(pass as CLI arg or set the env var, e.g. via systemd EnvironmentFile=)")

    adapter = Adapter(args.console_url, args.email, args.password)
    print(f"[gamutrf_backend_adapter] logged in. Subscribing to "
          f"mqtt://{args.mqtt_host}:{args.mqtt_port} topic='{args.mqtt_topic}'. "
          f"RX ONLY -- no transmission, this process only consumes MQTT and POSTs "
          f"detections; it never talks to the HackRF directly.")

    client = mqtt.Client()

    def on_connect(c, userdata, flags, rc):
        if rc == 0:
            print(f"[gamutrf_backend_adapter] connected to MQTT broker, subscribing "
                  f"to '{args.mqtt_topic}'")
            c.subscribe(args.mqtt_topic)
        else:
            print(f"[gamutrf_backend_adapter] MQTT connect failed, rc={rc}", file=sys.stderr)

    def on_message(c, userdata, msg):
        adapter.handle_message(msg.payload)

    client.on_connect = on_connect
    client.on_message = on_message

    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_forever()


if __name__ == "__main__":
    main()
