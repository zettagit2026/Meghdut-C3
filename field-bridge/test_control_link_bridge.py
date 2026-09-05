#!/usr/bin/env python3
"""Unit tests for control_link_bridge.classification_for_detection() -- the
LIVE wiring between the detection plane's observable RF fields and the
over-the-air control-link FAMILY classifier.

These deliberately exercise the classifier THROUGH the bridge, on detection
dicts shaped exactly as hackrf_rx.py's confirmed-detection ingest builds them
(band-width `bandwidth_mhz` + occupied `occupied_bw_mhz` + `rf_signature_only`
hop evidence + `source`/`protocol` tags), NOT synthetic classifier-only inputs.
They are the regression guard for the two wiring bugs that previously stranded
the whole family classifier on advisory_only:

  * hop evidence: hackrf_rx.py writes the ELRS/Crossfire hop signature as
    `rf_signature_only`, which the bridge must bridge into the classifier's
    `fhss_hop_consistent` input (it used to read a never-written key).
  * occupied bandwidth: hackrf_rx.py's `bandwidth_mhz` is the whole sweep-band
    width; the bridge must feed the narrower `occupied_bw_mhz` into the
    wide/narrow divider so the narrowband hobby_rc_2g4 branch is reachable.

No hardware, no network -- classification_for_detection() is a pure function
over a detection dict.
"""
import control_link_bridge as clb

# Any 2.4 GHz per-protocol name must NEVER appear as a determination -- the
# passive energy sweep physically cannot separate these, so only the FAMILY is
# ever asserted. Guard the actual classifier output against all of them.
_FORBIDDEN_2G4_PROTOCOL_NAMES = (
    "ELRS", "FrSky", "FlySky", "Flysky", "Spektrum", "DSMX", "DSM2", "AFHDS",
)


def test_narrowband_2g4_reaches_hobby_rc_family_via_occupied_bw():
    """A confirmed 2.4 GHz contact whose OCCUPIED bandwidth is narrow (<8 MHz)
    must reach the hobby_rc_2g4 family branch -- even though its band-width
    `bandwidth_mhz` (the whole 2400-2483 sweep band) is wideband. This is the
    core FIX B regression: the classifier must divide on occupied bandwidth."""
    det = {
        "id": "d1",
        "model": "DJI Mini (candidate)",
        "protocol": "OcuSync/Wi-Fi",
        "center_freq_ghz": 2.44,
        "bandwidth_mhz": 83.0,     # whole sweep-band width -- would force wideband
        "occupied_bw_mhz": 1.5,    # actual occupied width -- narrowband control link
        "source": "HACKRF",
        "status": "ACTIVE",
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "hobby_rc_2g4"
    assert body["confidence_type"] == "advisory_only"  # 2.4 GHz is family-level advisory
    # Had the bridge fed the 83 MHz band width, this would be wideband_video.
    assert body["link_family"] != "wideband_video_2g4_5g8"


def test_2g4_link_type_is_family_level_never_a_named_protocol():
    """The 2.4 GHz determination must name the FAMILY only -- never ELRS vs
    FrSky vs FlySky vs Spektrum (an energy sweep cannot separate them)."""
    det = {
        "id": "d1b", "center_freq_ghz": 2.44,
        "bandwidth_mhz": 83.0, "occupied_bw_mhz": 1.2,
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "hobby_rc_2g4"
    for name in _FORBIDDEN_2G4_PROTOCOL_NAMES:
        assert name not in body["link_type"], f"{name!r} leaked into a 2.4 GHz determination"


def test_wideband_2g4_still_routes_to_video():
    """A genuinely wideband 2.4 GHz occupied bandwidth still routes to the
    video-class family (the divider is honest in both directions)."""
    det = {
        "id": "d1c", "center_freq_ghz": 2.44,
        "bandwidth_mhz": 83.0, "occupied_bw_mhz": 18.0,
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "wideband_video_2g4_5g8"


def test_dji_band_default_wide_contact_is_honest_wideband_video():
    """A realistic DJI-2G4 confirmed detection (the coarse energy band-default
    model/protocol) whose OCCUPIED bandwidth is genuinely wide is honestly a
    wideband video-class contact -- band+bandwidth only, no DJI decode
    overclaim."""
    det = {
        "id": "d1d",
        "model": "DJI Mini (candidate)",
        "protocol": "OcuSync/Wi-Fi",
        "center_freq_ghz": 2.44,
        "bandwidth_mhz": 83.0,
        "occupied_bw_mhz": 18.0,   # genuinely wide OFDM/video occupied bandwidth
        "source": "HACKRF",
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "wideband_video_2g4_5g8"
    assert body["confidence_type"] == "advisory_only"


def test_subghz_hop_signature_reaches_lrs_family():
    """A confirmed sub-GHz contact carrying the `rf_signature_only` hop
    evidence (as hackrf_rx.py attaches it for a hop-corroborated LRS-433/
    SRD-868 detection) must reach the lrs_subghz family at heuristic_binary --
    the core FIX A regression."""
    det = {
        "id": "d2",
        "model": "LRS/telemetry craft (candidate)",
        "protocol": "868MHz SRD LRS/telemetry",
        "center_freq_ghz": 0.868,
        "bandwidth_mhz": 7.0,
        "occupied_bw_mhz": 0.8,
        "rf_signature_only": True,    # ELRS/Crossfire-class hop consistency
        "hop_rate_hz": 50.0,
        "source": "HACKRF",
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "lrs_subghz"
    assert body["confidence_type"] == "heuristic_binary"
    assert "ELRS-Crossfire-class" in body["link_type"]


def test_sik915_hop_corroborated_is_lrs_not_mavlink_overclaim():
    """An ELRS-900/Crossfire emitter sitting in the SiK-915 band (902-928),
    hop-corroborated, must be the sub-GHz LRS family -- NOT a decoded MAVLink
    overclaim -- even though the detection still carries the coarse
    `SiK/MAVLink` protocol tag from BAND_DETECTION_META."""
    det = {
        "id": "d3",
        "model": "MAVLink craft (candidate)",
        "protocol": "SiK/MAVLink",
        "center_freq_ghz": 0.909,
        "bandwidth_mhz": 26.0,
        "occupied_bw_mhz": 1.0,
        "rf_signature_only": True,
        "hop_rate_hz": 40.0,
        "source": "SIK_RF_HEURISTIC",   # energy heuristic, NOT a real SiK decode
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "lrs_subghz"
    assert body["confidence_type"] == "heuristic_binary"


def test_sik915_energy_no_hop_is_continuous_telemetry_candidate_not_mavlink():
    """A plain SiK-915 energy detection (no hop evidence, no real decode) must
    be an honest '915 MHz continuous telemetry (candidate)' advisory -- NEVER a
    decoded MAVLink call, even though `protocol` contains 'MAVLink'. This is the
    item-4 honesty guard: an energy sweep does not decode MAVLink."""
    det = {
        "id": "d4",
        "model": "MAVLink craft (candidate)",
        "protocol": "SiK/MAVLink",
        "center_freq_ghz": 0.915,
        "bandwidth_mhz": 26.0,
        "occupied_bw_mhz": 2.0,
        "source": "SIK_RF_HEURISTIC",
        # no rf_signature_only, no protocol_confirmed
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "subghz_ism"
    assert body["confidence_type"] == "advisory_only"
    assert body["link_type"] == "915 MHz continuous telemetry (candidate)"
    assert body["link_family"] != "mavlink_sik"


def test_real_sik_decode_is_still_protocol_verified():
    """A GENUINE decode (protocol_confirmed) still earns mavlink_sik
    protocol_verified -- the honesty guard tightens overclaims, it does not
    suppress real decodes."""
    det = {
        "id": "d5", "protocol": "MAVLink", "center_freq_ghz": 0.915,
        "protocol_confirmed": True, "source": "SIK_RADIO",
    }
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "mavlink_sik"
    assert body["confidence_type"] == "protocol_verified"


def test_occupied_bw_absent_falls_back_to_bandwidth_mhz():
    """Detection sources that don't emit `occupied_bw_mhz` (e.g. non-hackrf
    ingest) must still classify off `bandwidth_mhz` -- backward compatibility."""
    det = {"id": "d6", "center_freq_ghz": 2.44, "bandwidth_mhz": 2.0}  # narrow, no occupied
    body = clb.classification_for_detection(det)
    assert body["link_family"] == "hobby_rc_2g4"  # used bandwidth_mhz fallback


def test_body_echoes_detection_id_and_freq():
    det = {"id": "abc", "center_freq_ghz": 0.433, "occupied_bw_mhz": 0.5}
    body = clb.classification_for_detection(det)
    assert body["detection_id"] == "abc"
    assert body["center_freq_ghz"] == 0.433
