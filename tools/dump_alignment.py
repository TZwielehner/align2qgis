#!/usr/bin/env python3
"""Parse a LandXML file and print WKT LINESTRING per alignment.

Lets you sanity-check the geometry without launching QGIS:

    python3 tools/dump_alignment.py path/to/file.xml
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import alignment_polyline  # noqa: E402
from plugin.landxml_parser import parse_alignments  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = argv[1]
    with open(path, "rb") as fh:
        alignments = parse_alignments(fh.read())
    if not alignments:
        print("# no <Alignment> elements found", file=sys.stderr)
        return 1

    print("wkt\tname\tlength_xml\tn_segments")
    for a in alignments:
        pts = alignment_polyline(a)
        if len(pts) < 2:
            continue
        coords = ", ".join(f"{x:.4f} {y:.4f}" for x, y in pts)
        wkt = f"LINESTRING ({coords})"
        length = "" if a.length is None else f"{a.length:.3f}"
        print(f"{wkt}\t{a.name}\t{length}\t{len(a.segments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
