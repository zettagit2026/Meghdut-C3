"""Zone/SOP no-code rules engine -- pure evaluation core (P2 of
zone-sop-engine.md).

WHAT THIS IS
------------
`evaluate(contact, zones, rules) -> [RuleFiring]`. Given ONE enriched contact
(a plain dict), the current zones, and the operator's SOP rules, decide which
rules fire and produce a plain-dict firing for each. Plain dicts/lists in,
plain dicts out. No Mongo, no websocket, no FastAPI, no threading. The ONLY
non-stdlib import is `geo_zone` (point-in-zone geometry). This module is a
pure leaf, exactly like `engagement_planner.py` and `geo_zone.py`.

WHAT THIS IS NOT -- CRITICAL SAFETY BOUNDARY (governing invariant #1)
--------------------------------------------------------------------
This engine has NO capability to transmit, jam, spoof, arm, key TX, clear a
TX-halt, mint any token, or mutate a detection/track. It imports NOTHING from
the transmit spine. Its strongest possible output is a
CUE_RECOMMENDATION whose `recommended_effect` is a DISPLAY LABEL ONLY, stamped
`PROPOSED_REQUIRES_HUMAN_AUTHORIZATION` (identical wording to
engagement_planner). There is deliberately NO code path here from a
`recommended_effect` string to any deploy/transmit call, and the action-type
enum deliberately has no fire/engage/deploy member -- any rule asking for one
is rejected. A static test (test_sop_engine.py) parses this file's imports and
asserts it references no arm-token / tx-halt / deploy symbol; that test is the
enforced form of this invariant.

HONESTY -- NO FABRICATED POSITIONS (governing invariant #2)
-----------------------------------------------------------
The primary detection stream is position-less (RSSI-only, no lat/lon). Only a
contact carrying a real `position_source` (RemoteID / ADS-B / DroneID-with-
position) plus `lon`/`lat` can be evaluated against a zone. A SPATIAL rule
(one whose conditions set `zone_membership` to "inside"/"outside", or set
`require_position: true`) simply DOES NOT MATCH a position-less contact -- an
honest miss. This module NEVER invents a coordinate to make a spatial rule
apply. Non-spatial conditions (protocol/class/family/band/confidence/threat)
are evaluated for every contact regardless of position.
"""
from __future__ import annotations

from typing import Any

import geo_zone

# The ONLY action types this engine will honour. There is deliberately NO
# fire / engage / deploy / jam member: the engine's strongest action is a
# proposal a human commander still clears through the existing gated
# endpoints. A rule whose action.type is outside this set is rejected (it
# produces no firing), so a mis-authored or malicious "engage" action is inert.
ALLOWED_ACTION_TYPES = frozenset({
    "ALERT",
    "ANNUNCIATE",
    "PRIORITIZE",
    "CUE_RECOMMENDATION",
})

# Exact wording mirrored from engagement_planner.PROPOSED_STATUS. A CUE is a
# SUGGESTION LABEL, never an authorization and never an effect call.
PROPOSED_STATUS = "PROPOSED_REQUIRES_HUMAN_AUTHORIZATION"


class _SafeFormatDict(dict):
    """dict for str.format_map that yields "" for any missing key, so a
    message_template referencing a field the contact does not have degrades
    gracefully instead of raising KeyError."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return ""


def _has_position(contact: dict) -> bool:
    """True when the contact carries a REAL position we may use for zone
    containment: a truthy `position_source` AND both lon and lat present.

    A contact without a position_source (the position-less RSSI-only default),
    or one missing lon/lat, has no usable position -- and we never fabricate
    one.
    """
    if not contact.get("position_source"):
        return False
    return contact.get("lon") is not None and contact.get("lat") is not None


def _is_spatial(conditions: dict) -> bool:
    """A rule is SPATIAL if it requires a position or checks zone membership
    (inside/outside). `zone_membership == "any"` with `require_position` false
    is NON-spatial and applies to every contact."""
    if conditions.get("require_position"):
        return True
    return conditions.get("zone_membership") in ("inside", "outside")


def _find_zone(zones: list[dict], zone_id: Any) -> dict | None:
    if zone_id is None:
        return None
    for zone in zones:
        if zone.get("id") == zone_id:
            return zone
    return None


def _spatial_match(contact: dict, rule: dict, zones: list[dict]) -> bool:
    """Evaluate the spatial gate of a rule against a contact.

    Honest-miss rules (governing invariant #2):
      * a position-less contact can NEVER satisfy a spatial rule -> False;
      * an inside/outside check whose zone_id resolves to no known zone cannot
        be evaluated -> False (we do not guess containment).
    A non-spatial rule always passes this gate.
    """
    conditions = rule.get("conditions") or {}
    if not _is_spatial(conditions):
        return True

    # Spatial rule: it needs a real position. No position -> honest miss.
    if not _has_position(contact):
        return False

    membership = conditions.get("zone_membership")
    if membership not in ("inside", "outside"):
        # require_position was set but no in/out zone test requested: having a
        # position is sufficient.
        return True

    zone = _find_zone(zones, rule.get("zone_id"))
    if zone is None:
        return False  # cannot evaluate containment -> honest miss

    contained = geo_zone.point_in_zone(
        {"lon": contact["lon"], "lat": contact["lat"]}, zone
    )
    if membership == "inside":
        return contained
    return not contained  # "outside"


def _in(value: Any, allowed: Any) -> bool:
    """Membership test that treats an empty list / None as "don't care" (pass).
    Otherwise the contact value must be a member of `allowed`."""
    if not allowed:
        return True
    return value in allowed


def _nonspatial_match(contact: dict, conditions: dict) -> bool:
    """AND-combine every non-spatial condition. Each is optional; an empty
    list / None means "don't care"."""
    if not _in(contact.get("protocol"), conditions.get("protocol_in")):
        return False
    if not _in(contact.get("class"), conditions.get("class_in")):
        return False
    if not _in(contact.get("family"), conditions.get("family_in")):
        return False
    if not _in(contact.get("band"), conditions.get("band_in")):
        return False
    if not _in(contact.get("confidence_type"), conditions.get("confidence_type_in")):
        return False
    if not _in(contact.get("threat_level"), conditions.get("threat_level_in")):
        return False

    min_conf = conditions.get("min_confidence")
    if min_conf is not None:
        conf = contact.get("confidence")
        if conf is None or conf < min_conf:
            return False

    return True


def _render_message(template: Any, context: dict) -> str:
    """Render `template` with `context`, tolerating missing keys (they render
    as "") and never raising on a malformed template."""
    if not template:
        return ""
    try:
        return str(template).format_map(_SafeFormatDict(context))
    except (ValueError, IndexError, KeyError):
        # A malformed template (e.g. an unbalanced brace) must not crash the
        # engine; fall back to the raw template text.
        return str(template)


def _build_firing(contact: dict, rule: dict, zone: dict | None) -> dict:
    action = rule.get("action") or {}
    action_type = action.get("type")

    context = dict(contact)
    context["zone_id"] = rule.get("zone_id")
    context["zone_name"] = (zone or {}).get("name", "")

    cue = None
    if action_type == "CUE_RECOMMENDATION":
        # LABEL ONLY. This does not, and cannot, trigger the recommended
        # effect -- there is no transmit path in this module. The commander
        # clears the effect through the existing gated endpoints.
        cue = {
            "recommended_effect": action.get("recommended_effect"),
            "status": PROPOSED_STATUS,
        }

    return {
        "rule_id": rule.get("id"),
        "rule_name": rule.get("name"),
        "zone_id": rule.get("zone_id"),
        "action_type": action_type,
        "severity": action.get("severity"),
        "message": _render_message(action.get("message_template"), context),
        "cue": cue,
        "rank_boost": action.get("rank_boost"),
    }


def evaluate(contact: dict, zones: list[dict], rules: list[dict]) -> list[dict]:
    """Evaluate `contact` against every enabled `rule`, returning a list of
    RuleFiring dicts (one per rule that fully matches).

    A RuleFiring is:
        {rule_id, rule_name, zone_id|None, action_type, severity, message,
         cue, rank_boost}
    where `cue` is None for ALERT/ANNUNCIATE/PRIORITIZE and, for
    CUE_RECOMMENDATION, is
        {"recommended_effect": <label|None>, "status": PROPOSED_STATUS}.

    Matching per rule:
      * skip if `enabled is False`;
      * skip if `action.type` is not one of ALLOWED_ACTION_TYPES (defensive --
        there is no fire/engage/deploy action);
      * SPATIAL gate: a spatial rule (zone_membership in/out, or
        require_position) cannot match a position-less contact (honest miss);
        a positioned contact is tested for containment of the rule's zone_id;
      * NON-SPATIAL gate: protocol_in / class_in / family_in / band_in
        (membership), min_confidence (>=), confidence_type_in, threat_level_in
        -- AND-combined, each optional.

    Firings are returned ordered by rule `priority` (higher priority first),
    ties preserving input order (stable sort).
    """
    contact = contact or {}
    zones = zones or []
    rules = rules or []

    firings: list[dict] = []
    for order, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        if rule.get("enabled") is False:
            continue

        action = rule.get("action") or {}
        if action.get("type") not in ALLOWED_ACTION_TYPES:
            continue  # reject fire/engage/deploy or unknown action types

        conditions = rule.get("conditions") or {}

        if not _spatial_match(contact, rule, zones):
            continue
        if not _nonspatial_match(contact, conditions):
            continue

        zone = _find_zone(zones, rule.get("zone_id"))
        firing = _build_firing(contact, rule, zone)
        # Carry sort keys transiently; stripped before returning.
        firings.append((rule.get("priority") or 0, order, firing))

    # Higher priority first; stable within equal priority via input order.
    firings.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in firings]
