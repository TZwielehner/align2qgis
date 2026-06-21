"""Geometry + parser tests that run without QGIS installed."""
from __future__ import annotations

import math
import sys
from pathlib import Path

# Make ``plugin`` importable as a top-level package for tests.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import (  # noqa: E402
    ArcPiece,
    LinePiece,
    _locate_walker,
    alignment_chainage,
    alignment_curve_pieces,
    alignment_polyline,
    alignment_xy_at_station,
    arc_points,
    line_points,
    segment_curve_pieces,
    spiral_arc_triples,
    spiral_points,
)
from plugin.landxml_parser import (  # noqa: E402
    Alignment,
    CurveSeg,
    LineSeg,
    SpiralSeg,
    parse_alignments,
)


def _close(a, b, tol=1e-6):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
SAMPLE_XML = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <Alignments>
    <Alignment name="A1" length="200.0" staStart="0.0">
      <CoordGeom>
        <Line length="100.0">
          <Start>1000.0 2000.0</Start>
          <End>1000.0 2100.0</End>
        </Line>
        <Curve rot="ccw" radius="50.0" length="78.539816">
          <Start>1000.0 2100.0</Start>
          <Center>1050.0 2100.0</Center>
          <End>1050.0 2150.0</End>
        </Curve>
      </CoordGeom>
    </Alignment>
  </Alignments>
</LandXML>
"""


def test_parser_reads_line_and_curve():
    alignments = parse_alignments(SAMPLE_XML.encode("utf-8"))
    assert len(alignments) == 1
    a = alignments[0]
    assert a.name == "A1"
    assert a.length == 200.0
    assert len(a.segments) == 2
    assert isinstance(a.segments[0], LineSeg)
    assert isinstance(a.segments[1], CurveSeg)
    assert a.segments[1].radius == 50.0
    # LandXML order: (north=1000, east=2000) -> stored as (1000, 2000)
    assert a.segments[0].start == (1000.0, 2000.0)


# ---------------------------------------------------------------------------
# Line
# ---------------------------------------------------------------------------
def test_line_points_swap_axes():
    seg = LineSeg(start=(1000.0, 2000.0), end=(1000.0, 2100.0))
    pts = line_points(seg)
    # LandXML (N=1000, E=2000) -> QGIS (x=2000, y=1000)
    assert pts == [(2000.0, 1000.0), (2100.0, 1000.0)]


# ---------------------------------------------------------------------------
# Arc — quarter circle, radius 50, center east of start
# ---------------------------------------------------------------------------
def test_arc_quarter_circle_length():
    # Start west of center, end north of center. The short way (quarter circle)
    # is clockwise in mathematical convention.
    seg = CurveSeg(
        start=(1000.0, 2100.0),  # N=1000, E=2100
        center=(1000.0, 2150.0),  # N=1000, E=2150
        end=(1050.0, 2150.0),  # N=1050, E=2150
        radius=50.0,
        rot="cw",
    )
    pts = arc_points(seg, max_chord_err=0.001)
    # Endpoint match
    assert _close(pts[0], (2100.0, 1000.0), tol=1e-9)
    assert _close(pts[-1], (2150.0, 1050.0), tol=1e-9)
    # All points lie on circle of radius 50 about (2150, 1000)
    cx, cy = 2150.0, 1000.0
    for x, y in pts:
        assert abs(math.hypot(x - cx, y - cy) - 50.0) < 1e-3
    # Polyline length ~ quarter circumference = π*50/2 ≈ 78.54
    L = sum(
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )
    assert abs(L - math.pi * 50.0 / 2.0) < 0.05


def test_arc_length_overrides_wrong_rot():
    # rot="ccw" would normally pick the long way (3π/2 ≈ 235.6m), but
    # length="78.54" (= π·50/2) tells us it's actually the short quarter arc.
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="ccw",
        length=math.pi * 50.0 / 2.0,
    )
    pts = arc_points(seg, max_chord_err=0.001)
    L = sum(
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )
    assert abs(L - math.pi * 50.0 / 2.0) < 0.05


def test_arc_zero_length_curve_is_degenerate():
    # ProVI writes <Curve length="0.000000" ...> alongside dirEnd==dirStart.
    # The plugin must emit a 2-point degenerate segment, never the whole circle.
    seg = CurveSeg(
        start=(5417468.885, 3477914.359),
        center=(5417300.0, 3478000.0),
        end=(5417468.885, 3477914.359),
        radius=214.74,
        rot="ccw",
        length=0.0,
    )
    pts = arc_points(seg)
    assert len(pts) == 2


def test_arc_ccw_takes_long_way():
    # Same endpoints, rot=ccw → sweeps the long way (3π/2 ≈ 235.6m)
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="ccw",
    )
    pts = arc_points(seg, max_chord_err=0.001)
    L = sum(
        math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
        for i in range(len(pts) - 1)
    )
    assert abs(L - 3 * math.pi * 50.0 / 2.0) < 0.5


# ---------------------------------------------------------------------------
# Spiral — degenerate to a straight line when both radii are infinite
# ---------------------------------------------------------------------------
def test_spiral_with_infinite_radii_is_straight():
    seg = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),  # tangent along +E
        end=(1000.0, 2100.0),
        length=100.0,
        radius_start=None,
        radius_end=None,
        rot="ccw",
    )
    pts = spiral_points(seg)
    assert _close(pts[0], (2000.0, 1000.0))
    assert _close(pts[-1], (2100.0, 1000.0))
    # All points lie on the y=1000 line.
    for _, y in pts:
        assert abs(y - 1000.0) < 1e-6


# ---------------------------------------------------------------------------
# Spiral — clothoid from straight (R=∞) into R=200, length 100
# Compare to closed-form Fresnel: at end, length L, A² = R*L → A = sqrt(200*100)
# Local end coords: x ≈ Fresnel cosine, y ≈ Fresnel sine integrals
# ---------------------------------------------------------------------------
def test_spiral_clothoid_endpoint_matches_fresnel():
    L = 100.0
    R = 200.0
    # Closed-form local end coordinates via direct numerical Fresnel.
    n = 20000
    ds = L / n
    x = y = 0.0
    cos_p, sin_p = 1.0, 0.0
    for i in range(1, n + 1):
        s = i * ds
        theta = s * s / (2.0 * R * L)  # k0=0, k1=1/R → θ(s)=s²/(2RL)
        ct, st = math.cos(theta), math.sin(theta)
        x += 0.5 * (cos_p + ct) * ds
        y += 0.5 * (sin_p + st) * ds
        cos_p, sin_p = ct, st
    end_local = (x, y)

    # Place start at (E=2000, N=1000) with tangent along +E. World end:
    sx, sy = 2000.0, 1000.0  # x=E, y=N
    ex_world = sx + end_local[0]
    ey_world = sy + end_local[1]
    # In LandXML (N, E) order:
    seg = SpiralSeg(
        start=(1000.0, 2000.0),  # N, E
        pi=(1000.0, 2050.0),  # tangent +E
        end=(ey_world, ex_world),  # N=ey_world, E=ex_world
        length=L,
        radius_start=None,  # ∞
        radius_end=R,
        rot="ccw",
    )
    pts = spiral_points(seg)
    # The mid-point of plugin output should agree with Fresnel halfway integration.
    half = pts[len(pts) // 2]
    # Recompute Fresnel at s = L/2
    n2 = 10000
    ds2 = (L / 2) / n2
    x2 = y2 = 0.0
    cp, sp = 1.0, 0.0
    for i in range(1, n2 + 1):
        s = i * ds2
        theta = s * s / (2.0 * R * L)
        ct, st = math.cos(theta), math.sin(theta)
        x2 += 0.5 * (cp + ct) * ds2
        y2 += 0.5 * (sp + st) * ds2
        cp, sp = ct, st
    expected_half = (sx + x2, sy + y2)
    assert abs(half[0] - expected_half[0]) < 0.05
    assert abs(half[1] - expected_half[1]) < 0.05


# ---------------------------------------------------------------------------
# Full alignment polyline
# ---------------------------------------------------------------------------
def test_alignment_polyline_joins_segments():
    a = parse_alignments(SAMPLE_XML.encode("utf-8"))[0]
    pts = alignment_polyline(a)
    assert len(pts) >= 3
    # Starts at line start in QGIS axes
    assert _close(pts[0], (2000.0, 1000.0))
    # Ends at curve end in QGIS axes
    assert _close(pts[-1], (2150.0, 1050.0))


# ---------------------------------------------------------------------------
# alignment_chainage — segment-driven, exact bearings + curvature metadata
# ---------------------------------------------------------------------------
def test_chainage_straight_line_zero_curvature():
    seg = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))  # N=0, E=0..100 → +E
    a = Alignment(name="L", length=100.0, sta_start=0.0, segments=[seg])
    cps = alignment_chainage(a, interval=25.0)
    assert [round(c.station, 6) for c in cps] == [0.0, 25.0, 50.0, 75.0, 100.0]
    for c in cps:
        assert c.seg_kind == "line"
        assert c.curvature == 0.0
        assert c.radius is None
        assert abs(c.bearing_deg) < 1e-9  # tangent points east


def test_chainage_arc_curvature_is_one_over_r():
    # Quarter circle, R=50, ccw — start (1000,2100), end (1050,2150) about center (1000,2150)
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="ccw",
        length=3 * math.pi * 50.0 / 2.0,  # disambiguator picks the long way (ccw)
    )
    a = Alignment(name="C", length=seg.length, sta_start=0.0, segments=[seg])
    cps = alignment_chainage(a, interval=25.0)
    assert len(cps) >= 3
    for c in cps:
        assert c.seg_kind == "curve"
        assert abs(abs(c.curvature) - 1.0 / 50.0) < 1e-9
        assert c.radius is not None and abs(abs(c.radius) - 50.0) < 1e-9


def test_chainage_spiral_curvature_interpolates_linearly():
    # Clothoid: R=∞ → R=200 over L=100. κ(s) = (1/200) * s / 100 = s/20000.
    seg = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),  # tangent along +E
        end=(1010.0, 2099.0),  # rough, exact endpoint only used by polyline-build path
        length=100.0,
        radius_start=None,
        radius_end=200.0,
        rot="ccw",
    )
    a = Alignment(name="S", length=100.0, sta_start=0.0, segments=[seg])
    cps = alignment_chainage(a, interval=25.0)
    # Stations 0, 25, 50, 75, 100 — curvature expected 0, 1/800, 1/400, 3/800, 1/200
    expected = [0.0, 25.0 / 20000.0, 50.0 / 20000.0, 75.0 / 20000.0, 100.0 / 20000.0]
    got = [c.curvature for c in cps]
    assert len(got) == len(expected)
    for g, e in zip(got, expected):
        assert abs(g - e) < 1e-12
    assert all(c.seg_kind == "spiral" for c in cps)
    assert all(c.transition_type == "clothoid" for c in cps)


def test_chainage_skips_degenerate_curve():
    # ProVI exports often contain <Curve length="0"> blocks with Start==End.
    # The chainage walker must NOT expand them into a full 2π circle —
    # that bug previously plotted stations all the way around the implied
    # circle in QGIS. Match the rendering path (arc_points → 2-point stub).
    bogus = CurveSeg(
        start=(5417468.885, 3477914.359),
        center=(5417300.0, 3478000.0),
        end=(5417468.885, 3477914.359),
        radius=214.74,
        rot="ccw",
        length=0.0,
    )
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))
    a = Alignment(name="X", length=100.0, sta_start=0.0, segments=[line, bogus])
    cps = alignment_chainage(a, interval=25.0)
    # Only the 100 m line contributes — stations 0, 25, 50, 75, 100.
    assert [round(c.station, 6) for c in cps] == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert all(c.seg_kind == "line" for c in cps)


def test_chainage_carries_desc_passthrough():
    seg = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0), desc="Vmax=160")
    a = Alignment(name="L", length=100.0, sta_start=0.0, segments=[seg])
    cps = alignment_chainage(a, interval=50.0)
    assert all(c.desc == "Vmax=160" for c in cps)


# ---------------------------------------------------------------------------
# Curve-piece discretization (line / circular-arc primitives for CompoundCurve)
# ---------------------------------------------------------------------------
def _circle_from_three_points(p0, p1, p2):
    """Return (cx, cy, r) of the circle through three non-collinear points."""
    (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2
    d = 2.0 * (x0 * (y1 - y2) + x1 * (y2 - y0) + x2 * (y0 - y1))
    ux = ((x0 * x0 + y0 * y0) * (y1 - y2)
          + (x1 * x1 + y1 * y1) * (y2 - y0)
          + (x2 * x2 + y2 * y2) * (y0 - y1)) / d
    uy = ((x0 * x0 + y0 * y0) * (x2 - x1)
          + (x1 * x1 + y1 * y1) * (x0 - x2)
          + (x2 * x2 + y2 * y2) * (x1 - x0)) / d
    r = math.hypot(x0 - ux, y0 - uy)
    return ux, uy, r


def test_line_piece_passes_through():
    seg = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))
    pieces = segment_curve_pieces(seg)
    assert len(pieces) == 1
    assert isinstance(pieces[0], LinePiece)
    assert pieces[0].start == (0.0, 0.0)
    assert pieces[0].end == (100.0, 0.0)


def test_curve_seg_yields_one_arc_piece_on_circle():
    # Quarter circle, R=50 cw — mirrors test_arc_quarter_circle_length.
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="cw",
    )
    pieces = segment_curve_pieces(seg)
    assert len(pieces) == 1
    assert isinstance(pieces[0], ArcPiece)
    # All three points lie on the circle of radius 50 about (E=2150, N=1000).
    cx, cy = 2150.0, 1000.0
    for p in (pieces[0].start, pieces[0].mid, pieces[0].end):
        assert abs(math.hypot(p[0] - cx, p[1] - cy) - 50.0) < 1e-9


def test_degenerate_curve_collapses_to_line_piece():
    seg = CurveSeg(
        start=(5417468.885, 3477914.359),
        center=(5417300.0, 3478000.0),
        end=(5417468.885, 3477914.359),
        radius=214.74,
        rot="ccw",
        length=0.0,
    )
    pieces = segment_curve_pieces(seg)
    assert len(pieces) == 1
    assert isinstance(pieces[0], LinePiece)


def _fresnel_clothoid_seg(L: float, R: float):
    """Build a geometrically consistent SpiralSeg from R=∞ → R, length L.

    Returns ``(seg, end_local_xy)`` — the end in local-frame coords matches
    what trapezoidal integration of the same κ profile produces, so the
    plugin's world-end pinning is a no-op.
    """
    n = 20000
    ds = L / n
    x = y = 0.0
    cos_p, sin_p = 1.0, 0.0
    for i in range(1, n + 1):
        s = i * ds
        theta = s * s / (2.0 * R * L)
        ct, st = math.cos(theta), math.sin(theta)
        x += 0.5 * (cos_p + ct) * ds
        y += 0.5 * (sin_p + st) * ds
        cos_p, sin_p = ct, st
    sx, sy = 2000.0, 1000.0  # x=E, y=N
    seg = SpiralSeg(
        start=(sy, sx),  # LandXML (N, E)
        pi=(sy, sx + 50.0),  # tangent +E
        end=(sy + y, sx + x),  # LandXML (N, E)
        length=L,
        radius_start=None,
        radius_end=R,
        rot="ccw",
    )
    return seg, (sx + x, sy + y)


def test_spiral_arc_triples_chain_endpoints_match():
    L, R = 100.0, 200.0
    seg, end_world = _fresnel_clothoid_seg(L, R)
    triples = spiral_arc_triples(seg, max_chord_err=0.01)
    assert len(triples) >= 2  # non-trivial dκ/ds drives multi-piece split
    assert _close(triples[0][0], (2000.0, 1000.0))
    assert _close(triples[-1][2], end_world, tol=1e-6)
    # Consecutive arcs share their join points exactly.
    for i in range(len(triples) - 1):
        assert triples[i][2] == triples[i + 1][0]


def test_spiral_arc_chord_error_stays_under_budget():
    # Each truth sample (a densely-integrated point on the clothoid) should
    # land within ``max_chord_err`` of *some* fitted arc-piece circle. That's
    # exactly the property arc-discretization claims — the chord deviation
    # between the rendered CompoundCurve and the analytic clothoid is bounded.
    L = 100.0
    R = 200.0
    budget = 0.01
    seg, _ = _fresnel_clothoid_seg(L, R)
    triples = spiral_arc_triples(seg, max_chord_err=budget)
    circles = [_circle_from_three_points(s, m, e) for s, m, e in triples]
    truth = spiral_points(seg, samples_per_meter=10.0)
    worst = 0.0
    for tx, ty in truth:
        # Nearest fitted circle's |distance-to-center − radius| is the chord
        # deviation along the radial direction.
        best = min(abs(math.hypot(tx - cx, ty - cy) - rr) for cx, cy, rr in circles)
        worst = max(worst, best)
    # Allow a small safety factor over the budget — the per-piece bound is
    # third-order, but the constant in the bound isn't exactly 24.
    assert worst < budget * 3


def test_spiral_picks_correct_rot_against_wrong_landxml_attr():
    # Construct a clothoid whose geometry is CCW (radius_end positive, end
    # north of the start tangent) but mark it rot="cw" in LandXML. The
    # disambiguator must integrate both sign conventions and prefer the one
    # whose integrated end matches the LandXML <End>.
    L, R = 100.0, 200.0
    seg, end_world = _fresnel_clothoid_seg(L, R)
    # Flip rot to the wrong value — geometry stays CCW.
    bad = SpiralSeg(
        start=seg.start,
        pi=seg.pi,
        end=seg.end,
        length=seg.length,
        radius_start=seg.radius_start,
        radius_end=seg.radius_end,
        rot="cw",  # deliberately wrong
    )
    pts = spiral_points(bad)
    # The picked rot should still produce the correct world end.
    assert _close(pts[-1], end_world, tol=1e-6)
    # Mid-spiral point should land on the CCW-side of the start tangent
    # (positive y in this local frame after the tangent-aligned rotation).
    mid = pts[len(pts) // 2]
    sy = 1000.0  # start N in QGIS axes (x=E, y=N)
    # The start tangent points +x; CCW spiral curves toward +y.
    assert mid[1] > sy + 1e-6


def test_spiral_with_infinite_radii_yields_single_piece():
    # Both radii infinite → dκ/ds == 0 → one arc piece (geometrically a line).
    seg = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),
        end=(1000.0, 2100.0),
        length=100.0,
        radius_start=None,
        radius_end=None,
        rot="ccw",
    )
    triples = spiral_arc_triples(seg)
    assert len(triples) == 1


# ---------------------------------------------------------------------------
# Cumulative-length index for the segment walker
# ---------------------------------------------------------------------------
def test_locate_walker_picks_first_walker_whose_cum_end_covers_s():
    # Three back-to-back segments of length 10, 20, 30 → cum = [10, 30, 60].
    cum = [10.0, 30.0, 60.0]
    # Strictly inside each segment.
    assert _locate_walker(cum, 5.0) == 0
    assert _locate_walker(cum, 15.0) == 1
    assert _locate_walker(cum, 45.0) == 2
    # On a boundary: bisect_left places s == cum[i] at walker i (not i+1),
    # matching the original linear-scan semantics.
    assert _locate_walker(cum, 10.0) == 0
    assert _locate_walker(cum, 30.0) == 1
    # Past the end clamps to the last walker.
    assert _locate_walker(cum, 60.0) == 2
    assert _locate_walker(cum, 100.0) == 2
    # Before the start clamps to walker 0.
    assert _locate_walker(cum, -1.0) == 0


def test_alignment_xy_at_segment_boundary_matches_segment_end():
    # Build a kinked alignment so that the station at the join is unambiguous.
    line_a = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))   # +E 100 m
    line_b = LineSeg(start=(0.0, 100.0), end=(50.0, 100.0))  # +N 50 m
    a = Alignment(name="K", length=150.0, sta_start=0.0, segments=[line_a, line_b])
    # Exactly at the boundary station — the index-based locator must return
    # the join point, not somewhere mid-segment.
    xy = alignment_xy_at_station(a, 100.0)
    assert xy is not None
    assert _close(xy, (100.0, 0.0))


def test_alignment_curve_pieces_joins_segments():
    a = parse_alignments(SAMPLE_XML.encode("utf-8"))[0]
    pieces = alignment_curve_pieces(a)
    # SAMPLE_XML has one line + one arc.
    assert len(pieces) == 2
    assert isinstance(pieces[0], LinePiece)
    assert isinstance(pieces[1], ArcPiece)
    # Line end coincides with arc start (in QGIS axes).
    assert _close(pieces[0].end, pieces[1].start)
