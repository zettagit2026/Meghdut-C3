"""Real-detection state-machine helpers for the CEMA/cUAS console.

This module intentionally contains NO data generation of any kind — only
stage-advancement helpers that operate on detections already created from
real ingested RF/MAVLink data (see /detections/ingest in server.py). The
former `simulator.py` (deleted) also contained `new_detection()` /
`generate_waterfall()` / `parse_iq_file_stub()`, which fabricated random
detections, spectrum rows, and IQ-file metadata respectively. Per the
project's standing "no synthetic data, anywhere, ever" rule, those were
removed rather than kept behind a flag.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Dict

CEMA_STAGES = ["CAPTURE", "ANALYZE", "SEGREGATE", "DEMODULATE",
               "DECODE", "DECRYPT", "EXPLOIT"]

KILL_CHAIN = ["DETECT", "TRACK", "IDENTIFY", "DECIDE", "DEFEAT"]


def advance_cema(det: Dict) -> Dict:
    idx = det.get("cema_stage_index", 0)
    if idx < len(CEMA_STAGES) - 1:
        det["cema_stage_index"] = idx + 1
        det["cema_stage"] = CEMA_STAGES[idx + 1]
    det["last_seen"] = datetime.now(timezone.utc).isoformat()
    return det


def advance_kill_chain(det: Dict) -> Dict:
    idx = det.get("kill_chain_index", 0)
    if idx < len(KILL_CHAIN) - 1:
        det["kill_chain_index"] = idx + 1
        det["kill_chain_stage"] = KILL_CHAIN[idx + 1]
    det["last_seen"] = datetime.now(timezone.utc).isoformat()
    return det
