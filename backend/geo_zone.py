"""Zone/SOP engine -- geospatial primitives (P1 of zone-sop-engine.md).

WHAT THIS IS
------------
Pure point-in-polygon geometry for zone containment: `point_in_polygon`,
`point_in_zone`, `zones_containing`, plus a `validate_ring` guard. Everything
here is plain dicts/lists in, plain values out -- no I/O, no Mongo, no
websocket, no network. No new dependency: ray-casting is implemented with
stdlib only (numpy is available in requirements.txt but not needed for ~30
lines of arithmetic).

COORDINATE CONVENTION -- READ THIS BEFORE TOUCHING ANYTHING HERE
------------------------------------------------------------------
Every coordinate pair in this module is **[lon, lat]** (GeoJSON / Map.jsx
convention), NEVER [lat, lon]. `point_in_polygon(lon, lat, ring)` takes lon
and lat as separate positional args (in that order) specifically so callers
cannot silently swap them by passing a 2-tuple in the wrong order.

BOUNDARY CONVENTION
--------------------
A point exactly ON an edge, or exactly ON a vertex, of the OUTER (exterior)
ring is treated as INSIDE that ring. This is a deliberate choice (not the
only valid one) so that zone-edge contacts are not silently dropped from a
safety-relevant ALERT/ANNUNCIATE rule. `point_in_polygon` special-cases
on-boundary points before running the generic ray-casting crossing-number
test, so the result is exact (no floating epsilon guessing) for on-segment
and on-vertex cases.

The same safety-conservative principle applies, uniformly, to HOLE rings --
but it flips which side of the boundary is "safe": a hole should only
exclude its STRICT INTERIOR. A point exactly on a hole's boundary edge or
vertex is treated as NOT strictly inside the hole, i.e. it stays IN-ZONE.
This keeps the policy uniform in intent (a boundary point is never silently
dropped from a safety rule) even though outer and hole rings use opposite
`boundary_is_inside` settings to get there: for the outer ring, boundary=IN
maximizes zone membership; for a hole, boundary=IN (of the hole) would
minimize zone membership, so holes use boundary=NOT-in-hole (i.e. the hole
only claims its strict interior) to get the same "never silently dropped"
outcome. `point_in_polygon` exposes this via the keyword-only
`boundary_is_inside` parameter (default True, matching the original/outer
behaviour unchanged).

MULTI-RING / HOLES
-------------------
`point_in_zone` follows GeoJSON Polygon winding: `polygon["coordinates"][0]`
is the exterior ring, every subsequent ring is a hole. A point is "in zone"
iff it is inside the exterior ring (boundary counts as inside) AND NOT
strictly inside any of the holes (a hole's own boundary does NOT exclude).

KNOWN LIMITATION -- ANTIMERIDIAN
----------------------------------
This module does NOT special-case polygons that cross the +/-180 degree
antimeridian. A ring is treated as a flat set of [lon, lat] points in a
plane; a "global" polygon whose edges are meant to wrap around the dateline
will be evaluated with ordinary (non-wrapping) longitude arithmetic and will
therefore produce results that do not match the operator's real-world
intent near lon=+/-180. This is intentionally NOT worked around here (no
lon-normalization, no wrap-aware edge splitting) -- see
test_geo_zone.py::test_antimeridian_known_limitation, which documents and
locks in the current (unsupported) behaviour rather than faking correctness.
"""
from __future__ import annotations

from typing import Any


def validate_ring(ring: Any) -> tuple[bool, str]:
    """Validate a GeoJSON-style ring: list of [lon, lat] pairs.

    Rules:
      * must be a list/tuple of at least 3 DISTINCT vertices (after any
        closing vertex is dropped for the distinctness check),
      * every vertex must be a 2-element [lon, lat] pair,
      * lon in [-180, 180], lat in [-90, 90],
      * if the ring is not already closed (first vertex != last vertex) it
        is considered auto-closable, not an error.

    Returns (ok, reason). reason is "" when ok is True.
    """
    if not isinstance(ring, (list, tuple)) or len(ring) == 0:
        return False, "ring must be a non-empty list of [lon, lat] pairs"

    points = list(ring)

    for i, pt in enumerate(points):
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return False, f"vertex {i} is not a [lon, lat] pair"
        lon, lat = pt
        if not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
            return False, f"vertex {i} coordinates must be numeric"
        if not (-180.0 <= lon <= 180.0):
            return False, f"vertex {i} lon {lon} out of range [-180,180]"
        if not (-90.0 <= lat <= 90.0):
            return False, f"vertex {i} lat {lat} out of range [-90,90]"

    # Auto-close: if first != last, treat as an open ring that would be
    # closed by appending the first vertex again. Distinctness is judged on
    # the ring WITHOUT a trailing duplicate-of-first closing vertex.
    distinct = points[:-1] if len(points) > 1 and tuple(points[0]) == tuple(points[-1]) else points

    unique = {tuple(p) for p in distinct}
    if len(unique) < 3:
        return False, "ring must have at least 3 distinct vertices"

    return True, ""


def _closed_ring(ring: list[list[float]]) -> list[list[float]]:
    """Return `ring` with the closing vertex appended if it is missing."""
    if len(ring) > 1 and tuple(ring[0]) == tuple(ring[-1]):
        return list(ring)
    return list(ring) + [ring[0]]


def _on_segment(lon: float, lat: float, a: list[float], b: list[float]) -> bool:
    """True iff (lon, lat) lies exactly on the closed segment a-b (collinear
    and within the segment's bounding box)."""
    ax, ay = a
    bx, by = b
    cross = (bx - ax) * (lat - ay) - (by - ay) * (lon - ax)
    if cross != 0:
        return False
    if min(ax, bx) <= lon <= max(ax, bx) and min(ay, by) <= lat <= max(ay, by):
        return True
    return False


def point_in_polygon(
    lon: float,
    lat: float,
    ring: list[list[float]],
    *,
    boundary_is_inside: bool = True,
) -> bool:
    """Ray-casting / crossing-number point-in-ring test.

    `ring` is a list of [lon, lat] pairs; it is auto-closed if the first and
    last vertices differ. A point exactly on an edge or on a vertex is
    reported as `boundary_is_inside` (default True, matching the original
    behaviour -- see module docstring "BOUNDARY CONVENTION"). Callers testing
    a GeoJSON hole ring pass `boundary_is_inside=False` so the hole only
    claims its strict interior and its own boundary does not exclude.
    """
    closed = _closed_ring(list(ring))

    # Boundary check first: exact edge/vertex membership is deterministic
    # and must not depend on which way the generic ray happens to cross.
    for i in range(len(closed) - 1):
        if _on_segment(lon, lat, closed[i], closed[i + 1]):
            return boundary_is_inside

    inside = False
    n = len(closed) - 1  # closed[-1] == closed[0]
    for i in range(n):
        x1, y1 = closed[i]
        x2, y2 = closed[i + 1]
        # Does the horizontal ray at `lat`, going in +lon direction, cross
        # edge (x1,y1)-(x2,y2)?
        crosses = (y1 > lat) != (y2 > lat)
        if crosses:
            x_intersect = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_intersect:
                inside = not inside

    return inside


def point_in_zone(pos: dict, zone: dict) -> bool:
    """True iff `pos` ({"lon":.., "lat":..}) is inside `zone`'s polygon.

    `zone["polygon"]` is a GeoJSON-style dict:
        {"type": "Polygon", "coordinates": [outer_ring, hole_ring, ...]}
    Point is in-zone iff inside coordinates[0] (exterior, boundary=inside)
    AND NOT strictly inside any of coordinates[1:] (holes, boundary=NOT
    inside -- a hole excludes only its strict interior; see module docstring
    "BOUNDARY CONVENTION").
    """
    polygon = zone.get("polygon") or {}
    rings = polygon.get("coordinates") or []
    if not rings:
        return False

    lon = pos["lon"]
    lat = pos["lat"]

    if not point_in_polygon(lon, lat, rings[0]):
        return False

    for hole in rings[1:]:
        if point_in_polygon(lon, lat, hole, boundary_is_inside=False):
            return False

    return True


def zones_containing(pos: dict, zones: list[dict]) -> list[dict]:
    """Return the subset of `zones` whose polygon contains `pos`, preserving
    input order. Zones with enabled == False are skipped."""
    result = []
    for zone in zones:
        if zone.get("enabled") is False:
            continue
        if point_in_zone(pos, zone):
            result.append(zone)
    return result
