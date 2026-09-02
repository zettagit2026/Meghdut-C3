#!/usr/bin/env python3
"""Analog FPV video OSD telemetry extractor (MAX7456 glyph-cell decoder).

RECEIVE/OFFLINE ONLY. This module never transmits and never touches a radio.
It consumes an already-demodulated grayscale video frame (the exact frame
format produced by fpv_video_bridge.py's AM-envelope demod path -- a 2D
uint8 grayscale array, i.e. a PIL mode "L" image) and recovers the on-screen
Betaflight/iNav-class OSD telemetry overlay (callsign, GPS, sats, battery,
RSSI, altitude, timer, flight mode) so the tool can *identify an airborne
MSP/Betaflight-based drone from its video downlink alone*, with no wire tap.

=============================================================================
WHAT THIS ACHIEVES  (read before trusting any output)
=============================================================================
Hobbyist analog FPV video transmitters (5.8GHz Raceband/Fatshark/Boscam-class
hardware -- the same signal fpv_video_bridge.py already demodulates) overlay
their flight-controller telemetry as an on-screen display (OSD) burned into
the composite video by a MAX7456-style character generator. That OSD is NOT
free-form pixels: it is a FIXED CHARACTER-CELL GRID. Every cell is one glyph
from a small, known bitmap font (12x18 px per glyph in the MAX7456). NTSC
lays this out as a 30x13 cell grid; PAL as 30x16.

Because the layout is a rigid grid of known glyphs, the robust, air-gap-clean
way to read it is CHARACTER-CELL GLYPH TEMPLATE MATCHING against that known
font -- NOT general-purpose OCR. We slice the frame into its character cells,
match each cell against a glyph template set by normalized cross-correlation,
emit a per-cell confidence, decode the grid to text, and parse the text into
a structured telemetry record. This needs nothing beyond numpy.

=============================================================================
HONEST LIMITS  (do NOT overclaim -- this does not read "every FPV drone")
=============================================================================
1. ANALOG ONLY. This works on analog composite-video OSD overlays. DIGITAL
   FPV video links -- DJI O3/O4 (OcuSync), HDZero, Walksnail Avatar -- carry
   a compressed/encrypted digital H.264/H.265 stream, NOT an analog OSD burned
   into a demodulable envelope. Their OSD is not recoverable this way. Against
   a digital link the correct counter-UAS action is jam/deny (see hackrf_jam.py),
   not OSD extraction. This module makes no attempt on digital video and will
   simply return an all-None telemetry record (low confidence) if fed one.
2. KNOWN-FONT DEPENDENT. Matching is only as good as the glyph template set.
   The embedded default font (default_font()) is a compact, self-consistent
   ASCII+symbol set sufficient to develop, test and demonstrate the pipeline
   end to end. For field decode of a SPECIFIC target drone you should load
   that airframe's actual MAX7456 font via GlyphFont.from_mcm(<file.mcm>) --
   Betaflight/iNav ship their fonts as standard .mcm files and this module
   parses them directly (dependency-free). An UNKNOWN / custom OSD font that
   is not loaded will decode poorly: the fix is to add its templates, not to
   "guess harder". This is disclosed, not hidden.
3. UPSTREAM DEMOD IS UNVERIFIED AGAINST A LIVE VTX. fpv_video_bridge.py
   itself documents that its NTSC/PAL reconstruction is not yet validated
   against a real transmitting analog VTX (no live signal was available).
   A cleanly-synced frame is a precondition for this decoder; a torn/unsynced
   frame will not grid-align and will decode to noise. This module's own
   correctness (grid slicing + glyph match + parse) is fully unit-tested with
   synthesized frames; the live-signal end-to-end path inherits fpv_video_
   bridge.py's outstanding "NOT VERIFIED against a live transmitter" caveat.
4. NO FABRICATION. A field is emitted ONLY if the glyph cells that spell it
   all clear the confidence threshold. Uncertain cells become an explicit
   unknown marker that breaks the field's parse pattern, so a noisy/ambiguous
   frame yields None for that field rather than an invented value.

=============================================================================
DEPENDENCY FOOTPRINT  (no heavy new deps added)
=============================================================================
Core path: numpy ONLY (already a field-bridge dependency). Pillow (already a
dependency, used by fpv_video_bridge.py) is used only to accept PIL "L" images
as input and is optional. There is an OPTIONAL general-OCR fallback behind the
`use_tesseract=True` flag that uses pytesseract IF installed; it is NOT the
default, NOT required, and pytesseract + the tesseract binary would have to be
VENDORED for the air-gapped appliance. The glyph-match path is the default and
works with zero external deps beyond numpy. No opencv, no torch, nothing new.

=============================================================================
INTENDED BACKEND INGEST  (deferred -- NOT wired here on purpose)
=============================================================================
Wiring this into backend/server.py is a deliberately deferred follow-up to
avoid colliding with the running backend workstream. This module only exposes
a clean API (extract_osd_telemetry() -> FpvOsdTelemetry) plus
FpvOsdTelemetry.to_ingest_dict(), which documents the shape the backend would
later POST/ingest (mirrors the source/method/confidence/caveat convention the
other bridges already use). See FpvOsdTelemetry.to_ingest_dict().
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

# --------------------------------------------------------------------------
# Grid / glyph geometry (MAX7456 character generator).
# --------------------------------------------------------------------------
# MAX7456 native glyph cell is 12 wide x 18 tall pixels. Templates are stored
# at this native resolution; extracted frame cells are resampled to it before
# matching, so the input frame can be any resolution as long as the OSD grid
# fills the region described by the OSDGridSpec.
TEMPLATE_W = 12
TEMPLATE_H = 18

# Standard MAX7456 OSD character-grid dimensions. NTSC has fewer visible lines
# than PAL, hence the shorter grid.
NTSC_COLS, NTSC_ROWS = 30, 13
PAL_COLS, PAL_ROWS = 30, 16

# Matching thresholds (see match_cell()). Tuned so a clean or moderately-noisy
# glyph matches confidently while pure noise / blank falls through to a space
# or the explicit UNKNOWN marker.
ACCEPT_THRESHOLD = 0.45   # min normalized-correlation to accept a glyph
CONTRAST_FLOOR = 45.0     # below this per-cell peak-to-peak (0..255) => blank/space

# Explicit "could not confidently read this cell" marker. It is deliberately a
# character that never appears inside a numeric telemetry token, so any field
# whose digits include an unknown cell simply fails to parse -> field stays
# None. This is the structural no-fabrication guard.
UNKNOWN = "�"  # U+FFFD REPLACEMENT CHARACTER
SPACE = " "

# Special OSD icon glyphs, represented internally by private-use control chars
# so they round-trip through the text grid without colliding with real ASCII.
SYM_SAT = "\x01"    # GPS satellite icon (precedes sat count)
SYM_BATT = "\x02"   # battery icon (precedes pack voltage)
SYM_RSSI = "\x03"   # RSSI / antenna icon (precedes link %)
SYM_ALT = "\x04"    # altitude icon (precedes metres)
SYM_DEG = "\x05"    # degree symbol
SYM_LAT = "\x06"    # latitude marker (precedes lat value)
SYM_LON = "\x07"    # longitude marker (precedes lon value)

# Flight-mode tokens Betaflight/iNav render on the mode row. Used to identify
# the flight-mode field and to exclude a mode row from being read as a craft
# name. Not exhaustive; extend as needed for a specific airframe.
FLIGHT_MODES = {
    "STAB", "ANGLE", "HORIZON", "HOR", "ACRO", "AIR", "MANU", "MANUAL",
    "RTH", "RTL", "WP", "CRUISE", "LAUNCH", "HOLD", "ALTHOLD", "POSHOLD",
    "FAILSAFE", "ARM", "ARMED", "DISARMED",
}


# --------------------------------------------------------------------------
# Embedded compact 5x7 bitmap font.
# --------------------------------------------------------------------------
# Human-verifiable in source: each glyph is 7 rows of 5 columns using '#' for
# ink and ' ' (or '.') for background. Rendered (nearest-upscaled + centred)
# into the 12x18 MAX7456 cell to build the default template set. This is a
# SELF-CONSISTENT development/demo font, not the exact proprietary MAX7456
# font -- for field decode of a real airframe, load its actual .mcm via
# GlyphFont.from_mcm() (see module docstring limit #2).
_FONT_5x7: Dict[str, List[str]] = {
    " ": ["     ", "     ", "     ", "     ", "     ", "     ", "     "],
    "0": [" ### ", "#   #", "#  ##", "# # #", "##  #", "#   #", " ### "],
    "1": ["  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "2": [" ### ", "#   #", "    #", "   # ", "  #  ", " #   ", "#####"],
    "3": ["#####", "   # ", "  #  ", "   # ", "    #", "#   #", " ### "],
    "4": ["   # ", "  ## ", " # # ", "#  # ", "#####", "   # ", "   # "],
    "5": ["#####", "#    ", "#### ", "    #", "    #", "#   #", " ### "],
    "6": ["  ## ", " #   ", "#    ", "#### ", "#   #", "#   #", " ### "],
    "7": ["#####", "    #", "   # ", "  #  ", " #   ", " #   ", " #   "],
    "8": [" ### ", "#   #", "#   #", " ### ", "#   #", "#   #", " ### "],
    "9": [" ### ", "#   #", "#   #", " ####", "    #", "   # ", " ##  "],
    "A": [" ### ", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "B": ["#### ", "#   #", "#   #", "#### ", "#   #", "#   #", "#### "],
    "C": [" ### ", "#   #", "#    ", "#    ", "#    ", "#   #", " ### "],
    "D": ["###  ", "#  # ", "#   #", "#   #", "#   #", "#  # ", "###  "],
    "E": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"],
    "F": ["#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#    "],
    "G": [" ### ", "#   #", "#    ", "# ###", "#   #", "#   #", " ### "],
    "H": ["#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"],
    "I": [" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "],
    "J": ["  ###", "   # ", "   # ", "   # ", "#  # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"],
    "L": ["#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #", "#   #", "#   #"],
    "N": ["#   #", "##  #", "# # #", "#  ##", "#   #", "#   #", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "P": ["#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "#   #", "#   #", "# # #", "#  # ", " ## #"],
    "R": ["#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"],
    "S": [" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "],
    "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "],
    "V": ["#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "#   #", "# # #", "# # #", "## ##", "#   #"],
    "X": ["#   #", "#   #", " # # ", "  #  ", " # # ", "#   #", "#   #"],
    "Y": ["#   #", "#   #", " # # ", "  #  ", "  #  ", "  #  ", "  #  "],
    "Z": ["#####", "    #", "   # ", "  #  ", " #   ", "#    ", "#####"],
    ".": ["     ", "     ", "     ", "     ", "     ", " ##  ", " ##  "],
    "-": ["     ", "     ", "     ", "#####", "     ", "     ", "     "],
    ":": ["     ", " ##  ", " ##  ", "     ", " ##  ", " ##  ", "     "],
    "%": ["##  #", "##  #", "   # ", "  #  ", " #   ", "#  ##", "#  ##"],
    "/": ["    #", "    #", "   # ", "  #  ", " #   ", "#    ", "#    "],
    "_": ["     ", "     ", "     ", "     ", "     ", "     ", "#####"],
    "+": ["     ", "  #  ", "  #  ", "#####", "  #  ", "  #  ", "     "],
    # --- special OSD icon glyphs (distinct shapes, distinct from letters) ---
    SYM_SAT: ["# # #", " ### ", "#####", " ### ", "# # #", "  #  ", " ### "],
    SYM_BATT: [" ### ", "#####", "#   #", "#   #", "#####", "#####", "#####"],
    SYM_RSSI: ["    #", "    #", "  # #", "  # #", "# # #", "# # #", "# # #"],
    SYM_ALT: ["  #  ", " ### ", "#####", "  #  ", "  #  ", "  #  ", "  #  "],
    SYM_DEG: [" ##  ", "#  # ", "#  # ", " ##  ", "     ", "     ", "     "],
    SYM_LAT: ["#    ", "#    ", "#    ", "#    ", "#####", "     ", " ### "],
    SYM_LON: [" ### ", "#   #", "#   #", "#   #", " ### ", "     ", " ### "],
}


def _art_to_bitmap(art_rows: List[str]) -> np.ndarray:
    """Convert '#'/' ' ASCII art rows into a float bitmap (255=ink, 0=bg)."""
    h = len(art_rows)
    w = max(len(r) for r in art_rows)
    bm = np.zeros((h, w), dtype=np.float32)
    for r, row in enumerate(art_rows):
        for c, ch in enumerate(row):
            if ch == "#":
                bm[r, c] = 255.0
    return bm


def _resample_nn(img: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """Dependency-free nearest-neighbour resample (no cv2/PIL needed)."""
    ih, iw = img.shape[:2]
    if ih == out_h and iw == out_w:
        return img.astype(np.float32)
    ys = np.minimum((np.arange(out_h) * ih // max(out_h, 1)), ih - 1)
    xs = np.minimum((np.arange(out_w) * iw // max(out_w, 1)), iw - 1)
    return img[ys][:, xs].astype(np.float32)


def _scale_glyph(bm: np.ndarray, out_h: int = TEMPLATE_H, out_w: int = TEMPLATE_W) -> np.ndarray:
    """Upscale a small glyph bitmap into a centred MAX7456-sized template."""
    # Leave a small margin so glyphs do not touch cell edges (matches how the
    # MAX7456 pads its 5x7-ish strokes inside the 12x18 cell).
    inner_h = max(1, out_h - 4)
    inner_w = max(1, out_w - 2)
    scaled = _resample_nn(bm, inner_h, inner_w)
    canvas = np.zeros((out_h, out_w), dtype=np.float32)
    y0 = (out_h - inner_h) // 2
    x0 = (out_w - inner_w) // 2
    canvas[y0:y0 + inner_h, x0:x0 + inner_w] = scaled
    return canvas


# --------------------------------------------------------------------------
# Glyph font (template set).
# --------------------------------------------------------------------------
@dataclass
class GlyphFont:
    """A glyph template set: maps each character to a TEMPLATE_H x TEMPLATE_W
    float bitmap. `space_char` is matched by low-contrast detection rather than
    correlation (a uniform cell has no variance to correlate against)."""
    templates: Dict[str, np.ndarray]
    cell_h: int = TEMPLATE_H
    cell_w: int = TEMPLATE_W

    @classmethod
    def from_art(cls, art: Dict[str, List[str]]) -> "GlyphFont":
        tmpl = {ch: _scale_glyph(_art_to_bitmap(rows))
                for ch, rows in art.items() if ch != SPACE}
        return cls(templates=tmpl)

    @classmethod
    def from_mcm(cls, path: str, charmap: Optional[Dict[int, str]] = None) -> "GlyphFont":
        """Load a real MAX7456 .mcm font file (Betaflight/iNav ship these).

        The .mcm format is ASCII: a "MAX7456" header line followed by 256
        glyphs, each 64 lines of 8 bits ("01"). Each pixel is 2 bits
        (00=black, 10=white, 01/11=transparent); a 12x18 glyph uses 18 rows x
        3 bytes = 54 of the 64 lines (the rest are padding). We render
        white->255, black/transparent->0.

        `charmap` maps a MAX7456 glyph INDEX to the character/symbol it should
        decode to (fonts place ASCII at conventional offsets, e.g. Betaflight
        puts '0'..'9' at 0x30..0x39, 'A'..'Z' at 0x41..0x5A). If omitted, the
        standard ASCII offsets are assumed for the printable range, which
        covers the digits/letters this decoder needs. Dependency-free.
        """
        with open(path, "r", encoding="ascii", errors="ignore") as fh:
            lines = [ln.strip() for ln in fh if ln.strip() != ""]
        if not lines or not lines[0].upper().startswith("MAX7456"):
            raise ValueError(f"{path}: not a MAX7456 .mcm font (missing header)")
        body = lines[1:]
        if charmap is None:
            charmap = {i: chr(i) for i in range(0x20, 0x7F)}
        templates: Dict[str, np.ndarray] = {}
        # 64 lines per glyph.
        for idx in range(min(256, len(body) // 64)):
            ch = charmap.get(idx)
            if ch is None or ch == SPACE:
                continue
            glyph_lines = body[idx * 64:idx * 64 + 64]
            bm = np.zeros((TEMPLATE_H, TEMPLATE_W), dtype=np.float32)
            for row in range(TEMPLATE_H):
                # 3 bytes (lines) per pixel-row, 4 px per byte.
                bits = "".join(glyph_lines[row * 3:row * 3 + 3])
                for col in range(TEMPLATE_W):
                    two = bits[col * 2:col * 2 + 2]
                    if two == "10":  # white pixel
                        bm[row, col] = 255.0
            templates[ch] = bm
        if not templates:
            raise ValueError(f"{path}: no glyphs decoded from .mcm")
        return cls(templates=templates)


_DEFAULT_FONT: Optional[GlyphFont] = None


def default_font() -> GlyphFont:
    """The embedded, self-consistent development/demo glyph set (cached)."""
    global _DEFAULT_FONT
    if _DEFAULT_FONT is None:
        _DEFAULT_FONT = GlyphFont.from_art(_FONT_5x7)
    return _DEFAULT_FONT


# --------------------------------------------------------------------------
# Grid specification + frame handling.
# --------------------------------------------------------------------------
@dataclass
class OSDGridSpec:
    """Where the OSD character grid sits in the frame. Defaults to the whole
    frame divided into cols x rows equal cells (the common case: the OSD
    overlay spans the active video area). Provide origin/width/height to crop
    to a known sub-region if the demod places the grid elsewhere."""
    cols: int
    rows: int
    origin_x: int = 0
    origin_y: int = 0
    width: Optional[int] = None
    height: Optional[int] = None


def ntsc_grid() -> OSDGridSpec:
    return OSDGridSpec(cols=NTSC_COLS, rows=NTSC_ROWS)


def pal_grid() -> OSDGridSpec:
    return OSDGridSpec(cols=PAL_COLS, rows=PAL_ROWS)


FrameLike = Union[np.ndarray, "object"]  # np.ndarray or PIL.Image.Image


def _to_gray_frame(frame: FrameLike) -> np.ndarray:
    """Accept the SAME frame format fpv_video_bridge.py produces (2D uint8
    grayscale / PIL mode 'L') plus a couple of convenience forms, and return
    a 2D float32 array."""
    # PIL image (duck-typed to avoid a hard Pillow import in the core path).
    if hasattr(frame, "convert") and hasattr(frame, "size"):
        frame = np.asarray(frame.convert("L"))
    arr = np.asarray(frame)
    if arr.ndim == 3:  # colour -> luma-ish mean (fpv demod is grayscale, but be tolerant)
        arr = arr.mean(axis=2)
    if arr.ndim != 2:
        raise ValueError(f"expected a 2D grayscale frame, got shape {arr.shape}")
    return arr.astype(np.float32)


def extract_cells(frame: np.ndarray, spec: OSDGridSpec) -> List[List[np.ndarray]]:
    """Slice the frame region into spec.rows x spec.cols cells, each resampled
    to the template resolution."""
    H, W = frame.shape
    w = spec.width if spec.width is not None else (W - spec.origin_x)
    h = spec.height if spec.height is not None else (H - spec.origin_y)
    cw = w / spec.cols
    ch = h / spec.rows
    grid: List[List[np.ndarray]] = []
    for r in range(spec.rows):
        row_cells: List[np.ndarray] = []
        y0 = int(round(spec.origin_y + r * ch))
        y1 = int(round(spec.origin_y + (r + 1) * ch))
        for c in range(spec.cols):
            x0 = int(round(spec.origin_x + c * cw))
            x1 = int(round(spec.origin_x + (c + 1) * cw))
            sub = frame[max(0, y0):max(y0 + 1, y1), max(0, x0):max(x0 + 1, x1)]
            if sub.size == 0:
                sub = np.zeros((TEMPLATE_H, TEMPLATE_W), dtype=np.float32)
            row_cells.append(_resample_nn(sub, TEMPLATE_H, TEMPLATE_W))
        grid.append(row_cells)
    return grid


# --------------------------------------------------------------------------
# Cell matching.
# --------------------------------------------------------------------------
def _corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation coefficient (brightness/contrast invariant)."""
    af = a.ravel().astype(np.float64)
    bf = b.ravel().astype(np.float64)
    af -= af.mean()
    bf -= bf.mean()
    da = np.sqrt((af * af).sum())
    db = np.sqrt((bf * bf).sum())
    if da < 1e-9 or db < 1e-9:
        return 0.0
    return float((af * bf).sum() / (da * db))


def match_cell(
    cell: np.ndarray,
    font: GlyphFont,
    accept_threshold: float = ACCEPT_THRESHOLD,
    contrast_floor: float = CONTRAST_FLOOR,
) -> Tuple[str, float]:
    """Match one resampled cell against the font. Returns (char, confidence).

    - A low-contrast (near-uniform) cell is a blank -> (' ', 1.0).
    - Otherwise the best-correlating glyph is returned; if its correlation is
      below `accept_threshold` the cell is UNKNOWN (breaks numeric parsing,
      preventing fabrication).
    Assumes white-ink-on-dark polarity (standard MAX7456 white text). Contrast/
    brightness are normalized out by the correlation, so absolute levels of the
    demod frame do not matter, only glyph shape.
    """
    c = np.asarray(cell, dtype=np.float32)
    if float(c.max() - c.min()) < contrast_floor:
        return SPACE, 1.0
    best_ch = UNKNOWN
    best_r = -2.0
    for ch, tmpl in font.templates.items():
        r = _corr(c, tmpl)
        if r > best_r:
            best_r = r
            best_ch = ch
    conf = max(0.0, best_r)
    if conf < accept_threshold:
        return UNKNOWN, conf
    return best_ch, conf


# --------------------------------------------------------------------------
# Grid decode.
# --------------------------------------------------------------------------
@dataclass
class OSDDecodeResult:
    """Raw decoded character grid + per-cell confidence."""
    char_grid: List[List[str]]
    conf_grid: List[List[float]]
    lines: List[str]              # per-row decoded text (sentinels preserved)
    line_confs: List[List[float]] # per-row, per-char confidence (aligned to lines)
    mean_confidence: float


def decode_grid(
    frame: FrameLike,
    spec: OSDGridSpec,
    font: Optional[GlyphFont] = None,
    accept_threshold: float = ACCEPT_THRESHOLD,
    contrast_floor: float = CONTRAST_FLOOR,
) -> OSDDecodeResult:
    """Decode a frame's OSD grid to a character grid + confidences."""
    font = font or default_font()
    gray = _to_gray_frame(frame)
    cells = extract_cells(gray, spec)
    char_grid: List[List[str]] = []
    conf_grid: List[List[float]] = []
    lines: List[str] = []
    line_confs: List[List[float]] = []
    all_conf: List[float] = []
    for row_cells in cells:
        chars: List[str] = []
        confs: List[float] = []
        for cell in row_cells:
            ch, cf = match_cell(cell, font, accept_threshold, contrast_floor)
            chars.append(ch)
            confs.append(cf)
            # mean_confidence reflects the quality of glyphs we ACTUALLY decoded:
            # exclude confident blanks (SPACE) and undecoded cells (UNKNOWN, which
            # are noise/ambiguous and were deliberately not committed to a glyph).
            if ch not in (SPACE, UNKNOWN):
                all_conf.append(cf)
        char_grid.append(chars)
        conf_grid.append(confs)
        lines.append("".join(chars))
        line_confs.append(confs)
    mean_conf = float(np.mean(all_conf)) if all_conf else 0.0
    return OSDDecodeResult(char_grid, conf_grid, lines, line_confs, mean_conf)


# --------------------------------------------------------------------------
# Telemetry parse.
# --------------------------------------------------------------------------
@dataclass
class FpvOsdTelemetry:
    """Structured telemetry parsed from an analog FPV OSD frame.

    Every telemetry field is Optional; a field is None when it was not present
    or could not be read with sufficient confidence (no fabrication)."""
    craft_name: Optional[str] = None
    gps_lat: Optional[float] = None
    gps_lon: Optional[float] = None
    sats: Optional[int] = None
    altitude: Optional[float] = None          # metres
    battery_voltage: Optional[float] = None   # volts
    current: Optional[float] = None           # amps
    rssi: Optional[int] = None                # percent
    timer: Optional[str] = None               # "MM:SS"
    flight_mode: Optional[str] = None
    # ---- metadata / provenance ----
    video_standard: str = "NTSC"
    grid_cols: int = 0
    grid_rows: int = 0
    mean_confidence: float = 0.0
    raw_text_lines: List[str] = field(default_factory=list)
    field_confidence: Dict[str, float] = field(default_factory=dict)

    def telemetry_fields(self) -> Dict[str, object]:
        return {
            "craft_name": self.craft_name,
            "gps_lat": self.gps_lat,
            "gps_lon": self.gps_lon,
            "sats": self.sats,
            "altitude": self.altitude,
            "battery_voltage": self.battery_voltage,
            "current": self.current,
            "rssi": self.rssi,
            "timer": self.timer,
            "flight_mode": self.flight_mode,
        }

    def has_any(self) -> bool:
        return any(v is not None for v in self.telemetry_fields().values())

    def to_dict(self) -> dict:
        d = dict(self.telemetry_fields())
        d.update({
            "video_standard": self.video_standard,
            "grid_cols": self.grid_cols,
            "grid_rows": self.grid_rows,
            "mean_confidence": round(self.mean_confidence, 4),
            "field_confidence": {k: round(v, 4) for k, v in self.field_confidence.items()},
            "raw_text_lines": self.raw_text_lines,
        })
        return d

    def to_ingest_dict(self) -> dict:
        """Intended backend-ingest shape (DEFERRED -- not wired to server.py).

        Mirrors the source/method/confidence/caveat convention used by the
        sibling bridges (e.g. droneid_decode_bridge.py -> /api/detections/ingest).
        A future follow-up would POST this to a new /api/fpv/osd-telemetry
        endpoint. Kept here as living documentation of the contract."""
        return {
            "source": "FPV_OSD_OCR",
            "method": "max7456_glyph_template_match",
            "signal_class": "analog_fpv_video_osd",
            "telemetry": self.telemetry_fields(),
            "mean_confidence": round(self.mean_confidence, 4),
            "field_confidence": {k: round(v, 4) for k, v in self.field_confidence.items()},
            "video_standard": self.video_standard,
            "raw_osd_text": self.raw_text_lines,
            "caveats": [
                "analog OSD only; DJI/HDZero/Walksnail digital video NOT decodable here",
                "decode quality depends on the loaded MAX7456 glyph font",
                "fields below confidence threshold are reported as null, not guessed",
            ],
        }


def _clean_line(line: str) -> str:
    """Strip special icon sentinels and unknown markers for name matching."""
    out = line
    for sym in (SYM_SAT, SYM_BATT, SYM_RSSI, SYM_ALT, SYM_DEG, SYM_LAT, SYM_LON):
        out = out.replace(sym, " ")
    out = out.replace(UNKNOWN, " ")
    return out


def _span_conf(confs: List[float], start: int, end: int) -> float:
    seg = confs[start:end]
    return float(min(seg)) if seg else 0.0


def _search(lines: List[str], line_confs: List[List[float]], pattern: str,
            group: int = 1) -> Optional[Tuple[str, float, int, int]]:
    """Find the first regex match across lines. Returns (text, min_conf, row, col)."""
    rx = re.compile(pattern)
    for row, (ln, cf) in enumerate(zip(lines, line_confs)):
        m = rx.search(ln)
        if m:
            s, e = m.span(group)
            return m.group(group), _span_conf(cf, s, e), row, s
    return None


def parse_telemetry(decoded: OSDDecodeResult, *, video_standard: str = "NTSC") -> FpvOsdTelemetry:
    """Parse a decoded OSD character grid into structured telemetry.

    Uncertain cells are already UNKNOWN markers, so numeric patterns that
    include one simply fail to match -> the field stays None (no fabrication).
    """
    lines = decoded.lines
    confs = decoded.line_confs
    t = FpvOsdTelemetry(
        video_standard=video_standard,
        grid_cols=len(decoded.char_grid[0]) if decoded.char_grid else 0,
        grid_rows=len(decoded.char_grid),
        mean_confidence=decoded.mean_confidence,
        raw_text_lines=[_clean_line(ln).rstrip() for ln in lines],
    )

    # --- battery voltage: "<icon>16.2" or "16.2V" ---
    hit = _search(lines, confs, re.escape(SYM_BATT) + r"\s*(\d{1,2}\.\d)")
    if hit is None:
        hit = _search(lines, confs, r"(\d{1,2}\.\d)\s*V")
    if hit:
        t.battery_voltage = float(hit[0])
        t.field_confidence["battery_voltage"] = hit[1]

    # --- current: "12.5A" ---
    hit = _search(lines, confs, r"(\d{1,3}\.\d)\s*A")
    if hit:
        t.current = float(hit[0])
        t.field_confidence["current"] = hit[1]

    # --- sats: "<sat-icon>12" or "SAT 12" ---
    hit = _search(lines, confs, re.escape(SYM_SAT) + r"\s*(\d{1,2})")
    if hit is None:
        hit = _search(lines, confs, r"SATS?\s*(\d{1,2})")
    if hit:
        t.sats = int(hit[0])
        t.field_confidence["sats"] = hit[1]

    # --- altitude: "<alt-icon>150" or "150M" ---
    hit = _search(lines, confs, re.escape(SYM_ALT) + r"\s*(-?\d{1,4})")
    if hit is None:
        hit = _search(lines, confs, r"(-?\d{1,4})\s*M(?![A-Z])")
    if hit:
        t.altitude = float(hit[0])
        t.field_confidence["altitude"] = hit[1]

    # --- rssi: "<rssi-icon>98" or "98%" ---
    hit = _search(lines, confs, re.escape(SYM_RSSI) + r"\s*(\d{1,3})")
    if hit is None:
        hit = _search(lines, confs, r"(\d{1,3})\s*%")
    if hit:
        val = int(hit[0])
        if 0 <= val <= 100:
            t.rssi = val
            t.field_confidence["rssi"] = hit[1]

    # --- timer: "MM:SS" ---
    hit = _search(lines, confs, r"(\d{1,2}:\d{2})")
    if hit:
        t.timer = hit[0]
        t.field_confidence["timer"] = hit[1]

    # --- GPS lat/lon ---
    lat_hit = _search(lines, confs, re.escape(SYM_LAT) + r"\s*(-?\d{1,3}\.\d{3,})")
    lon_hit = _search(lines, confs, re.escape(SYM_LON) + r"\s*(-?\d{1,3}\.\d{3,})")
    if lat_hit and lon_hit:
        t.gps_lat = float(lat_hit[0])
        t.gps_lon = float(lon_hit[0])
        t.field_confidence["gps_lat"] = lat_hit[1]
        t.field_confidence["gps_lon"] = lon_hit[1]
    else:
        # Fallback: two coordinate-like numbers in reading order.
        rx = re.compile(r"(-?\d{1,3}\.\d{3,})")
        coords: List[Tuple[float, float]] = []
        for ln, cf in zip(lines, confs):
            for m in rx.finditer(ln):
                s, e = m.span(1)
                coords.append((float(m.group(1)), _span_conf(cf, s, e)))
        if len(coords) >= 2:
            t.gps_lat, t.field_confidence["gps_lat"] = coords[0]
            t.gps_lon, t.field_confidence["gps_lon"] = coords[1]

    # --- flight mode ---
    for ln, cf in zip(lines, confs):
        cleaned = _clean_line(ln)
        for tok in re.findall(r"[A-Z]{2,}", cleaned):
            if tok in FLIGHT_MODES:
                # confidence = min over the token's cells
                idx = ln.find(tok)
                t.flight_mode = tok
                if idx >= 0:
                    t.field_confidence["flight_mode"] = _span_conf(cf, idx, idx + len(tok))
                break
        if t.flight_mode:
            break

    # --- craft name: a topmost alphabetic row that is not telemetry/mode ---
    for ln, cf in zip(lines, confs):
        if UNKNOWN in ln:
            continue  # do not emit a partially-read name (no fabrication)
        cleaned = _clean_line(ln).strip()
        if len(cleaned) < 2:
            continue
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9 _-]*", cleaned):
            continue
        letters = re.sub(r"[^A-Z]", "", cleaned)
        if len(letters) < 2:
            continue
        # Exclude telemetry-looking or pure flight-mode rows.
        if re.search(r"\d\.\d|:\d|%|\bM\b", cleaned):
            continue
        if cleaned in FLIGHT_MODES:
            continue
        t.craft_name = cleaned
        # confidence = min over the non-space cells of this row
        row_confs = [c for ch, c in zip(ln, cf) if ch not in (SPACE,)]
        t.field_confidence["craft_name"] = float(min(row_confs)) if row_confs else 0.0
        break

    return t


# --------------------------------------------------------------------------
# Top-level API.
# --------------------------------------------------------------------------
def extract_osd_telemetry(
    frame: FrameLike,
    *,
    video_standard: str = "NTSC",
    grid_spec: Optional[OSDGridSpec] = None,
    font: Optional[GlyphFont] = None,
    accept_threshold: float = ACCEPT_THRESHOLD,
    contrast_floor: float = CONTRAST_FLOOR,
    use_tesseract: bool = False,
) -> FpvOsdTelemetry:
    """Extract structured telemetry from one demodulated analog FPV video frame.

    Parameters
    ----------
    frame : the demodulated grayscale video frame -- the SAME format
        fpv_video_bridge.py produces (2D uint8 grayscale numpy array, i.e. a
        PIL mode "L" image). PIL images and colour arrays are also accepted.
    video_standard : "NTSC" (30x13 cells) or "PAL" (30x16 cells). Ignored if
        `grid_spec` is given explicitly.
    grid_spec : override the OSD grid location/size (see OSDGridSpec).
    font : glyph template set (defaults to the embedded default_font(); load a
        real airframe font with GlyphFont.from_mcm() for field decode).
    use_tesseract : OPTIONAL general-OCR fallback (pytesseract). NOT the
        default, NOT air-gap-clean (tesseract would need vendoring). The
        glyph-match path above is the default and needs only numpy.

    Returns
    -------
    FpvOsdTelemetry with any successfully-read fields; unread fields are None.
    """
    std = (video_standard or "NTSC").upper()
    if grid_spec is None:
        grid_spec = pal_grid() if std == "PAL" else ntsc_grid()

    if use_tesseract:
        return _tesseract_fallback(frame, video_standard=std, grid_spec=grid_spec)

    decoded = decode_grid(frame, grid_spec, font, accept_threshold, contrast_floor)
    return parse_telemetry(decoded, video_standard=std)


def _tesseract_fallback(frame: FrameLike, *, video_standard: str,
                        grid_spec: OSDGridSpec) -> FpvOsdTelemetry:
    """Optional general-OCR path. Documented, non-default, NOT air-gap-clean.

    Requires `pytesseract` (Python) AND the `tesseract` binary on PATH. For the
    air-gapped appliance BOTH would have to be vendored -- which is exactly why
    the glyph-match path is the sovereign default. Provided only as an escape
    hatch for a frame whose font is unknown and unavailable as an .mcm."""
    try:
        import pytesseract  # noqa: F401
        from PIL import Image
    except Exception as exc:  # pragma: no cover - env without tesseract
        raise RuntimeError(
            "use_tesseract=True but pytesseract/PIL unavailable. The glyph-match "
            "path (use_tesseract=False, the default) needs only numpy and is the "
            "air-gap-clean default; tesseract would need vendoring for the "
            f"appliance. Underlying import error: {exc}"
        )
    gray = _to_gray_frame(frame).astype(np.uint8)
    text = pytesseract.image_to_string(Image.fromarray(gray, mode="L"))
    # Reuse the same parser over tesseract's lines (no per-cell confidence
    # available from this path, so field_confidence stays empty).
    lines = text.splitlines()
    decoded = OSDDecodeResult(
        char_grid=[list(ln) for ln in lines],
        conf_grid=[[1.0] * len(ln) for ln in lines],
        lines=lines,
        line_confs=[[1.0] * len(ln) for ln in lines],
        mean_confidence=0.0,
    )
    return parse_telemetry(decoded, video_standard=video_standard)


# --------------------------------------------------------------------------
# Frame synthesis (for tests, calibration, and building OSD test cards).
# --------------------------------------------------------------------------
def render_osd_frame(
    text_rows: List[str],
    *,
    spec: Optional[OSDGridSpec] = None,
    font: Optional[GlyphFont] = None,
    cell_h: int = TEMPLATE_H,
    cell_w: int = TEMPLATE_W,
    bg: int = 0,
    fg_scale: float = 1.0,
) -> np.ndarray:
    """Render text rows onto a MAX7456-style character grid -> uint8 frame.

    Used to synthesize OSD frames (with the SAME glyphs the decoder matches
    against) for unit tests and to build operator test cards. Each string in
    `text_rows` may contain the SYM_* icon sentinels defined above.
    """
    font = font or default_font()
    if spec is None:
        spec = OSDGridSpec(cols=max((len(r) for r in text_rows), default=1),
                           rows=len(text_rows))
    frame = np.full((spec.rows * cell_h, spec.cols * cell_w), bg, dtype=np.float32)
    for r in range(min(spec.rows, len(text_rows))):
        row = text_rows[r]
        for c in range(min(spec.cols, len(row))):
            ch = row[c]
            tmpl = font.templates.get(ch)
            if tmpl is None:
                continue  # space / unknown glyph -> leave background
            cell = _resample_nn(tmpl, cell_h, cell_w) * fg_scale
            y0, x0 = r * cell_h, c * cell_w
            frame[y0:y0 + cell_h, x0:x0 + cell_w] = np.clip(cell, 0, 255)
    return frame.astype(np.uint8)


def add_gaussian_noise(frame: np.ndarray, sigma: float, seed: Optional[int] = None) -> np.ndarray:
    """Add zero-mean Gaussian noise (test/calibration helper). Analog video is
    noisy; this models that for robustness testing."""
    rng = np.random.default_rng(seed)
    noisy = frame.astype(np.float32) + rng.normal(0.0, sigma, size=frame.shape)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def self_test() -> None:
    """Lightweight embedded self-test (mirrors the pytest suite's core case).
    Run standalone via `python3 fpv_osd_ocr.py --self-test`."""
    rows = [
        "HAWK01",
        SYM_BATT + "16.2V " + "12.5A",
        SYM_SAT + "14 " + SYM_ALT + "150M",
        SYM_LAT + "28.613920",
        SYM_LON + "77.209000",
        SYM_RSSI + "92% 03:24",
        "ANGLE",
    ]
    spec = OSDGridSpec(cols=30, rows=13)
    frame = render_osd_frame(rows, spec=spec)
    t = extract_osd_telemetry(frame, video_standard="NTSC", grid_spec=spec)
    assert t.craft_name == "HAWK01", t.craft_name
    assert t.battery_voltage == 16.2, t.battery_voltage
    assert t.current == 12.5, t.current
    assert t.sats == 14, t.sats
    assert t.altitude == 150.0, t.altitude
    assert abs((t.gps_lat or 0) - 28.613920) < 1e-4, t.gps_lat
    assert abs((t.gps_lon or 0) - 77.209000) < 1e-4, t.gps_lon
    assert t.rssi == 92, t.rssi
    assert t.timer == "03:24", t.timer
    assert t.flight_mode == "ANGLE", t.flight_mode
    print("fpv_osd_ocr self_test: ALL PASSED")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--self-test", action="store_true", help="run the embedded self-test")
    args = ap.parse_args()
    if args.self_test:
        self_test()
    else:
        ap.print_help()
