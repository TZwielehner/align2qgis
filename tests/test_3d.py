"""3D alignment helpers: closed-form profile elevation + per-vertex stations."""
from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import (  # noqa: E402
    ArcPiece,
    LinePiece,
    alignment_curve_pieces_3d,
)
from plugin.landxml_parser import (  # noqa: E402
    Alignment,
    CurveSeg,
    LineSeg,
    PVI,
    ProfAlign,
    Profile,
    SpiralSeg,
    VertCurve,
    profile_elevation_at_station,
    profile_samples,
)


def _profile(elements):
    return Profile(name="P", alignments=[ProfAlign(name="PA", elements=list(elements))])


# ---------------------------------------------------------------------------
# profile_elevation_at_station — closed form
# ---------------------------------------------------------------------------
def test_elevation_linear_between_pvis():
    prof = _profile([
        PVI(station=0.0, elev=100.0),
        PVI(station=100.0, elev=110.0),  # +10% grade
    ])
    assert abs(profile_elevation_at_station(prof, 50.0) - 105.0) < 1e-9
    assert abs(profile_elevation_at_station(prof, 0.0) - 100.0) < 1e-9
    assert abs(profile_elevation_at_station(prof, 100.0) - 110.0) < 1e-9


def test_elevation_returns_none_outside_range():
    prof = _profile([
        PVI(station=0.0, elev=100.0),
        PVI(station=100.0, elev=110.0),
    ])
    assert profile_elevation_at_station(prof, -1.0) is None
    assert profile_elevation_at_station(prof, 1000.0) is None


def test_elevation_parabolic_inside_vert_curve_matches_densified():
    # Crest curve: -2% in, +4% out (sag), PVI at sta=200, elev=50, L=100.
    prof = _profile([
        PVI(station=0.0, elev=70.0),       # back tangent grade = (50-70)/200 = -0.10... actually let me use simpler grades
        PVI(station=100.0, elev=80.0),     # g_back of next = (80-70)/100 = +0.10
        VertCurve(station=200.0, elev=60.0, length=100.0),
        PVI(station=400.0, elev=80.0),     # g_ahead from VC end = (80-60)/200 = +0.10
    ])
    # Densified truth at fine step:
    truth = profile_samples(prof, step=0.5)
    truth_dict = {round(s, 6): e for s, e in truth}

    # Sample several points inside the curve (sta_bvc=150, sta_evc=250):
    for sta in (150.0, 175.0, 200.0, 225.0, 250.0):
        analytic = profile_elevation_at_station(prof, sta)
        densified = truth_dict.get(round(sta, 6))
        assert analytic is not None
        # profile_samples evaluates the same parabola at discrete steps,
        # so the two should agree to floating-point precision at samples.
        if densified is not None:
            assert abs(analytic - densified) < 1e-9


def test_elevation_at_pvi_with_vertcurve_is_curve_value_not_pvi_value():
    # The parabola at sta=PVI is NOT pvi.elev — it's offset by the curve.
    prof = _profile([
        PVI(station=0.0, elev=100.0),
        VertCurve(station=500.0, elev=120.0, length=200.0),  # sag-ish curve
        PVI(station=1000.0, elev=120.0),
    ])
    # At PVI station: grades g_back = (120-100)/500 = 0.04, g_ahead = (120-120)/500 = 0.
    # ds = L/2 = 100, elev_bvc = 120 - 100*0.04 = 116.
    # elev(PVI) = 116 + 0.04*100 + (0 - 0.04)/(2*200)*100² = 116 + 4 + (-0.04/400)*10000 = 116 + 4 - 1 = 119.
    elev = profile_elevation_at_station(prof, 500.0)
    assert elev is not None
    assert abs(elev - 119.0) < 1e-9


# ---------------------------------------------------------------------------
# alignment_curve_pieces_3d — internal station per vertex
# ---------------------------------------------------------------------------
def test_curve_pieces_3d_line_carries_endpoint_stations():
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 100.0))
    a = Alignment(
        name="L", length=100.0, sta_start=500.0, segments=[line],
        profile=_profile([PVI(0.0, 0.0)]),
    )
    pieces = alignment_curve_pieces_3d(a)
    assert len(pieces) == 1
    piece, stations = pieces[0]
    assert isinstance(piece, LinePiece)
    # Two vertex stations: start = sta_start, end = sta_start + L.
    assert stations == [500.0, 600.0]


def test_curve_pieces_3d_arc_has_midpoint_at_half_arclength():
    # Quarter circle, R=50, CW (short way) — same construction the existing
    # arc geometry tests use; segment_length resolves to π·50/2.
    arc = CurveSeg(
        start=(1000.0, 2100.0),
        center=(1000.0, 2150.0),
        end=(1050.0, 2150.0),
        radius=50.0,
        rot="cw",
    )
    quarter = math.pi * 50.0 / 2.0
    a = Alignment(
        name="A", length=quarter, sta_start=0.0, segments=[arc],
        profile=_profile([PVI(0.0, 0.0)]),
    )
    pieces = alignment_curve_pieces_3d(a)
    assert len(pieces) == 1
    piece, stations = pieces[0]
    assert isinstance(piece, ArcPiece)
    assert len(stations) == 3
    # Midpoint station is exactly halfway along the arc.
    assert abs(stations[1] - stations[0] - quarter / 2.0) < 1e-9
    assert abs(stations[2] - stations[0] - quarter) < 1e-9


def test_curve_pieces_3d_spiral_pieces_share_endpoints_in_station():
    # Standard clothoid produces multiple ArcPieces; their stations must form
    # a continuous chain along the alignment.
    L = 100.0
    R = 200.0
    n_truth = 20000
    ds = L / n_truth
    x = y = 0.0
    cos_p, sin_p = 1.0, 0.0
    for i in range(1, n_truth + 1):
        s = i * ds
        theta = s * s / (2.0 * R * L)
        ct, st = math.cos(theta), math.sin(theta)
        x += 0.5 * (cos_p + ct) * ds
        y += 0.5 * (sin_p + st) * ds
        cos_p, sin_p = ct, st
    seg = SpiralSeg(
        start=(1000.0, 2000.0),
        pi=(1000.0, 2050.0),
        end=(1000.0 + y, 2000.0 + x),
        length=L,
        radius_start=None,
        radius_end=R,
        rot="ccw",
    )
    a = Alignment(
        name="S", length=L, sta_start=1000.0, segments=[seg],
        profile=_profile([PVI(1000.0, 100.0), PVI(1100.0, 100.0)]),
    )
    pieces = alignment_curve_pieces_3d(a)
    assert len(pieces) >= 2
    # First piece starts at sta_start; last piece ends at sta_start + L.
    assert pieces[0][1][0] == 1000.0
    assert abs(pieces[-1][1][-1] - 1100.0) < 1e-9
    # Adjacent pieces share their join station (modulo float drift across
    # the uniform L/N step accumulator).
    for i in range(len(pieces) - 1):
        assert abs(pieces[i][1][-1] - pieces[i + 1][1][0]) < 1e-9
