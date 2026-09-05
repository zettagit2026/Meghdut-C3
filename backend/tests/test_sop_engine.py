"""Unit tests for the Zone/SOP no-code rules engine -- backend/sop_engine.py
(P2 of zone-sop-engine.md).

True unit tests: pure functions over plain dicts, no live server / Mongo /
requests, following the same pattern as test_engagement_planner.py /
test_geo_zone.py. These prove the SAFETY-CRITICAL properties an adversarial
review will scrutinise:

  * a SPATIAL rule does NOT match a position-less contact (core honesty test
    -- no fabricated coordinate, no exception);
  * every non-spatial condition matches and rejects correctly;
  * positioned RemoteID contacts resolve inside/outside a zone;
  * a CUE_RECOMMENDATION is a LABEL-ONLY proposal stamped
    PROPOSED_REQUIRES_HUMAN_AUTHORIZATION;
  * the module is INERT: its source references no arm-token / tx-halt / deploy
    symbol and imports nothing outside stdlib + geo_zone (static assertion).

Coordinate convention: [lon, lat] (GeoJSON), never [lat, lon].

Run: pytest backend/tests/test_sop_engine.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sop_engine  # noqa: E402
from sop_engine import PROPOSED_STATUS, evaluate  # noqa: E402


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
# A 10x10 square zone centred near the origin (in [lon, lat] degrees).
ZONE = {
    "id": "zone-1",
    "name": "Alpha Sector",
    "enabled": True,
    "polygon": {
        "type": "Polygon",
        "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]]],
    },
}
ZONES = [ZONE]


def _positionless_contact(**overrides):
    """A HackRF-style RSSI-only contact: NO position_source, no lon/lat."""
    base = {
        "id": "det-hackrf-1",
        "protocol": "control_link",
        "class": "control_link",
        "family": "OcuSync",
        "band": "2.4GHz",
        "confidence": 0.82,
        "confidence_type": "heuristic_binary",
        "threat_level": "HIGH",
        "position_source": None,
    }
    base.update(overrides)
    return base


def _positioned_contact(lon, lat, **overrides):
    """A RemoteID-style contact carrying a REAL decoded position."""
    base = {
        "id": "det-remoteid-1",
        "protocol": "remoteid",
        "class": "multirotor",
        "family": "DJI",
        "band": "2.4GHz",
        "confidence": 0.95,
        "confidence_type": "protocol_verified",
        "threat_level": "CRITICAL",
        "position_source": "remoteid",
        "lon": lon,
        "lat": lat,
    }
    base.update(overrides)
    return base


def _rule(**overrides):
    base = {
        "id": "rule-1",
        "name": "Test rule",
        "enabled": True,
        "priority": 10,
        "zone_id": None,
        "conditions": {},
        "action": {"type": "ALERT", "severity": "WARNING", "message_template": "hit"},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# Non-spatial rule matches a position-less contact
# --------------------------------------------------------------------------
def test_nonspatial_rule_matches_positionless_hackrf():
    rule = _rule(
        conditions={
            "zone_membership": "any",
            "class_in": ["control_link"],
            "protocol_in": ["control_link"],
            "min_confidence": 0.5,
        },
        action={"type": "ALERT", "severity": "CAUTION",
                "message_template": "control-link contact {family}"},
    )
    firings = evaluate(_positionless_contact(), ZONES, [rule])
    assert len(firings) == 1
    f = firings[0]
    assert f["rule_id"] == "rule-1"
    assert f["action_type"] == "ALERT"
    assert f["severity"] == "CAUTION"
    assert f["message"] == "control-link contact OcuSync"
    assert f["cue"] is None
    assert f["zone_id"] is None


# --------------------------------------------------------------------------
# CORE HONESTY TEST: a spatial rule NEVER matches a position-less contact
# --------------------------------------------------------------------------
def test_spatial_rule_does_not_match_positionless_contact():
    """A zone_membership='inside' rule must produce NO firing for a contact
    with no position_source -- an honest miss. No exception is raised, and no
    synthetic position is fabricated onto the contact."""
    contact = _positionless_contact()
    rule = _rule(
        zone_id="zone-1",
        conditions={"zone_membership": "inside", "class_in": ["control_link"]},
    )

    firings = evaluate(contact, ZONES, [rule])

    assert firings == []  # honest miss, no firing
    # No fabricated coordinates were written onto the contact.
    assert "lon" not in contact
    assert "lat" not in contact
    assert contact.get("position_source") is None


def test_spatial_rule_positionless_does_not_raise():
    """Even with lon/lat entirely absent AND a missing zone, evaluation of a
    spatial rule is a clean honest miss (no KeyError / no crash)."""
    contact = _positionless_contact()
    rule_in = _rule(zone_id="zone-1", conditions={"zone_membership": "inside"})
    rule_out = _rule(id="rule-2", zone_id="zone-1",
                     conditions={"zone_membership": "outside"})
    rule_reqpos = _rule(id="rule-3", conditions={"require_position": True})

    assert evaluate(contact, ZONES, [rule_in, rule_out, rule_reqpos]) == []


# --------------------------------------------------------------------------
# Positioned RemoteID contact -- inside vs outside
# --------------------------------------------------------------------------
def test_positioned_contact_matches_inside_when_in_zone():
    inside_contact = _positioned_contact(5, 5)  # centre of the square
    rule = _rule(zone_id="zone-1", conditions={"zone_membership": "inside"},
                 action={"type": "ANNUNCIATE", "severity": "CRITICAL",
                         "message_template": "{family} inside {zone_name}"})

    firings = evaluate(inside_contact, ZONES, [rule])
    assert len(firings) == 1
    assert firings[0]["action_type"] == "ANNUNCIATE"
    assert firings[0]["message"] == "DJI inside Alpha Sector"


def test_positioned_contact_inside_rule_misses_when_out_of_zone():
    outside_contact = _positioned_contact(50, 50)  # far outside the square
    rule = _rule(zone_id="zone-1", conditions={"zone_membership": "inside"})
    assert evaluate(outside_contact, ZONES, [rule]) == []


def test_positioned_contact_matches_outside_when_out_of_zone():
    outside_contact = _positioned_contact(50, 50)
    rule = _rule(zone_id="zone-1", conditions={"zone_membership": "outside"})
    firings = evaluate(outside_contact, ZONES, [rule])
    assert len(firings) == 1


def test_positioned_contact_outside_rule_misses_when_in_zone():
    inside_contact = _positioned_contact(5, 5)
    rule = _rule(zone_id="zone-1", conditions={"zone_membership": "outside"})
    assert evaluate(inside_contact, ZONES, [rule]) == []


def test_spatial_inside_rule_with_unknown_zone_is_honest_miss():
    """A positioned contact against a zone_id that is not in `zones` cannot be
    evaluated for containment -> honest miss, not a crash or a fabricated
    'inside'."""
    inside_contact = _positioned_contact(5, 5)
    rule = _rule(zone_id="does-not-exist", conditions={"zone_membership": "inside"})
    assert evaluate(inside_contact, ZONES, [rule]) == []


# --------------------------------------------------------------------------
# require_position semantics
# --------------------------------------------------------------------------
def test_require_position_true_matches_positioned_contact():
    rule = _rule(conditions={"require_position": True, "class_in": ["multirotor"]})
    assert len(evaluate(_positioned_contact(5, 5), ZONES, [rule])) == 1


def test_require_position_true_misses_positionless_contact():
    rule = _rule(conditions={"require_position": True})
    assert evaluate(_positionless_contact(), ZONES, [rule]) == []


def test_position_source_present_but_no_coords_is_positionless():
    """A contact that claims a position_source but has no lon/lat is treated
    as position-less (we do not fabricate coords) and cannot match a spatial
    rule."""
    contact = _positionless_contact(position_source="remoteid")  # no lon/lat
    rule = _rule(conditions={"require_position": True})
    assert evaluate(contact, ZONES, [rule]) == []


# --------------------------------------------------------------------------
# Each non-spatial condition, independently: match AND non-match
# --------------------------------------------------------------------------
def test_protocol_in_match_and_nonmatch():
    match = _rule(conditions={"protocol_in": ["control_link", "wifi"]})
    miss = _rule(conditions={"protocol_in": ["remoteid"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_class_in_match_and_nonmatch():
    match = _rule(conditions={"class_in": ["control_link"]})
    miss = _rule(conditions={"class_in": ["multirotor"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_family_in_match_and_nonmatch():
    match = _rule(conditions={"family_in": ["OcuSync"]})
    miss = _rule(conditions={"family_in": ["Lightbridge"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_band_in_match_and_nonmatch():
    match = _rule(conditions={"band_in": ["2.4GHz", "5.8GHz"]})
    miss = _rule(conditions={"band_in": ["900MHz"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_min_confidence_match_and_nonmatch():
    match = _rule(conditions={"min_confidence": 0.8})   # contact is 0.82
    miss = _rule(conditions={"min_confidence": 0.9})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_min_confidence_missing_contact_confidence_misses():
    rule = _rule(conditions={"min_confidence": 0.5})
    contact = _positionless_contact(confidence=None)
    assert evaluate(contact, ZONES, [rule]) == []


def test_confidence_type_in_match_and_nonmatch():
    match = _rule(conditions={"confidence_type_in": ["heuristic_binary"]})
    miss = _rule(conditions={"confidence_type_in": ["protocol_verified"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_threat_level_in_match_and_nonmatch():
    match = _rule(conditions={"threat_level_in": ["HIGH", "CRITICAL"]})
    miss = _rule(conditions={"threat_level_in": ["LOW"]})
    assert len(evaluate(_positionless_contact(), ZONES, [match])) == 1
    assert evaluate(_positionless_contact(), ZONES, [miss]) == []


def test_conditions_are_and_combined():
    """All conditions must hold; one failing condition rejects the firing."""
    rule = _rule(conditions={
        "class_in": ["control_link"],       # matches
        "threat_level_in": ["LOW"],         # does NOT match (contact is HIGH)
    })
    assert evaluate(_positionless_contact(), ZONES, [rule]) == []


# --------------------------------------------------------------------------
# CUE_RECOMMENDATION -- label-only proposal
# --------------------------------------------------------------------------
def test_cue_recommendation_is_label_only_proposal():
    rule = _rule(
        conditions={"class_in": ["control_link"]},
        action={"type": "CUE_RECOMMENDATION", "severity": "WARNING",
                "message_template": "consider countermeasure",
                "recommended_effect": "jam"},
    )
    firings = evaluate(_positionless_contact(), ZONES, [rule])
    assert len(firings) == 1
    cue = firings[0]["cue"]
    assert cue is not None
    assert cue["status"] == PROPOSED_STATUS
    assert cue["status"] == "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"
    # recommended_effect is carried through as a DISPLAY LABEL only.
    assert cue["recommended_effect"] == "jam"


def test_non_cue_actions_have_null_cue():
    for atype in ("ALERT", "ANNUNCIATE", "PRIORITIZE"):
        rule = _rule(conditions={"class_in": ["control_link"]},
                     action={"type": atype, "severity": "INFO",
                             "message_template": "x", "rank_boost": 5})
        firings = evaluate(_positionless_contact(), ZONES, [rule])
        assert len(firings) == 1
        assert firings[0]["cue"] is None
        assert firings[0]["rank_boost"] == 5


# --------------------------------------------------------------------------
# Rejection of non-allowed action types (no fire/engage/deploy)
# --------------------------------------------------------------------------
def test_fire_type_action_is_rejected():
    for bad in ("FIRE", "ENGAGE", "DEPLOY", "JAM", "TRANSMIT", None, ""):
        rule = _rule(conditions={"class_in": ["control_link"]},
                     action={"type": bad, "severity": "CRITICAL",
                             "message_template": "should never fire"})
        assert evaluate(_positionless_contact(), ZONES, [rule]) == []


# --------------------------------------------------------------------------
# enabled / ordering
# --------------------------------------------------------------------------
def test_disabled_rule_is_skipped():
    rule = _rule(enabled=False, conditions={"class_in": ["control_link"]})
    assert evaluate(_positionless_contact(), ZONES, [rule]) == []


def test_firings_ordered_by_priority_then_input_order():
    low = _rule(id="low", priority=1, conditions={"class_in": ["control_link"]})
    high = _rule(id="high", priority=100, conditions={"class_in": ["control_link"]})
    mid_a = _rule(id="mid-a", priority=50, conditions={"class_in": ["control_link"]})
    mid_b = _rule(id="mid-b", priority=50, conditions={"class_in": ["control_link"]})

    firings = evaluate(_positionless_contact(), ZONES, [low, mid_a, mid_b, high])
    ids = [f["rule_id"] for f in firings]
    # highest priority first; equal priority keeps input order (mid-a < mid-b).
    assert ids == ["high", "mid-a", "mid-b", "low"]


# --------------------------------------------------------------------------
# message_template rendering resilience
# --------------------------------------------------------------------------
def test_message_template_missing_key_does_not_crash():
    rule = _rule(
        conditions={"class_in": ["control_link"]},
        action={"type": "ALERT", "severity": "INFO",
                "message_template": "{family} at {nonexistent_field} zone={zone_name}"},
    )
    firings = evaluate(_positionless_contact(), ZONES, [rule])
    assert len(firings) == 1
    # Missing key renders as "" -- no KeyError, output still produced.
    assert firings[0]["message"] == "OcuSync at  zone="


def test_message_template_malformed_does_not_crash():
    rule = _rule(
        conditions={"class_in": ["control_link"]},
        action={"type": "ALERT", "severity": "INFO",
                "message_template": "unbalanced {brace"},
    )
    firings = evaluate(_positionless_contact(), ZONES, [rule])
    assert len(firings) == 1  # falls back to raw template, no crash


# --------------------------------------------------------------------------
# STATIC SAFETY TEST -- the enforced inertness invariant
# --------------------------------------------------------------------------
_SOP_SRC_PATH = Path(__file__).resolve().parent.parent / "sop_engine.py"

# Forbidden identifiers: any reference to the transmit spine / arm-token /
# tx-halt / deploy chain would break invariant #1. "iff" is checked as a whole
# word so it does not false-positive on "diff"/"tariff"/etc.
_FORBIDDEN_SUBSTRINGS = [
    "tx_halt",
    "_tx_halted",
    "arm_token",
    "range_auth",
    "require_commander",
    "deploy_jam",
    "deploy_gnss",
    "broadcast_packet",
    "mavlink_inject",
    "import server",
]
_FORBIDDEN_WORDS = ["iff"]


def test_source_references_no_transmit_or_armtoken_symbols():
    src = _SOP_SRC_PATH.read_text().lower()
    for tok in _FORBIDDEN_SUBSTRINGS:
        assert tok not in src, f"forbidden identifier {tok!r} present in sop_engine.py"
    for word in _FORBIDDEN_WORDS:
        assert not re.search(rf"\b{re.escape(word)}\b", src), \
            f"forbidden word {word!r} present in sop_engine.py"


def test_only_nonstdlib_import_is_geo_zone():
    """Parse sop_engine.py's import statements with `ast` and assert that the
    ONLY module it imports outside the standard library is `geo_zone`."""
    tree = ast.parse(_SOP_SRC_PATH.read_text())
    imported_top_levels = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_top_levels.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 would be a relative import (there are none); module may
            # be None only for `from . import x`, which we also disallow here.
            if node.module:
                imported_top_levels.add(node.module.split(".")[0])

    # Classify each imported top-level as stdlib vs non-stdlib WITHOUT relying
    # on sys.stdlib_module_names (added in 3.10). A module is stdlib if it is
    # built-in/frozen or its source file lives under the stdlib directory; the
    # local `geo_zone` resolves to the backend directory instead.
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
    assert non_stdlib == ["geo_zone"], (
        f"sop_engine.py must import only stdlib + geo_zone; "
        f"unexpected non-stdlib imports: {non_stdlib}"
    )


def test_module_exposes_no_transmit_callable():
    """Defensive: the public surface is `evaluate` + constants only -- there is
    no deploy/transmit/arm function to call."""
    public = [n for n in dir(sop_engine) if not n.startswith("_")]
    banned = {"deploy", "transmit", "jam", "arm", "fire", "engage", "broadcast"}
    assert banned.isdisjoint({p.lower() for p in public})
