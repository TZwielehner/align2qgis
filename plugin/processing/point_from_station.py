"""Processing algorithm: place points at given (alignment, station, offset) rows."""
from __future__ import annotations

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from ..geometry_builder import (
    alignment_pose_at_station,
    display_to_internal,
    internal_to_display,
)
from ..landxml_parser import profile_elevation_at_station
from ..alignment_cache import alignments_by_name


class PointFromStationOffsetAlgorithm(QgsProcessingAlgorithm):
    """Resolve each input row to a map point via (alignment, station, offset).

    Inverse of :class:`StationFromPointAlgorithm`. Reads a table with
    ``alignment_name``, ``station``, and an optional signed ``offset``;
    produces a point feature at the corresponding map location. Z comes
    from the LandXML ``<Profile>`` when the source alignment carries one;
    offset is purely planar (cross-sections aren't applied to Z).
    """

    INPUT = "INPUT"
    ALIGNMENT = "ALIGNMENT"
    NAME_FIELD = "NAME_FIELD"
    STATION_FIELD = "STATION_FIELD"
    OFFSET_FIELD = "OFFSET_FIELD"
    OUTPUT = "OUTPUT"

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("Align2QGIS", text)

    def createInstance(self):  # noqa: N802
        return PointFromStationOffsetAlgorithm()

    def name(self) -> str:
        return "pointfromstationoffset"

    def displayName(self) -> str:  # noqa: N802
        return self.tr("Point from station and offset")

    def group(self) -> str:
        return self.tr("Alignment")

    def groupId(self) -> str:  # noqa: N802
        return "alignment"

    def shortHelpString(self) -> str:  # noqa: N802
        return self.tr(
            "Place a point at the (station, offset) location along a "
            "LandXML alignment for each row of an input table. Offset is "
            "signed left-positive of the chainage-increasing direction; "
            "Z is sampled from the LandXML <Profile> when available."
        )

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, self.tr("Input rows (alignment_name, station[, offset])"),
            [QgsProcessing.TypeVector],
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ALIGNMENT, self.tr("Alignments layer"),
            [QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorAnyGeometry],
        ))
        self.addParameter(QgsProcessingParameterField(
            self.NAME_FIELD, self.tr("Alignment name field"),
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.String,
            defaultValue="alignment_name", optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.STATION_FIELD, self.tr("Station field"),
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.Numeric,
            defaultValue="station", optional=True,
        ))
        self.addParameter(QgsProcessingParameterField(
            self.OFFSET_FIELD, self.tr("Offset field (signed, left-positive)"),
            parentLayerParameterName=self.INPUT,
            type=QgsProcessingParameterField.Numeric,
            defaultValue="offset", optional=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("Resolved points"),
        ))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        src = self.parameterAsSource(parameters, self.INPUT, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        align_layer = self.parameterAsVectorLayer(parameters, self.ALIGNMENT, context)
        if align_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.ALIGNMENT))
        name_field = self.parameterAsString(parameters, self.NAME_FIELD, context)
        station_field = self.parameterAsString(parameters, self.STATION_FIELD, context)
        offset_field = self.parameterAsString(parameters, self.OFFSET_FIELD, context)

        by_name = alignments_by_name(align_layer)
        if not by_name:
            raise QgsProcessingException(self.tr(
                "Could not load the LandXML behind the alignments layer. "
                "Make sure the layer was created by Align2QGIS."
            ))

        is_3d = QgsWkbTypes.hasZ(align_layer.wkbType())
        out_fields = QgsFields(src.fields())
        out_fields.append(QgsField("foot_x", QMetaType.Type.Double))
        out_fields.append(QgsField("foot_y", QMetaType.Type.Double))
        if is_3d:
            out_fields.append(QgsField("foot_z", QMetaType.Type.Double))

        wkb = QgsWkbTypes.PointZ if is_3d else QgsWkbTypes.Point
        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, out_fields, wkb,
            QgsCoordinateReferenceSystem(align_layer.crs()),
        )
        if sink is None:
            raise QgsProcessingException(self.invalidSinkError(parameters, self.OUTPUT))

        n = src.featureCount() or 0
        for i, feat in enumerate(src.getFeatures()):
            if feedback.isCanceled():
                break
            if n > 0:
                feedback.setProgress(int(100 * i / n))
            name = feat.attribute(name_field) if name_field else None
            station = feat.attribute(station_field) if station_field else None
            if name is None or station is None:
                continue
            try:
                station = float(station)
            except (TypeError, ValueError):
                feedback.pushWarning(self.tr(
                    f"Row {i}: station value '{station}' is not a number; skipped."
                ))
                continue
            offset = 0.0
            if offset_field:
                raw = feat.attribute(offset_field)
                if raw is not None:
                    try:
                        offset = float(raw)
                    except (TypeError, ValueError):
                        offset = 0.0
            alignment = by_name.get(str(name))
            if alignment is None:
                feedback.pushWarning(self.tr(
                    f"Row {i}: alignment '{name}' not found in the source LandXML; skipped."
                ))
                continue
            pose = alignment_pose_at_station(alignment, station)
            if pose is None:
                feedback.pushWarning(self.tr(
                    f"Row {i}: station {station} is outside the alignment "
                    f"(or in a station-equation gap); skipped."
                ))
                continue
            x, y, bearing_deg = pose
            b = math.radians(bearing_deg)
            # Left-perp offset.
            px = x + offset * (-math.sin(b))
            py = y + offset * math.cos(b)
            z = 0.0
            if is_3d:
                profile = alignment.profile
                internal = display_to_internal(alignment, station)
                if profile is not None and internal is not None:
                    sd = internal_to_display(alignment, internal)
                    elev = profile_elevation_at_station(profile, sd)
                    if elev is not None:
                        z = float(elev)

            out_feat = QgsFeature(out_fields)
            in_attrs = list(feat.attributes())
            if is_3d:
                out_feat.setAttributes(in_attrs + [px, py, z])
                out_feat.setGeometry(QgsGeometry(QgsPoint(px, py, z)))
            else:
                out_feat.setAttributes(in_attrs + [px, py])
                out_feat.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(px, py)))
            sink.addFeature(out_feat)

        return {self.OUTPUT: dest_id}
