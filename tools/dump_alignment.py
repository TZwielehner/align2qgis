#!/usr/bin/env python3
"""Parse a LandXML file and print COMPOUNDCURVE WKT per alignment.

Lets you sanity-check the geometry without launching QGIS:

    python3 tools/dump_alignment.py path/to/file.xml
    python3 tools/dump_alignment.py --3d path/to/file.xml

The output matches what the plugin loads into QGIS — circular arcs and
clothoid spirals stay as analytic ``CIRCULARSTRING`` sub-pieces rather
than chord polylines. The ``--3d`` flag pulls elevation from the
LandXML ``<Profile>`` (when present) and emits ``COMPOUNDCURVE Z``.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.geometry_builder import (  # noqa: E402
    ArcPiece,
    LinePiece,
    alignment_curve_pieces,
    alignment_curve_pieces_3d,
    internal_to_display,
)
from plugin.landxml_parser import (  # noqa: E402
    parse_alignments,
    profile_elevation_at_station,
)


def _piece_to_wkt_2d(piece) -> str:
    if isinstance(piece, LinePiece):
        return (
            f"({piece.start[0]:.4f} {piece.start[1]:.4f}, "
            f"{piece.end[0]:.4f} {piece.end[1]:.4f})"
        )
    return (
        f"CIRCULARSTRING({piece.start[0]:.4f} {piece.start[1]:.4f}, "
        f"{piece.mid[0]:.4f} {piece.mid[1]:.4f}, "
        f"{piece.end[0]:.4f} {piece.end[1]:.4f})"
    )


def _piece_to_wkt_3d(piece, zs: list[float]) -> str:
    if isinstance(piece, LinePiece):
        return (
            f"({piece.start[0]:.4f} {piece.start[1]:.4f} {zs[0]:.4f}, "
            f"{piece.end[0]:.4f} {piece.end[1]:.4f} {zs[1]:.4f})"
        )
    return (
        f"CIRCULARSTRING({piece.start[0]:.4f} {piece.start[1]:.4f} {zs[0]:.4f}, "
        f"{piece.mid[0]:.4f} {piece.mid[1]:.4f} {zs[1]:.4f}, "
        f"{piece.end[0]:.4f} {piece.end[1]:.4f} {zs[2]:.4f})"
    )


def _alignment_wkt(alignment, want_3d: bool) -> str | None:
    if want_3d and alignment.profile is not None:
        pieces_with_stations = alignment_curve_pieces_3d(alignment)
        if not pieces_with_stations:
            return None
        chunks = []
        for piece, stations in pieces_with_stations:
            zs = []
            for s_internal in stations:
                s_display = internal_to_display(alignment, s_internal)
                z = profile_elevation_at_station(alignment.profile, s_display)
                zs.append(0.0 if z is None else float(z))
            chunks.append(_piece_to_wkt_3d(piece, zs))
        return f"COMPOUNDCURVE Z ({', '.join(chunks)})"

    pieces = alignment_curve_pieces(alignment)
    if not pieces:
        return None
    return f"COMPOUNDCURVE ({', '.join(_piece_to_wkt_2d(p) for p in pieces)})"


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    want_3d = False
    if args and args[0] == "--3d":
        want_3d = True
        args.pop(0)
    if not args:
        print(__doc__, file=sys.stderr)
        return 2
    path = args[0]
    with open(path, "rb") as fh:
        alignments = parse_alignments(fh.read())
    if not alignments:
        print("# no <Alignment> elements found", file=sys.stderr)
        return 1

    print("wkt\tname\tlength_xml\tn_segments")
    for a in alignments:
        wkt = _alignment_wkt(a, want_3d)
        if wkt is None:
            continue
        length = "" if a.length is None else f"{a.length:.3f}"
        print(f"{wkt}\t{a.name}\t{length}\t{len(a.segments)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
