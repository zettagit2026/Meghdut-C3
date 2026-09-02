#!/usr/bin/env python3
"""Unit tests for fpv_osd_ocr.py -- analog FPV OSD glyph-cell telemetry decoder.

These tests synthesize MAX7456-style OSD frames by rendering known text onto a
character grid with the SAME glyph font the decoder matches against (exactly
the approach described in the module docstring), add Gaussian noise to model a
noisy analog video downlink, run the extractor, and assert the parsed
telemetry. They cover:
  - a clean frame (all fields),
  - a noisy frame (robust decode),
  - a heavy-noise frame (numeric fields still correct, never a WRONG value),
  - a partial frame (missing fields stay None -- no fabrication),
  - pure-noise cells (low confidence, not committed to a glyph),
  - PAL vs NTSC grid dimensions,
  - the frame-format contract shared with fpv_video_bridge.py (2D uint8
    grayscale / PIL "L"),
  - the .mcm real-font loader round-trip,
  - the deferred backend-ingest dict shape.

No hardware, no network. Run: pytest field-bridge/test_fpv_osd_ocr.py -v
"""
import numpy as np
import pytest

import fpv_osd_ocr as m


# --------------------------------------------------------------------------
# Fixtures / helpers.
# --------------------------------------------------------------------------
NTSC = m.OSDGridSpec(cols=30, rows=13)

FULL_ROWS = [
    "HAWK01",
    m.SYM_BATT + "16.2V 12.5A",
    m.SYM_SAT + "14 " + m.SYM_ALT + "150M",
    m.SYM_LAT + "28.613920",
    m.SYM_LON + "77.209000",
    m.SYM_RSSI + "92% 03:24",
    "ANGLE",
]


def _full_frame():
    return m.render_osd_frame(FULL_ROWS, spec=NTSC)


# --------------------------------------------------------------------------
# Embedded self-test.
# --------------------------------------------------------------------------
def test_embedded_self_test_passes():
    # The module's own self-test is the primary correctness gate; run it here
    # so pytest fails loudly if it ever regresses.
    m.self_test()


# --------------------------------------------------------------------------
# Clean frame: every field decodes.
# --------------------------------------------------------------------------
def test_clean_frame_all_fields():
    frame = _full_frame()
    t = m.extract_osd_telemetry(frame, video_standard="NTSC", grid_spec=NTSC)
    assert t.craft_name == "HAWK01"
    assert t.battery_voltage == 16.2
    assert t.current == 12.5
    assert t.sats == 14
    assert t.altitude == 150.0
    assert t.gps_lat == pytest.approx(28.613920, abs=1e-4)
    assert t.gps_lon == pytest.approx(77.209000, abs=1e-4)
    assert t.rssi == 92
    assert t.timer == "03:24"
    assert t.flight_mode == "ANGLE"
    assert t.mean_confidence > 0.9
    assert t.has_any()
    # Every emitted field carries a confidence entry.
    for key in ("craft_name", "battery_voltage", "current", "sats", "altitude",
                "gps_lat", "gps_lon", "rssi", "timer", "flight_mode"):
        assert key in t.field_confidence
        assert t.field_confidence[key] >= m.ACCEPT_THRESHOLD


# --------------------------------------------------------------------------
# Frame-format contract: same format fpv_video_bridge.py produces.
# --------------------------------------------------------------------------
def test_frame_is_2d_uint8_grayscale():
    frame = _full_frame()
    assert frame.dtype == np.uint8
    assert frame.ndim == 2  # grayscale, matching envelope_to_image() mode "L"


def test_accepts_pil_L_image():
    pytest.importorskip("PIL")
    from PIL import Image
    frame = _full_frame()
    img = Image.fromarray(frame).convert("L")
    t = m.extract_osd_telemetry(img, grid_spec=NTSC)
    assert t.craft_name == "HAWK01"
    assert t.battery_voltage == 16.2


# --------------------------------------------------------------------------
# Noisy frame: robust decode of a noisy analog downlink.
# --------------------------------------------------------------------------
def test_noisy_frame_decodes():
    frame = _full_frame()
    noisy = m.add_gaussian_noise(frame, sigma=8.0, seed=42)
    t = m.extract_osd_telemetry(noisy, grid_spec=NTSC)
    assert t.craft_name == "HAWK01"
    assert t.battery_voltage == 16.2
    assert t.sats == 14
    assert t.altitude == 150.0
    assert t.gps_lat == pytest.approx(28.613920, abs=1e-4)
    assert t.rssi == 92
    assert t.timer == "03:24"
    assert t.flight_mode == "ANGLE"


def test_heavy_noise_never_reports_wrong_value():
    # Under heavy noise, structured numeric fields (format-validated) still
    # decode; the key honesty property is that we NEVER emit a WRONG value --
    # a field is either correct or None, never fabricated.
    frame = _full_frame()
    noisy = m.add_gaussian_noise(frame, sigma=22.0, seed=7)
    t = m.extract_osd_telemetry(noisy, grid_spec=NTSC)
    if t.battery_voltage is not None:
        assert t.battery_voltage == 16.2
    if t.sats is not None:
        assert t.sats == 14
    if t.altitude is not None:
        assert t.altitude == 150.0
    if t.rssi is not None:
        assert t.rssi == 92
    if t.timer is not None:
        assert t.timer == "03:24"
    if t.gps_lat is not None:
        assert t.gps_lat == pytest.approx(28.613920, abs=1e-3)


# --------------------------------------------------------------------------
# Partial frame: missing fields stay None (no fabrication).
# --------------------------------------------------------------------------
def test_partial_frame_missing_fields_are_none():
    rows = ["FALCON", m.SYM_BATT + "11.1V"]
    frame = m.render_osd_frame(rows, spec=NTSC)
    t = m.extract_osd_telemetry(frame, grid_spec=NTSC)
    # Present:
    assert t.craft_name == "FALCON"
    assert t.battery_voltage == 11.1
    # Absent -> None, NOT invented:
    assert t.sats is None
    assert t.altitude is None
    assert t.current is None
    assert t.gps_lat is None
    assert t.gps_lon is None
    assert t.rssi is None
    assert t.timer is None
    assert t.flight_mode is None


def test_blank_frame_yields_nothing():
    frame = np.zeros((NTSC.rows * m.TEMPLATE_H, NTSC.cols * m.TEMPLATE_W), dtype=np.uint8)
    t = m.extract_osd_telemetry(frame, grid_spec=NTSC)
    assert not t.has_any()
    assert all(v is None for v in t.telemetry_fields().values())


# --------------------------------------------------------------------------
# Per-cell confidence: pure noise is not committed to a glyph.
# --------------------------------------------------------------------------
def test_pure_noise_cell_low_confidence():
    rng = np.random.default_rng(0)
    cell = rng.normal(128, 60, (m.TEMPLATE_H, m.TEMPLATE_W)).clip(0, 255).astype(np.float32)
    ch, conf = m.match_cell(cell, m.default_font())
    # Random noise must not be confidently accepted as a real glyph.
    assert ch == m.UNKNOWN
    assert conf < m.ACCEPT_THRESHOLD


def test_blank_cell_reads_as_space():
    cell = np.zeros((m.TEMPLATE_H, m.TEMPLATE_W), dtype=np.float32)
    ch, conf = m.match_cell(cell, m.default_font())
    assert ch == m.SPACE
    assert conf == pytest.approx(1.0)


def test_clean_glyph_high_confidence():
    font = m.default_font()
    cell = font.templates["7"].copy()
    ch, conf = m.match_cell(cell, font)
    assert ch == "7"
    assert conf > 0.9


def test_unknown_cell_breaks_numeric_field():
    # A voltage row with a low-confidence (UNKNOWN) digit must NOT yield a
    # partial/guessed voltage: the pattern fails and the field stays None.
    rows = ["", m.SYM_BATT + "1" + m.UNKNOWN + ".2V"]
    frame = m.render_osd_frame(rows, spec=NTSC)  # UNKNOWN glyph isn't in the font -> blank cell
    t = m.extract_osd_telemetry(frame, grid_spec=NTSC)
    assert t.battery_voltage is None


# --------------------------------------------------------------------------
# Grid dimensions: NTSC vs PAL.
# --------------------------------------------------------------------------
def test_grid_dimension_presets():
    assert (m.ntsc_grid().cols, m.ntsc_grid().rows) == (30, 13)
    assert (m.pal_grid().cols, m.pal_grid().rows) == (30, 16)


def test_pal_grid_decode():
    pal = m.pal_grid()
    rows = ["PALCRAFT"] + [""] * 14 + [m.SYM_BATT + "22.2V"]  # 16 rows
    frame = m.render_osd_frame(rows, spec=pal)
    t = m.extract_osd_telemetry(frame, video_standard="PAL", grid_spec=pal)
    assert t.grid_rows == 16
    assert t.grid_cols == 30
    assert t.video_standard == "PAL"
    assert t.craft_name == "PALCRAFT"
    assert t.battery_voltage == 22.2


def test_video_standard_selects_grid():
    # Without an explicit grid_spec, video_standard drives the grid dims.
    frame = m.render_osd_frame(["ABC"], spec=m.pal_grid())
    t = m.extract_osd_telemetry(frame, video_standard="PAL")
    assert t.grid_rows == 16


# --------------------------------------------------------------------------
# Font: required glyph coverage + .mcm real-font loader round-trip.
# --------------------------------------------------------------------------
def test_default_font_has_required_glyphs():
    font = m.default_font()
    for ch in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.:-%/":
        assert ch in font.templates, ch
    for sym in (m.SYM_SAT, m.SYM_BATT, m.SYM_RSSI, m.SYM_ALT, m.SYM_LAT, m.SYM_LON):
        assert sym in font.templates


def test_mcm_font_loader_roundtrip(tmp_path):
    # Emit a tiny valid MAX7456 .mcm with a recognizable glyph at index 0x41
    # ('A': a solid 12x18 block) and confirm the loader decodes white pixels.
    lines = ["MAX7456"]
    for idx in range(256):
        for row in range(64):
            if idx == 0x41 and row < 54:  # 18 rows x 3 bytes = solid white glyph
                lines.append("10" * 4)    # 4 white pixels per byte-line
            else:
                lines.append("0" * 8)
    mcm = tmp_path / "test.mcm"
    mcm.write_text("\n".join(lines) + "\n")
    font = m.GlyphFont.from_mcm(str(mcm))
    assert "A" in font.templates
    a = font.templates["A"]
    assert a.shape == (m.TEMPLATE_H, m.TEMPLATE_W)
    assert a.max() == 255.0  # white pixels decoded
    assert a.mean() > 200.0  # mostly-solid block


def test_mcm_rejects_non_mcm(tmp_path):
    bad = tmp_path / "bad.mcm"
    bad.write_text("NOTAFONT\n0000\n")
    with pytest.raises(ValueError):
        m.GlyphFont.from_mcm(str(bad))


# --------------------------------------------------------------------------
# Output shapes: dict + deferred backend-ingest contract.
# --------------------------------------------------------------------------
def test_to_dict_shape():
    t = m.extract_osd_telemetry(_full_frame(), grid_spec=NTSC)
    d = t.to_dict()
    for key in ("craft_name", "gps_lat", "gps_lon", "sats", "altitude",
                "battery_voltage", "current", "rssi", "timer", "flight_mode",
                "video_standard", "grid_cols", "grid_rows", "mean_confidence",
                "raw_text_lines"):
        assert key in d


def test_to_ingest_dict_shape():
    t = m.extract_osd_telemetry(_full_frame(), grid_spec=NTSC)
    ing = t.to_ingest_dict()
    assert ing["source"] == "FPV_OSD_OCR"
    assert ing["method"] == "max7456_glyph_template_match"
    assert ing["signal_class"] == "analog_fpv_video_osd"
    assert isinstance(ing["telemetry"], dict)
    assert ing["telemetry"]["craft_name"] == "HAWK01"
    assert isinstance(ing["caveats"], list) and ing["caveats"]


# --------------------------------------------------------------------------
# Color-frame tolerance (defensive: fpv demod is grayscale, but be robust).
# --------------------------------------------------------------------------
def test_color_frame_tolerated():
    gray = _full_frame()
    color = np.stack([gray, gray, gray], axis=2)
    t = m.extract_osd_telemetry(color, grid_spec=NTSC)
    assert t.craft_name == "HAWK01"


def test_optional_tesseract_flag_is_guarded():
    # use_tesseract=True must not silently succeed without the dependency; it
    # raises a clear, documented error (glyph-match path is the default).
    try:
        import pytesseract  # noqa: F401
        has_tess = True
    except Exception:
        has_tess = False
    if not has_tess:
        with pytest.raises(RuntimeError):
            m.extract_osd_telemetry(_full_frame(), grid_spec=NTSC, use_tesseract=True)
