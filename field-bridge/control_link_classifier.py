#!/usr/bin/env python3
"""Over-the-air control-link FAMILY classifier (heuristic, RX-only).

WHAT THIS IS -- AND WHAT IT DELIBERATELY IS NOT
================================================
Given the OBSERVABLE RF fields this project's detection plane already produces
for a contact (center frequency, coarse occupied bandwidth, and -- when
available -- ELRS/Crossfire-class FHSS hop-consistency evidence from
hackrf_rx.py task #88), this maps that contact to a control-link *FAMILY*
(DJI OcuSync, 2.4 GHz hobby-RC LRS such as ELRS/DSMX/FrSky, sub-GHz LRS such
as ELRS-900/Crossfire, MAVLink-over-SiK telemetry, or analog FPV video).

It is a BAND + SIGNATURE HEURISTIC, not a protocol decode. It never claims a
specific airframe, a specific transmitter model, or a decoded serial. The
honest ceiling of a passive energy/bandwidth/hop observation is a family-level
"this emission looks like an X-class control link", and the confidence_type it
emits says exactly that:
  - "heuristic_binary"  : a persistence/FHSS-corroborated family call (e.g. a
                          narrowband sub-GHz emitter that DID show the ELRS/
                          Crossfire FHSS hop-consistency signature).
  - "advisory_only"     : band + bandwidth overlap only, no hop/decoded
                          corroboration -- weaker, explicitly labeled.
  - "protocol_verified" : ONLY when the caller passes a genuinely protocol-
                          confirmed hint (e.g. a real decoded MAVLink frame
                          from mavlink_sniffer / SiK bridge), never from
                          energy features alone.

Reuses the SAME band definitions the rest of the field-bridge already uses:
LRS-433 / SRD-868 come from consumer_iot_signatures.CONSUMER_IOT_BAND_RANGES_MHZ,
and the 2.4/5.8 GHz ISM edges match hackrf_rx.py's sweep bands. This module
adds no transmit path and touches no hardware -- it is a pure function over
already-observed contact fields, so it is trivially unit-testable and cannot
starve the detection sweep.
"""
from __future__ import annotations

from typing import Dict, Optional

# Band edges in GHz. 2.4/5.8 ISM edges match hackrf_rx.py's sweep bands; the
# sub-GHz LRS bands mirror consumer_iot_signatures.CONSUMER_IOT_BAND_RANGES_MHZ
# (LRS-433: 420-450 MHz, SRD-868: 863-870 MHz) widened to the full 900 MHz ISM
# control span used by ELRS-900/Crossfire (902-928) and SiK (915) radios.
BAND_LRS_433_GHZ = (0.400, 0.470)
BAND_ISM_900_GHZ = (0.860, 0.930)
BAND_ISM_2G4_GHZ = (2.400, 2.4835)
BAND_ISM_5G8_GHZ = (5.650, 5.950)

# Occupied-bandwidth threshold separating wideband OFDM/analog-video emissions
# (DJI OcuSync digital video-link, analog FPV video) from the narrowband
# frequency-hopping RC control links (ELRS/DSMX/FrSky/Crossfire). Coarse by
# design -- hackrf_rx.py's occupied_bw_mhz is itself a contiguous-run-above-
# half-threshold estimate, not a spectral-mask measurement (see rf_features.
# compute_bandwidth_mhz), so this is a family divider, not a precise cutoff.
WIDEBAND_MHZ = 8.0


def _in(band_ghz, f_ghz: Optional[float]) -> bool:
    return f_ghz is not None and band_ghz[0] <= f_ghz <= band_ghz[1]


def classify_control_link(
    *,
    center_freq_ghz: Optional[float],
    bandwidth_mhz: Optional[float] = None,
    fhss_hop_consistent: Optional[bool] = None,
    protocol: Optional[str] = None,
    protocol_confirmed: bool = False,
    source: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict:
    """Classify one contact's control-link family from observable RF fields.

    Returns a dict with keys: link_type, link_family, confidence_type,
    rationale, evidence. link_type == "unknown" (never fabricated) when the
    contact does not fall in a recognized control-link band.
    """
    evidence = {
        "center_freq_ghz": center_freq_ghz,
        "bandwidth_mhz": bandwidth_mhz,
        "fhss_hop_consistent": fhss_hop_consistent,
    }
    proto = (protocol or "").lower()
    mdl = (model or "").lower()
    src = (source or "").upper()

    # --- Strongest signal first: a genuinely protocol-confirmed hint -------
    # These come from real decodes elsewhere in the pipeline, not from energy
    # features, so they earn "protocol_verified" (or at least a confident
    # family call) rather than the heuristic tiers below.
    #
    # HONEST GUARD (item 4): a MAVLink/SiK family call is made ONLY on a real
    # decode signal -- a protocol-confirmed decode, or the dedicated SiK-radio
    # bridge source (SIK_RADIO). A bare "SiK/MAVLink" protocol STRING on an
    # energy-sweep detection (source SIK_RF_HEURISTIC, protocol_confirmed False)
    # is NOT a decode and must NOT be called MAVLink -- it falls through to the
    # sub-GHz band logic below, where 902-928 without hop corroboration is
    # honestly labeled a "915 MHz continuous telemetry (candidate)" instead of
    # overclaiming a decoded MAVLink link.
    if src == "SIK_RADIO" or ("mavlink" in proto and protocol_confirmed):
        return {
            "link_type": "MAVLink over SiK telemetry radio",
            "link_family": "mavlink_sik",
            "confidence_type": "protocol_verified" if protocol_confirmed else "heuristic_binary",
            "rationale": (
                "MAVLink/SiK-radio hint on the contact"
                + (" (protocol-confirmed decode)" if protocol_confirmed
                   else " (SiK-radio bridge source tag, not a fresh CRC-verified decode)")
            ),
            "evidence": evidence,
        }
    # HONEST GUARD (item 4): a DJI OcuSync FAMILY call is made only on a genuine
    # DJI/OcuSync/DroneID identification -- and protocol_verified ONLY when a
    # DroneID frame actually CRC-decodes (protocol_confirmed). The coarse energy
    # band-default that hackrf_rx.py attaches to EVERY 2.4/5.8 GHz confirmed
    # contact ("DJI Mini (candidate)" model + the ambiguous "OcuSync/Wi-Fi"
    # protocol) is NOT such an identification -- the "/Wi-Fi" says outright the
    # energy sweep cannot tell OcuSync from ordinary Wi-Fi. Treating it as DJI
    # would (a) overclaim and (b) short-circuit every 2.4 GHz contact past the
    # occupied-bandwidth divider, keeping the hobby_rc_2g4 family unreachable.
    # So the ambiguous band-default falls through to the band/bandwidth logic
    # below, where FIX B's occupied bandwidth honestly splits narrowband hobby-
    # RC control links from wideband OFDM/video.
    dji_band_default = "wi-fi" in proto or "wifi" in proto
    dji_signal = "droneid" in proto or "ocusync" in proto or "dji" in mdl
    if dji_signal and not dji_band_default:
        return {
            "link_type": "DJI OcuSync (digital video + control)",
            "link_family": "dji_ocusync",
            "confidence_type": "protocol_verified" if protocol_confirmed else "advisory_only",
            "rationale": (
                "DJI/OcuSync/DroneID identification on this contact"
                + (" (DroneID CRC-decoded)" if protocol_confirmed
                   else " (tag only, not a fresh DroneID CRC decode)")
            ),
            "evidence": evidence,
        }

    wide = bandwidth_mhz is not None and bandwidth_mhz >= WIDEBAND_MHZ
    narrow = bandwidth_mhz is not None and bandwidth_mhz < WIDEBAND_MHZ

    # --- 2.4 / 5.8 GHz ISM --------------------------------------------------
    if _in(BAND_ISM_2G4_GHZ, center_freq_ghz) or _in(BAND_ISM_5G8_GHZ, center_freq_ghz):
        band_label = "2.4 GHz" if _in(BAND_ISM_2G4_GHZ, center_freq_ghz) else "5.8 GHz"
        if wide:
            # Wideband OFDM/analog: DJI digital video-link or analog FPV video.
            return {
                "link_type": f"{band_label} wideband OFDM/video-class (DJI OcuSync or analog FPV downlink)",
                "link_family": "wideband_video_2g4_5g8",
                "confidence_type": "advisory_only",
                "rationale": (
                    f"Wide occupied bandwidth (~{bandwidth_mhz:.0f} MHz) in {band_label} ISM -- "
                    "consistent with a digital video-link (DJI OcuSync) or analog FPV video "
                    "downlink; band+bandwidth only, no decode."
                ),
                "evidence": evidence,
            }
        # Narrowband 2.4: hobby-RC FHSS control links -- FAMILY LEVEL ONLY.
        # HONEST CEILING (item 4): the determination is the family, never a
        # specific protocol. A passive energy/bandwidth sweep physically cannot
        # separate the 2.4 GHz FHSS hobby-RC protocols from one another, so no
        # protocol name is emitted as a call. In the live pipeline 2.4 GHz is
        # not hop-tracked, so fhss_hop_consistent is not set and this is
        # advisory_only in practice; the heuristic_binary tier remains reachable
        # only for a caller that supplies genuine FHSS hop corroboration.
        if _in(BAND_ISM_2G4_GHZ, center_freq_ghz):
            confident = fhss_hop_consistent is True
            return {
                "link_type": "2.4 GHz FHSS hobby-RC control link (family)",
                "link_family": "hobby_rc_2g4",
                "confidence_type": "heuristic_binary" if confident else "advisory_only",
                "rationale": (
                    "Narrowband 2.4 GHz emission"
                    + (" WITH FHSS hop corroboration"
                       if confident else
                       " (bandwidth-only; no FHSS hop corroboration this cycle)")
                    + " -- family-level hobby-RC control-link class. An energy/"
                    "bandwidth sweep cannot separate the specific 2.4 GHz FHSS "
                    "hobby-RC protocols, so only the family is asserted, never a "
                    "specific protocol decode."
                ),
                "evidence": evidence,
            }
        return {
            "link_type": "5.8 GHz narrowband control (uncommon)",
            "link_family": "narrowband_5g8",
            "confidence_type": "advisory_only",
            "rationale": "Narrowband 5.8 GHz emission -- band+bandwidth only, uncommon control band.",
            "evidence": evidence,
        }

    # --- Sub-GHz LRS / telemetry (433 / 868-928 MHz) ------------------------
    if _in(BAND_LRS_433_GHZ, center_freq_ghz) or _in(BAND_ISM_900_GHZ, center_freq_ghz):
        is_433 = _in(BAND_LRS_433_GHZ, center_freq_ghz)
        band_label = "433 MHz" if is_433 else "868/915 MHz"
        if fhss_hop_consistent is True:
            # Hop-corroborated: family-level long-range control-link class.
            # "ELRS-Crossfire-class" names the FAMILY, not a determination of
            # ELRS-vs-Crossfire -- a hop-consistency heuristic can't separate
            # them, and it deliberately does not name SiK either.
            return {
                "link_type": f"Sub-GHz LRS / ELRS-Crossfire-class ({band_label})",
                "link_family": "lrs_subghz",
                "confidence_type": "heuristic_binary",
                "rationale": (
                    f"Sub-GHz {band_label} emission WITH ELRS/Crossfire-class FHSS hop "
                    "consistency -- family-level long-range control-link class, not a "
                    "specific-protocol decode."
                ),
                "evidence": evidence,
            }
        if not is_433:
            # 902-928 without hop corroboration: honestly a continuous-telemetry
            # candidate (SiK/MAVLink, LoRa, or ambient consumer-IoT) -- NOT a
            # decoded MAVLink link (that would take a real decode; see the SiK
            # branch above) and NOT an LRS hopping call (no FHSS corroboration).
            return {
                "link_type": "915 MHz continuous telemetry (candidate)",
                "link_family": "subghz_ism",
                "confidence_type": "advisory_only",
                "rationale": (
                    "902-928 MHz emission WITHOUT ELRS/Crossfire FHSS hop corroboration "
                    "and WITHOUT a real MAVLink/SiK decode -- honestly a continuous-"
                    "telemetry candidate (SiK/MAVLink, LoRa, or ambient consumer-IoT), "
                    "not a decoded link and not an LRS hopping call."
                ),
                "evidence": evidence,
            }
        return {
            "link_type": f"Sub-GHz ISM ({band_label}) -- SiK telemetry or consumer-IoT (not FHSS-confirmed)",
            "link_family": "subghz_ism",
            "confidence_type": "advisory_only",
            "rationale": (
                f"Sub-GHz {band_label} emission WITHOUT the ELRS/Crossfire FHSS hop "
                "signature -- at least as plausibly SiK telemetry or ambient consumer-IoT "
                "as an LRS control link (see consumer_iot_signatures.py)."
            ),
            "evidence": evidence,
        }

    # --- Out of any recognized control-link band ----------------------------
    return {
        "link_type": "unknown",
        "link_family": None,
        "confidence_type": "advisory_only",
        "rationale": (
            "Center frequency is outside the recognized control-link bands "
            "(433 / 868-928 MHz / 2.4 / 5.8 GHz) -- no control-link family assigned."
        ),
        "evidence": evidence,
    }
