"""In-memory QgsVectorLayer builders, one per output kind.

Each builder takes the parsed ``alignments`` + ``crs_authid`` plus a few
options and returns a fresh memory layer. No QGIS UI side effects — the
plugin orchestrator adds them to the project / writes them to GPKG.

Common scaffolding (CRS resolution, memory-layer URI assembly, attribute
schema attachment) lives in :func:`_new_memory_layer` so each builder
stays focused on its own field set + per-feature loop.
"""
from __future__ import annotations

import re

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsVectorLayer,
)

from .constants import PROP_CRS, PROP_SOURCE_PATH
from .dimensions import build_dimensions
from .geometry_builder import (
    alignment_chainage,
    alignment_polyline,
    segment_curvature,
    segment_length,
    segment_points,
)
from .landxml_parser import CurveSeg, SpiralSeg
from .stationing import format_station, upright_bearing


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def safe_name(stem: str) -> str:
    """Sanitize a filename stem into a GeoPackage-friendly layer name."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_") or "layer"


def tag_layer(layer: QgsVectorLayer, source_path: str, crs_authid: str) -> QgsVectorLayer:
    """Tag a layer with the plugin's source-path / CRS custom properties.

    The dock + Re-apply / purge logic all key off these — tagging is the
    contract that says "I created this".
    """
    layer.setCustomProperty(PROP_SOURCE_PATH, source_path)
    layer.setCustomProperty(PROP_CRS, crs_authid)
    return layer


def _new_memory_layer(
    name: str, crs_authid: str, geom_type: str, fields: list[QgsField]
) -> QgsVectorLayer:
    crs = QgsCoordinateReferenceSystem(crs_authid)
    uri = f"{geom_type}?crs={crs.authid() if crs.isValid() else crs_authid}"
    layer = QgsVectorLayer(uri, name, "memory")
    layer.dataProvider().addAttributes(fields)
    layer.updateFields()
    return layer


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def build_alignment_layer(
    alignments, layer_name: str, crs_authid: str
) -> QgsVectorLayer:
    """One polyline feature per alignment — the rendered horizontal curve."""
    layer = _new_memory_layer(
        layer_name, crs_authid, "LineString",
        [
            QgsField("name", QVariant.String),
            QgsField("length_xml", QVariant.Double),
            QgsField("length_geom", QVariant.Double),
            QgsField("sta_start", QVariant.Double),
            QgsField("n_segments", QVariant.Int),
        ],
    )

    features: list[QgsFeature] = []
    for alignment in alignments:
        pts = alignment_polyline(alignment)
        if len(pts) < 2:
            continue
        geom = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts])
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttribute("name", alignment.name)
        feat.setAttribute("length_xml", alignment.length)
        feat.setAttribute("length_geom", geom.length())
        feat.setAttribute("sta_start", alignment.sta_start)
        feat.setAttribute("n_segments", len(alignment.segments))
        features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def build_segment_layer(
    alignments, layer_name: str, crs_authid: str
) -> QgsVectorLayer:
    """One polyline feature per LandXML segment (Line / Curve / Spiral)."""
    layer = _new_memory_layer(
        layer_name, crs_authid, "LineString",
        [
            QgsField("alignment", QVariant.String),
            QgsField("seg_index", QVariant.Int),
            QgsField("kind", QVariant.String),
            QgsField("transition_type", QVariant.String),
            QgsField("length", QVariant.Double),
            QgsField("radius_start", QVariant.Double),
            QgsField("radius_end", QVariant.Double),
            QgsField("curvature_start", QVariant.Double),
            QgsField("curvature_end", QVariant.Double),
            QgsField("spiral_a", QVariant.Double),
            QgsField("sta_start", QVariant.Double),
            QgsField("sta_end", QVariant.Double),
            QgsField("rot", QVariant.String),
            QgsField("desc", QVariant.String),
        ],
    )

    features: list[QgsFeature] = []
    for alignment in alignments:
        sta = alignment.sta_start or 0.0
        for idx, seg in enumerate(alignment.segments):
            length = segment_length(seg)
            if length <= 0:
                continue
            pts = segment_points(seg)
            if len(pts) < 2:
                continue
            geom = QgsGeometry.fromPolylineXY([QgsPointXY(x, y) for x, y in pts])

            k_start, k_end = segment_curvature(seg)
            radius_start = (1.0 / k_start) if abs(k_start) > 1e-12 else None
            radius_end = (1.0 / k_end) if abs(k_end) > 1e-12 else None

            transition_type = ""
            spiral_a: float | None = None
            rot = ""
            if isinstance(seg, CurveSeg):
                rot = (seg.rot or "").lower()
            elif isinstance(seg, SpiralSeg):
                transition_type = (seg.spi_type or "clothoid").lower()
                rot = (seg.rot or "").lower()
                r = next(
                    (r for r in (seg.radius_end, seg.radius_start) if r is not None and r > 0),
                    None,
                )
                if r is not None:
                    spiral_a = (r * length) ** 0.5

            feat = QgsFeature(layer.fields())
            feat.setGeometry(geom)
            feat.setAttribute("alignment", alignment.name)
            feat.setAttribute("seg_index", idx)
            feat.setAttribute("kind", seg.kind)
            feat.setAttribute("transition_type", transition_type)
            feat.setAttribute("length", length)
            feat.setAttribute("radius_start", radius_start)
            feat.setAttribute("radius_end", radius_end)
            feat.setAttribute("curvature_start", k_start)
            feat.setAttribute("curvature_end", k_end)
            feat.setAttribute("spiral_a", spiral_a)
            feat.setAttribute("sta_start", sta)
            feat.setAttribute("sta_end", sta + length)
            feat.setAttribute("rot", rot)
            feat.setAttribute("desc", getattr(seg, "desc", None) or "")
            features.append(feat)
            sta += length

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def build_chainage_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    interval: float,
    perpendicular: bool = False,
) -> QgsVectorLayer:
    """One point feature per round chainage station."""
    layer = _new_memory_layer(
        layer_name, crs_authid, "Point",
        [
            QgsField("alignment", QVariant.String),
            QgsField("station", QVariant.Double),
            QgsField("label", QVariant.String),
            QgsField("rotation", QVariant.Double),
            QgsField("rotation_perp", QVariant.Double),
            QgsField("seg_index", QVariant.Int),
            QgsField("seg_kind", QVariant.String),
            QgsField("transition_type", QVariant.String),
            QgsField("curvature", QVariant.Double),
            QgsField("radius", QVariant.Double),
            QgsField("desc", QVariant.String),
            QgsField("is_endpoint", QVariant.Bool),
        ],
    )

    label_offset = 90.0 if perpendicular else 0.0
    features: list[QgsFeature] = []
    for alignment in alignments:
        stations = alignment_chainage(alignment, interval, include_endpoints=True)
        if not stations:
            continue
        last_idx = len(stations) - 1
        for i, cp in enumerate(stations):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cp.x, cp.y)))
            feat.setAttribute("alignment", alignment.name)
            feat.setAttribute("station", cp.station)
            feat.setAttribute("label", format_station(cp.station))
            # QGIS rotation is CW in screen space; our bearing is math CCW
            # in map space (y-up). Negate so labels/markers align with the
            # actual tangent direction.
            feat.setAttribute(
                "rotation", upright_bearing(-(cp.bearing_deg + label_offset))
            )
            feat.setAttribute(
                "rotation_perp", upright_bearing(-(cp.bearing_deg + 90.0))
            )
            feat.setAttribute("seg_index", cp.seg_index)
            feat.setAttribute("seg_kind", cp.seg_kind)
            feat.setAttribute("transition_type", cp.transition_type)
            feat.setAttribute("curvature", cp.curvature)
            feat.setAttribute("radius", cp.radius)
            feat.setAttribute("desc", cp.desc)
            feat.setAttribute("is_endpoint", i == 0 or i == last_idx)
            features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def build_dimension_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    arcs: bool,
    spirals: bool,
    tangents: bool,
    perpendicular: bool = False,
) -> QgsVectorLayer:
    """One point feature per dimensionable segment (R=… arcs, A=… spirals)."""
    layer = _new_memory_layer(
        layer_name, crs_authid, "Point",
        [
            QgsField("alignment", QVariant.String),
            QgsField("seg_index", QVariant.Int),
            QgsField("kind", QVariant.String),
            QgsField("label", QVariant.String),
            QgsField("rotation", QVariant.Double),
            QgsField("radius", QVariant.Double),
            QgsField("spiral_a", QVariant.Double),
            QgsField("seg_length", QVariant.Double),
        ],
    )

    label_offset = 90.0 if perpendicular else 0.0
    features: list[QgsFeature] = []
    for alignment in alignments:
        for d in build_dimensions(
            alignment, arcs=arcs, spirals=spirals, tangents=tangents
        ):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(d.x, d.y)))
            feat.setAttribute("alignment", d.alignment)
            feat.setAttribute("seg_index", d.seg_index)
            feat.setAttribute("kind", d.seg_kind)
            feat.setAttribute("label", d.label)
            feat.setAttribute(
                "rotation", upright_bearing(-(d.bearing_deg + label_offset))
            )
            feat.setAttribute("radius", d.radius)
            feat.setAttribute("spiral_a", d.spiral_a)
            feat.setAttribute("seg_length", d.seg_length)
            features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer
