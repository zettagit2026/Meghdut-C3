#!/usr/bin/env python3
"""Unit tests for fpv_osd_bridge.py -- wiring the OSD reader to the live FPV
video-bridge frame output. No hardware, no network.

Round-trips a synthesized OSD frame through the SAME PNG encode fpv_video_bridge
uses, decodes it back, and asserts the /api/fpv/osd/ingest payload -- including
the honest empty case (a blank frame yields zero fabricated telemetry fields).
"""
import io

import numpy as np
from PIL import Image

import fpv_osd_ocr as m
import fpv_osd_bridge as fb


NTSC = m.OSDGridSpec(cols=30, rows=13)
FULL_ROWS = [
    "HAWK01",
    m.SYM_BATT + "16.2V 12.5A",
    m.SYM_SAT + "14 " + m.SYM_ALT + "150M",
    m.SYM_LAT + "28.613920",
    m.SYM_LON + "77.209000",
    m.SYM_RSSI + "92% 03:24",
]


def _png_bytes(frame):
    buf = io.BytesIO()
    Image.fromarray(frame).save(buf, format="PNG")
    return buf.getvalue()


def test_png_roundtrip_preserves_frame():
    frame = m.render_osd_frame(FULL_ROWS, spec=NTSC)
    back = fb.frame_from_png_bytes(_png_bytes(frame))
    assert back.shape == frame.shape
    assert np.array_equal(back, frame)


def test_full_frame_yields_ingest_dict_with_telemetry():
    frame = m.render_osd_frame(FULL_ROWS, spec=NTSC)
    body = fb.telemetry_ingest_from_frame(fb.frame_from_png_bytes(_png_bytes(frame)),
                                          video_standard="NTSC")
    assert body["source"] == "FPV_OSD_OCR"
    assert body["method"] == "max7456_glyph_template_match"
    tele = body["telemetry"]
    assert tele["craft_name"] == "HAWK01"
    assert tele["battery_voltage"] == 16.2
    assert tele["sats"] == 14
    assert tele["altitude"] == 150.0
    assert tele["rssi"] == 92
    assert body["caveats"]  # analog-only caveats always attached


def test_blank_frame_fabricates_nothing():
    blank = np.zeros((NTSC.rows * m.TEMPLATE_H, NTSC.cols * m.TEMPLATE_W), dtype=np.uint8)
    body = fb.telemetry_ingest_from_frame(blank, video_standard="NTSC")
    assert body["source"] == "FPV_OSD_OCR"
    assert all(v is None for v in body["telemetry"].values())
