"""Build automatic dimensioning features (arc radii, spiral A-values, …).

One point feature per dimensionable segment, anchored at the segment
midpoint and rotated to the tangent so labels read along the curve. The
output layer is independent from the chainage layer so users can toggle
dimensions on/off in the QGIS layer panel without touching their station
grid.

Each dimension category is its own toggle on the import dialog. Tangent
lengths are off by default because they're noisy on dense alignments.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry_builder import segment_length, segment_pose
from .landxml_parser import Alignment, CurveSeg, LineSeg, SpiralSeg


@dataclass(frozen=True)
class DimensionFeature:
    x: float
    y: float
    bearing_deg: float    # math convention, CCW from east
    label: str
    alignment: str
    seg_index: int
    seg_kind: str         # "curve" | "spiral" | "line"
    radius: float | None  # for arcs and spirals (signed convention not preserved)
    spiral_a: float | None
    seg_length: float


def _format_radius(r: float) -> str:
    return f"R={r:.1f} m"


def _spiral_label(seg: SpiralSeg) -> tuple[str, float | None]:
    """``(label, A-value)`` for a clothoid spiral.

    ``A`` is the clothoid constant ``A = √(R·L)`` using the finite-radius end
    of the spiral. For a biparametric spiral with two finite radii, the end
    with the tighter curve drives A.
    """
    L = seg.length
    radii = [r for r in (seg.radius_start, seg.radius_end) if r is not None and r > 0]
    if not radii:
        return (f"L={L:.1f} m", None)
    r = min(radii)
    A = math.sqrt(r * L)
    return (f"A={A:.0f} / L={L:.0f} m", A)


def build_dimensions(
    alignment: Alignment,
    arcs: bool = True,
    spirals: bool = True,
    tangents: bool = False,
) -> list[DimensionFeature]:
    """Emit dimension features for ``alignment`` subject to category toggles."""
    out: list[DimensionFeature] = []
    for idx, seg in enumerate(alignment.segments):
        length = segment_length(seg)
        if length <= 0:
            continue
        pose = segment_pose(seg, length / 2.0)
        if pose is None:
            continue
        x, y, bearing = pose

        if isinstance(seg, CurveSeg) and arcs:
            out.append(DimensionFeature(
                x=x, y=y, bearing_deg=bearing,
                label=_format_radius(seg.radius),
                alignment=alignment.name, seg_index=idx,
                seg_kind="curve", radius=seg.radius,
                spiral_a=None, seg_length=length,
            ))
        elif isinstance(seg, SpiralSeg) and spirals:
            label, A = _spiral_label(seg)
            r = next(
                (r for r in (seg.radius_end, seg.radius_start) if r is not None),
                None,
            )
            out.append(DimensionFeature(
                x=x, y=y, bearing_deg=bearing,
                label=label,
                alignment=alignment.name, seg_index=idx,
                seg_kind="spiral", radius=r,
                spiral_a=A, seg_length=length,
            ))
        elif isinstance(seg, LineSeg) and tangents:
            out.append(DimensionFeature(
                x=x, y=y, bearing_deg=bearing,
                label=f"L={length:.1f} m",
                alignment=alignment.name, seg_index=idx,
                seg_kind="line", radius=None, spiral_a=None,
                seg_length=length,
            ))
    return out
