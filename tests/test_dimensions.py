"""Dimensioning feature tests (no QGIS required)."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.dimensions import build_dimensions  # noqa: E402
from plugin.landxml_parser import (  # noqa: E402
    Alignment,
    CurveSeg,
    LineSeg,
    SpiralSeg,
)


def _alignment(*segments) -> Alignment:
    return Alignment(name="X", length=None, sta_start=0.0, segments=list(segments))


def test_arc_dimension_label_and_radius():
    seg = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="cw",
    )
    dims = build_dimensions(_alignment(seg), arcs=True, spirals=False, tangents=False)
    assert len(dims) == 1
    d = dims[0]
    assert d.seg_kind == "curve"
    assert d.radius == 50.0
    assert d.label.startswith("R=")
    assert "50" in d.label


def test_spiral_dimension_uses_clothoid_A():
    seg = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),
        end=(1010.0, 2099.0),
        length=100.0,
        radius_start=None,
        radius_end=200.0,
        rot="ccw",
    )
    dims = build_dimensions(_alignment(seg), arcs=False, spirals=True, tangents=False)
    assert len(dims) == 1
    d = dims[0]
    assert d.seg_kind == "spiral"
    # A = sqrt(R * L) = sqrt(200 * 100) ≈ 141.42
    assert d.spiral_a is not None
    assert abs(d.spiral_a - math.sqrt(200 * 100)) < 1e-6
    assert d.label.startswith("A=")


def test_tangent_dimension_off_by_default():
    seg = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))
    dims_off = build_dimensions(_alignment(seg), arcs=True, spirals=True, tangents=False)
    dims_on = build_dimensions(_alignment(seg), arcs=True, spirals=True, tangents=True)
    assert dims_off == []
    assert len(dims_on) == 1
    assert dims_on[0].seg_kind == "line"
    assert dims_on[0].label.startswith("L=")


def test_dimension_position_at_segment_midpoint_for_line():
    seg = LineSeg(start=(0.0, 0.0), end=(0.0, 200.0))  # N=0, E=0..200 → +E
    d = build_dimensions(_alignment(seg), tangents=True)[0]
    # Midpoint in QGIS axes is (E=100, N=0) i.e. (100, 0)
    assert abs(d.x - 100.0) < 1e-9
    assert abs(d.y - 0.0) < 1e-9


def test_toggles_filter_categories_independently():
    arc = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="cw",
    )
    spiral = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),
        end=(1010.0, 2099.0),
        length=100.0,
        radius_start=None,
        radius_end=200.0,
        rot="ccw",
    )
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))
    a = _alignment(line, arc, spiral)

    only_arcs = build_dimensions(a, arcs=True, spirals=False, tangents=False)
    only_spirals = build_dimensions(a, arcs=False, spirals=True, tangents=False)
    only_lines = build_dimensions(a, arcs=False, spirals=False, tangents=True)

    assert {d.seg_kind for d in only_arcs} == {"curve"}
    assert {d.seg_kind for d in only_spirals} == {"spiral"}
    assert {d.seg_kind for d in only_lines} == {"line"}
