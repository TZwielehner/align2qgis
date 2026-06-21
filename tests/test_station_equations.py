"""Station-equation mapping + chainage walker integration."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import (  # noqa: E402
    alignment_chainage,
    alignment_xy_at_station,
    display_to_internal,
    internal_to_display,
)
from plugin.landxml_parser import (  # noqa: E402
    Alignment,
    LineSeg,
    StaEquation,
    parse_alignments,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
EQ_XML = """<?xml version="1.0"?>
<LandXML xmlns="http://www.landxml.org/schema/LandXML-1.2" version="1.2">
  <Alignments>
    <Alignment name="A1" length="200.0" staStart="100.0">
      <CoordGeom>
        <Line length="200.0">
          <Start>0.0 0.0</Start>
          <End>0.0 200.0</End>
        </Line>
      </CoordGeom>
      <StaEquation staBack="200.0" staAhead="300.0"/>
    </Alignment>
  </Alignments>
</LandXML>
"""


def test_parser_reads_station_equation():
    alignments = parse_alignments(EQ_XML.encode("utf-8"))
    assert len(alignments) == 1
    eqs = alignments[0].equations
    assert len(eqs) == 1
    assert eqs[0].sta_back == 200.0
    assert eqs[0].sta_ahead == 300.0
    # staInternal omitted → defaults to staBack.
    assert eqs[0].sta_internal == 200.0


# ---------------------------------------------------------------------------
# internal_to_display / display_to_internal
# ---------------------------------------------------------------------------
def _alignment(equations):
    return Alignment(
        name="X",
        length=0.0,
        sta_start=0.0,
        segments=[],
        equations=list(equations),
    )


def test_identity_when_no_equations():
    a = _alignment([])
    for s in (0.0, 50.0, 123.456):
        assert internal_to_display(a, s) == s
        assert display_to_internal(a, s) == s


def test_forward_equation_introduces_gap_in_display():
    # Internal 200 → display 200 (before eq) → display jumps to 300 (after eq).
    eq = StaEquation(sta_back=200.0, sta_ahead=300.0, sta_internal=200.0)
    a = _alignment([eq])
    # Before the equation: identity.
    assert internal_to_display(a, 150.0) == 150.0
    # At the equation point (sta_internal): "before" branch → sta_back.
    assert internal_to_display(a, 200.0) == 200.0
    # After: shifted by +100.
    assert internal_to_display(a, 200.5) == 300.5
    assert internal_to_display(a, 250.0) == 350.0
    # Inverse: display 250 is in the (200, 300) gap → None.
    assert display_to_internal(a, 250.0) is None
    # Display values on either side of the gap map back correctly.
    assert display_to_internal(a, 150.0) == 150.0
    assert display_to_internal(a, 350.0) == 250.0


def test_backward_equation_resolves_to_before_branch():
    # Back equation: at internal=200, display jumps from 200 back to 150.
    # Display range (150, 200] is owned by both the "before" segment and
    # the "after" segment; we resolve to the "before" branch.
    eq = StaEquation(sta_back=200.0, sta_ahead=150.0, sta_internal=200.0)
    a = _alignment([eq])
    assert internal_to_display(a, 175.0) == 175.0
    assert internal_to_display(a, 200.0) == 200.0  # before branch
    assert internal_to_display(a, 200.5) == 150.5  # after branch
    # 175 is in the overlap zone — prefers before.
    assert display_to_internal(a, 175.0) == 175.0


def test_roundtrip_outside_gap():
    eq = StaEquation(sta_back=500.0, sta_ahead=750.0, sta_internal=500.0)
    a = _alignment([eq])
    for s_int in (50.0, 200.0, 499.999, 500.001, 600.0, 800.0):
        s_disp = internal_to_display(a, s_int)
        back = display_to_internal(a, s_disp)
        assert back is not None
        assert abs(back - s_int) < 1e-9


# ---------------------------------------------------------------------------
# alignment_chainage end-to-end
# ---------------------------------------------------------------------------
def test_chainage_emits_round_display_stations_around_a_gap():
    # 200 m line, staStart=100, equation at sta_internal=200 → sta_ahead=300.
    # Display range: 100..200 (before eq) + 300..400 (after eq), 100 m gap.
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 200.0))
    eq = StaEquation(sta_back=200.0, sta_ahead=300.0, sta_internal=200.0)
    a = Alignment(
        name="L", length=200.0, sta_start=100.0,
        segments=[line], equations=[eq],
    )
    cps = alignment_chainage(a, interval=50.0)
    stations = [round(c.station, 6) for c in cps]
    # Endpoints (100, 400) + round display stations on each side.
    assert stations == [100.0, 150.0, 200.0, 350.0, 400.0]
    # station_internal stays continuous: 100, 150, 200, 250, 300.
    internals = [round(c.station_internal, 6) for c in cps]
    assert internals == [100.0, 150.0, 200.0, 250.0, 300.0]


def test_xy_at_station_returns_none_in_gap():
    line = LineSeg(start=(0.0, 0.0), end=(0.0, 200.0))
    eq = StaEquation(sta_back=200.0, sta_ahead=300.0, sta_internal=200.0)
    a = Alignment(
        name="L", length=200.0, sta_start=100.0,
        segments=[line], equations=[eq],
    )
    # 250 is in the gap between 200 (display end of pre-eq) and 300 (display start of post-eq).
    assert alignment_xy_at_station(a, 250.0) is None
    # 350 is past the gap: arclength = 350 - (300-200) - 100 = 150 from start.
    xy = alignment_xy_at_station(a, 350.0)
    assert xy is not None
    # LandXML line is N=(0..0), E=(0..200) → in QGIS axes (x=E, y=N) at arclength 150: (150, 0).
    assert abs(xy[0] - 150.0) < 1e-9
    assert abs(xy[1] - 0.0) < 1e-9
