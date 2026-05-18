"""In-memory cache of parsed alignments + their densified vertical profile.

Keyed by source LandXML path so the profile dock can pull data without
re-parsing the XML or re-running parabolic curve densification on every
active-layer change. Module-level so it survives plugin reload during
development; reset on QGIS restart.
"""
from __future__ import annotations

from .landxml_parser import Alignment, profile_samples

_CACHE: dict[str, tuple[list[Alignment], list[tuple[float, float]]]] = {}


def remember(source_path: str, alignments: list[Alignment]) -> None:
    """Cache the parsed alignments plus their flattened vertical profile.

    Densification happens once here so the dock's per-selection-change
    refresh stays cheap.
    """
    vert: list[tuple[float, float]] = []
    for a in alignments:
        vert.extend(profile_samples(a.profile))
    vert.sort()
    _CACHE[source_path] = (alignments, vert)


def forget(source_path: str) -> None:
    _CACHE.pop(source_path, None)


def vert_samples(source_path: str) -> list[tuple[float, float]]:
    entry = _CACHE.get(source_path)
    return entry[1] if entry else []


def alignments(source_path: str) -> list[Alignment]:
    entry = _CACHE.get(source_path)
    return entry[0] if entry else []
