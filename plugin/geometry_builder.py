"""Discretize LandXML alignment segments into ordered (x, y) polylines.

All output is in QGIS axis order: x = Easting, y = Northing. Inputs from
``landxml_parser`` are in LandXML order (north, east) and are swapped here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .landxml_parser import Alignment, CurveSeg, LineSeg, Segment, SpiralSeg


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


def spiral_points(seg: SpiralSeg, samples_per_meter: float = 0.5) -> list[XY]:
    """Discretize a clothoid spiral and place it in world coords.

    Strategy:
    1. Integrate locally with start at origin and initial tangent = +x.
    2. Compute the rotation/translation that maps local start→world start
       AND local initial tangent → world tangent at start.

    The world tangent at start is inferred from the PI point when available
    (Start→PI direction is the tangent at Start); otherwise we fall back to
    solving against the End point.
    """
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    L = seg.length

    # Curvature at start / end. None means infinite radius (straight tangent).
    r0 = seg.radius_start
    r1 = seg.radius_end
    sign = -1.0 if seg.rot.lower() == "cw" else 1.0
    k0 = 0.0 if r0 is None else sign / r0
    k1 = 0.0 if r1 is None else sign / r1

    n = max(16, int(math.ceil(L * samples_per_meter)))
    local = _local_clothoid(L, k0, k1, n)

    # Determine world rotation. Prefer PI tangent if present.
    if seg.pi is not None:
        px, py = ne_to_xy(seg.pi)
        tx = px - sx
        ty = py - sy
        norm = math.hypot(tx, ty)
        if norm > 1e-9:
            cos_a = tx / norm
            sin_a = ty / norm
        else:
            cos_a, sin_a = _solve_rotation_from_endpoints(local, sx, sy, ex, ey)
    else:
        cos_a, sin_a = _solve_rotation_from_endpoints(local, sx, sy, ex, ey)

    world: list[XY] = []
    for lx, ly in local:
        wx = sx + cos_a * lx - sin_a * ly
        wy = sy + sin_a * lx + cos_a * ly
        world.append((wx, wy))
    # Pin endpoints — small integration drift is expected for long spirals.
    world[0] = (sx, sy)
    world[-1] = (ex, ey)
    return world


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
    """``(sx, sy, cos_a, sin_a, k0, k1)`` — world placement + curvature endpoints."""
    sx, sy = ne_to_xy(seg.start)
    ex, ey = ne_to_xy(seg.end)
    L = seg.length
    sign = -1.0 if seg.rot.lower() == "cw" else 1.0
    k0 = 0.0 if seg.radius_start is None else sign / seg.radius_start
    k1 = 0.0 if seg.radius_end is None else sign / seg.radius_end

    cos_a = 1.0
    sin_a = 0.0
    if seg.pi is not None:
        px, py = ne_to_xy(seg.pi)
        tx = px - sx
        ty = py - sy
        norm = math.hypot(tx, ty)
        if norm > 1e-9:
            cos_a, sin_a = tx / norm, ty / norm
        else:
            n = max(16, int(math.ceil(L * 0.5)))
            local = _local_clothoid(L, k0, k1, n)
            cos_a, sin_a = _solve_rotation_from_endpoints(local, sx, sy, ex, ey)
    else:
        n = max(16, int(math.ceil(L * 0.5)))
        local = _local_clothoid(L, k0, k1, n)
        cos_a, sin_a = _solve_rotation_from_endpoints(local, sx, sy, ex, ey)

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


def _segment_walkers(alignment: Alignment) -> list[tuple]:
    """Return ``[(length, pose_fn, kind, transition, k_start, k_end, desc, idx), …]``.

    ``pose_fn(s)`` returns ``(x, y, bearing_deg)`` at arclength ``s`` from the
    segment start. Curvature endpoints (``k_start``, ``k_end``) let the
    chainage walker linearly interpolate κ along clothoids; for line/arc the
    two values are equal. Setup work (arc sweep, spiral rotation) is
    precomputed once per segment.
    """
    walkers: list[tuple] = []
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
    return walkers


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

    walkers = _segment_walkers(alignment)
    if not walkers:
        return []
    total = sum(w[0] for w in walkers)
    if total <= 0:
        return []

    sta_start = alignment.sta_start or 0.0
    sta_end = sta_start + total

    def sample(station: float) -> ChainagePoint | None:
        s = station - sta_start
        if s < -1e-9 or s > total + 1e-9:
            return None
        if s >= total:
            length, pose_fn, kind, ttype, _k0, k1, desc, idx = walkers[-1]
            x, y, bearing = pose_fn(length)
            k = k1
        else:
            for length, pose_fn, kind, ttype, k0, k1, desc, idx in walkers:
                if s <= length + 1e-9:
                    local_s = max(0.0, s)
                    x, y, bearing = pose_fn(local_s)
                    t = local_s / length if length > 0 else 0.0
                    k = k0 + (k1 - k0) * t
                    break
                s -= length
            else:  # pragma: no cover — guarded by the cumulative-length checks above
                return None
        radius = (1.0 / k) if abs(k) > 1e-12 else None
        return ChainagePoint(
            station=station,
            x=x,
            y=y,
            bearing_deg=bearing,
            seg_index=idx,
            seg_kind=kind,
            transition_type=ttype,
            curvature=k,
            radius=radius,
            desc=desc,
        )

    out: list[ChainagePoint] = []
    if include_endpoints:
        cp = sample(sta_start)
        if cp is not None:
            out.append(cp)

    first = math.ceil(sta_start / interval) * interval
    if first <= sta_start + 1e-9:
        first += interval

    station = first
    while station < sta_end - 1e-9:
        cp = sample(station)
        if cp is not None:
            out.append(cp)
        station += interval

    if include_endpoints:
        cp = sample(sta_end)
        if cp is not None:
            out.append(cp)

    return out
