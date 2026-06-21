"""Processing algorithm: project points onto an alignment and emit (station, offset)."""
from __future__ import annotations


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
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterNumber,
    QgsWkbTypes,
)
from qgis.PyQt.QtCore import QCoreApplication, QMetaType

from ..geometry_builder import alignment_project_point
from ..alignment_cache import alignments_for_layer


class StationFromPointAlgorithm(QgsProcessingAlgorithm):
    """Project each input point onto the closest alignment.

    Emits a point feature at the projected foot, carrying the
    displayed station, signed offset (left-positive), and the matched
    alignment name. Inputs whose nearest projection exceeds
    ``MAX_OFFSET`` are dropped with a warning.
    """

    INPUT = "INPUT"
    ALIGNMENT = "ALIGNMENT"
    MAX_OFFSET = "MAX_OFFSET"
    NEAREST_ONLY = "NEAREST_ONLY"
    OUTPUT = "OUTPUT"

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("Align2QGIS", text)

    def createInstance(self):  # noqa: N802
        return StationFromPointAlgorithm()

    def name(self) -> str:
        return "stationfrompoint"

    def displayName(self) -> str:  # noqa: N802
        return self.tr("Station from point")

    def group(self) -> str:
        return self.tr("Alignment")

    def groupId(self) -> str:  # noqa: N802
        return "alignment"

    def shortHelpString(self) -> str:  # noqa: N802
        return self.tr(
            "Project each input point onto the closest LandXML alignment and "
            "emit the (station, offset) along the alignment. The offset is "
            "signed left-positive of the chainage-increasing direction. "
            "Requires an Alignments layer created by Align2QGIS (carries the "
            "source LandXML path needed for spiral-aware projection)."
        )

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.INPUT, self.tr("Input points"), [QgsProcessing.TypeVectorPoint],
        ))
        self.addParameter(QgsProcessingParameterFeatureSource(
            self.ALIGNMENT, self.tr("Alignments layer"),
            [QgsProcessing.TypeVectorLine, QgsProcessing.TypeVectorAnyGeometry],
        ))
        self.addParameter(QgsProcessingParameterNumber(
            self.MAX_OFFSET, self.tr("Maximum offset (m, 0 = unlimited)"),
            type=QgsProcessingParameterNumber.Double,
            defaultValue=0.0, minValue=0.0,
        ))
        self.addParameter(QgsProcessingParameterBoolean(
            self.NEAREST_ONLY, self.tr("Keep only the nearest alignment per point"),
            defaultValue=True,
        ))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, self.tr("Projected points"),
        ))

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        src = self.parameterAsSource(parameters, self.INPUT, context)
        if src is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.INPUT))
        align_layer = self.parameterAsVectorLayer(parameters, self.ALIGNMENT, context)
        if align_layer is None:
            raise QgsProcessingException(self.invalidSourceError(parameters, self.ALIGNMENT))
        max_offset = self.parameterAsDouble(parameters, self.MAX_OFFSET, context)
        nearest_only = self.parameterAsBoolean(parameters, self.NEAREST_ONLY, context)

        alignments = alignments_for_layer(align_layer)
        if not alignments:
            raise QgsProcessingException(self.tr(
                "Could not load the LandXML behind the alignments layer. "
                "Make sure the layer was created by Align2QGIS."
            ))

        out_fields = QgsFields(src.fields())
        out_fields.append(QgsField("alignment_name", QMetaType.Type.QString))
        out_fields.append(QgsField("station", QMetaType.Type.Double))
        out_fields.append(QgsField("offset_signed", QMetaType.Type.Double))
        out_fields.append(QgsField("side", QMetaType.Type.QString))
        out_fields.append(QgsField("residual", QMetaType.Type.Double))

        is_3d = QgsWkbTypes.hasZ(align_layer.wkbType())
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
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            p = geom.asPoint()
            results = []
            for alignment in alignments:
                proj = alignment_project_point(alignment, p.x(), p.y())
                if proj is None:
                    continue
                if max_offset > 0 and abs(proj.offset_signed) > max_offset:
                    continue
                results.append((alignment.name, proj))
            if not results:
                continue
            if nearest_only:
                results.sort(key=lambda r: r[1].residual)
                results = results[:1]
            for name, proj in results:
                new_feat = QgsFeature(out_fields)
                # Carry input attributes + projection fields.
                in_attrs = list(feat.attributes())
                new_feat.setAttributes(in_attrs + [
                    name,
                    proj.station_display,
                    proj.offset_signed,
                    "L" if proj.offset_signed >= 0 else "R",
                    proj.residual,
                ])
                if is_3d:
                    new_feat.setGeometry(QgsGeometry(QgsPoint(proj.foot_x, proj.foot_y, 0.0)))
                else:
                    new_feat.setGeometry(QgsGeometry.fromPointXY(
                        QgsPointXY(proj.foot_x, proj.foot_y),
                    ))
                sink.addFeature(new_feat)

        return {self.OUTPUT: dest_id}
