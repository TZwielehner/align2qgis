"""Point↔alignment projection helpers."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import (  # noqa: E402
    LinePiece,
    _project_to_curve_seg,
    _project_to_line_piece,
    _project_to_spiral_seg,
    alignment_pose_at_station,
    alignment_project_point,
)
from plugin.landxml_parser import (  # noqa: E402
    Alignment,
    CurveSeg,
    LineSeg,
    SpiralSeg,
)


# ---------------------------------------------------------------------------
# Line projection
# ---------------------------------------------------------------------------
def test_line_projection_foot_inside():
    piece = LinePiece((0.0, 0.0), (100.0, 0.0))
    # Point above the line at x=30. Foot is (30, 0), left-offset = +10.
    s, off, fx, fy, res = _project_to_line_piece(piece, 30.0, 10.0)
    assert abs(s - 30.0) < 1e-9
    assert abs(off - 10.0) < 1e-9
    assert abs(fx - 30.0) < 1e-9
    assert abs(fy - 0.0) < 1e-9
    assert abs(res - 10.0) < 1e-9


def test_line_projection_clamps_past_endpoints():
    piece = LinePiece((0.0, 0.0), (100.0, 0.0))
    # Past end: foot pinned at (100, 0), residual = distance to end.
    s, off, fx, fy, res = _project_to_line_piece(piece, 150.0, 20.0)
    assert abs(s - 100.0) < 1e-9
    assert abs(fx - 100.0) < 1e-9
    assert abs(fy - 0.0) < 1e-9
    # Past start: foot pinned at (0, 0).
    s, off, fx, fy, res = _project_to_line_piece(piece, -10.0, 5.0)
    assert abs(s - 0.0) < 1e-9
    assert abs(fx - 0.0) < 1e-9


def test_line_projection_offset_sign_is_left_positive():
    # Forward direction is +x; left is +y.
    piece = LinePiece((0.0, 0.0), (100.0, 0.0))
    s, off_pos, *_ = _project_to_line_piece(piece, 50.0, 7.0)
    assert off_pos > 0
    s, off_neg, *_ = _project_to_line_piece(piece, 50.0, -7.0)
    assert off_neg < 0


# ---------------------------------------------------------------------------
# Circular arc projection
# ---------------------------------------------------------------------------
def test_arc_projection_radial_point_lands_on_arc():
    # Quarter circle CW (the short way), R=50, around center (E=2150, N=1000).
    # Use the same LandXML construction as the geometry tests.
    seg = CurveSeg(
        start=(1000.0, 2100.0),  # N=1000, E=2100 → QGIS (2100, 1000)
        center=(1000.0, 2150.0),  # QGIS (2150, 1000)
        end=(1050.0, 2150.0),  # QGIS (2150, 1050)
        radius=50.0,
        rot="cw",
    )
    # Point at radial distance 60 from center along the arc midpoint angle.
    # Arc midpoint angle in QGIS frame: starts at atan2(0, -50)=π, sweeps cw
    # to atan2(50, 0)=π/2. Midpoint angle = 3π/4.
    cx, cy = 2150.0, 1000.0
    a_mid = 3 * math.pi / 4
    p = (cx + 60.0 * math.cos(a_mid), cy + 60.0 * math.sin(a_mid))
    s, off, fx, fy, res = _project_to_curve_seg(seg, *p)
    # Foot should sit on the arc at the same angle, radius 50.
    assert abs(math.hypot(fx - cx, fy - cy) - 50.0) < 1e-9
    # Residual = 60 - 50 = 10.
    assert abs(res - 10.0) < 1e-9
    # |s| is halfway along the quarter circle.
    assert abs(s - math.pi * 50.0 / 4.0) < 1e-6


def test_arc_projection_point_off_arc_lands_on_endpoint():
    # Same quarter arc; point diametrically opposite the arc midpoint
    # (in the unswept half) → should land on whichever endpoint is closer.
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="cw",
    )
    # Point near the end (which is at angle π/2 from center in QGIS frame
    # i.e. (2150, 1050)).
    p = (2160.0, 1060.0)
    s, off, fx, fy, res = _project_to_curve_seg(seg, *p)
    # Should clamp to one of the endpoints.
    sx, sy = 2100.0, 1000.0
    ex, ey = 2150.0, 1050.0
    foot = (round(fx, 6), round(fy, 6))
    assert foot in {(sx, sy), (ex, ey)}


# ---------------------------------------------------------------------------
# Spiral projection
# ---------------------------------------------------------------------------
def _fresnel_spiral(L: float, R: float) -> SpiralSeg:
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
    sx, sy = 2000.0, 1000.0
    return SpiralSeg(
        start=(sy, sx),
        pi=(sy, sx + 50.0),
        end=(sy + y, sx + x),
        length=L,
        radius_start=None,
        radius_end=R,
        rot="ccw",
    )


def test_spiral_projection_point_on_spiral_has_zero_residual():
    seg = _fresnel_spiral(100.0, 200.0)
    # The spiral starts at (2000, 1000) in QGIS axes. Its endpoint is the
    # SpiralSeg's end; pose at that endpoint should round-trip.
    from plugin.geometry_builder import _spiral_pose, _spiral_setup
    setup = _spiral_setup(seg)
    for target_s in (10.0, 50.0, 90.0):
        x, y, _ = _spiral_pose(seg, setup, target_s)
        s, off, fx, fy, res = _project_to_spiral_seg(seg, x, y)
        assert abs(s - target_s) < 1e-3, f"expected s≈{target_s}, got {s}"
        assert res < 1e-3


def test_spiral_projection_offset_point_finds_normal_distance():
    seg = _fresnel_spiral(100.0, 200.0)
    from plugin.geometry_builder import _spiral_pose, _spiral_setup
    setup = _spiral_setup(seg)
    # Take a point on the spiral and shift it 5m to the left of the tangent.
    target_s = 60.0
    x, y, bearing_deg = _spiral_pose(seg, setup, target_s)
    b = math.radians(bearing_deg)
    # Left-perp = (-sin b, cos b). Move +5 along that direction.
    px = x + 5.0 * (-math.sin(b))
    py = y + 5.0 * math.cos(b)
    s, off, fx, fy, res = _project_to_spiral_seg(seg, px, py)
    assert abs(s - target_s) < 0.05
    assert abs(res - 5.0) < 0.01
    assert off > 0  # left of forward direction


# ---------------------------------------------------------------------------
# alignment_project_point — multi-segment
# ---------------------------------------------------------------------------
def test_alignment_project_picks_closest_segment():
    line_a = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))  # +E 100
    line_b = LineSeg(start=(0.0, 100.0), end=(50.0, 100.0))  # +N 50
    a = Alignment(name="K", length=150.0, sta_start=0.0, segments=[line_a, line_b])
    # Point near the second segment.
    p = alignment_project_point(a, 110.0, 30.0)
    assert p is not None
    assert p.seg_index == 1
    # Foot is somewhere on line_b — second segment in QGIS frame goes from
    # (100, 0) to (100, 50). Point (110, 30) projects to (100, 30) with
    # residual 10, offset_signed = +10 (left of +y forward direction is +x).
    assert abs(p.residual - 10.0) < 1e-9
    # Station = arclength on line_a (100) + on line_b (30) = 130.
    assert abs(p.station_display - 130.0) < 1e-9


def test_alignment_pose_returns_xy_and_bearing():
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))  # +E 100, bearing 0°
    a = Alignment(name="L", length=100.0, sta_start=500.0, segments=[line])
    pose = alignment_pose_at_station(a, 550.0)
    assert pose is not None
    x, y, bearing = pose
    assert abs(x - 50.0) < 1e-9
    assert abs(y - 0.0) < 1e-9
    assert abs(bearing) < 1e-9  # tangent points +x → 0°
