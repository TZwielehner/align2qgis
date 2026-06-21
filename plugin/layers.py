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

from qgis.PyQt.QtCore import QMetaType
from qgis.core import (
    QgsCircularString,
    QgsCompoundCurve,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsLineString,
    QgsMultiLineString,
    QgsPoint,
    QgsPointXY,
    QgsVectorLayer,
)

from .constants import (
    FIELD_ALIGNMENT,
    FIELD_CURVATURE_END,
    FIELD_CURVATURE_START,
    FIELD_ELEVATION,
    FIELD_KIND,
    FIELD_RADIUS_START,
    FIELD_STA_END,
    FIELD_STA_START,
    FIELD_STATION,
    FIELD_VC_LENGTH,
    PROP_CRS,
    PROP_SOURCE_PATH,
    SOURCE_FILE_FIELD,
)
from .dimensions import build_dimensions
from .geometry_builder import (
    ArcPiece,
    LinePiece,
    alignment_chainage,
    alignment_curve_pieces,
    alignment_curve_pieces_3d,
    alignment_pose_at_station,
    alignment_xy_at_station,
    internal_to_display,
    segment_curvature,
    segment_curve_pieces,
    segment_length,
)
from .landxml_parser import (
    CgPointRecord,
    CrossSectionSurface,
    CurveSeg,
    LandXMLMetadata,
    SpiralSeg,
    VertCurve,
    profile_elevation_at_station,
)
from .stationing import format_station, upright_bearing

import math


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


def _point_wkt(is_3d: bool) -> str:
    """WKT geometry token for a point layer, Z-promoted when the batch is 3D."""
    return "PointZ" if is_3d else "Point"


def _curve_wkt(is_3d: bool) -> str:
    """WKT geometry token for a compound-curve layer, Z-promoted when 3D."""
    return "CompoundCurveZ" if is_3d else "CompoundCurve"


def _compound_curve_from_pieces(pieces) -> QgsCompoundCurve:
    """Build a QgsCompoundCurve from a list of LinePiece / ArcPiece values.

    Lines become two-point ``QgsLineString`` sub-curves; arcs become
    three-point ``QgsCircularString`` sub-curves. QGIS stores this as native
    curved geometry — offsets, buffers, and length() use the analytic
    formulas rather than the chord polyline.
    """
    cc = QgsCompoundCurve()
    for piece in pieces:
        if isinstance(piece, LinePiece):
            cc.addCurve(QgsLineString([
                QgsPoint(*piece.start), QgsPoint(*piece.end),
            ]))
        elif isinstance(piece, ArcPiece):
            cc.addCurve(QgsCircularString(
                QgsPoint(*piece.start),
                QgsPoint(*piece.mid),
                QgsPoint(*piece.end),
            ))
    return cc


def _compound_curve_from_pieces_with_z(
    pieces_with_z: list[tuple[object, list[float]]],
) -> QgsCompoundCurve:
    """3D counterpart of :func:`_compound_curve_from_pieces`.

    Each entry pairs a curve piece with a per-vertex Z list (2 entries for
    a LinePiece, 3 for an ArcPiece). Z is taken verbatim — callers fall
    back to 0.0 when no profile elevation is available.
    """
    cc = QgsCompoundCurve()
    for piece, zs in pieces_with_z:
        if isinstance(piece, LinePiece):
            cc.addCurve(QgsLineString([
                QgsPoint(piece.start[0], piece.start[1], zs[0]),
                QgsPoint(piece.end[0], piece.end[1], zs[1]),
            ]))
        elif isinstance(piece, ArcPiece):
            cc.addCurve(QgsCircularString(
                QgsPoint(piece.start[0], piece.start[1], zs[0]),
                QgsPoint(piece.mid[0], piece.mid[1], zs[1]),
                QgsPoint(piece.end[0], piece.end[1], zs[2]),
            ))
    return cc


def _elev_at_internal(alignment, s_internal: float) -> float:
    """Look up profile elevation at an alignment-internal station.

    Converts through the equation map (display↔internal) and returns 0.0
    when the alignment has no profile or the station is outside the
    profile's range — the layer-level Z=0 fallback documented at each
    builder.
    """
    profile = getattr(alignment, "profile", None)
    if profile is None:
        return 0.0
    s_display = internal_to_display(alignment, s_internal)
    z = profile_elevation_at_station(profile, s_display)
    return 0.0 if z is None else float(z)


def _pieces_with_z(alignment, max_chord_err: float = 0.01):
    """``alignment_curve_pieces_3d`` results with Z resolved per vertex."""
    out = []
    for piece, stations in alignment_curve_pieces_3d(alignment, max_chord_err):
        zs = [_elev_at_internal(alignment, s) for s in stations]
        out.append((piece, zs))
    return out


def _batch_is_3d(alignments) -> bool:
    """A layer goes 3D when *any* alignment in the batch carries a profile.

    QGIS layers carry a single geometry type, so we can't mix 2D and 3D
    features in one layer — when at least one alignment has elevations we
    promote the whole layer and give profileless alignments a Z=0 fallback.
    """
    return any(getattr(a, "profile", None) is not None for a in alignments)


def _alignments_by_name(alignments) -> dict[str, object]:
    """Return ``{alignment.name: alignment}`` so per-station lookups can
    avoid linear scans across the alignment list."""
    return {a.name: a for a in alignments}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def build_alignment_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
    source_path: str = "",
    imported_at: str = "",
    metadata: LandXMLMetadata | None = None,
) -> QgsVectorLayer:
    """One CompoundCurve feature per alignment — the rendered horizontal curve.

    Lines stay as line segments and arcs/spirals are emitted as native
    circular-arc sub-curves so QGIS renders, offsets, and measures them
    analytically rather than as chord polylines. When *any* alignment in
    the batch has a ``<Profile>``, the layer is promoted to
    ``CompoundCurveZ`` and Z is sampled from the profile at every vertex;
    profile-less alignments get a Z=0 fallback so the layer's geometry
    type stays uniform.
    """
    is_3d = _batch_is_3d(alignments)
    layer = _new_memory_layer(
        layer_name, crs_authid,
        _curve_wkt(is_3d),
        alignment_fields(),
    )
    meta = metadata or LandXMLMetadata()

    features: list[QgsFeature] = []
    for alignment in alignments:
        if is_3d:
            pieces_with_z = _pieces_with_z(alignment)
            if not pieces_with_z:
                continue
            cc = _compound_curve_from_pieces_with_z(pieces_with_z)
        else:
            pieces = alignment_curve_pieces(alignment)
            if not pieces:
                continue
            cc = _compound_curve_from_pieces(pieces)
        if cc.nCurves() == 0:
            continue
        # ``QgsGeometry(cc)`` would transfer ownership of ``cc``; the
        # ``.clone()`` keeps that transfer unambiguous under QGIS 4's SIP
        # bindings without affecting QGIS 3 behaviour.
        geom = QgsGeometry(cc.clone())
        feat = QgsFeature(layer.fields())
        feat.setGeometry(geom)
        feat.setAttribute("name", alignment.name)
        feat.setAttribute("length_xml", alignment.length)
        feat.setAttribute("length_geom", geom.length())
        feat.setAttribute(FIELD_STA_START, alignment.sta_start)
        feat.setAttribute("n_segments", len(alignment.segments))
        feat.setAttribute(SOURCE_FILE_FIELD, source_file)
        feat.setAttribute("source_path", source_path)
        feat.setAttribute("imported_at", imported_at)
        feat.setAttribute("landxml_version", meta.landxml_version)
        feat.setAttribute("project_name", meta.project_name)
        feat.setAttribute("track_number", alignment.track_number)
        features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def alignment_fields() -> list[QgsField]:
    return [
        QgsField("name", QMetaType.Type.QString),
        QgsField("length_xml", QMetaType.Type.Double),
        QgsField("length_geom", QMetaType.Type.Double),
        QgsField(FIELD_STA_START, QMetaType.Type.Double),
        QgsField("n_segments", QMetaType.Type.Int),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
        QgsField("source_path", QMetaType.Type.QString),
        QgsField("imported_at", QMetaType.Type.QString),
        QgsField("landxml_version", QMetaType.Type.QString),
        QgsField("project_name", QMetaType.Type.QString),
        QgsField("track_number", QMetaType.Type.QString),
    ]


def segment_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField("seg_index", QMetaType.Type.Int),
        QgsField(FIELD_KIND, QMetaType.Type.QString),
        QgsField("transition_type", QMetaType.Type.QString),
        QgsField("length", QMetaType.Type.Double),
        QgsField(FIELD_RADIUS_START, QMetaType.Type.Double),
        QgsField("radius_end", QMetaType.Type.Double),
        QgsField(FIELD_CURVATURE_START, QMetaType.Type.Double),
        QgsField(FIELD_CURVATURE_END, QMetaType.Type.Double),
        QgsField("spiral_a", QMetaType.Type.Double),
        QgsField(FIELD_STA_START, QMetaType.Type.Double),
        QgsField(FIELD_STA_END, QMetaType.Type.Double),
        QgsField("rot", QMetaType.Type.QString),
        QgsField("desc", QMetaType.Type.QString),
        QgsField("status", QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
    ]


def build_segment_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
) -> QgsVectorLayer:
    """One CompoundCurve feature per LandXML segment (Line / Curve / Spiral).

    Circular arcs are emitted exactly; clothoid spirals are discretized into
    a chain of circular arcs whose chord deviation from the true clothoid
    stays under a centimeter for typical road geometry. Promoted to
    ``CompoundCurveZ`` when any alignment in the batch carries a profile;
    Z is sampled per vertex, Z=0 for profile-less alignments.
    """
    is_3d = _batch_is_3d(alignments)
    layer = _new_memory_layer(
        layer_name, crs_authid,
        _curve_wkt(is_3d),
        segment_fields(),
    )

    features: list[QgsFeature] = []
    for alignment in alignments:
        sta = alignment.sta_start or 0.0
        # Bucket the alignment-wide 3D pieces back to their source segments
        # so each segment feature gets its own contiguous slice; reuses the
        # canonical station math in :func:`alignment_curve_pieces_3d`
        # rather than re-deriving it inline.
        pieces_with_z_by_seg: dict[int, list[tuple[object, list[float]]]] = {}
        if is_3d:
            all_pieces = alignment_curve_pieces_3d(alignment)
            seg_arclengths = [segment_length(s) for s in alignment.segments]
            seg_ends = []
            acc = sta
            for L in seg_arclengths:
                acc += L
                seg_ends.append(acc)
            seg_idx = 0
            for piece, stations in all_pieces:
                # Advance to the segment whose end station covers this piece's end.
                piece_end = stations[-1]
                while seg_idx < len(seg_ends) - 1 and piece_end > seg_ends[seg_idx] + 1e-6:
                    seg_idx += 1
                pieces_with_z_by_seg.setdefault(seg_idx, []).append(
                    (piece, [_elev_at_internal(alignment, s) for s in stations])
                )
        for idx, seg in enumerate(alignment.segments):
            length = segment_length(seg)
            if length <= 0:
                continue
            if is_3d:
                pwz = pieces_with_z_by_seg.get(idx, [])
                if not pwz:
                    continue
                cc = _compound_curve_from_pieces_with_z(pwz)
            else:
                pieces = segment_curve_pieces(seg)
                if not pieces:
                    continue
                cc = _compound_curve_from_pieces(pieces)
            if cc.nCurves() == 0:
                continue
            geom = QgsGeometry(cc.clone())

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
            feat.setAttribute(FIELD_ALIGNMENT, alignment.name)
            feat.setAttribute("seg_index", idx)
            feat.setAttribute(FIELD_KIND, seg.kind)
            feat.setAttribute("transition_type", transition_type)
            feat.setAttribute("length", length)
            feat.setAttribute(FIELD_RADIUS_START, radius_start)
            feat.setAttribute("radius_end", radius_end)
            feat.setAttribute(FIELD_CURVATURE_START, k_start)
            feat.setAttribute(FIELD_CURVATURE_END, k_end)
            feat.setAttribute("spiral_a", spiral_a)
            feat.setAttribute(FIELD_STA_START, sta)
            feat.setAttribute(FIELD_STA_END, sta + length)
            feat.setAttribute("rot", rot)
            feat.setAttribute("desc", getattr(seg, "desc", None) or "")
            feat.setAttribute("status", getattr(seg, "status", "") or "")
            feat.setAttribute(SOURCE_FILE_FIELD, source_file)
            features.append(feat)
            sta += length

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def stations_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField(FIELD_STATION, QMetaType.Type.Double),
        QgsField("station_internal", QMetaType.Type.Double),
        QgsField("label", QMetaType.Type.QString),
        QgsField("rotation", QMetaType.Type.Double),
        QgsField("rotation_perp", QMetaType.Type.Double),
        QgsField("seg_index", QMetaType.Type.Int),
        QgsField("seg_kind", QMetaType.Type.QString),
        QgsField("transition_type", QMetaType.Type.QString),
        QgsField("curvature", QMetaType.Type.Double),
        QgsField("radius", QMetaType.Type.Double),
        QgsField("desc", QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
    ]


def build_stations_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    perpendicular: bool = False,
    source_file: str = "",
) -> QgsVectorLayer:
    """Defining stations only — alignment start + every segment endpoint.

    Annotation stations at fixed intervals belong on the Alignments layer's
    label engine (see :func:`build_chainage_label_layer` for the auxiliary
    in-memory layer that backs them when symbol-level labeling is wanted).
    Promoted to ``PointZ`` when any alignment carries a profile so slope
    analyses against the alignment have a real Z column.
    """
    is_3d = _batch_is_3d(alignments)
    layer = _new_memory_layer(
        layer_name, crs_authid, _point_wkt(is_3d), stations_fields(),
    )

    label_offset = 90.0 if perpendicular else 0.0
    features: list[QgsFeature] = []
    for alignment in alignments:
        # interval=0 emits only segment endpoints + alignment start.
        stations = alignment_chainage(alignment, 0.0, include_endpoints=True)
        if not stations:
            continue
        for cp in stations:
            feat = QgsFeature(layer.fields())
            if is_3d:
                z = _elev_at_internal(alignment, cp.station_internal)
                feat.setGeometry(QgsGeometry(QgsPoint(cp.x, cp.y, z)))
            else:
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cp.x, cp.y)))
            feat.setAttribute(FIELD_ALIGNMENT, alignment.name)
            feat.setAttribute(FIELD_STATION, cp.station)
            feat.setAttribute("station_internal", cp.station_internal)
            feat.setAttribute("label", format_station(cp.station))
            # QGIS rotation is CW in screen space; our bearing is math CCW
            # in map space (y-up). Negate so labels/markers align with the
            # actual tangent direction.
            feat.setAttribute(
                "rotation", upright_bearing(-(cp.bearing_deg + label_offset))
            )
            # Marker rotation in QGIS is CW from "up" (north); a Line marker
            # at rotation=-bearing already draws perpendicular to a math-CCW
            # tangent. The earlier "+90" double-rotated and produced a tangent.
            feat.setAttribute(
                "rotation_perp", upright_bearing(-cp.bearing_deg)
            )
            feat.setAttribute("seg_index", cp.seg_index)
            feat.setAttribute("seg_kind", cp.seg_kind)
            feat.setAttribute("transition_type", cp.transition_type)
            feat.setAttribute("curvature", cp.curvature)
            feat.setAttribute("radius", cp.radius)
            feat.setAttribute("desc", cp.desc)
            feat.setAttribute(SOURCE_FILE_FIELD, source_file)
            features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def build_chainage_label_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    interval: float,
    *,
    perpendicular: bool = False,
) -> QgsVectorLayer:
    """In-memory-only points every ``interval`` metres along each alignment.

    Carries the same schema as the Stations layer so the existing station
    label/tick styling applies unchanged. Not written to the GeoPackage —
    rebuilt on every import alongside the persisted Alignments table.
    Promoted to ``PointZ`` when any alignment carries a profile.
    """
    is_3d = _batch_is_3d(alignments)
    layer = _new_memory_layer(
        layer_name, crs_authid, _point_wkt(is_3d), stations_fields(),
    )

    label_offset = 90.0 if perpendicular else 0.0
    features: list[QgsFeature] = []
    for alignment in alignments:
        stations = alignment_chainage(alignment, interval, include_endpoints=False)
        for cp in stations:
            feat = QgsFeature(layer.fields())
            if is_3d:
                z = _elev_at_internal(alignment, cp.station_internal)
                feat.setGeometry(QgsGeometry(QgsPoint(cp.x, cp.y, z)))
            else:
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(cp.x, cp.y)))
            feat.setAttribute(FIELD_ALIGNMENT, alignment.name)
            feat.setAttribute(FIELD_STATION, cp.station)
            feat.setAttribute("station_internal", cp.station_internal)
            feat.setAttribute("label", format_station(cp.station))
            feat.setAttribute(
                "rotation", upright_bearing(-(cp.bearing_deg + label_offset))
            )
            # Marker rotation in QGIS is CW from "up" (north); a Line marker
            # at rotation=-bearing already draws perpendicular to a math-CCW
            # tangent. The earlier "+90" double-rotated and produced a tangent.
            feat.setAttribute(
                "rotation_perp", upright_bearing(-cp.bearing_deg)
            )
            feat.setAttribute("seg_index", cp.seg_index)
            feat.setAttribute("seg_kind", cp.seg_kind)
            feat.setAttribute("transition_type", cp.transition_type)
            feat.setAttribute("curvature", cp.curvature)
            feat.setAttribute("radius", cp.radius)
            feat.setAttribute("desc", cp.desc)
            features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


# Back-compat alias — old callers and tests still reach for build_chainage_layer.
build_chainage_layer = build_chainage_label_layer


def dimension_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField("seg_index", QMetaType.Type.Int),
        QgsField(FIELD_KIND, QMetaType.Type.QString),
        QgsField("label", QMetaType.Type.QString),
        QgsField("rotation", QMetaType.Type.Double),
        QgsField("radius", QMetaType.Type.Double),
        QgsField("spiral_a", QMetaType.Type.Double),
        QgsField("seg_length", QMetaType.Type.Double),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
    ]


def build_dimension_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    arcs: bool,
    spirals: bool,
    tangents: bool,
    perpendicular: bool = False,
    source_file: str = "",
) -> QgsVectorLayer:
    """One point feature per dimensionable segment (R=… arcs, A=… spirals)."""
    layer = _new_memory_layer(layer_name, crs_authid, "Point", dimension_fields())

    label_offset = 90.0 if perpendicular else 0.0
    features: list[QgsFeature] = []
    for alignment in alignments:
        for d in build_dimensions(
            alignment, arcs=arcs, spirals=spirals, tangents=tangents
        ):
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(d.x, d.y)))
            feat.setAttribute(FIELD_ALIGNMENT, d.alignment)
            feat.setAttribute("seg_index", d.seg_index)
            feat.setAttribute(FIELD_KIND, d.seg_kind)
            feat.setAttribute("label", d.label)
            feat.setAttribute(
                "rotation", upright_bearing(-(d.bearing_deg + label_offset))
            )
            feat.setAttribute("radius", d.radius)
            feat.setAttribute("spiral_a", d.spiral_a)
            feat.setAttribute("seg_length", d.seg_length)
            feat.setAttribute(SOURCE_FILE_FIELD, source_file)
            features.append(feat)

    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def vertical_profile_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
        QgsField(FIELD_STATION, QMetaType.Type.Double),
        QgsField(FIELD_ELEVATION, QMetaType.Type.Double),
        QgsField(FIELD_VC_LENGTH, QMetaType.Type.Double),
        QgsField(FIELD_KIND, QMetaType.Type.QString),
    ]


def build_vertical_profile_layer(
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
) -> QgsVectorLayer:
    """One point feature per PVI / VertCurve vertex in each alignment's profile.

    Geometry = plan-XY at the PVI station. The dock re-densifies (station,
    elevation) on the fly via ``profile_samples``.
    """
    layer = _new_memory_layer(
        layer_name, crs_authid, "Point", vertical_profile_fields(),
    )
    features: list[QgsFeature] = []
    for alignment in alignments:
        if alignment.profile is None:
            continue
        for prof_align in alignment.profile.alignments:
            for item in prof_align.elements:
                xy = alignment_xy_at_station(alignment, item.station)
                if xy is None:
                    continue
                feat = QgsFeature(layer.fields())
                feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*xy)))
                feat.setAttribute(FIELD_ALIGNMENT, alignment.name)
                feat.setAttribute(SOURCE_FILE_FIELD, source_file)
                feat.setAttribute(FIELD_STATION, item.station)
                feat.setAttribute(FIELD_ELEVATION, item.elev)
                vc_len = getattr(item, "length", 0.0) if isinstance(item, VertCurve) else 0.0
                feat.setAttribute(FIELD_VC_LENGTH, float(vc_len or 0.0))
                feat.setAttribute(FIELD_KIND, item.kind)
                features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def cross_sections_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
        QgsField(FIELD_STATION, QMetaType.Type.Double),
        QgsField("offset", QMetaType.Type.Double),
        QgsField(FIELD_ELEVATION, QMetaType.Type.Double),
    ]


def build_cross_sections_layer(
    cross_sections,
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
) -> QgsVectorLayer:
    """One point per cross-section sample. Created empty when none in file.

    Promoted to ``PointZ`` when any alignment carries a profile — the
    sample's own ``elevation`` already provides a real Z so the geometry's
    Z matches the attribute exactly.
    """
    is_3d = _batch_is_3d(alignments)
    layer = _new_memory_layer(
        layer_name, crs_authid,
        _point_wkt(is_3d),
        cross_sections_fields(),
    )
    by_name = _alignments_by_name(alignments)
    features: list[QgsFeature] = []
    for cs in cross_sections:
        alignment = by_name.get(cs.alignment_name)
        if alignment is None:
            continue
        xy = alignment_xy_at_station(alignment, cs.station)
        if xy is None:
            continue
        feat = QgsFeature(layer.fields())
        if is_3d:
            feat.setGeometry(QgsGeometry(QgsPoint(xy[0], xy[1], float(cs.elevation))))
        else:
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(*xy)))
        feat.setAttribute(FIELD_ALIGNMENT, cs.alignment_name)
        feat.setAttribute(SOURCE_FILE_FIELD, source_file)
        feat.setAttribute(FIELD_STATION, cs.station)
        feat.setAttribute("offset", cs.offset)
        feat.setAttribute(FIELD_ELEVATION, cs.elevation)
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def cg_point_fields() -> list[QgsField]:
    return [
        QgsField("name", QMetaType.Type.QString),
        QgsField("code", QMetaType.Type.QString),
        QgsField("elev", QMetaType.Type.Double),
        QgsField("group_name", QMetaType.Type.QString),
        QgsField("group_desc", QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
    ]


def build_cg_points_layer(
    points: list[CgPointRecord],
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
) -> QgsVectorLayer:
    """One point feature per ``<CgPoint>`` (named survey / control point).

    Promoted to ``PointZ`` whenever any record carries an elevation; missing
    elevations fall back to Z=0 so the layer's geometry type stays uniform.
    Coordinates are expected to already be in ``crs_authid`` — the plugin
    reprojects from the source CRS before calling this builder, matching how
    alignment segments are handled.
    """
    is_3d = any(p.elev is not None for p in points)
    layer = _new_memory_layer(
        layer_name, crs_authid,
        _point_wkt(is_3d),
        cg_point_fields(),
    )
    features: list[QgsFeature] = []
    for p in points:
        feat = QgsFeature(layer.fields())
        # LandXML stores (N, E); QGIS map XY is (E, N).
        if is_3d:
            z = float(p.elev) if p.elev is not None else 0.0
            feat.setGeometry(QgsGeometry(QgsPoint(p.east, p.north, z)))
        else:
            feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(p.east, p.north)))
        feat.setAttribute("name", p.name)
        feat.setAttribute("code", p.code)
        feat.setAttribute("elev", float(p.elev) if p.elev is not None else None)
        feat.setAttribute("group_name", p.group_name)
        feat.setAttribute("group_desc", p.group_desc)
        feat.setAttribute(SOURCE_FILE_FIELD, source_file)
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def cross_section_surface_fields() -> list[QgsField]:
    return [
        QgsField(FIELD_ALIGNMENT, QMetaType.Type.QString),
        QgsField(FIELD_STATION, QMetaType.Type.Double),
        QgsField("surf_name", QMetaType.Type.QString),
        QgsField("desc", QMetaType.Type.QString),
        QgsField(SOURCE_FILE_FIELD, QMetaType.Type.QString),
    ]


def build_cross_section_surfaces_layer(
    surfaces: list[CrossSectionSurface],
    alignments,
    layer_name: str,
    crs_authid: str,
    *,
    source_file: str = "",
) -> QgsVectorLayer:
    """Multi-part LineStringZ layer — one feature per ``<CrossSectSurf>``.

    The (offset, elevation) pairs from each ``<PntList2D>`` are projected
    onto the alignment's right-perpendicular at the cross-section's station,
    so the surface plots across the alignment at the right place in map
    space. Offset sign follows the LandXML convention (right-positive in
    direction of stationing); the alignment's bearing at the station
    controls the perpendicular's orientation.
    """
    layer = _new_memory_layer(
        layer_name, crs_authid, "MultiLineStringZ", cross_section_surface_fields(),
    )
    by_name = _alignments_by_name(alignments)
    features: list[QgsFeature] = []
    for surf in surfaces:
        alignment = by_name.get(surf.alignment_name)
        if alignment is None:
            continue
        pose = alignment_pose_at_station(alignment, surf.station)
        if pose is None:
            continue
        cx, cy, bearing_deg = pose
        b = math.radians(bearing_deg)
        rx, ry = math.sin(b), -math.cos(b)

        mls = QgsMultiLineString()
        added = 0
        for run in surf.parts:
            pts = [
                QgsPoint(cx + off * rx, cy + off * ry, elev)
                for off, elev in run
            ]
            if len(pts) < 2:
                continue
            mls.addGeometry(QgsLineString(pts))
            added += 1
        if added == 0:
            continue
        feat = QgsFeature(layer.fields())
        feat.setGeometry(QgsGeometry(mls.clone()))
        feat.setAttribute(FIELD_ALIGNMENT, surf.alignment_name)
        feat.setAttribute(FIELD_STATION, surf.station)
        feat.setAttribute("surf_name", surf.name)
        feat.setAttribute("desc", surf.desc)
        feat.setAttribute(SOURCE_FILE_FIELD, source_file)
        features.append(feat)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer
