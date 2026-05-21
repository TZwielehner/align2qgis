"""Tests for parse_cg_points + parse_cross_section_surfaces.

Exercises both a tiny inline LandXML snippet (for shape coverage) and the
real ProVI export bundled in ``tests/data`` (for end-to-end sanity).
"""
from __future__ import annotations

import os

from plugin.landxml_parser import (
    parse_cg_points,
    parse_cross_section_surfaces,
)


_SAMPLE_XML = os.path.join(
    os.path.dirname(__file__), "data", "Ubahn_Nuernberg_Langwasser.xml",
)


_INLINE_XML = b"""<?xml version="1.0"?>
<LandXML version="1.2" xmlns="http://www.landxml.org/schema/LandXML-1.2">
  <CgPoints name="Switches" desc="Weichen">
    <CgPoint name="WA 1" code="">100.1 200.2 333.3</CgPoint>
    <CgPoint name="WA 2" code="X">110.0 210.0</CgPoint>
  </CgPoints>
  <Alignments name="A">
    <Alignment name="A1" length="50" staStart="0">
      <CoordGeom/>
      <CrossSects>
        <CrossSect sta="10.0">
          <CrossSectSurf name="1" desc="Planum">
            <PntList2D>-3.0 100.0 0.0 100.5 3.0 100.0</PntList2D>
          </CrossSectSurf>
          <CrossSectSurf name="2" desc="Bettung">
            <PntList2D>-1.0 101.0 1.0 101.0</PntList2D>
            <PntList2D>-2.0 102.0 2.0 102.0</PntList2D>
          </CrossSectSurf>
        </CrossSect>
      </CrossSects>
    </Alignment>
  </Alignments>
</LandXML>
"""


def test_parse_cg_points_inline_basic():
    pts = parse_cg_points(_INLINE_XML)
    assert len(pts) == 2
    p1, p2 = pts
    assert p1.name == "WA 1"
    assert p1.north == 100.1 and p1.east == 200.2
    assert p1.elev == 333.3
    assert p1.group_name == "Switches" and p1.group_desc == "Weichen"
    # 2-value form has no elevation
    assert p2.elev is None
    assert p2.code == "X"


def test_parse_cross_section_surfaces_inline_basic():
    surfs = parse_cross_section_surfaces(_INLINE_XML)
    assert len(surfs) == 2
    planum, bettung = surfs
    assert planum.alignment_name == "A1"
    assert planum.station == 10.0
    assert planum.desc == "Planum"
    # Single PntList2D → one part with 3 points
    assert len(planum.parts) == 1
    assert planum.parts[0] == [(-3.0, 100.0), (0.0, 100.5), (3.0, 100.0)]
    # Two PntList2D → two parts (multipart surface)
    assert bettung.desc == "Bettung"
    assert len(bettung.parts) == 2
    assert bettung.parts[0][0] == (-1.0, 101.0)
    assert bettung.parts[1][-1] == (2.0, 102.0)


def test_parse_real_provi_export_cg_points():
    if not os.path.exists(_SAMPLE_XML):
        return  # sample not vendored in this checkout — silent skip
    with open(_SAMPLE_XML, "rb") as fh:
        xml = fh.read()
    pts = parse_cg_points(xml)
    assert len(pts) == 51
    # All belong to the single CgPoints group in this file.
    assert all(p.group_name == "Alle_WPs.PT" for p in pts)
    # Every point should have a name and elevation.
    assert all(p.name for p in pts)
    assert all(isinstance(p.elev, float) for p in pts)


def test_parse_real_provi_export_cross_section_surfaces():
    if not os.path.exists(_SAMPLE_XML):
        return
    with open(_SAMPLE_XML, "rb") as fh:
        xml = fh.read()
    surfs = parse_cross_section_surfaces(xml)
    # ProVI export contains 5 surface kinds across two alignments; 270
    # surface records with usable PntList2D content.
    assert len(surfs) == 270
    by_desc = {}
    for s in surfs:
        by_desc.setdefault(s.desc, 0)
        by_desc[s.desc] += 1
    assert set(by_desc) == {"Planum", "Bettung", "Schwelle", "PSS", "FSS"}
    # Each surface should have at least one usable polyline part.
    assert all(len(s.parts) >= 1 for s in surfs)
    # Stations should be non-negative and sorted-ish within each alignment.
    for s in surfs:
        assert s.station >= 0.0
        for part in s.parts:
            assert len(part) >= 2
            for off, elev in part:
                assert isinstance(off, float) and isinstance(elev, float)
