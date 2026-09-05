"""Unit tests for backend/geo_zone.py (P1 of zone-sop-engine.md).

True unit tests: pure functions over plain lists/dicts, no live server /
Mongo / requests, following the same pattern as test_engagement_planner.py /
test_swarm_classifier.py.

Coordinate convention throughout: [lon, lat] (GeoJSON), never [lat, lon].

Run: pytest backend/tests/test_geo_zone.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from geo_zone import (  # noqa: E402
    point_in_polygon,
    point_in_zone,
    validate_ring,
    zones_containing,
)


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
SQUARE_RING = [[0, 0], [10, 0], [10, 10], [0, 10]]

# A "C" shape opening to the left: the outer square with a rectangular
# notch (x in [0,6], y in [4,6]) cut out of the left side. Its bounding
# box is identical to SQUARE_RING's, so a naive bbox-only test would
# wrongly call a point inside the notch "inside".
C_SHAPE_RING = [
    [0, 0], [10, 0], [10, 10], [0, 10],
    [0, 6], [6, 6], [6, 4], [0, 4],
]

HOLE_RING = [[3, 3], [7, 3], [7, 7], [3, 7]]


def _zone(zone_id, ring, *, holes=None, enabled=True, zone_type="ALERT"):
    coords = [ring] + list(holes or [])
    return {
        "id": zone_id,
        "name": zone_id,
        "zone_type": zone_type,
        "enabled": enabled,
        "polygon": {"type": "Polygon", "coordinates": coords},
    }


# --------------------------------------------------------------------------
# point_in_polygon -- basic square
# --------------------------------------------------------------------------
def test_point_clearly_inside_square():
    assert point_in_polygon(5, 5, SQUARE_RING) is True


def test_point_clearly_outside_square():
    assert point_in_polygon(15, 5, SQUARE_RING) is False


def test_point_on_edge_counts_as_inside():
    # Documented boundary convention: on-edge counts as inside.
    assert point_in_polygon(5, 0, SQUARE_RING) is True


def test_point_on_vertex_counts_as_inside():
    # Documented boundary convention: on-vertex counts as inside.
    assert point_in_polygon(0, 0, SQUARE_RING) is True
    assert point_in_polygon(10, 10, SQUARE_RING) is True


def test_point_in_polygon_boundary_is_inside_false_flips_edge_and_vertex():
    # The keyword-only boundary_is_inside option (used internally for holes)
    # must flip the on-boundary result without touching the default.
    assert point_in_polygon(5, 0, SQUARE_RING, boundary_is_inside=False) is False
    assert point_in_polygon(0, 0, SQUARE_RING, boundary_is_inside=False) is False
    # Default (omitted) is unchanged: boundary still counts as inside.
    assert point_in_polygon(5, 0, SQUARE_RING) is True


# --------------------------------------------------------------------------
# point_in_polygon -- concave (C-shape) where a bbox test would be wrong
# --------------------------------------------------------------------------
def test_concave_notch_point_is_outside_despite_being_in_bounding_box():
    # (3, 5) sits inside the outer square's bounding box [0,10]x[0,10],
    # but it is inside the notch cut out of the C -- a naive bbox check
    # would wrongly say "inside"; the real polygon must say False.
    assert point_in_polygon(3, 5, C_SHAPE_RING) is False


def test_concave_solid_arm_point_is_inside():
    # (8, 5) sits in the solid right arm of the C -- genuinely inside.
    assert point_in_polygon(8, 5, C_SHAPE_RING) is True


def test_concave_far_outside_point_is_outside():
    assert point_in_polygon(20, 20, C_SHAPE_RING) is False


# --------------------------------------------------------------------------
# point_in_zone -- polygon with a hole
# --------------------------------------------------------------------------
def test_point_in_hole_is_not_in_zone():
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 5, "lat": 5}, zone) is False  # inside the hole


def test_point_in_solid_part_around_hole_is_in_zone():
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 1, "lat": 1}, zone) is True  # outside hole, inside outer


def test_point_outside_outer_ring_is_not_in_zone_even_without_hole_check():
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 50, "lat": 50}, zone) is False


def test_point_strictly_inside_hole_is_excluded_from_zone():
    # (5, 5) is the hole's center -- strictly inside it, not on any edge.
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 5, "lat": 5}, zone) is False


def test_point_on_hole_boundary_edge_is_in_zone():
    # (3, 5) sits exactly on the hole's left edge (x=3, y in [3,7]).
    # Boundary convention: a hole excludes only its STRICT interior, so a
    # point on the hole's own boundary is NOT excluded -- it stays in-zone.
    # This is the opposite direction from a naive "boundary=inside" hole
    # check, which would wrongly exclude it (less safe).
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 3, "lat": 5}, zone) is True


def test_point_on_hole_boundary_vertex_is_in_zone():
    # (3, 3) is a hole vertex -- also must not be excluded.
    zone = _zone("z-hole", SQUARE_RING, holes=[HOLE_RING])
    assert point_in_zone({"lon": 3, "lat": 3}, zone) is True


# --------------------------------------------------------------------------
# validate_ring
# --------------------------------------------------------------------------
def test_validate_ring_accepts_valid_square():
    ok, reason = validate_ring(SQUARE_RING)
    assert ok is True
    assert reason == ""


def test_validate_ring_accepts_already_closed_ring():
    closed = SQUARE_RING + [SQUARE_RING[0]]
    ok, _ = validate_ring(closed)
    assert ok is True


def test_validate_ring_rejects_degenerate_ring_under_three_vertices():
    ok, reason = validate_ring([[0, 0], [1, 1]])
    assert ok is False
    assert "3 distinct vertices" in reason


def test_validate_ring_rejects_three_points_that_collapse_to_two_distinct():
    # First == last (auto-close vertex) leaves only 2 distinct points.
    ok, reason = validate_ring([[0, 0], [1, 1], [0, 0]])
    assert ok is False
    assert "3 distinct vertices" in reason


def test_validate_ring_rejects_out_of_range_lon():
    ok, reason = validate_ring([[200, 0], [1, 1], [1, 0]])
    assert ok is False
    assert "lon" in reason


def test_validate_ring_rejects_out_of_range_lat():
    ok, reason = validate_ring([[0, 0], [1, 95], [1, 0]])
    assert ok is False
    assert "lat" in reason


# --------------------------------------------------------------------------
# zones_containing
# --------------------------------------------------------------------------
def test_zones_containing_returns_matching_subset_preserving_order():
    z1 = _zone("z1", SQUARE_RING)  # contains (5,5)
    z2 = _zone("z2", [[100, 100], [110, 100], [110, 110], [100, 110]])  # does not
    z3 = _zone("z3", [[0, 0], [20, 0], [20, 20], [0, 20]])  # also contains (5,5)

    result = zones_containing({"lon": 5, "lat": 5}, [z1, z2, z3])

    assert [z["id"] for z in result] == ["z1", "z3"]


def test_zones_containing_skips_disabled_zones():
    enabled_zone = _zone("z-on", SQUARE_RING, enabled=True)
    disabled_zone = _zone("z-off", SQUARE_RING, enabled=False)  # geometrically contains too

    result = zones_containing({"lon": 5, "lat": 5}, [enabled_zone, disabled_zone])

    assert [z["id"] for z in result] == ["z-on"]


def test_zones_containing_empty_when_no_match():
    z1 = _zone("z1", SQUARE_RING)
    result = zones_containing({"lon": 500, "lat": 500}, [z1])
    assert result == []


# --------------------------------------------------------------------------
# KNOWN LIMITATION: antimeridian (lon wrap +/-180) is NOT supported.
# This test documents and locks in the CURRENT (unsupported) behaviour --
# it does not fake correctness for dateline-crossing polygons.
# --------------------------------------------------------------------------
def test_antimeridian_known_limitation():
    # A polygon intended (by an operator drawing on a real map) to span the
    # dateline from lon=170 eastward, wrapping through +/-180, to lon=-170,
    # 0..10 lat. A point at (175, 5) is "inside" that intended wrapping
    # rectangle in real-world terms. This module has NO antimeridian
    # handling -- coordinates are treated as flat plane values -- so the
    # edge from (170,0) to (-170,0) is a straight line straight across the
    # map (NOT a short hop across the dateline), and the point is reported
    # as OUTSIDE. Global/dateline-spanning zones are unsupported; this
    # assertion pins down the honest current behaviour rather than
    # pretending wrap-around works.
    dateline_ring = [[170, 0], [-170, 0], [-170, 10], [170, 10]]
    assert point_in_polygon(175, 5, dateline_ring) is False
