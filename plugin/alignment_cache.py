"""Lazy LandXML → ``list[Alignment]`` resolver keyed by source path + mtime.

Callers (Processing algorithms, the profile dock) get a ``QgsVectorLayer``
created by Align2QGIS and need the original
:class:`~plugin.landxml_parser.Alignment` dataclasses to do spiral-aware
projection, equation lookup, or vertical-curve analysis. Re-parsing the
LandXML on every invocation is wasteful — and the same file is typically
the source for many queries — so cache by ``(path, mtime)`` and invalidate
on file changes.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from typing import TYPE_CHECKING

from .constants import PROP_SOURCE_PATH
from .landxml_parser import Alignment, parse_alignments_with_meta

if TYPE_CHECKING:
    from qgis.core import QgsVectorLayer


_CACHE: "OrderedDict[tuple[str, float], list[Alignment]]" = OrderedDict()
_CACHE_MAX_ENTRIES = 16


def alignments_for_layer(layer: "QgsVectorLayer") -> list[Alignment]:
    """Resolve the LandXML behind ``layer`` and return the parsed alignments.

    Returns ``[]`` when the layer has no ``align2qgis/source_path`` custom
    property (e.g. a hand-drawn layer the user picked by mistake) or when
    the file is no longer readable. ``getmtime`` doubles as the existence
    probe — the ``OSError`` catch covers both "missing" and "not readable"
    without a TOCTOU race against a separate ``os.path.exists``.
    """
    path = layer.customProperty(PROP_SOURCE_PATH)
    if not path or not isinstance(path, str):
        return []
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    key = (path, mtime)
    cached = _CACHE.get(key)
    if cached is not None:
        _CACHE.move_to_end(key)
        return cached
    try:
        with open(path, "rb") as fh:
            alignments, _ = parse_alignments_with_meta(fh.read())
    except (OSError, ValueError):
        return []
    _CACHE[key] = alignments
    while len(_CACHE) > _CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)
    return alignments


def alignments_by_name(layer: "QgsVectorLayer") -> dict[str, Alignment]:
    """Convenience: ``{alignment.name: Alignment}`` for the layer's source."""
    return {a.name: a for a in alignments_for_layer(layer)}
