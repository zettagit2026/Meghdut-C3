#!/usr/bin/env python3
"""LIVE control-link classifier service: detection plane -> family label.

RX-ONLY. Pure heuristic over ALREADY-observed contacts. No hardware access, no
transmit path -- it reads the backend's own detection list and cannot starve
the detection sweep (it never touches the radio).

=============================================================================
WHAT THIS IS
=============================================================================
The detection plane (hackrf_rx.py + ml_classify_bridge.py) already publishes
live contacts with observable RF fields (center frequency, coarse occupied
bandwidth, protocol/model tags). This service polls that live list
(GET /api/detections), runs control_link_classifier.classify_control_link()
over each ACTIVE contact to attach an over-the-air control-link FAMILY
(DJI OcuSync / 2.4 GHz hobby-RC LRS / sub-GHz LRS / MAVLink-SiK / analog video),
and POSTs the result to /api/control-link/ingest, plus a per-cycle heartbeat.

HONEST: this is a band + signature HEURISTIC, not a protocol decode -- it says
"this emission looks like an X-class control link", never a decoded serial or a
specific transmitter. The confidence_type it emits reflects that (see
control_link_classifier.py). It is genuinely LIVE whenever the detection plane
is producing contacts (it classifies real observed emissions); it shows READY
when running but no active contact falls in a recognized control-link band.

=============================================================================
CONFIG (env vars)
=============================================================================
CEMA_API_URL / CEMA_EMAIL / CEMA_PASSWORD   backend base URL + operator login
CONTROL_LINK_POLL_INTERVAL_S   seconds between detection polls (default 5)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Dict, List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from control_link_classifier import classify_control_link

BRIDGE_NAME = "control_link_bridge"
PROTOCOL_ID = "control_link"


def classification_for_detection(det: Dict) -> Dict:
    """Build the /api/control-link/ingest body for one detection doc, from its
    observable RF fields only. `fhss_hop_consistent` is not stored on the
    detection document today, so it is passed as None -- the classifier then
    degrades sub-GHz calls to advisory_only honestly rather than overclaiming."""
    result = classify_control_link(
        center_freq_ghz=det.get("center_freq_ghz"),
        bandwidth_mhz=det.get("bandwidth_mhz"),
        fhss_hop_consistent=det.get("fhss_hop_consistent"),  # None unless a future field adds it
        protocol=det.get("protocol"),
        protocol_confirmed=bool(det.get("protocol_confirmed")),
        source=det.get("source"),
        model=det.get("model"),
    )
    return {
        "detection_id": det.get("id"),
        "center_freq_ghz": det.get("center_freq_ghz"),
        "link_type": result["link_type"],
        "link_family": result["link_family"],
        "confidence_type": result["confidence_type"],
        "rationale": result["rationale"],
        "evidence": result["evidence"],
    }


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
          email: str, password: str, timeout: float = 6) -> "requests.Response":
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


def poll_once(console_url: str, headers: dict, email: str, password: str) -> int:
    """One cycle: fetch ACTIVE detections, classify each, ingest each
    classification. Returns the number of NON-unknown classifications posted
    (0 is a normal, honest result). Always heartbeats."""
    classified = 0
    try:
        r = _get(console_url, "/api/detections", headers, email, password)
        detections: List[Dict] = r.json() if r.status_code == 200 else []
    except (requests.RequestException, ValueError) as e:
        print(f"[{BRIDGE_NAME}] detections poll failed: {e}", file=sys.stderr)
        detections = []

    for det in detections:
        if det.get("status") not in (None, "ACTIVE"):
            continue
        body = classification_for_detection(det)
        try:
            resp = _post(console_url, "/api/control-link/ingest", body, headers, email, password)
            if resp.status_code == 200 and body["link_type"].lower() != "unknown":
                classified += 1
        except requests.RequestException as e:
            print(f"[{BRIDGE_NAME}] control-link ingest failed: {e}", file=sys.stderr)

    try:
        _post(console_url, "/api/protocols/heartbeat",
              {"protocol": PROTOCOL_ID,
               "note": f"classified {len(detections)} contacts"},
              headers, email, password, timeout=5)
    except requests.RequestException:
        pass
    return classified


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--console-url", default=os.environ.get("CEMA_API_URL"))
    ap.add_argument("--email", default=os.environ.get("CEMA_EMAIL"))
    ap.add_argument("--password", default=os.environ.get("CEMA_PASSWORD"))
    ap.add_argument("--interval-s", type=float,
                     default=float(os.environ.get("CONTROL_LINK_POLL_INTERVAL_S", "5.0")))
    ap.add_argument("--iterations", type=int, default=0, help="0 = run forever")
    args = ap.parse_args()

    missing = [n for n, v in (("--console-url/CEMA_API_URL", args.console_url),
                               ("--email/CEMA_EMAIL", args.email),
                               ("--password/CEMA_PASSWORD", args.password)) if not v]
    if missing:
        ap.error(f"missing required value(s): {', '.join(missing)}")

    token = login(args.console_url, args.email, args.password)
    headers = {"Authorization": f"Bearer {token}"}
    print(f"[{BRIDGE_NAME}] logged in. Classifying live contacts every "
          f"{args.interval_s}s (heuristic, RX-only, no radio access).")

    i = 0
    while args.iterations == 0 or i < args.iterations:
        poll_once(args.console_url, headers, args.email, args.password)
        i += 1
        time.sleep(args.interval_s)


if __name__ == "__main__":
    main()
