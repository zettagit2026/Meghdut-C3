#!/usr/bin/env python3
"""Per-pixel radiometric temperature extraction from FLIR-calibrated R-JPEG
thermal images (task #64/#83 close-out, AI Engineer, 2026-07-25).

=============================================================================
WHAT THIS IS (and is not)
=============================================================================
This is a SECOND, independent thermal-analysis path alongside
thermal_bridge.py's torchvision object-detection scaffold. It does NOT
detect drones. It converts a raw 14/16-bit thermal sensor value per pixel
into an actual temperature (degrees C) using the sensor manufacturer's
published radiometric calibration formula (Planck's law applied to a
calibrated microbolometer's raw digital counts) plus per-image calibration
constants that FLIR-format cameras embed in the file's metadata. This is
useful on its own (e.g. "is anything in frame above ambient/engine/battery
temperature" without needing a trained detector at all) and could later
feed a hot-spot-based detection heuristic as an alternative/complement to
thermal_bridge.py's model-based path.

STATUS: MATH-VERIFIED ONLY. NOT VALIDATED AGAINST A REAL CAMERA FILE.
No FLIR-calibrated R-JPEG sample exists anywhere in this project (no
thermal camera hardware has been purchased -- see
CAMERA_THERMAL_ACOUSTIC_SCOPE.md Sec.3 BOM). Every function here that
touches a real image file (extract_flir_calibration_constants,
extract_raw_thermal_array, radiometric_temperature_array) has NOT been
exercised against real camera output in this session -- there is no file to
exercise it against. Only the pure math (raw_to_temperature_celsius,
raw_array_to_temperature_celsius) has been tested, against hand-computed
values derived independently from the formula below, not against any
camera's ground truth. Treat this exactly like thermal_bridge.py's own
honesty pattern: do not read a passing test suite here as "this extracts
correct temperatures from real FLIR files" -- it means "the formula is
implemented correctly and the plumbing around a real file is shaped
correctly."

=============================================================================
SOURCE OF THE FORMULA / TAGS, AND WHY IT IS REIMPLEMENTED HERE (LICENSING)
=============================================================================
This technique -- FLIR raw-to-temperature conversion via Planck's law using
per-image calibration constants stored in the file's FLIR metadata segment
-- was evaluated in this project on 2026-07-26 by reference to
torkian/Drone-Thermal (https://github.com/torkian/Drone-Thermal), which
implements this among other camera-vendor paths (its DJI path separately
wraps DJI's official SDK via Docker -- irrelevant here, skipped).

LICENSE CHECK (done this session, per the 2026-07-26 evaluation's flagged
open item "claimed permissive but no SPDX/LICENSE file confirmed"):
that repo's README states "Open source - use freely for any purpose" but
the repository has NO LICENSE file and NO SPDX identifier anywhere in it.
An informal "use freely" statement in a README is not a license grant this
project can safely rely on for verbatim code reuse -- this is the exact
same situation as the DSHOT/HoTT precedent elsewhere in this codebase
(dshot_telemetry_parser.py / graupner_hott_parser.py), where no-clear-
license source was NOT copied; instead the underlying protocol/physics was
reimplemented from public documentation. That precedent is followed here:

  - Planck's law is a public law of physics (1900), not copyrightable
    expression. The general form relating spectral radiance to temperature
    is textbook physics, independent of any one project's code.
  - The specific SIMPLIFIED single-band inversion used by FLIR-format
    camera tooling --
        T_celsius = B / ln(R1 / (R2 * (raw + O)) + F) - 273.15
    -- is FLIR Systems' own published camera-calibration convention. The
    tag names (PlanckR1, PlanckR2, PlanckB, PlanckF, PlanckO) are FLIR's own
    metadata field names, independently documented and widely reused across
    multiple unrelated open tools that all cite FLIR's own format
    (e.g. Exiftool's public FLIR tag documentation, the R-package
    "Thermimage" by gtatters, and flirpy) -- i.e. this is a documented
    external fact about how FLIR cameras label their own calibration data,
    not expressive code copied from torkian/Drone-Thermal. No code from
    that repository was read line-by-line or transcribed; the formula and
    tag names below were independently corroborated against this multi-
    source documentation trail before being typed here.
  - What IS reimplemented independently in this file: the Python structure
    (dataclass, exiftool subprocess invocation, numpy vectorization, error
    handling) is original to this project, written to this project's own
    conventions (mirrors gamutrf_infer.py's "load once, real errors,
    weights_only-style honesty" pattern), not copied from any source.

KNOWN SIMPLIFICATION (be honest about this, not just the license): the
formula above is the SIMPLE single-band radiometric inversion. FLIR's own
more complete pipeline also applies an atmospheric-transmission correction
(using Emissivity, ObjectDistance, RelativeHumidity,
ReflectedApparentTemperature, AtmosphericTemperature, IRWindowTransmission
metadata fields) before the Planck inversion, for scenes with meaningful
atmospheric path length or off-unity emissivity. This module does NOT
implement that correction -- it is out of scope for this pass and would
need real-world validation data (a real camera + known-temperature
reference targets) to verify, which does not exist here. Temperatures
computed by this module are therefore an uncorrected first-order estimate,
not FLIR-tool-grade absolute accuracy. This limitation is intentional and
documented, not an oversight.

=============================================================================
EXIF/METADATA EXTRACTION MECHANISM
=============================================================================
FLIR's calibration constants and the raw 16-bit thermal sensor plane are
NOT standard EXIF IFD tags readable by Pillow's plain .getexif() -- they
live in a FLIR-proprietary binary "FFF"-format record embedded in the
JPEG's APP1/APP3 segment. The de facto standard tool for parsing this
format (used by exiftool itself, flirpy, and effectively every other open
FLIR-parsing tool, because reverse-engineering that binary format from
scratch is its own multi-week undertaking) is Phil Harvey's `exiftool`
CLI. This project has no existing exif dependency (verified: no piexif/
exifread/exiftool usage anywhere else in field-bridge/, and `exiftool` is
NOT installed on this Mac dev copy -- verified via `which exiftool`).
Rather than add an unverified pure-Python FLIR-binary-format parser (high
risk of silently-wrong output with no real file to validate against), this
module shells out to the real `exiftool` CLI, which must be installed
separately (e.g. `brew install exiftool` on macOS, `apt install
libimage-exiftool-perl` on Debian/Ubuntu -- both distribute exiftool under
the Perl Artistic License/GPL dual license, which is a separate CLI tool
invocation, not a code dependency linked into this project). If exiftool is
not on PATH, functions here raise a clear RuntimeError rather than
guessing/fabricating calibration constants -- same "no real number, don't
fake one" discipline as thermal_bridge.py's bearing_deg_placeholder.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

import numpy as np

EXIFTOOL_MISSING_MSG = (
    "exiftool is not installed / not on PATH. FLIR-format raw thermal data "
    "and Planck calibration constants live in a proprietary binary metadata "
    "segment that requires exiftool (or an equivalent FLIR-format parser) "
    "to read -- Pillow's plain EXIF reader cannot see them. Install "
    "exiftool (e.g. `brew install exiftool` / `apt install "
    "libimage-exiftool-perl`) before calling functions in this module "
    "against a real file."
)

# The exact FLIR metadata field names this module reads, as documented by
# exiftool's public FLIR tag reference and cross-corroborated against the
# Thermimage R package and flirpy (see module docstring's license section
# for why these are treated as documented external facts, not code copied
# from any one source).
FLIR_PLANCK_TAGS = ("PlanckR1", "PlanckR2", "PlanckB", "PlanckF", "PlanckO")


@dataclass
class FlirPlanckConstants:
    """One R-JPEG's per-image Planck calibration constants, as embedded by
    the camera at capture time (varies with the camera's integration time /
    gain state, hence per-image rather than a fixed constant per camera
    model)."""
    r1: float
    r2: float
    b: float
    f: float
    o: float


def _require_exiftool() -> str:
    path = shutil.which("exiftool")
    if path is None:
        raise RuntimeError(EXIFTOOL_MISSING_MSG)
    return path


def raw_to_temperature_celsius(raw: float, constants: FlirPlanckConstants) -> float:
    """Convert ONE raw 14/16-bit thermal sensor digital count to a
    temperature in degrees Celsius, via FLIR's published single-band
    Planck-law inversion:

        T_kelvin = B / ln(R1 / (R2 * (raw + O)) + F)
        T_celsius = T_kelvin - 273.15

    This is the simplified form WITHOUT atmospheric-transmission/emissivity
    correction -- see module docstring's "KNOWN SIMPLIFICATION" section.

    Pure math, no file I/O -- this is the part of this module that can be
    (and is, in test_thermal_radiometric.py) verified independently by
    hand-computing the same formula, since Planck's law and this inversion
    are public, well-defined arithmetic, not something that requires a real
    camera file to check.
    """
    import math
    return constants.b / math.log(
        constants.r1 / (constants.r2 * (raw + constants.o)) + constants.f
    ) - 273.15


def raw_array_to_temperature_celsius(raw: np.ndarray, constants: FlirPlanckConstants) -> np.ndarray:
    """Vectorized (numpy) form of raw_to_temperature_celsius over a whole
    raw thermal frame -- same formula, applied per-pixel. Returns a float64
    array the same shape as `raw`."""
    raw = np.asarray(raw, dtype=np.float64)
    denom = constants.r2 * (raw + constants.o)
    return constants.b / np.log(constants.r1 / denom + constants.f) - 273.15


def extract_flir_calibration_constants(image_path: str) -> FlirPlanckConstants:
    """Read the five FLIR Planck calibration tags from a real R-JPEG file's
    metadata via exiftool. Raises RuntimeError if exiftool is not
    installed, or ValueError if the file does not carry FLIR Planck tags
    (i.e. it is not a FLIR-format radiometric R-JPEG at all).

    NOT EXERCISED AGAINST A REAL FILE in this session -- no FLIR-calibrated
    R-JPEG sample exists in this project. See module docstring.
    """
    exiftool = _require_exiftool()
    proc = subprocess.run(
        [exiftool, "-j"] + [f"-{tag}" for tag in FLIR_PLANCK_TAGS] + [image_path],
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(proc.stdout)[0]
    missing = [tag for tag in FLIR_PLANCK_TAGS if tag not in parsed]
    if missing:
        raise ValueError(
            f"{image_path} is missing FLIR Planck calibration tag(s) "
            f"{missing} -- this is not a FLIR-format radiometric R-JPEG "
            "(or exiftool could not parse its FLIR metadata segment)."
        )
    return FlirPlanckConstants(
        r1=float(parsed["PlanckR1"]),
        r2=float(parsed["PlanckR2"]),
        b=float(parsed["PlanckB"]),
        f=float(parsed["PlanckF"]),
        o=float(parsed["PlanckO"]),
    )


def extract_raw_thermal_array(image_path: str) -> np.ndarray:
    """Extract the embedded raw 16-bit thermal sensor plane from a FLIR
    R-JPEG via `exiftool -b -RawThermalImage`, which is itself a PNG or
    raw-TIFF blob embedded inside the JPEG's metadata (FLIR stores the
    actual per-pixel sensor data this way, separate from the visible/
    display JPEG image data). Decodes that extracted blob with Pillow
    (already a project dependency) into a 2D numpy array of raw digital
    counts.

    NOT EXERCISED AGAINST A REAL FILE in this session -- no FLIR-calibrated
    R-JPEG sample exists in this project. See module docstring.
    """
    exiftool = _require_exiftool()
    import io
    from PIL import Image

    proc = subprocess.run(
        [exiftool, "-b", "-RawThermalImage", image_path],
        capture_output=True, check=True,
    )
    if not proc.stdout:
        raise ValueError(
            f"{image_path} has no embedded RawThermalImage tag -- this is "
            "not a FLIR-format radiometric R-JPEG."
        )
    img = Image.open(io.BytesIO(proc.stdout))
    return np.array(img)


def radiometric_temperature_array(image_path: str) -> np.ndarray:
    """End-to-end: read calibration constants + raw thermal plane from a
    real FLIR R-JPEG and return a per-pixel temperature-in-Celsius array
    the same shape as the raw sensor plane.

    NOT EXERCISED END-TO-END against a real file in this session -- see
    module docstring. This function simply composes
    extract_flir_calibration_constants + extract_raw_thermal_array +
    raw_array_to_temperature_celsius, each of which is documented above.
    """
    constants = extract_flir_calibration_constants(image_path)
    raw = extract_raw_thermal_array(image_path)
    return raw_array_to_temperature_celsius(raw, constants)
