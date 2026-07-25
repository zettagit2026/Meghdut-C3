"""Consumer-IoT (non-drone) device flagging for the LRS-433 / SRD-868 bands
(task #72).

Context: tasks #51/#88 added LRS-433 (420-450MHz) and SRD-868 (863-870MHz) to
hackrf_rx.py's band sweep to catch long-range drone control links
(ELRS/Crossfire-class LRS radios), and task #88 added an FHSS
hop-interval-consistency heuristic (see ELRS_HOP_RATE_RANGE_HZ /
classify_hop_interval() / update_hop_track() in hackrf_rx.py) that gives
circumstantial evidence FOR that ELRS/Crossfire conclusion.

But both bands are also busy, legitimate consumer-IoT ISM spectrum --
garage/gate remotes, weather stations, utility meters, tyre-pressure sensors,
etc. Those devices are NOT frequency-hopping (they sit on one fixed carrier
and transmit short OOK/FSK bursts), so a confirmed LRS-433/SRD-868 contact
that does NOT show the FHSS hop-consistency signature is at least as
plausibly one of these ordinary ambient devices as an actual LRS control
link. This module gives that "not FHSS" case an honest, catalogue-grounded
label instead of leaving it as an unqualified "LRS/telemetry craft
(candidate)" flag.

Data source: this reuses the SAME 692-device RF-Protocol-Database v4.0.0
catalogue already bundled for task #39/#82's Protocol Library page, at
frontend/src/data/rf_protocols_db.json -- no re-fetch, no second parse of the
raw RF-Protocol-Database repo, no duplicated catalogue-loading logic.

IMPORTANT honesty note: the catalogue entries that fall in these two bands
do NOT carry decoded OOK/FSK pulse-timing fields (short_width/long_width/
sync_width are None for every 433/868MHz row as of v4.0.0 -- verified by
inspection, see test_consumer_iot_signatures.py). That means this module
cannot do a real per-device timing-signature match; it can only tell you
"this band has N catalogued consumer-IoT devices, mostly in category X" and
offer that as a plausible non-drone explanation. Anything claiming a named
single device (e.g. "this is a Chamberlain garage remote") would be an
overclaim not supported by the data -- so the label this module produces is
deliberately phrased as an example from the band's catalogue, not a decoded
identification, and confidence_type is "advisory_only" (weaker than the
persistence-confirmed "heuristic_binary" used elsewhere in hackrf_rx.py).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Dict, List, Optional, Tuple

# Path to the already-bundled catalogue (relative to this file: field-bridge/
# -> ../frontend/src/data/rf_protocols_db.json). Overridable via env var for
# tests / alternate deployments.
_DEFAULT_CATALOGUE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "frontend", "src", "data", "rf_protocols_db.json",
)

# Band ranges, in MHz -- MUST match EXTRA_BANDS_MHZ / HOP_TRACKED_BANDS in
# hackrf_rx.py (LRS-433: 420-450MHz, SRD-868: 863-870MHz). Duplicated here
# (rather than imported) to keep this module import-independent of
# hackrf_rx.py; hackrf_rx.py is the caller, not the other way around.
CONSUMER_IOT_BAND_RANGES_MHZ: Dict[str, Tuple[float, float]] = {
    "LRS-433": (420.0, 450.0),
    "SRD-868": (863.0, 870.0),
}


def _band_for_frequency_hz(freq_hz: Optional[float]) -> Optional[str]:
    if not freq_hz:
        return None
    mhz = freq_hz / 1e6
    for band, (lo, hi) in CONSUMER_IOT_BAND_RANGES_MHZ.items():
        if lo <= mhz <= hi:
            return band
    return None


def load_band_catalogue(path: Optional[str] = None) -> Dict[str, List[dict]]:
    """Load the bundled RF-Protocol-Database catalogue and bucket devices by
    LRS-433 / SRD-868 band membership (by their `frequency` field, in Hz).

    Returns {} for a band with no catalogue devices, and returns {} entirely
    (not an exception) if the catalogue file is missing/unreadable -- this is
    an additive, advisory-only feature, so its absence must never break the
    core sweep/ingest loop in hackrf_rx.py.
    """
    catalogue_path = path or _DEFAULT_CATALOGUE_PATH
    try:
        with open(catalogue_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}

    devices = data.get("devices", [])
    buckets: Dict[str, List[dict]] = {band: [] for band in CONSUMER_IOT_BAND_RANGES_MHZ}
    for dev in devices:
        band = _band_for_frequency_hz(dev.get("frequency"))
        if band is not None:
            buckets[band].append(dev)
    return buckets


class ConsumerIotCatalogue:
    """Cached, queryable view over the band-bucketed catalogue."""

    def __init__(self, path: Optional[str] = None):
        self.buckets = load_band_catalogue(path)

    def has_devices(self, band: str) -> bool:
        return bool(self.buckets.get(band))

    def summarize(self, band: str, top_n: int = 3, examples_n: int = 3) -> Optional[dict]:
        """Return a summary dict for `band`, or None if the catalogue has no
        entries in that band (e.g. catalogue failed to load, or a future
        band with no known consumer-IoT devices).

        {
          "device_count": int,
          "top_categories": [(category, count), ...],   # up to top_n
          "example_names": [str, ...],                   # up to examples_n
        }
        """
        devs = self.buckets.get(band)
        if not devs:
            return None
        categories = Counter(d.get("category") or "Unknown" for d in devs)
        top_categories = categories.most_common(top_n)
        # Prefer named examples from the most common category so the label
        # reads coherently (e.g. "Gate & Garage Remotes: CAME, Princeton").
        top_cat_name = top_categories[0][0] if top_categories else None
        examples = [
            d.get("name") for d in devs
            if (d.get("category") or "Unknown") == top_cat_name and d.get("name")
        ][:examples_n]
        if not examples:
            examples = [d.get("name") for d in devs if d.get("name")][:examples_n]
        return {
            "device_count": len(devs),
            "top_categories": top_categories,
            "example_names": examples,
        }

    def annotation_for_band(self, band: str) -> Optional[dict]:
        """Build the honest, catalogue-grounded annotation dict this module
        contributes to a confirmed hackrf_rx.py detection. Returns None if
        there's no catalogue coverage for `band` (nothing to annotate with).
        """
        summary = self.summarize(band)
        if summary is None:
            return None

        top_cat_name, top_cat_count = summary["top_categories"][0]
        examples_str = ", ".join(summary["example_names"]) if summary["example_names"] else "n/a"
        band_mhz_label = "433MHz" if band == "LRS-433" else "868MHz"

        return {
            "consumer_iot_candidate": True,
            "label": (
                f"Consumer IoT ({band_mhz_label}) — possible match: {top_cat_name} "
                f"(e.g. {examples_str})"
            ),
            "confidence_type": "advisory_only",  # band-overlap + non-FHSS only, no decoded timing match
            "catalogue_device_count": summary["device_count"],
            "catalogue_top_categories": [
                {"category": cat, "count": count} for cat, count in summary["top_categories"]
            ],
            "catalogue_example_names": summary["example_names"],
            "notes": (
                f"{summary['device_count']} devices in the RF-Protocol-Database "
                f"v4.0.0 catalogue (task #39/#82) fall in the {band} band "
                f"({band_mhz_label} range), most commonly '{top_cat_name}' "
                f"({top_cat_count} of them, e.g. {examples_str}). This detection did "
                "NOT show the ELRS/Crossfire-class FHSS hop-consistency signature "
                "(see hackrf_rx.py task #88), so it is at least as plausible as "
                "ordinary ambient consumer-IoT traffic as it is a long-range control "
                "link. This is a band-overlap + absence-of-hopping heuristic only -- "
                "the catalogue entries in this band do not carry decoded OOK/FSK "
                "pulse-timing data, so no specific device was identified by signal "
                "decode; the named examples above are illustrative catalogue "
                "members, not a confirmed identification."
            ),
        }


# Module-level singleton so hackrf_rx.py's per-cycle loop doesn't reload/
# re-parse the JSON file on every sweep cycle.
_default_catalogue: Optional[ConsumerIotCatalogue] = None


def get_default_catalogue() -> ConsumerIotCatalogue:
    global _default_catalogue
    if _default_catalogue is None:
        _default_catalogue = ConsumerIotCatalogue()
    return _default_catalogue


def consumer_iot_annotation(band: str) -> Optional[dict]:
    """Convenience wrapper hackrf_rx.py calls per confirmed LRS-433/SRD-868
    detection that lacks FHSS hop-consistency evidence. See
    ConsumerIotCatalogue.annotation_for_band() for the honesty caveats.
    """
    return get_default_catalogue().annotation_for_band(band)
