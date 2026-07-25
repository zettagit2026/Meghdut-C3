#!/usr/bin/env python3
"""Unit tests for consumer_iot_signatures.py (task #72: 433/868MHz
consumer-IoT device flagging using RF-Protocol-Database's real catalogue).

Covers: band-bucketing of the bundled catalogue, summary/annotation
generation, and hackrf_rx.py's integration point (annotate-not-conflict with
the task #88 ELRS/Crossfire hop-consistency heuristic).

Run: pytest field-bridge/test_consumer_iot_signatures.py -v
"""
import json
import os

import pytest

from consumer_iot_signatures import (
    CONSUMER_IOT_BAND_RANGES_MHZ,
    ConsumerIotCatalogue,
    _band_for_frequency_hz,
    consumer_iot_annotation,
    get_default_catalogue,
    load_band_catalogue,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLED_CATALOGUE_PATH = os.path.join(
    REPO_ROOT, "frontend", "src", "data", "rf_protocols_db.json")


def test_bundled_catalogue_file_exists():
    """This module reuses the SAME bundle task #39/#82 already ship for the
    Protocol Library page -- it must not require a second copy/fetch."""
    assert os.path.exists(BUNDLED_CATALOGUE_PATH)


def test_band_ranges_match_hackrf_rx_bands():
    """Must track LRS-433 (420-450MHz) / SRD-868 (863-870MHz) from
    hackrf_rx.py's EXTRA_BANDS_MHZ exactly."""
    assert CONSUMER_IOT_BAND_RANGES_MHZ["LRS-433"] == (420.0, 450.0)
    assert CONSUMER_IOT_BAND_RANGES_MHZ["SRD-868"] == (863.0, 870.0)


def test_band_for_frequency_hz():
    assert _band_for_frequency_hz(433_920_000) == "LRS-433"
    assert _band_for_frequency_hz(868_950_000) == "SRD-868"
    assert _band_for_frequency_hz(915_000_000) is None
    assert _band_for_frequency_hz(None) is None
    assert _band_for_frequency_hz(0) is None


def test_load_band_catalogue_real_data_has_devices_in_both_bands():
    """Sanity check against the real, unmodified catalogue: both bands
    should have a non-trivial number of cataloged consumer-IoT devices."""
    buckets = load_band_catalogue(BUNDLED_CATALOGUE_PATH)
    assert set(buckets.keys()) == {"LRS-433", "SRD-868"}
    assert len(buckets["LRS-433"]) > 20
    assert len(buckets["SRD-868"]) > 20


def test_load_band_catalogue_missing_file_returns_empty_not_exception():
    """Advisory-only feature: a missing/corrupt catalogue must degrade to
    'no annotation available', never raise and break the sweep loop."""
    buckets = load_band_catalogue("/nonexistent/path/does/not/exist.json")
    assert buckets == {}


def test_catalogue_annotation_for_lrs433():
    cat = ConsumerIotCatalogue(BUNDLED_CATALOGUE_PATH)
    assert cat.has_devices("LRS-433")
    ann = cat.annotation_for_band("LRS-433")
    assert ann is not None
    assert ann["consumer_iot_candidate"] is True
    assert ann["confidence_type"] == "advisory_only"
    assert "433MHz" in ann["label"]
    assert ann["catalogue_device_count"] > 0
    assert len(ann["catalogue_top_categories"]) >= 1
    assert isinstance(ann["catalogue_example_names"], list)
    assert "FHSS hop-consistency" in ann["notes"]


def test_catalogue_annotation_for_srd868():
    cat = ConsumerIotCatalogue(BUNDLED_CATALOGUE_PATH)
    ann = cat.annotation_for_band("SRD-868")
    assert ann is not None
    assert "868MHz" in ann["label"]
    assert ann["catalogue_device_count"] > 0


def test_annotation_none_for_band_with_no_catalogue_coverage():
    cat = ConsumerIotCatalogue(BUNDLED_CATALOGUE_PATH)
    assert cat.annotation_for_band("DJI-2G4") is None
    assert cat.summarize("DJI-2G4") is None


def test_annotation_none_when_catalogue_fails_to_load():
    cat = ConsumerIotCatalogue("/nonexistent/path.json")
    assert cat.annotation_for_band("LRS-433") is None


def test_summarize_top_categories_and_examples_are_consistent():
    cat = ConsumerIotCatalogue(BUNDLED_CATALOGUE_PATH)
    summary = cat.summarize("LRS-433", top_n=3, examples_n=3)
    assert summary["device_count"] == len(cat.buckets["LRS-433"])
    # top_categories counts must sum to <= device_count and be sorted desc
    counts = [c for _, c in summary["top_categories"]]
    assert counts == sorted(counts, reverse=True)
    assert len(summary["example_names"]) <= 3


def test_consumer_iot_annotation_module_function_uses_default_singleton():
    ann1 = consumer_iot_annotation("LRS-433")
    ann2 = consumer_iot_annotation("LRS-433")
    assert ann1 is not None and ann2 is not None
    assert ann1["catalogue_device_count"] == ann2["catalogue_device_count"]
    # singleton should be the same object across calls
    assert get_default_catalogue() is get_default_catalogue()


def test_synthetic_catalogue_frequency_edge_boundaries(tmp_path):
    """Boundary check: 420.0 and 450.0 MHz are inclusive edges for LRS-433,
    863.0/870.0 for SRD-868; just outside must not be bucketed."""
    synthetic = {
        "version": "test",
        "total_devices": 6,
        "devices": [
            {"name": "edge-low-433", "frequency": 420_000_000, "category": "Test"},
            {"name": "edge-high-433", "frequency": 450_000_000, "category": "Test"},
            {"name": "just-outside-433", "frequency": 419_999_000, "category": "Test"},
            {"name": "edge-low-868", "frequency": 863_000_000, "category": "Test"},
            {"name": "edge-high-868", "frequency": 870_000_000, "category": "Test"},
            {"name": "just-outside-868", "frequency": 870_100_000, "category": "Test"},
        ],
    }
    p = tmp_path / "synthetic_rf_protocols.json"
    p.write_text(json.dumps(synthetic))
    buckets = load_band_catalogue(str(p))
    assert len(buckets["LRS-433"]) == 2
    assert len(buckets["SRD-868"]) == 2
    names_433 = {d["name"] for d in buckets["LRS-433"]}
    names_868 = {d["name"] for d in buckets["SRD-868"]}
    assert names_433 == {"edge-low-433", "edge-high-433"}
    assert names_868 == {"edge-low-868", "edge-high-868"}


def test_synthetic_top_category_and_examples_reflect_most_common(tmp_path):
    synthetic = {
        "version": "test",
        "total_devices": 4,
        "devices": [
            {"name": "GarageA", "frequency": 433_920_000, "category": "Gate & Garage Remotes"},
            {"name": "GarageB", "frequency": 433_920_000, "category": "Gate & Garage Remotes"},
            {"name": "GarageC", "frequency": 433_920_000, "category": "Gate & Garage Remotes"},
            {"name": "WeatherA", "frequency": 433_920_000, "category": "Weather Stations & Sensors"},
        ],
    }
    p = tmp_path / "synthetic2.json"
    p.write_text(json.dumps(synthetic))
    cat = ConsumerIotCatalogue(str(p))
    ann = cat.annotation_for_band("LRS-433")
    assert "Gate & Garage Remotes" in ann["label"]
    assert ann["catalogue_top_categories"][0]["category"] == "Gate & Garage Remotes"
    assert ann["catalogue_top_categories"][0]["count"] == 3
    assert set(ann["catalogue_example_names"]).issubset({"GarageA", "GarageB", "GarageC"})


def test_hackrf_rx_integration_annotates_only_when_not_hop_consistent():
    """Integration check against hackrf_rx.py: HOP_TRACKED_BANDS must match
    this module's band coverage, and the annotation must be available for
    both tracked bands (so the `not hop_consistent` gate in hackrf_rx.py's
    main() loop always has something real to annotate with when it fires)."""
    from hackrf_rx import HOP_TRACKED_BANDS

    assert set(HOP_TRACKED_BANDS) == set(CONSUMER_IOT_BAND_RANGES_MHZ.keys())
    for band in HOP_TRACKED_BANDS:
        assert consumer_iot_annotation(band) is not None
