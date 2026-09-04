#!/usr/bin/env python3
"""Unit tests for control_link_classifier.py -- the over-the-air control-link
FAMILY heuristic. No hardware, no network.

These assert the classifier's HONEST behavior: family-level calls only, the
right confidence tier for each evidence level, and an explicit "unknown" (never
fabricated) for out-of-band contacts.
"""
import control_link_classifier as clc


def test_dji_wideband_2g4():
    r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=18.0)
    assert r["link_family"] == "wideband_video_2g4_5g8"
    assert r["confidence_type"] == "advisory_only"  # band+BW only, no decode


def test_dji_wideband_5g8():
    r = clc.classify_control_link(center_freq_ghz=5.8, bandwidth_mhz=20.0)
    assert r["link_family"] == "wideband_video_2g4_5g8"


def test_2g4_narrow_with_fhss_is_heuristic_binary():
    r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5,
                                  fhss_hop_consistent=True)
    assert r["link_family"] == "hobby_rc_2g4"
    assert r["confidence_type"] == "heuristic_binary"


def test_2g4_narrow_without_fhss_is_advisory_only():
    r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5)
    assert r["link_family"] == "hobby_rc_2g4"
    assert r["confidence_type"] == "advisory_only"


def test_subghz_fhss_is_lrs():
    r = clc.classify_control_link(center_freq_ghz=0.915, bandwidth_mhz=1.0,
                                  fhss_hop_consistent=True)
    assert r["link_family"] == "lrs_subghz"
    assert r["confidence_type"] == "heuristic_binary"


def test_subghz_no_fhss_is_ism_advisory():
    r = clc.classify_control_link(center_freq_ghz=0.433, bandwidth_mhz=0.5)
    assert r["link_family"] == "subghz_ism"
    assert r["confidence_type"] == "advisory_only"


def test_mavlink_sik_protocol_confirmed_is_protocol_verified():
    r = clc.classify_control_link(center_freq_ghz=0.915, protocol="MAVLink",
                                  protocol_confirmed=True)
    assert r["link_family"] == "mavlink_sik"
    assert r["confidence_type"] == "protocol_verified"


def test_sik_radio_source_hint():
    r = clc.classify_control_link(center_freq_ghz=0.915, source="SIK_RADIO")
    assert r["link_family"] == "mavlink_sik"
    # source tag only, not a fresh decode -> heuristic tier, not protocol_verified
    assert r["confidence_type"] == "heuristic_binary"


def test_dji_tag_short_circuits():
    r = clc.classify_control_link(center_freq_ghz=2.44, protocol="OcuSync 2.0")
    assert r["link_family"] == "dji_ocusync"


def test_out_of_band_is_unknown_never_fabricated():
    r = clc.classify_control_link(center_freq_ghz=1.2, bandwidth_mhz=5.0)
    assert r["link_type"] == "unknown"
    assert r["link_family"] is None


def test_missing_frequency_is_unknown():
    r = clc.classify_control_link(center_freq_ghz=None)
    assert r["link_type"] == "unknown"


def test_evidence_is_echoed_back():
    r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5,
                                  fhss_hop_consistent=False)
    assert r["evidence"]["center_freq_ghz"] == 2.44
    assert r["evidence"]["bandwidth_mhz"] == 1.5
    assert r["evidence"]["fhss_hop_consistent"] is False
