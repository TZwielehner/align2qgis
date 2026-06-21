"""Discretize LandXML alignment segments into ordered (x, y) polylines.

All output is in QGIS axis order: x = Easting, y = Northing. Inputs from
``landxml_parser`` are in LandXML order (north, east) and are swapped here.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import Iterable, NamedTuple

from .landxml_parser import (
    Alignment,
    CurveSeg,
    LineSeg,
    Segment,
    SpiralSeg,
)


XY = tuple[float, float]


def ne_to_xy(p: tuple[float, float]) -> XY:
    """Swap LandXML (north, east) → QGIS (x=east, y=north)."""
    return (p[1], p[0])


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------
def line_points(seg: LineSeg) -> list[XY]:
    return [ne_to_xy(seg.start), ne_to_xy(seg.end)]


# ---------------------------------------------------------------------------
# Circular arcs
# ---------------------------------------------------------------------------
def _arc_geometry(seg: CurveSeg) -> tuple[float, float, float, float, float, float]:
    """Resolve ``(cx, cy, r, a0, dtheta, arclength)`` for ``seg``.

    Returns ``arclength=0`` (and zero sweep) for degenerate arcs — a
    ``<Curve length="0">`` block with Start==End (common in ProVI exports)
    must not be expanded into a full 2π circle. Shared by :func:`arc_points`
    (rendering) and the chainage walker so both paths agree on what is and
    isn't actually swept.

    LandXML ``rot`` (cw/ccw) is unreliable in the wild — when ``length`` is
    provided we pick whichever of the two candidate sweeps matches it best.
    """
    cx, cy = ne_to_xy(seg.center)
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    r = seg.radius if seg.radius > 0 else math.hypot(sx - cx, sy - cy)

    chord = math.hypot(ex - sx, ey - sy)
    if (seg.length is not None and seg.length < 1e-6) or chord < 1e-6 or r < 1e-6:
        return (cx, cy, r, 0.0, 0.0, 0.0)

    a0 = math.atan2(sy - cy, sx - cx)
    a1 = math.atan2(ey - cy, ex - cx)
    ccw_sweep = a1 - a0
    while ccw_sweep <= 0:
        ccw_sweep += 2 * math.pi
    cw_sweep = ccw_sweep - 2 * math.pi  # negative

    if seg.length is not None and seg.length > 0:
        target = seg.length / r
        dtheta = ccw_sweep if abs(ccw_sweep - target) <= abs(abs(cw_sweep) - target) else cw_sweep
    else:
        dtheta = ccw_sweep if seg.rot.lower() != "cw" else cw_sweep

    return (cx, cy, r, a0, dtheta, abs(dtheta) * r)


def arc_points(seg: CurveSeg, max_chord_err: float = 0.01) -> list[XY]:
    """Discretize a circular arc."""
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    cx, cy, r, a0, dtheta, length = _arc_geometry(seg)
    if length <= 0:
        return [(sx, sy), (ex, ey)]

    sweep = abs(dtheta)
    err = max(max_chord_err, 1e-6)
    cos_arg = max(-1.0, min(1.0, 1.0 - err / max(r, 1e-9)))
    step = 2.0 * math.acos(cos_arg) if r > err else math.radians(5)
    n = max(8, int(math.ceil(sweep / step)))

    pts: list[XY] = []
    for i in range(n + 1):
        t = i / n
        a = a0 + dtheta * t
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    pts[0] = (sx, sy)
    pts[-1] = (ex, ey)
    return pts


# ---------------------------------------------------------------------------
# Clothoid spirals
# ---------------------------------------------------------------------------
def _local_clothoid(
    length: float,
    k0: float,
    k1: float,
    n: int,
) -> list[XY]:
    """Integrate a clothoid in its local frame: start at origin, initial tangent +x.

    Curvature varies linearly: κ(s) = k0 + (k1-k0) * s/length.
    Heading: θ(s) = ∫κ ds = k0*s + (k1-k0)*s²/(2L).
    Position: simple trapezoidal integration of (cos θ, sin θ).
    """
    if n < 2:
        n = 2
    ds = length / n
    pts: list[XY] = [(0.0, 0.0)]
    x = y = 0.0
    cos_prev = 1.0
    sin_prev = 0.0
    for i in range(1, n + 1):
        s = i * ds
        theta = k0 * s + (k1 - k0) * s * s / (2.0 * length)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        x += 0.5 * (cos_prev + cos_t) * ds
        y += 0.5 * (sin_prev + sin_t) * ds
        pts.append((x, y))
        cos_prev, sin_prev = cos_t, sin_t
    return pts


def _solve_rotation_from_endpoints(
    local: list[XY], sx: float, sy: float, ex: float, ey: float
) -> tuple[float, float]:
    """Find (cos a, sin a) so that R(a)·local[-1] + start == end."""
    lx, ly = local[-1]
    dx = ex - sx
    dy = ey - sy
    denom = lx * lx + ly * ly
    if denom < 1e-12:
        return 1.0, 0.0
    cos_a = (lx * dx + ly * dy) / denom
    sin_a = (lx * dy - ly * dx) / denom
    return cos_a, sin_a


def _spiral_placement(
    seg: SpiralSeg, n_samples: int,
) -> tuple[float, float, float, float, float, float, list[XY]]:
    """Resolve ``(sx, sy, cos_a, sin_a, k0, k1, local)`` with rot disambiguation.

    LandXML ``rot`` is unreliable in the wild — some exporters always write
    ``ccw`` regardless of the actual turning direction. We integrate the
    clothoid under both sign conventions and score which one reproduces the
    LandXML ``<End>`` more faithfully, mirroring the length-based picker in
    :func:`_arc_geometry`.

    Residual depends on what fixes the world rotation:

    * With ``<PI>`` the rotation is set by the start tangent; we measure
      metric distance between the transformed integrated end and ``<End>``.
    * Without ``<PI>`` the rotation is solved to land on ``<End>`` by
      construction, so we score by how close ``cos²+sin²`` is to 1.0 — a
      non-unit rotation means the integrated end has the wrong magnitude,
      i.e. the curvature signs are wrong.

    The declared ``rot`` is tried first so ties resolve to the LandXML
    attribute and existing geometry stays bit-stable.
    """
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    L = seg.length

    pi_dir: tuple[float, float] | None = None
    if seg.pi is not None:
        px, py = ne_to_xy(seg.pi)
        tx, ty = px - sx, py - sy
        norm = math.hypot(tx, ty)
        if norm > 1e-9:
            pi_dir = (tx / norm, ty / norm)

    declared_sign = -1.0 if seg.rot.lower() == "cw" else 1.0

    # Residual below 1 mm (or 1 mm²-ish for the unit-rotation metric) means
    # the declared rot already lands cleanly — skip the second integration.
    short_circuit = 1e-3

    best_residual = math.inf
    best: tuple[float, float, float, float, list[XY]] = (1.0, 0.0, 0.0, 0.0, [])
    for sign in (declared_sign, -declared_sign):
        k0 = 0.0 if seg.radius_start is None else sign / seg.radius_start
        k1 = 0.0 if seg.radius_end is None else sign / seg.radius_end
        local = _local_clothoid(L, k0, k1, n_samples)
        if pi_dir is not None:
            cos_a, sin_a = pi_dir
            lx, ly = local[-1]
            wx = sx + cos_a * lx - sin_a * ly
            wy = sy + sin_a * lx + cos_a * ly
            residual = math.hypot(wx - ex, wy - ey)
        else:
            cos_a, sin_a = _solve_rotation_from_endpoints(local, sx, sy, ex, ey)
            residual = abs(cos_a * cos_a + sin_a * sin_a - 1.0)
        if residual < best_residual:
            best_residual = residual
            best = (cos_a, sin_a, k0, k1, local)
        if best_residual < short_circuit:
            break

    cos_a, sin_a, k0, k1, local = best
    return (sx, sy, cos_a, sin_a, k0, k1, local)


def spiral_points(seg: SpiralSeg, samples_per_meter: float = 0.5) -> list[XY]:
    """Discretize a clothoid spiral and place it in world coords.

    Strategy:
    1. Integrate locally with start at origin and initial tangent = +x.
    2. Compute the rotation/translation that maps local start→world start
       AND local initial tangent → world tangent at start.

    The world tangent at start is inferred from the PI point when available
    (Start→PI direction is the tangent at Start); otherwise we fall back to
    solving against the End point. Rotation sign is disambiguated against
    LandXML ``<End>`` so a wrong ``rot`` attribute doesn't flip the spiral.
    """
    L = seg.length
    n = max(16, int(math.ceil(L * samples_per_meter)))
    sx, sy, cos_a, sin_a, _, _, local = _spiral_placement(seg, n)
    ex, ey = ne_to_xy(seg.end)
    world: list[XY] = []
    for lx, ly in local:
        wx = sx + cos_a * lx - sin_a * ly
        wy = sy + sin_a * lx + cos_a * ly
        world.append((wx, wy))
    # Pin endpoints — small integration drift is expected for long spirals.
    world[0] = (sx, sy)
    world[-1] = (ex, ey)
    return world


# ---------------------------------------------------------------------------
# Curve-piece discretization — line / circular-arc primitives that QGIS can
# render exactly as a CompoundCurve. Used by the layer builders for the
# Alignments and Segments outputs so offsets, buffers, and labeling against
# the alignment don't inherit the chord-faceting of a polyline approximation.
# Each arc piece is described by three points (start, on-arc midpoint, end),
# the form CircularString accepts.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LinePiece:
    start: XY
    end: XY


@dataclass(frozen=True)
class ArcPiece:
    start: XY
    mid: XY  # any point on the arc strictly between start and end
    end: XY


CurvePiece = LinePiece | ArcPiece


def _curve_arc_piece(seg: CurveSeg) -> ArcPiece | LinePiece | None:
    """One ArcPiece for a CurveSeg — exact, no approximation.

    Degenerate (zero-length) curves collapse to a 2-point LinePiece so the
    output still joins, matching how :func:`arc_points` handles them.
    """
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    cx, cy, r, a0, dtheta, length = _arc_geometry(seg)
    if length <= 0 or r <= 0:
        return LinePiece((sx, sy), (ex, ey))
    a_mid = a0 + dtheta / 2.0
    mx = cx + r * math.cos(a_mid)
    my = cy + r * math.sin(a_mid)
    return ArcPiece((sx, sy), (mx, my), (ex, ey))


def spiral_arc_triples(
    seg: SpiralSeg, max_chord_err: float = 0.01,
) -> list[tuple[XY, XY, XY]]:
    """Approximate a clothoid as a chain of circular arcs in world XY.

    Each tuple is ``(start, on-arc midpoint, end)`` — the three points a
    ``QgsCircularString`` needs. Consecutive arcs share an endpoint so the
    chain is gap-free.

    Piece count is chosen from the clothoid's curvature gradient
    ``dκ/ds = (k1 - k0) / L``. A 3-point arc fit through a clothoid piece of
    length ``h`` has third-order chord error ≈ ``|dκ/ds| · h³ / 24``, so
    setting ``h ≤ (24 · max_chord_err / |dκ/ds|)^(1/3)`` keeps the per-arc
    deviation under the budget. Straight or constant-radius spirals
    (``dκ/ds ≈ 0``) collapse to a single piece.
    """
    L = seg.length
    if L <= 0:
        return []

    # Choose piece count from the curvature gradient before resolving the
    # final rot sign — magnitude of dκ/ds is identical for either sign so
    # the count is sign-stable.
    declared_sign = -1.0 if seg.rot.lower() == "cw" else 1.0
    k0_decl = 0.0 if seg.radius_start is None else declared_sign / seg.radius_start
    k1_decl = 0.0 if seg.radius_end is None else declared_sign / seg.radius_end
    err = max(max_chord_err, 1e-6)
    dk_ds = abs(k1_decl - k0_decl) / L
    if dk_ds < 1e-12:
        n_arcs = 1
    else:
        h_max = (24.0 * err / dk_ds) ** (1.0 / 3.0)
        n_arcs = max(1, int(math.ceil(L / h_max)))

    # Sample on a grid twice as fine as the arc count so each arc gets a
    # start, midpoint, and end taken directly from the integrated clothoid.
    n_samples = 2 * n_arcs
    sx, sy, cos_a, sin_a, _, _, local = _spiral_placement(seg, n_samples)
    ex, ey = ne_to_xy(seg.end)

    world: list[XY] = []
    for lx, ly in local:
        world.append((sx + cos_a * lx - sin_a * ly, sy + sin_a * lx + cos_a * ly))
    # Pin the chain's outer endpoints to the LandXML values — small
    # integration drift is expected on long spirals.
    world[0] = (sx, sy)
    world[-1] = (ex, ey)

    triples: list[tuple[XY, XY, XY]] = []
    for i in range(n_arcs):
        triples.append((world[2 * i], world[2 * i + 1], world[2 * i + 2]))
    return triples


def segment_curve_pieces(
    seg: Segment, max_chord_err: float = 0.01,
) -> list[CurvePiece]:
    """Decompose a segment into line / circular-arc pieces.

    Lines pass through unchanged; circular arcs become one ArcPiece;
    clothoid spirals become a chain of ArcPieces sized by ``max_chord_err``.
    """
    if isinstance(seg, LineSeg):
        sx, sy = ne_to_xy(seg.start)
        ex, ey = ne_to_xy(seg.end)
        return [LinePiece((sx, sy), (ex, ey))]
    if isinstance(seg, CurveSeg):
        piece = _curve_arc_piece(seg)
        return [piece] if piece is not None else []
    if isinstance(seg, SpiralSeg):
        return [ArcPiece(s, m, e) for s, m, e in spiral_arc_triples(seg, max_chord_err)]
    raise TypeError(f"unknown segment type: {type(seg).__name__}")


def alignment_curve_pieces(
    alignment: Alignment, max_chord_err: float = 0.01,
) -> list[CurvePiece]:
    """Concatenate every segment's curve pieces into one ordered list.

    No deduplication between pieces is needed — adjacent segments already
    share endpoints by construction, and CompoundCurve assembly tolerates
    coincident join points.
    """
    out: list[CurvePiece] = []
    for seg in alignment.segments:
        out.extend(segment_curve_pieces(seg, max_chord_err))
    return out


def alignment_curve_pieces_3d(
    alignment: Alignment, max_chord_err: float = 0.01,
) -> list[tuple[CurvePiece, list[float]]]:
    """Each piece + the alignment-*internal* station at every defining vertex.

    ``LinePiece`` carries two stations (start, end); ``ArcPiece`` carries
    three (start, on-arc midpoint, end). Callers run those stations through
    :func:`internal_to_display` then :func:`profile_elevation_at_station` to
    fetch Z, and pass the resulting points into ``QgsCircularString`` /
    ``QgsLineString`` constructors to build a ``CompoundCurveZ`` feature.

    Spirals are discretized into N equal-arclength arcs, so vertex stations
    are simply ``cum + i·h``, ``cum + (i + 0.5)·h``, ``cum + (i + 1)·h``
    for piece ``i`` where ``h = L / N``.
    """
    out: list[tuple[CurvePiece, list[float]]] = []
    cum = alignment.sta_start or 0.0
    for seg in alignment.segments:
        L = segment_length(seg)
        if L <= 0:
            continue
        pieces = segment_curve_pieces(seg, max_chord_err)
        if isinstance(seg, SpiralSeg):
            n = len(pieces) or 1
            h = L / n
            for i, piece in enumerate(pieces):
                s0 = cum + i * h
                if isinstance(piece, ArcPiece):
                    out.append((piece, [s0, s0 + h / 2.0, s0 + h]))
                else:
                    out.append((piece, [s0, s0 + h]))
        else:
            # Line or single circular arc — one piece spans the whole segment.
            for piece in pieces:
                if isinstance(piece, ArcPiece):
                    out.append((piece, [cum, cum + L / 2.0, cum + L]))
                else:
                    out.append((piece, [cum, cum + L]))
        cum += L
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------
def segment_points(seg: Segment) -> list[XY]:
    if isinstance(seg, LineSeg):
        return line_points(seg)
    if isinstance(seg, CurveSeg):
        return arc_points(seg)
    if isinstance(seg, SpiralSeg):
        return spiral_points(seg)
    raise TypeError(f"unknown segment type: {type(seg).__name__}")


def alignment_polyline(alignment: Alignment) -> list[XY]:
    """Concatenate all segments into one polyline, dropping duplicate joins."""
    out: list[XY] = []
    for seg in alignment.segments:
        pts = segment_points(seg)
        if not pts:
            continue
        if out and _close(out[-1], pts[0]):
            out.extend(pts[1:])
        else:
            out.extend(pts)
    return out


def _close(a: XY, b: XY, tol: float = 1e-4) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def alignments_to_polylines(alignments: Iterable[Alignment]) -> list[tuple[Alignment, list[XY]]]:
    return [(a, alignment_polyline(a)) for a in alignments]


# ---------------------------------------------------------------------------
# Closed-form pose sampling — used by the chainage walker so labels get the
# true tangent at each station rather than a chord approximation. Spiral
# bearings in particular drift several degrees between densified vertices.
# ---------------------------------------------------------------------------
def _line_length(seg: LineSeg) -> float:
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    return math.hypot(ex - sx, ey - sy)


def _line_pose(seg: LineSeg, s: float) -> tuple[float, float, float]:
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    L = math.hypot(ex - sx, ey - sy)
    t = 0.0 if L <= 0 else s / L
    x = sx + t * (ex - sx)
    y = sy + t * (ey - sy)
    bearing = math.degrees(math.atan2(ey - sy, ex - sx))
    return (x, y, bearing)


def _arc_pose(setup: tuple[float, float, float, float, float, float], s: float) -> tuple[float, float, float]:
    cx, cy, r, a0, dtheta, length = setup
    t = 0.0 if length <= 0 else s / length
    a = a0 + dtheta * t
    x = cx + r * math.cos(a)
    y = cy + r * math.sin(a)
    bearing = math.degrees(a + (math.pi / 2 if dtheta >= 0 else -math.pi / 2))
    return (x, y, bearing)


def _spiral_setup(seg: SpiralSeg) -> tuple[float, float, float, float, float, float]:
    """``(sx, sy, cos_a, sin_a, k0, k1)`` — world placement + curvature endpoints.

    Thin wrapper around :func:`_spiral_placement` that drops the local
    integration buffer (the chainage walker re-samples per station via
    :func:`_spiral_pose`).
    """
    n = max(16, int(math.ceil(seg.length * 0.5)))
    sx, sy, cos_a, sin_a, k0, k1, _ = _spiral_placement(seg, n)
    return (sx, sy, cos_a, sin_a, k0, k1)


def _spiral_pose(seg: SpiralSeg, setup: tuple, s: float) -> tuple[float, float, float]:
    sx, sy, cos_a, sin_a, k0, k1 = setup
    L = seg.length
    s = max(0.0, min(L, s))
    rot_angle = math.atan2(sin_a, cos_a)
    if s <= 0.0:
        return (sx, sy, math.degrees(rot_angle))
    n = max(2, int(math.ceil(s * 0.5)))
    ds = s / n
    lx = ly = 0.0
    cos_prev, sin_prev = 1.0, 0.0
    theta = 0.0
    for i in range(1, n + 1):
        t_s = i * ds
        theta = k0 * t_s + (k1 - k0) * t_s * t_s / (2.0 * L)
        cos_t = math.cos(theta)
        sin_t = math.sin(theta)
        lx += 0.5 * (cos_prev + cos_t) * ds
        ly += 0.5 * (sin_prev + sin_t) * ds
        cos_prev, sin_prev = cos_t, sin_t
    wx = sx + cos_a * lx - sin_a * ly
    wy = sy + sin_a * lx + cos_a * ly
    return (wx, wy, math.degrees(rot_angle + theta))


def segment_curvature(seg: Segment) -> tuple[float, float]:
    """Signed curvature ``(k_start, k_end)`` in 1/m.

    Positive = CCW turning. Lines return ``(0, 0)``; arcs are constant
    curvature so both ends share the same value; clothoid spirals linearly
    transition from ``k0`` to ``k1``. Sign convention matches what the
    chainage walker writes into the ``curvature`` attribute, so the
    segments layer and the stations layer stay numerically consistent.
    """
    if isinstance(seg, LineSeg):
        return (0.0, 0.0)
    if isinstance(seg, CurveSeg):
        _, _, r, _, dtheta, length = _arc_geometry(seg)
        if length <= 0 or r <= 0:
            return (0.0, 0.0)
        k = (1.0 if dtheta >= 0 else -1.0) / r
        return (k, k)
    if isinstance(seg, SpiralSeg):
        sign = -1.0 if seg.rot.lower() == "cw" else 1.0
        k0 = 0.0 if seg.radius_start is None else sign / seg.radius_start
        k1 = 0.0 if seg.radius_end is None else sign / seg.radius_end
        return (k0, k1)
    return (0.0, 0.0)


def segment_length(seg: Segment) -> float:
    """Public arclength accessor — matches the value the chainage walker uses."""
    if isinstance(seg, LineSeg):
        return _line_length(seg)
    if isinstance(seg, CurveSeg):
        return _arc_geometry(seg)[5]
    if isinstance(seg, SpiralSeg):
        return seg.length
    return 0.0


def segment_pose(seg: Segment, s: float) -> tuple[float, float, float] | None:
    """``(x, y, bearing_deg)`` at arclength ``s`` along ``seg``.

    Public counterpart to the internal walker helpers — used by the
    dimensioning module to anchor labels at segment midpoints without
    having to reach into geometry_builder internals.
    """
    if isinstance(seg, LineSeg):
        if _line_length(seg) <= 0:
            return None
        return _line_pose(seg, s)
    if isinstance(seg, CurveSeg):
        setup = _arc_geometry(seg)
        if setup[5] <= 0:
            return None
        return _arc_pose(setup, s)
    if isinstance(seg, SpiralSeg):
        if seg.length <= 0:
            return None
        setup = _spiral_setup(seg)
        return _spiral_pose(seg, setup, s)
    return None


@dataclass(frozen=True)
class ChainagePoint:
    """One station marker enriched with the LandXML segment context.

    Fields are flat scalars so they map directly to QGIS feature attributes
    (and to GeoPackage columns when the plugin persists results). Together
    they let the user style by curvature, segment kind, transition type, or
    any free-text ``desc`` from the source file.

    ``station`` is the *displayed* station (LandXML semantics, with station
    equations applied). ``station_internal`` is the continuous arclength
    coordinate from the alignment start — equal to ``station`` for
    alignments without ``<StaEquation>`` children.
    """

    station: float
    x: float
    y: float
    bearing_deg: float
    seg_index: int
    seg_kind: str          # "line" | "curve" | "spiral"
    transition_type: str   # "" for line/curve; ``spiType`` value for spirals
    curvature: float       # 1/m, signed (positive = CCW turning)
    radius: float | None   # signed metres; None when curvature is ~0
    desc: str              # passthrough of the segment's LandXML ``desc``
    station_internal: float = 0.0  # continuous arclength coord (no equations applied)


# ---------------------------------------------------------------------------
# Station equations — internal arclength ↔ displayed station mapping.
#
# Internal station = ``alignment.sta_start + arclength_from_start``. Display
# station = internal + sum of ``(sta_ahead - sta_internal)`` jumps for every
# equation whose ``sta_internal`` lies at or before the internal position.
# The equation map is a piecewise-constant offset function on the internal
# axis with discontinuities at each equation point; inverting it can yield
# either ``None`` (forward equation: a gap in the display axis) or the
# "before" branch's value (backward equation: overlap zone is ambiguous;
# returning the chronologically-earlier interpretation keeps the geometry
# coherent and matches surveyor convention).
# ---------------------------------------------------------------------------
_EQUATION_CACHE_ATTR = "_align2qgis_equation_segments_cache"


def _equation_segments(
    alignment: Alignment,
) -> list[tuple[float, float, float]]:
    """Return ``[(internal_lo, internal_hi, display_offset), …]`` covering the real line.

    Each segment is a maximal interval of internal stations sharing the
    same constant display offset. With no equations the result is a single
    span ``(-∞, +∞)`` at offset 0. Result is cached on the alignment via a
    private attribute so the hot chainage / projection paths don't re-sort
    on every call.
    """
    cached = getattr(alignment, _EQUATION_CACHE_ATTR, None)
    if cached is not None and cached[0] is alignment.equations:
        return cached[1]
    eqs = sorted(alignment.equations, key=lambda e: e.sta_internal)
    if not eqs:
        segs: list[tuple[float, float, float]] = [(-math.inf, math.inf, 0.0)]
    else:
        segs = []
        prev = -math.inf
        offset = 0.0
        for eq in eqs:
            segs.append((prev, eq.sta_internal, offset))
            offset += eq.sta_ahead - eq.sta_internal
            prev = eq.sta_internal
        segs.append((prev, math.inf, offset))
    try:
        object.__setattr__(alignment, _EQUATION_CACHE_ATTR, (alignment.equations, segs))
    except (AttributeError, TypeError):
        pass
    return segs


def internal_to_display(alignment: Alignment, s_internal: float) -> float:
    """Map an internal station to its displayed value via the equation map.

    Returns ``s_internal`` unchanged when the alignment has no equations.
    Equation points themselves resolve to the "before" branch (display
    ``sta_back``), matching the convention that the equation applies *after*
    its internal coordinate.
    """
    for lo, hi, off in _equation_segments(alignment):
        if lo - 1e-9 <= s_internal <= hi + 1e-9:
            return s_internal + off
    return s_internal


def display_to_internal(
    alignment: Alignment, s_display: float,
) -> float | None:
    """Inverse of :func:`internal_to_display`.

    Returns ``None`` when ``s_display`` falls in a forward equation's gap.
    For backward equations the display segments overlap; we return the
    first (chronologically earliest) internal value so an offset table
    indexed by display station resolves to the upstream interpretation.
    """
    for lo, hi, off in _equation_segments(alignment):
        d_lo = (lo + off) if lo != -math.inf else -math.inf
        d_hi = (hi + off) if hi != math.inf else math.inf
        if d_lo - 1e-9 <= s_display <= d_hi + 1e-9:
            return s_display - off
    return None


def _walker_at(
    walkers: list[tuple], cum: list[float], s: float,
) -> tuple[tuple, float]:
    """Return ``(walker, local_s)`` for the walker that contains arclength ``s``.

    Wraps the cumulative-length lookup and the per-walker offset
    subtraction shared by :func:`alignment_xy_at_station`,
    :func:`alignment_pose_at_station`, and :func:`alignment_chainage`'s
    sample closure. ``local_s`` is clamped at zero so values that fall
    on the segment boundary evaluate at the start of the next walker
    rather than past the end of the previous one.
    """
    i = _locate_walker(cum, s)
    local_s = max(0.0, s - (cum[i - 1] if i > 0 else 0.0))
    return walkers[i], local_s


def _locate_walker(cum: list[float], s: float) -> int:
    """Return the walker index whose arclength span contains ``s``.

    ``cum`` is the strictly increasing list of cumulative end-stations
    ``[len_0, len_0+len_1, …]``. We pick ``bisect_left(cum, s)`` so
    boundary stations land on the walker whose cumulative-end equals ``s``
    (matching the original linear-scan semantics: "first walker whose
    cum end ≥ s, then evaluate at local end"). The result is clamped at
    the upper end; ``bisect_left`` already returns ``≥ 0``.
    """
    idx = bisect.bisect_left(cum, s)
    if idx >= len(cum):
        return len(cum) - 1
    return idx


_WALKERS_CACHE_ATTR = "_align2qgis_walkers_cache"


def _segment_walkers(alignment: Alignment) -> tuple[list[tuple], list[float]]:
    """Return ``(walkers, cum)`` where each walker is
    ``(length, pose_fn, kind, transition, k_start, k_end, desc, idx)`` and
    ``cum[i]`` is the cumulative arclength up to and including walker ``i``.

    ``pose_fn(s)`` returns ``(x, y, bearing_deg)`` at arclength ``s`` from the
    segment start. Curvature endpoints (``k_start``, ``k_end``) let the
    chainage walker linearly interpolate κ along clothoids; for line/arc the
    two values are equal. Setup work (arc sweep, spiral rotation) is
    precomputed once per segment. The cumulative-end array lets callers use
    :func:`_locate_walker` to find the containing walker in O(log N) instead
    of scanning every station linearly.

    Result is cached on the alignment instance keyed off
    ``alignment.segments`` identity — cross-section layer builders that call
    per-station lookups hundreds of times stop paying the setup cost on each
    call. Reprojection mutates segment fields in place, so the cache must
    only be populated *after* any reprojection — every entry point that
    touches geometry already runs reprojection first.
    """
    cached = getattr(alignment, _WALKERS_CACHE_ATTR, None)
    if cached is not None and cached[0] is alignment.segments:
        return cached[1]
    walkers: list[tuple] = []
    cum: list[float] = []
    total = 0.0
    for idx, seg in enumerate(alignment.segments):
        desc = (getattr(seg, "desc", None) or "") if hasattr(seg, "desc") else ""
        if isinstance(seg, LineSeg):
            length = _line_length(seg)
            if length <= 0:
                continue
            walkers.append((
                length,
                (lambda s, seg=seg: _line_pose(seg, s)),
                "line", "", 0.0, 0.0, desc, idx,
            ))
            total += length
            cum.append(total)
        elif isinstance(seg, CurveSeg):
            setup = _arc_geometry(seg)
            _, _, r, _, dtheta, length = setup
            if length <= 0 or r <= 0:
                continue
            k = (1.0 if dtheta >= 0 else -1.0) / r
            walkers.append((
                length,
                (lambda s, setup=setup: _arc_pose(setup, s)),
                "curve", "", k, k, desc, idx,
            ))
            total += length
            cum.append(total)
        elif isinstance(seg, SpiralSeg):
            length = seg.length
            if length <= 0:
                continue
            setup = _spiral_setup(seg)
            _, _, _, _, k0, k1 = setup
            walkers.append((
                length,
                (lambda s, seg=seg, setup=setup: _spiral_pose(seg, setup, s)),
                "spiral", (seg.spi_type or "clothoid").lower(), k0, k1, desc, idx,
            ))
            total += length
            cum.append(total)
    result = (walkers, cum)
    object.__setattr__(alignment, _WALKERS_CACHE_ATTR, (alignment.segments, result))
    return result


def alignment_xy_at_station(
    alignment: Alignment, station: float,
) -> tuple[float, float] | None:
    """Plan-XY at ``station``, or None if out of range. (x, y) only.

    Thin wrapper around :func:`alignment_pose_at_station` (defined below in
    the projection section) for callers that don't need the tangent bearing.
    """
    pose = alignment_pose_at_station(alignment, station)
    return None if pose is None else (pose[0], pose[1])


# ---------------------------------------------------------------------------
# Projection — closest-point-on-alignment for the Processing tools.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProjectionResult:
    """Closest-point projection of an external point onto an alignment.

    ``offset_signed`` follows the left-positive convention: positive values
    lie on the left of the forward (chainage-increasing) direction. The
    residual is the unsigned distance from the point to the foot — equal
    to ``abs(offset_signed)`` except in degenerate cases (zero-length
    segment, point exactly on the curve).
    """

    station_display: float
    station_internal: float
    offset_signed: float
    residual: float
    seg_index: int
    foot_x: float
    foot_y: float


class ProjectionStep(NamedTuple):
    """One segment's closest-point result, before alignment-level station math.

    Returned by each ``_project_to_*`` helper and consumed only by
    :func:`alignment_project_point`, which picks the smallest-``residual``
    step and converts ``s_local`` into the displayed/internal stations of
    the final :class:`ProjectionResult`. ``offset_signed`` is left-positive
    of the forward direction; ``(foot_x, foot_y)`` is the foot of the
    perpendicular on the segment.

    A :class:`~typing.NamedTuple` rather than a dataclass so the helpers'
    field names self-document the consumer while the value still unpacks as
    the ordered 5-tuple the projection tests assert against.
    """

    s_local: float
    offset_signed: float
    foot_x: float
    foot_y: float
    residual: float


def _signed_left_offset(
    px: float, py: float, fx: float, fy: float, tx: float, ty: float,
) -> float:
    """Signed distance from ``(fx, fy)`` to ``(px, py)`` along the left-perpendicular of ``(tx, ty)``.

    ``(tx, ty)`` is the forward unit tangent at the foot; the left-perp is
    ``(-ty, tx)``. Positive = left of the chainage-increasing direction.
    """
    return (px - fx) * (-ty) + (py - fy) * tx


def _project_to_line_piece(
    piece: LinePiece, px: float, py: float,
) -> ProjectionStep:
    """Closest point on a line piece."""
    sx, sy = piece.start
    ex, ey = piece.end
    dx, dy = ex - sx, ey - sy
    L = math.hypot(dx, dy)
    if L < 1e-12:
        return ProjectionStep(0.0, 0.0, sx, sy, math.hypot(px - sx, py - sy))
    tx, ty = dx / L, dy / L
    s = (px - sx) * tx + (py - sy) * ty
    s_clamped = max(0.0, min(L, s))
    fx = sx + s_clamped * tx
    fy = sy + s_clamped * ty
    offset_signed = _signed_left_offset(px, py, fx, fy, tx, ty)
    residual = math.hypot(px - fx, py - fy)
    return ProjectionStep(s_clamped, offset_signed, fx, fy, residual)


def _project_to_curve_seg(
    seg: CurveSeg, px: float, py: float,
) -> ProjectionStep:
    """Closest point on a circular arc."""
    cx, cy, r, a0, dtheta, L = _arc_geometry(seg)
    if L <= 0 or r <= 0:
        sx, sy = ne_to_xy(seg.start)
        return ProjectionStep(0.0, 0.0, sx, sy, math.hypot(px - sx, py - sy))
    abs_sweep = abs(dtheta)
    sign = 1.0 if dtheta >= 0 else -1.0
    a_p = math.atan2(py - cy, px - cx)
    # CCW angular distance from a0 to a_p, in [0, 2π).
    ccw_delta = (a_p - a0) % (2 * math.pi)
    # Convert to the arc's sweep direction.
    forward_delta = ccw_delta if sign > 0 else (-ccw_delta) % (2 * math.pi)
    if forward_delta <= abs_sweep + 1e-12:
        t = forward_delta / abs_sweep
    else:
        # Point falls in the angular gap between the arc end and arc start
        # going around the unswept side; foot is whichever endpoint is
        # closer along the gap.
        past_end = forward_delta - abs_sweep
        gap = 2 * math.pi - abs_sweep
        t = 1.0 if past_end < gap / 2.0 else 0.0
    s_local = t * L
    a_foot = a0 + dtheta * t
    fx = cx + r * math.cos(a_foot)
    fy = cy + r * math.sin(a_foot)
    rad_x, rad_y = math.cos(a_foot), math.sin(a_foot)
    if sign > 0:
        tx, ty = -rad_y, rad_x
    else:
        tx, ty = rad_y, -rad_x
    offset_signed = _signed_left_offset(px, py, fx, fy, tx, ty)
    residual = math.hypot(px - fx, py - fy)
    return ProjectionStep(s_local, offset_signed, fx, fy, residual)


def _project_to_spiral_seg(
    seg: SpiralSeg, px: float, py: float, n_sweep: int = 24,
) -> ProjectionStep:
    """Closest point on a clothoid.

    No closed-form inversion exists for the Fresnel integrals; we coarse-
    sweep ``n_sweep`` candidates over ``[0, L]`` to seed a golden-section
    minimization. The seed is exact enough that ~40 golden-section steps
    converge to sub-millimetre precision on typical road radii.
    """
    L = seg.length
    if L <= 0:
        sx, sy = ne_to_xy(seg.start)
        return ProjectionStep(0.0, 0.0, sx, sy, math.hypot(px - sx, py - sy))
    setup = _spiral_setup(seg)

    def dist_sq(s: float) -> float:
        x, y, _ = _spiral_pose(seg, setup, s)
        return (x - px) ** 2 + (y - py) ** 2

    best_s = 0.0
    best_d = dist_sq(0.0)
    for i in range(1, n_sweep + 1):
        s = L * i / n_sweep
        d = dist_sq(s)
        if d < best_d:
            best_d = d
            best_s = s
    step = L / n_sweep
    lo = max(0.0, best_s - step)
    hi = min(L, best_s + step)
    inv_phi = 2.0 / (1.0 + math.sqrt(5.0))
    a = hi - inv_phi * (hi - lo)
    b = lo + inv_phi * (hi - lo)
    fa, fb = dist_sq(a), dist_sq(b)
    for _ in range(50):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - inv_phi * (hi - lo)
            fa = dist_sq(a)
        else:
            lo, a, fa = a, b, fb
            b = lo + inv_phi * (hi - lo)
            fb = dist_sq(b)
        if hi - lo < 1e-9:
            break
    s_opt = 0.5 * (lo + hi)
    fx, fy, bearing_deg = _spiral_pose(seg, setup, s_opt)
    bearing = math.radians(bearing_deg)
    tx, ty = math.cos(bearing), math.sin(bearing)
    offset_signed = _signed_left_offset(px, py, fx, fy, tx, ty)
    residual = math.hypot(px - fx, py - fy)
    return ProjectionStep(s_opt, offset_signed, fx, fy, residual)


def alignment_project_point(
    alignment: Alignment, px: float, py: float,
) -> ProjectionResult | None:
    """Project ``(px, py)`` onto ``alignment`` and return the closest match.

    Walks every segment, projects onto each, and keeps the foot with the
    smallest residual. Station values follow the equation map (``station``
    is the displayed value; ``station_internal`` is the continuous
    arclength coordinate).
    """
    sta_start = alignment.sta_start or 0.0
    cum = sta_start
    best_residual = math.inf
    best: ProjectionResult | None = None
    for idx, seg in enumerate(alignment.segments):
        L = segment_length(seg)
        if L <= 0:
            continue
        if isinstance(seg, LineSeg):
            sx, sy = ne_to_xy(seg.start)
            ex, ey = ne_to_xy(seg.end)
            piece = LinePiece((sx, sy), (ex, ey))
            step = _project_to_line_piece(piece, px, py)
        elif isinstance(seg, CurveSeg):
            step = _project_to_curve_seg(seg, px, py)
        elif isinstance(seg, SpiralSeg):
            step = _project_to_spiral_seg(seg, px, py)
        else:
            cum += L
            continue
        if step.residual < best_residual:
            best_residual = step.residual
            station_internal = cum + step.s_local
            station_display = internal_to_display(alignment, station_internal)
            best = ProjectionResult(
                station_display=station_display,
                station_internal=station_internal,
                offset_signed=step.offset_signed,
                residual=step.residual,
                seg_index=idx,
                foot_x=step.foot_x,
                foot_y=step.foot_y,
            )
        cum += L
    return best


def alignment_pose_at_station(
    alignment: Alignment, station_display: float,
) -> tuple[float, float, float] | None:
    """``(x, y, bearing_deg)`` at the given *displayed* station, or None.

    Used by the inverse processing algorithm (point-from-station+offset)
    to compute the perpendicular offset. ``bearing_deg`` is math
    convention (CCW from east), consumed by callers that apply
    ``(x', y') = (x - offset·sin(b), y + offset·cos(b))`` for left-positive
    offset.
    """
    internal = display_to_internal(alignment, station_display)
    if internal is None:
        return None
    walkers, cum_arr = _segment_walkers(alignment)
    if not walkers:
        return None
    total = cum_arr[-1]
    sta_start = alignment.sta_start or 0.0
    s = internal - sta_start
    if s < -1e-6 or s > total + 1e-6:
        return None
    if s >= total:
        length, pose_fn, *_ = walkers[-1]
        return pose_fn(length)
    walker, local_s = _walker_at(walkers, cum_arr, s)
    _, pose_fn, *_ = walker
    return pose_fn(local_s)


def alignment_chainage(
    alignment: Alignment,
    interval: float,
    include_endpoints: bool = True,
) -> list[ChainagePoint]:
    """Round-station markers from exact segment math.

    Each :class:`ChainagePoint` carries the true tangent bearing (in math
    convention, CCW from east) and the curvature at that station — for
    spirals, ``κ`` is linearly interpolated between ``k0`` and ``k1`` along
    arclength, matching the clothoid definition the rendering uses.

    Round stations land on absolute multiples of ``interval`` (surveyor's
    convention shared with :func:`stationing.chainage_points`).
    """
    if interval <= 0:
        return []

    walkers, cum = _segment_walkers(alignment)
    if not walkers:
        return []
    total = cum[-1]
    if total <= 0:
        return []

    sta_start = alignment.sta_start or 0.0
    sta_internal_end = sta_start + total
    sta_display_end = internal_to_display(alignment, sta_internal_end)

    def sample(internal_station: float, display_station: float) -> ChainagePoint | None:
        s = internal_station - sta_start
        if s < -1e-9 or s > total + 1e-9:
            return None
        if s >= total:
            length, pose_fn, kind, ttype, _k0, k1, desc, idx = walkers[-1]
            x, y, bearing = pose_fn(length)
            k = k1
        else:
            walker, local_s = _walker_at(walkers, cum, s)
            length, pose_fn, kind, ttype, k0, k1, desc, idx = walker
            x, y, bearing = pose_fn(local_s)
            t = local_s / length if length > 0 else 0.0
            k = k0 + (k1 - k0) * t
        radius = (1.0 / k) if abs(k) > 1e-12 else None
        return ChainagePoint(
            station=display_station,
            x=x,
            y=y,
            bearing_deg=bearing,
            seg_index=idx,
            seg_kind=kind,
            transition_type=ttype,
            curvature=k,
            radius=radius,
            desc=desc,
            station_internal=internal_station,
        )

    out: list[ChainagePoint] = []
    if include_endpoints:
        cp = sample(sta_start, sta_start)
        if cp is not None:
            out.append(cp)

    # Walk each segment of the equation map separately so round stations
    # land on multiples of ``interval`` in the *displayed* axis on each
    # side of every equation discontinuity. Each equation point itself is
    # emitted as an explicit station marker (display = sta_back, the
    # "before" branch) so surveyors see the equation in the layer.
    segments = _equation_segments(alignment)
    for idx, (lo_int, hi_int, offset) in enumerate(segments):
        sub_lo = max(lo_int, sta_start)
        sub_hi = min(hi_int, sta_internal_end)
        if sub_lo > sub_hi - 1e-9:
            continue
        disp_lo = sub_lo + offset
        disp_hi = sub_hi + offset
        first = math.ceil(disp_lo / interval) * interval
        if first <= disp_lo + 1e-9:
            first += interval
        sta_disp = first
        while sta_disp < disp_hi - 1e-9:
            cp = sample(sta_disp - offset, sta_disp)
            if cp is not None:
                out.append(cp)
            sta_disp += interval
        # If this segment ends at an equation point that's not the alignment
        # end, emit it explicitly. ``disp_hi`` is the display value at the
        # equation's "before" branch (= sta_back in LandXML).
        if (
            idx < len(segments) - 1
            and hi_int < sta_internal_end - 1e-9
            and hi_int > sta_start + 1e-9
        ):
            cp = sample(hi_int, disp_hi)
            if cp is not None:
                out.append(cp)

    if include_endpoints:
        cp = sample(sta_internal_end, sta_display_end)
        if cp is not None:
            out.append(cp)

    return out
