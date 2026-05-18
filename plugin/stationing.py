"""Place chainage (station) points along a polyline.

The plugin emits one feature per round station (``interval`` metres apart, at
*absolute* multiples of the interval, not relative to ``sta_start``) so a
rail plan that starts at sta 1023.45 gets markers at 1050, 1100, 1150… —
the usual surveyor's convention. The polyline endpoints are added too with
their real stations, so the BoP/EoP are always visible.

Each point carries the tangent bearing of the segment it sits on, normalized
to the (-90°, 90°] half-circle so labels drawn at that rotation stay upright
even when the alignment is run east→west.
"""
from __future__ import annotations

import math

XY = tuple[float, float]


def _bearing_deg(p0: XY, p1: XY) -> float:
    """Tangent direction of segment p0→p1 in degrees (math convention,
    CCW from east). Matches QGIS data-defined ``LabelRotation`` semantics."""
    return math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))


def upright_bearing(angle_deg: float) -> float:
    """Fold a bearing into (-90°, 90°] so labels render right-side up.

    A 180° flip is visually identical for an unrotated reader, so an alignment
    running west (bearing 180°) is shown as 0°. Without this, station labels
    on a westbound alignment would print upside-down.
    """
    a = ((angle_deg + 180.0) % 360.0) - 180.0
    if a > 90.0:
        a -= 180.0
    elif a <= -90.0:
        a += 180.0
    return a


def chainage_points(
    polyline: list[XY],
    sta_start: float,
    interval: float,
    include_endpoints: bool = True,
) -> list[tuple[float, float, float, float]]:
    """Return ``[(station_m, x, y, bearing_deg), …]`` at round chainage stations.

    ``polyline`` is densified in (x, y); we walk it by arclength and linearly
    interpolate the station position. Round stations are placed at absolute
    multiples of ``interval`` (e.g. interval=50 → 50, 100, 150, …) so adjacent
    alignments share a station grid. ``bearing_deg`` is the tangent direction
    of the polyline segment that hosts the station, normalized via
    :func:`upright_bearing`.
    """
    if interval <= 0 or len(polyline) < 2:
        return []

    cum = [0.0]
    for i in range(1, len(polyline)):
        d = math.hypot(
            polyline[i][0] - polyline[i - 1][0],
            polyline[i][1] - polyline[i - 1][1],
        )
        cum.append(cum[-1] + d)
    total = cum[-1]
    sta_end = sta_start + total
    if total <= 0:
        return []

    out: list[tuple[float, float, float, float]] = []
    if include_endpoints:
        b = upright_bearing(_bearing_deg(polyline[0], polyline[1]))
        out.append((sta_start, polyline[0][0], polyline[0][1], b))

    first = math.ceil(sta_start / interval) * interval
    if first <= sta_start + 1e-9:
        first += interval

    j = 0
    station = first
    while station < sta_end - 1e-9:
        target = station - sta_start
        while j < len(cum) - 1 and cum[j + 1] < target:
            j += 1
        if j >= len(cum) - 1:
            break
        seg_len = cum[j + 1] - cum[j]
        if seg_len <= 0:
            station += interval
            continue
        t = (target - cum[j]) / seg_len
        x = polyline[j][0] + t * (polyline[j + 1][0] - polyline[j][0])
        y = polyline[j][1] + t * (polyline[j + 1][1] - polyline[j][1])
        b = upright_bearing(_bearing_deg(polyline[j], polyline[j + 1]))
        out.append((station, x, y, b))
        station += interval

    if include_endpoints:
        b = upright_bearing(_bearing_deg(polyline[-2], polyline[-1]))
        out.append((sta_end, polyline[-1][0], polyline[-1][1], b))

    return out


def format_station(station_m: float) -> str:
    """Format ``1250.0`` as ``"1+250.00"`` — rail/road plan convention."""
    km = int(station_m // 1000)
    rest = station_m - km * 1000
    return f"{km}+{rest:06.2f}"
