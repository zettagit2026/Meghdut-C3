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


# --- Honest-label guards (item 4) -------------------------------------------
# The 2.4 GHz set stays FAMILY-LEVEL: never a named-protocol determination, and
# SiK/MAVLink is only claimed on a real decode, never from an energy tag.

_FORBIDDEN_2G4_PROTOCOL_NAMES = (
    "ELRS", "FrSky", "FlySky", "Flysky", "Spektrum", "DSMX", "DSM2", "AFHDS",
)


def test_2g4_link_type_never_names_a_specific_protocol():
    """Neither the advisory nor the (corroborated) 2.4 GHz call may name a
    specific protocol -- the energy sweep cannot separate them."""
    for fhss in (None, False, True):
        r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5,
                                      fhss_hop_consistent=fhss)
        assert r["link_family"] == "hobby_rc_2g4"
        for name in _FORBIDDEN_2G4_PROTOCOL_NAMES:
            assert name not in r["link_type"], f"{name!r} leaked into 2.4 GHz link_type"


def test_2g4_family_label_text():
    r = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5)
    assert r["link_type"] == "2.4 GHz FHSS hobby-RC control link (family)"


def test_sik_energy_tag_without_decode_is_not_mavlink():
    """A 'SiK/MAVLink' protocol STRING on an energy-sweep detection (source
    SIK_RF_HEURISTIC, not confirmed) must NOT be called MAVLink -- it degrades
    honestly to a 915 MHz continuous-telemetry candidate."""
    r = clc.classify_control_link(center_freq_ghz=0.915, protocol="SiK/MAVLink",
                                  source="SIK_RF_HEURISTIC", protocol_confirmed=False)
    assert r["link_family"] == "subghz_ism"
    assert r["confidence_type"] == "advisory_only"
    assert r["link_type"] == "915 MHz continuous telemetry (candidate)"


def test_subghz_lrs_label_is_family_class():
    r = clc.classify_control_link(center_freq_ghz=0.915, fhss_hop_consistent=True)
    assert r["link_family"] == "lrs_subghz"
    assert "ELRS-Crossfire-class" in r["link_type"]


def test_sik_energy_hop_corroborated_in_900_band_is_lrs():
    """Hop corroboration in 902-928 promotes to the sub-GHz LRS family even
    with the coarse SiK/MAVLink tag present (the tag isn't a decode)."""
    r = clc.classify_control_link(center_freq_ghz=0.909, protocol="SiK/MAVLink",
                                  source="SIK_RF_HEURISTIC", fhss_hop_consistent=True)
    assert r["link_family"] == "lrs_subghz"
    assert r["confidence_type"] == "heuristic_binary"


def test_dji_ambiguous_energy_band_default_falls_through_to_divider():
    """The coarse 2.4 GHz energy band-default ('DJI Mini (candidate)' +
    'OcuSync/Wi-Fi') must NOT be called DJI -- the '/Wi-Fi' says the sweep can't
    tell OcuSync from Wi-Fi. A NARROW occupied bandwidth reaches hobby_rc_2g4;
    a WIDE one reaches wideband video. Either way, never a DJI overclaim."""
    narrow = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=1.5,
                                       protocol="OcuSync/Wi-Fi", model="DJI Mini (candidate)",
                                       source="HACKRF")
    assert narrow["link_family"] == "hobby_rc_2g4"
    wide = clc.classify_control_link(center_freq_ghz=2.44, bandwidth_mhz=20.0,
                                     protocol="OcuSync/Wi-Fi", model="DJI Mini (candidate)",
                                     source="HACKRF")
    assert wide["link_family"] == "wideband_video_2g4_5g8"


def test_dji_droneid_crc_decode_is_protocol_verified():
    """A genuine DroneID CRC decode still earns dji_ocusync protocol_verified --
    the honesty guard tightens the ambiguous energy default, not real decodes."""
    r = clc.classify_control_link(center_freq_ghz=2.44, protocol="DroneID",
                                  protocol_confirmed=True)
    assert r["link_family"] == "dji_ocusync"
    assert r["confidence_type"] == "protocol_verified"
