"""Unit tests for the Effector-Selection engine -- backend/effector_selection.py
(P1+P2 of decision-effector-selection-engine.md).

True unit tests: pure functions over plain dicts, no live server / Mongo /
requests / websocket, following the same pattern as test_engagement_planner.py /
test_sop_engine.py. These prove the SAFETY-CRITICAL / HONESTY properties an
adversarial review will scrutinise:

  * the module is INERT: its ONLY non-stdlib imports are threat_library +
    mavlink_codec, and its source references no transmit-spine / arm-token /
    tx-halt-clear / deploy symbol (static AST + source-scan assertions);
  * feasibility verdicts are HONEST: an unknown link never yields a confident
    takeover; an encrypted link is NOT_FEASIBLE for takeover and falls back to
    jam; a legacy/unencrypted MAVLink link is FEASIBLE; GNSS-deny always carries
    the v1 placeholder verdict, never plain FEASIBLE; and when the two takeover
    signals DISAGREE the conservative NOT_FEASIBLE verdict is taken with BOTH
    signals surfaced;
  * position honesty: a position-less contact gets full protocol feasibility but
    a null proximity_factor; a positioned contact gets a proximity score;
  * failover/dedup: a halted TX marks every effector unavailable with a reason;
    a preferred effector whose bridge is down is failed over past; an already-
    engaged detection is flagged.

Run: pytest backend/tests/test_effector_selection.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import effector_selection  # noqa: E402
from effector_selection import (  # noqa: E402
    PROPOSED_STATUS,
    build_effector_recommendations,
)


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------
def _availability(tx_halted=False, jam=(True, True), gnss=(True, True),
                  mav=(True, True), wifi_deauth=(True, True), arsdk=(True, True)):
    """A read-only effector-availability snapshot, mirroring the contract shape
    the server passes in ({tx_halted, jam, gnss_spoof, mavlink_sdr_inject,
    wifi_deauth, arsdk_inject})."""
    return {
        "tx_halted": tx_halted,
        "jam": {"bridge_up": jam[0], "range_auth_enabled": jam[1]},
        "gnss_spoof": {"bridge_up": gnss[0], "range_auth_enabled": gnss[1],
                       "maturity": "v1_placeholder"},
        "mavlink_sdr_inject": {"bridge_up": mav[0], "range_auth_enabled": mav[1]},
        "wifi_deauth": {"bridge_up": wifi_deauth[0],
                        "range_auth_enabled": wifi_deauth[1],
                        "maturity": "link_drop_not_takeover"},
        "arsdk_inject": {"bridge_up": arsdk[0], "range_auth_enabled": arsdk[1],
                         "maturity": "unencrypted_parrot_tello_only"},
    }


def _legacy_mavlink_contact(**overrides):
    """Unencrypted legacy MAVLink-over-SiK airframe: takeover-viable."""
    base = {
        "detection_id": "det-legacy",
        "callsign": "BANDIT-1",
        # NOTE: classify_override_link substring-matches and fail-closes on the
        # token "encrypted" -- so the string must NOT contain "unencrypted"
        # (which embeds "encrypted"). "plaintext" says the same thing safely.
        "protocol": "MAVLink telemetry over 915 MHz SiK radio (plaintext)",
        "family": "MAVLink-SiK",
        "band": "915MHz",
        "confidence_type": "protocol_verified",
        "threat_level": "HIGH",
        "position_source": None,
        "countermeasures": {
            "jam_bands": ["433MHz", "915MHz"],
            "gnss_deny_applicable": True,
            "cyber_takeover_applicable": True,
        },
    }
    base.update(overrides)
    return base


def _encrypted_dji_contact(**overrides):
    """Encrypted OcuSync/DJI link: takeover NOT applicable (inject NO-OP)."""
    base = {
        "detection_id": "det-dji",
        "callsign": "DJI-2",
        "protocol": "OcuSync 4",
        "family": "DJI",
        "band": "2.4GHz",
        "confidence_type": "rf_signature",
        "threat_level": "HIGH",
        "position_source": None,
        "countermeasures": {
            "jam_bands": ["2.4GHz", "5.8GHz"],
            "gnss_deny_applicable": True,
            "cyber_takeover_applicable": False,
        },
    }
    base.update(overrides)
    return base


def _unknown_link_contact(**overrides):
    """No usable protocol ID and no matched countermeasures: honest unknowns."""
    base = {
        "detection_id": "det-unk",
        "callsign": "UNK-3",
        "protocol": "control_link",     # generic -> classify == 'unknown'
        "family": None,
        "band": None,
        "confidence_type": None,
        "threat_level": "MEDIUM",
        "position_source": None,
        "countermeasures": None,
    }
    base.update(overrides)
    return base


def _disagreement_contact(**overrides):
    """classify_override_link -> legacy_mavlink, but the matched library entry
    says cyber_takeover_applicable=false. The two signals DISAGREE."""
    base = {
        "detection_id": "det-dis",
        "callsign": "DIS-4",
        "protocol": "MAVLink",          # classify -> legacy_mavlink
        "family": "MAVLink",
        "band": "915MHz",
        "confidence_type": "heuristic_binary",
        "threat_level": "HIGH",
        "position_source": None,
        "countermeasures": {
            "jam_bands": ["915MHz"],
            "gnss_deny_applicable": False,
            "cyber_takeover_applicable": False,   # <- disagrees with classify
        },
    }
    base.update(overrides)
    return base


def _wifi_parrot_contact(**overrides):
    """Identified Wi-Fi/ARSDK Parrot contact: deauth link-drop AND the ARSDK
    inject are both viable per the matched library countermeasures."""
    base = {
        "detection_id": "det-parrot",
        "callsign": "ANAFI-7",
        "protocol": "Wi-Fi 802.11 a/n (ARSDK3)",
        "control_link_family": "Wi-Fi/ARSDK",
        "family": "Wi-Fi/ARSDK",
        "band": "2.4GHz",
        "confidence_type": "rf_signature",
        "threat_level": "MEDIUM",
        "position_source": None,
        "countermeasures": {
            "jam_bands": ["2.4GHz", "5GHz-WiFi"],
            "gnss_deny_applicable": True,
            "cyber_takeover_applicable": False,
            "wifi_deauth_applicable": True,
            "arsdk_inject_applicable": True,
        },
    }
    base.update(overrides)
    return base


def _wifi_unknown_make_contact(**overrides):
    """A Wi-Fi-class control link with NO make/model identification -- an
    explicit countermeasures block carrying no arsdk_inject_applicable flag
    (unlike _countermeasures_for's library-match fallback, which would fuzzy-
    match a generic 'Wi-Fi' band/family onto the nearest real entry and defeat
    the point of this fixture): honest UNKNOWN for the identity-gated ARSDK
    inject, but deauth still applies generally to any identified Wi-Fi link
    (link-drop, not identity-gated)."""
    base = {
        "detection_id": "det-wifi-unk",
        "callsign": "DIRECT-4F2A",
        "protocol": "Wi-Fi 802.11 (generic softAP)",
        "control_link_family": "Wi-Fi",
        "family": None,
        "band": "2.4GHz",
        "confidence_type": "rf_signature",
        "threat_level": "MEDIUM",
        "position_source": None,
        "countermeasures": {
            "jam_bands": ["2.4GHz"],
            "gnss_deny_applicable": None,
            "cyber_takeover_applicable": None,
        },
    }
    base.update(overrides)
    return base


def _empty_plan():
    return {"proposals": [], "excluded": []}


# --------------------------------------------------------------------------
# HONESTY: takeover verdicts grounded in real signals, never invented
# --------------------------------------------------------------------------
def test_unknown_link_takeover_is_unknown_never_feasible():
    out = build_effector_recommendations(
        [_unknown_link_contact()], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    mav = rec["feasibility"]["mavlink_takeover"]
    assert mav["verdict"] == "UNKNOWN"
    assert mav["verdict"] != "FEASIBLE"
    assert mav["link_class"] == "unknown"


def test_encrypted_link_takeover_not_feasible_and_falls_back_to_jam():
    out = build_effector_recommendations(
        [_encrypted_dji_contact()], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["feasibility"]["mavlink_takeover"]["verdict"] == "NOT_FEASIBLE"
    # takeover being off the table, doctrine falls back to jam.
    assert rec["recommended_effector"] == "jam"


def test_legacy_unencrypted_mavlink_takeover_feasible():
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    mav = rec["feasibility"]["mavlink_takeover"]
    assert mav["verdict"] == "FEASIBLE"
    assert mav["link_class"] == "legacy_mavlink"
    # surgical takeover is preferred and clearable -> recommended.
    assert rec["recommended_effector"] == "mavlink_takeover"


def test_gnss_deny_always_placeholder_never_plain_feasible():
    # applicable=True must yield the v1 placeholder verdict, NOT plain FEASIBLE.
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(), _availability(), set())
    gnss = out["recommendations"][0]["feasibility"]["gnss_deny"]
    assert gnss["verdict"] == "FEASIBLE_PLACEHOLDER_V1"
    assert gnss["verdict"] != "FEASIBLE"


def test_gnss_deny_not_applicable_is_not_feasible():
    out = build_effector_recommendations(
        [_disagreement_contact()], _empty_plan(), _availability(), set())
    gnss = out["recommendations"][0]["feasibility"]["gnss_deny"]
    assert gnss["verdict"] == "NOT_FEASIBLE"


def test_gnss_deny_undecidable_is_unknown():
    out = build_effector_recommendations(
        [_unknown_link_contact()], _empty_plan(), _availability(), set())
    gnss = out["recommendations"][0]["feasibility"]["gnss_deny"]
    assert gnss["verdict"] == "UNKNOWN"


def test_jam_is_always_feasible_unverified_range():
    for contact in (_legacy_mavlink_contact(), _encrypted_dji_contact(),
                    _unknown_link_contact(), _disagreement_contact()):
        out = build_effector_recommendations(
            [contact], _empty_plan(), _availability(), set())
        jam = out["recommendations"][0]["feasibility"]["jam"]
        assert jam["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"


def test_takeover_signal_disagreement_is_conservative_not_feasible():
    """classify=legacy_mavlink vs countermeasures.cyber_takeover_applicable=false
    -> take the conservative fail-closed verdict AND surface BOTH signals."""
    out = build_effector_recommendations(
        [_disagreement_contact()], _empty_plan(), _availability(), set())
    mav = out["recommendations"][0]["feasibility"]["mavlink_takeover"]
    assert mav["verdict"] == "NOT_FEASIBLE"
    rationale = mav["rationale"]
    # both signals surfaced in the rationale
    assert "DISAGREE" in rationale
    assert "legacy_mavlink" in rationale                      # classify signal
    assert "cyber_takeover_applicable=False" in rationale     # library signal
    # and the encrypted-inject fallback: jam is recommended.
    assert out["recommendations"][0]["recommended_effector"] == "jam"


def test_encrypted_link_ignores_library_applicable_disagreement():
    """An encrypted link with a (wrong) library cyber_takeover_applicable=true is
    STILL NOT_FEASIBLE, with the disagreement surfaced -- fail closed."""
    contact = _encrypted_dji_contact(countermeasures={
        "jam_bands": ["2.4GHz"], "gnss_deny_applicable": True,
        "cyber_takeover_applicable": True,
    })
    out = build_effector_recommendations(
        [contact], _empty_plan(), _availability(), set())
    mav = out["recommendations"][0]["feasibility"]["mavlink_takeover"]
    assert mav["verdict"] == "NOT_FEASIBLE"
    assert "DISAGREE" in mav["rationale"]


# --------------------------------------------------------------------------
# Threat scoring (4.5.4) -- explainable integer/categorical breakdown
# --------------------------------------------------------------------------
def test_score_breakdown_is_explainable_integers_not_a_blended_float():
    plan = {
        "proposals": [{
            "detection_id": "det-legacy",
            "is_controller_candidate": True,
            "score_breakdown": {"controller_first_bonus": 1000},
            "rank": 1,
        }],
        "excluded": [],
    }
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], plan, _availability(), set())
    rec = out["recommendations"][0]
    sb = rec["score_breakdown"]
    # threat_weight HIGH=75, controller bonus joined (not recomputed)=1000,
    # confidence protocol_verified=20 -> total 1095. Integer, explainable.
    assert sb["threat_weight"]["value"] == 75
    assert sb["controller_bonus"]["value"] == 1000
    assert sb["confidence_factor"]["category"] == "HIGH"
    assert rec["threat_score"] == 1095
    assert isinstance(rec["threat_score"], int)
    assert rec["engagement_proposal_rank"] == 1


def test_controller_bonus_is_joined_not_recomputed():
    # No controller flag in the plan -> zero controller bonus, not invented.
    plan = {"proposals": [{
        "detection_id": "det-legacy", "is_controller_candidate": False,
        "score_breakdown": {"controller_first_bonus": 0}, "rank": 3,
    }], "excluded": []}
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], plan, _availability(), set())
    sb = out["recommendations"][0]["score_breakdown"]
    assert sb["controller_bonus"]["value"] == 0


# --------------------------------------------------------------------------
# Position honesty (governing invariant #2)
# --------------------------------------------------------------------------
def test_positionless_contact_has_null_proximity_but_full_feasibility():
    contact = _legacy_mavlink_contact(position_source=None)
    out = build_effector_recommendations(
        [contact], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["position_known"] is False
    assert rec["score_breakdown"]["proximity_factor"] is None
    assert rec["score_breakdown"]["proximity_note"] is not None
    # feasibility is still fully computed for a position-less contact.
    assert rec["feasibility"]["mavlink_takeover"]["verdict"] == "FEASIBLE"
    assert rec["feasibility"]["jam"]["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"


def test_positioned_contact_has_proximity_score_and_no_guessed_distance():
    contact = _encrypted_dji_contact(position_source="DRONEID", lat=12.9, lon=77.5)
    out = build_effector_recommendations(
        [contact], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["position_known"] is True
    prox = rec["score_breakdown"]["proximity_factor"]
    assert prox is not None
    assert isinstance(prox["value"], int)          # a real score is present
    assert prox["category"] == "POSITION_KNOWN"
    # honesty: no fabricated distance/range is emitted anywhere in the factor.
    assert "distance" not in prox["basis"].lower() or "no distance" in prox["basis"].lower()


# --------------------------------------------------------------------------
# Failover / availability (4.5.6/4.5.7) -- read-only snapshot, never clears
# --------------------------------------------------------------------------
def test_tx_halted_marks_every_effector_unavailable_with_reason():
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(),
        _availability(tx_halted=True), set())
    rec = out["recommendations"][0]
    assert rec["recommended_effector"] is None
    assert rec["failover_order"], "feasible effectors should still be listed"
    for entry in rec["failover_order"]:
        assert entry["available"] is False
        assert "TX halted" in entry["reason"]
    assert out["summary"]["tx_halted"] is True


def test_preferred_effector_bridge_down_fails_over_to_next():
    # legacy contact prefers takeover, but its bridge is down -> jam instead.
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(),
        _availability(mav=(False, True)), set())
    rec = out["recommendations"][0]
    assert rec["recommended_effector"] == "jam"
    # takeover is still FEASIBLE but appears in failover as unavailable w/ reason.
    takeover_fail = [f for f in rec["failover_order"]
                     if f["effector"] == "mavlink_takeover"]
    assert len(takeover_fail) == 1
    assert takeover_fail[0]["feasible"] is True
    assert takeover_fail[0]["available"] is False
    assert "bridge down" in takeover_fail[0]["reason"]


def test_range_auth_disabled_makes_effector_unavailable():
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(),
        _availability(mav=(True, False)), set())
    rec = out["recommendations"][0]
    assert rec["recommended_effector"] == "jam"
    takeover_fail = [f for f in rec["failover_order"]
                     if f["effector"] == "mavlink_takeover"][0]
    assert takeover_fail["available"] is False
    assert "range authorization" in takeover_fail["reason"]


# --------------------------------------------------------------------------
# Dedup (4.5.7) + plan-inherited exclusions
# --------------------------------------------------------------------------
def test_already_engaged_detection_is_flagged():
    out = build_effector_recommendations(
        [_legacy_mavlink_contact(), _encrypted_dji_contact()],
        _empty_plan(), _availability(), {"det-dji"})
    by_id = {r["detection_id"]: r for r in out["recommendations"]}
    assert by_id["det-dji"]["dedup_status"]["already_engaged"] is True
    assert "already under active engagement" in by_id["det-dji"]["dedup_status"]["reason"]
    assert by_id["det-legacy"]["dedup_status"]["already_engaged"] is False


def test_plan_excluded_contact_is_not_recommended():
    plan = {
        "proposals": [],
        "excluded": [{
            "detection_id": "det-friendly", "callsign": "FRIEND",
            "reason": "friendly-verified; friendly-fire interlock; never proposed.",
            "threat_level": "FRIENDLY",
        }],
    }
    contacts = [_legacy_mavlink_contact(),
                {"detection_id": "det-friendly", "callsign": "FRIEND"}]
    out = build_effector_recommendations(contacts, plan, _availability(), set())
    rec_ids = {r["detection_id"] for r in out["recommendations"]}
    assert "det-friendly" not in rec_ids
    excluded_ids = {e["detection_id"] for e in out["excluded"]}
    assert "det-friendly" in excluded_ids


def test_contact_without_detection_id_is_excluded():
    out = build_effector_recommendations(
        [{"callsign": "NO-ID"}], _empty_plan(), _availability(), set())
    assert out["recommendations"] == []
    assert out["excluded"][0]["detection_id"] is None


# --------------------------------------------------------------------------
# PROPOSED-only posture: every recommendation carries the disclaimer + status,
# and execution_paths are DOC STRINGS ONLY.
# --------------------------------------------------------------------------
def test_every_recommendation_is_proposed_requires_human_authorization():
    out = build_effector_recommendations(
        [_legacy_mavlink_contact(), _encrypted_dji_contact()],
        _empty_plan(), _availability(), set())
    assert out["status"] == "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"
    assert PROPOSED_STATUS == "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"
    for rec in out["recommendations"]:
        assert rec["status"] == "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"
        # execution_paths are documentation strings only -- no callable, no url
        # this module ever hits; each is a human-cleared, gated endpoint doc.
        assert isinstance(rec["execution_paths"], dict)
        for path in rec["execution_paths"].values():
            assert isinstance(path, str)
            assert "commander-gated" in path


def test_output_carries_disclaimer_and_availability_echo():
    avail = _availability()
    out = build_effector_recommendations(
        [_legacy_mavlink_contact()], _empty_plan(), avail, set())
    assert "PROPOSAL" in out["disclaimer"]
    assert "authorizes and fires" in out["disclaimer"]
    assert out["effector_availability_echo"] == avail


def test_recommendations_sorted_by_threat_score_desc():
    contacts = [
        _encrypted_dji_contact(detection_id="d-low", threat_level="LOW",
                               callsign="LOW"),
        _legacy_mavlink_contact(detection_id="d-hi", threat_level="CRITICAL",
                                callsign="HI"),
    ]
    out = build_effector_recommendations(contacts, _empty_plan(),
                                         _availability(), set())
    scores = [r["threat_score"] for r in out["recommendations"]]
    assert scores == sorted(scores, reverse=True)
    assert out["recommendations"][0]["detection_id"] == "d-hi"


# --------------------------------------------------------------------------
# Library-match fallback path (no explicit countermeasures on the contact)
# --------------------------------------------------------------------------
def test_missing_countermeasures_yields_honest_unknowns_no_crash():
    """A contact with an unrecognisable link and no attached countermeasures must
    fall through to honest UNKNOWNs, never a fabricated capability or a crash."""
    contact = _unknown_link_contact(countermeasures=None, family="Nonesuch",
                                    protocol="???")
    out = build_effector_recommendations(
        [contact], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["feasibility"]["mavlink_takeover"]["verdict"] == "UNKNOWN"
    # jam remains universally feasible even with no library data.
    assert rec["feasibility"]["jam"]["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"


# --------------------------------------------------------------------------
# Active Wi-Fi defeat (P4): wifi_deauth (link-drop) + arsdk_inject (unencrypted
# Parrot/Tello only) -- HONEST feasibility, never sold/ranked as takeover.
# --------------------------------------------------------------------------
def test_wifi_parrot_deauth_is_link_drop_not_takeover_and_inject_feasible():
    out = build_effector_recommendations(
        [_wifi_parrot_contact()], _empty_plan(), _availability(), set())
    fes = out["recommendations"][0]["feasibility"]
    deauth = fes["wifi_deauth"]
    assert deauth["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"
    assert "LINK-DROP" in deauth["rationale"]
    assert "NOT command takeover" in deauth["rationale"]
    inject = fes["arsdk_inject"]
    assert inject["verdict"] == "FEASIBLE"


def test_wifi_parrot_recommends_surgical_inject_over_deauth_and_jam():
    """The surgical, positively-identified ARSDK inject is preferred -- deauth
    (a mere link-drop) is never promoted ahead of it or of jam."""
    out = build_effector_recommendations(
        [_wifi_parrot_contact()], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["recommended_effector"] == "arsdk_inject"
    order = [f["effector"] for f in rec["failover_order"]]
    assert order.index("jam") < order.index("wifi_deauth")


def test_encrypted_dji_contact_inject_not_feasible_deauth_not_blanket_asserted():
    """An encrypted/DJI OcuSync contact: arsdk_inject NOT_FEASIBLE, and deauth
    is NOT blanket-asserted feasible since this detection's own fields do not
    indicate a Wi-Fi control link (only mark deauth feasible where the
    detection actually indicates one)."""
    out = build_effector_recommendations(
        [_encrypted_dji_contact()], _empty_plan(), _availability(), set())
    fes = out["recommendations"][0]["feasibility"]
    assert fes["arsdk_inject"]["verdict"] == "NOT_FEASIBLE"
    assert fes["wifi_deauth"]["verdict"] != "FEASIBLE_UNVERIFIED_RANGE"
    assert fes["wifi_deauth"]["verdict"] == "NOT_FEASIBLE"


def test_wifi_contact_unknown_make_inject_fails_closed_but_deauth_still_applies():
    """A Wi-Fi-class contact with no matched library entry: arsdk_inject fails
    CLOSED (never a confident FEASIBLE for an unidentified airframe), while
    deauth -- generic to any identified Wi-Fi link -- still applies."""
    out = build_effector_recommendations(
        [_wifi_unknown_make_contact()], _empty_plan(), _availability(), set())
    fes = out["recommendations"][0]["feasibility"]
    assert fes["arsdk_inject"]["verdict"] in ("UNKNOWN", "NOT_FEASIBLE")
    assert fes["arsdk_inject"]["verdict"] != "FEASIBLE"
    assert fes["wifi_deauth"]["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"


def test_wifi_inject_signal_disagreement_is_conservative_not_feasible():
    """A Wi-Fi link positively identified, but the matched library entry says
    arsdk_inject_applicable=false (e.g. an encrypted/hardened Wi-Fi airframe) --
    SIGNALS DISAGREE -> conservative NOT_FEASIBLE, both signals surfaced."""
    contact = _wifi_parrot_contact(countermeasures={
        "jam_bands": ["2.4GHz"], "gnss_deny_applicable": True,
        "cyber_takeover_applicable": False,
        "wifi_deauth_applicable": True,
        "arsdk_inject_applicable": False,
    })
    out = build_effector_recommendations(
        [contact], _empty_plan(), _availability(), set())
    inject = out["recommendations"][0]["feasibility"]["arsdk_inject"]
    assert inject["verdict"] == "NOT_FEASIBLE"
    assert "DISAGREE" in inject["rationale"]
    # deauth is unaffected by the inject-only disagreement -- still the link-drop.
    deauth = out["recommendations"][0]["feasibility"]["wifi_deauth"]
    assert deauth["verdict"] == "FEASIBLE_UNVERIFIED_RANGE"


def test_wifi_deauth_never_ranked_in_surgical_takeover_tier():
    """Doctrine-order property: wifi_deauth is NEVER placed in the surgical
    'takeover' preference slot that MAVLink-takeover / arsdk-inject occupy --
    it always sits behind jam, whether or not a surgical option is feasible."""
    feasible_with_surgical = {
        "wifi_deauth": "FEASIBLE_UNVERIFIED_RANGE",
        "jam": "FEASIBLE_UNVERIFIED_RANGE",
        "arsdk_inject": "FEASIBLE",
    }
    order = effector_selection._doctrine_order(feasible_with_surgical)
    assert order[0] == "arsdk_inject"
    assert order.index("wifi_deauth") > order.index("jam")

    feasible_no_surgical = {
        "wifi_deauth": "FEASIBLE_UNVERIFIED_RANGE",
        "jam": "FEASIBLE_UNVERIFIED_RANGE",
    }
    order2 = effector_selection._doctrine_order(feasible_no_surgical)
    # jam -- the universal defeat -- is preferred ahead of the unreliable
    # deauth link-drop; deauth is never promoted to the #1 recommendation.
    assert order2[0] == "jam"
    assert order2.index("wifi_deauth") > order2.index("jam")


def test_wifi_deauth_alone_feasible_recommends_jam_not_deauth():
    """When only jam + wifi_deauth are feasible (no surgical inject match),
    jam -- the universal defeat -- is recommended, never the unreliable
    deauth link-drop."""
    out = build_effector_recommendations(
        [_wifi_unknown_make_contact()], _empty_plan(), _availability(), set())
    rec = out["recommendations"][0]
    assert rec["recommended_effector"] == "jam"


# ==========================================================================
# STATIC SAFETY TESTS -- the enforced inertness invariant (mirror
# tests/test_sop_engine.py:15-24 / 382-461).
# ==========================================================================
_SRC_PATH = Path(__file__).resolve().parent.parent / "effector_selection.py"

# The two contract-defined READ-ONLY availability snapshot KEYS this module is
# REQUIRED to read (bridge_up ^ range_auth_enabled ^ !tx_halted). They contain
# the substrings 'tx_halt' / 'range_auth', so we mask them out FIRST and then
# assert that NO dangerous form of those (or any other transmit-spine) symbol
# remains -- proving the only references are the sanctioned read-only reads.
_SANCTIONED_SNAPSHOT_KEYS = ("tx_halted", "range_auth_enabled")

# Forbidden identifiers: any reference to the transmit spine / arm-token /
# tx-halt-clear / deploy chain would break the inertness invariant. Checked
# AFTER masking the two sanctioned snapshot keys above.
_FORBIDDEN_SUBSTRINGS = [
    "_tx_halted",        # the server's TX-halt global / its clearer
    "tx_halt",           # any other tx-halt reference (post-mask)
    "range_auth",        # any range-auth reference beyond the snapshot read
    "_consume_",         # arm/confirm-token consume
    "_issue_",           # token issue
    "arm_token",
    "confirm_token",
    "has_tx_consumer",
    "broadcast_packet",
    "deploy_",
    "mavlink_inject",
    "device_pin",
    "import server",
    "from server",
]
# 'iff' as a whole word (so it never false-positives on 'diff'/'stiff'/etc.).
_FORBIDDEN_WORDS = ["iff"]


def test_source_references_no_transmit_or_armtoken_symbols():
    src = _SRC_PATH.read_text().lower()
    masked = src
    for key in _SANCTIONED_SNAPSHOT_KEYS:
        masked = masked.replace(key, "")
    for tok in _FORBIDDEN_SUBSTRINGS:
        assert tok not in masked, (
            f"forbidden identifier {tok!r} present in effector_selection.py "
            "(outside the sanctioned read-only availability snapshot keys)"
        )
    for word in _FORBIDDEN_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", src), \
            f"forbidden word {word!r} present in effector_selection.py"


def test_sanctioned_snapshot_keys_are_the_only_txhalt_rangeauth_refs():
    """Positive companion to the scan above: the ONLY occurrences of
    'tx_halt'/'range_auth' in the source are the exact read-only snapshot keys
    'tx_halted' / 'range_auth_enabled' -- the module reads them, never a server
    clearer/accessor."""
    src = _SRC_PATH.read_text().lower()
    for m in re.finditer(r"tx_halt\w*", src):
        assert m.group(0) == "tx_halted", f"unexpected tx-halt token: {m.group(0)!r}"
    for m in re.finditer(r"range_auth\w*", src):
        assert m.group(0) == "range_auth_enabled", \
            f"unexpected range-auth token: {m.group(0)!r}"


def test_only_nonstdlib_imports_are_threat_library_and_mavlink_codec():
    """Parse effector_selection.py's import statements with `ast` and assert the
    ONLY modules it imports outside the standard library are `threat_library`
    and `mavlink_codec`."""
    tree = ast.parse(_SRC_PATH.read_text())
    imported_top_levels = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_top_levels.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_top_levels.add(node.module.split(".")[0])

    stdlib_dir = os.path.realpath(os.path.dirname(os.__file__))

    def _is_stdlib(name: str) -> bool:
        if name in sys.builtin_module_names:
            return True
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            return False
        if spec is None:
            return False
        origin = spec.origin
        if origin in (None, "built-in", "frozen"):
            return True
        return os.path.realpath(origin).startswith(stdlib_dir + os.sep)

    non_stdlib = sorted(m for m in imported_top_levels if not _is_stdlib(m))
    assert non_stdlib == ["mavlink_codec", "threat_library"], (
        "effector_selection.py must import only stdlib + threat_library + "
        f"mavlink_codec; unexpected non-stdlib imports: {non_stdlib}"
    )


def test_only_symbol_imported_from_mavlink_codec_is_classify_override_link():
    """mavlink_codec is a big module with transmit-frame builders. This module
    must pull ONLY the pure `classify_override_link` classifier from it."""
    tree = ast.parse(_SRC_PATH.read_text())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "mavlink_codec":
            names.extend(alias.name for alias in node.names)
    assert names == ["classify_override_link"], (
        f"effector_selection.py must import ONLY classify_override_link from "
        f"mavlink_codec; got {names}"
    )


def test_module_exposes_no_transmit_callable():
    """Defensive: the public surface is `build_effector_recommendations` +
    constants only -- there is no deploy/transmit/arm function to call."""
    public = [n for n in dir(effector_selection) if not n.startswith("_")]
    banned = {"deploy", "transmit", "jam", "arm", "fire", "engage", "broadcast",
              "spoof", "inject"}
    assert banned.isdisjoint({p.lower() for p in public})
