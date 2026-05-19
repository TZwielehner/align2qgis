"""Layer styling helpers — labels + station tick marker.

Best-effort: each optional QGIS API call is wrapped in its own try so a
single enum that moved between major versions doesn't take the whole
styling step down. Failures print to the QGIS Python console but never
raise to the caller — the underlying layer still imports.
"""
from __future__ import annotations

from qgis.core import QgsVectorLayer


def apply_station_labels(layer: QgsVectorLayer) -> None:
    """Rotated text labels for the stations layer, offset above the tick."""
    # Larger offset than the default 1.5 mm so text clears the perpendicular
    # tick mark that :func:`apply_station_symbol` renders on the same layer.
    _apply_rotated_labels(
        layer, field="label", font_size=8, color=(40, 40, 40), y_offset=3.5
    )


def apply_dimension_labels(layer: QgsVectorLayer) -> None:
    """Bold red rotated labels for the dimensions (R=…, A=…) layer."""
    _apply_rotated_labels(
        layer, field="label", font_size=9, color=(120, 40, 40), bold=True, y_offset=2.0
    )


# Strategy: the plugin renders interval chainage labels off an auxiliary
# in-memory point layer (see build_chainage_label_layer) rather than driving
# them off the Alignments line itself. The line-only path below is kept for
# callers that want labels without the helper layer — repeat distance is in
# mm so 50 m on a metric CRS map becomes 50000 mm.
def apply_alignment_chainage_labels(layer: QgsVectorLayer, interval_m: float) -> None:
    """Rule-based labels along an Alignments line every ``interval_m`` metres.

    Uses ``QgsPalLayerSettings.Line`` placement with a repeat distance — the
    label text is the alignment name; QGIS handles the spacing. Running
    chainage values are not displayed here (no clean per-repeat distance
    expression in PAL); use ``build_chainage_label_layer`` + the existing
    station styling when you need "km 0+050" style labels.
    """
    try:
        from qgis.core import (
            QgsPalLayerSettings,
            QgsTextFormat,
            QgsVectorLayerSimpleLabeling,
        )
        from qgis.PyQt.QtGui import QColor, QFont
        from qgis.PyQt.QtWidgets import QApplication
    except Exception as exc:  # pragma: no cover
        print(f"[align2qgis] alignment chainage labels skipped: {exc}")
        return

    text = QgsTextFormat()
    font = QFont(QApplication.font())
    font.setPointSize(7)
    text.setFont(font)
    text.setColor(QColor(60, 60, 60))

    pal = QgsPalLayerSettings()
    pal.fieldName = "name"
    pal.enabled = True
    pal.setFormat(text)
    try:
        placement = getattr(QgsPalLayerSettings, "Placement", QgsPalLayerSettings)
        pal.placement = getattr(placement, "Line", QgsPalLayerSettings.Line)
    except (AttributeError, TypeError) as exc:
        print(f"[align2qgis] alignment label placement skipped: {exc}")
    # Repeat distance is in millimetres for screen-space PAL.
    pal.repeatDistance = max(1.0, interval_m) * 1000.0

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)


def apply_station_symbol(layer: QgsVectorLayer) -> None:
    """Render each chainage point as a short tick perpendicular to the
    alignment, driven by the layer's ``rotation_perp`` field.
    """
    try:
        from qgis.core import (
            QgsMarkerSymbol,
            QgsProperty,
            QgsSimpleMarkerSymbolLayer,
            QgsSymbolLayer,
        )
        from qgis.PyQt.QtGui import QColor

        marker = QgsSimpleMarkerSymbolLayer()
        try:
            marker.setShape(QgsSimpleMarkerSymbolLayer.Line)
        except AttributeError:
            from qgis.core import Qgis
            marker.setShape(Qgis.MarkerShape.Line)
        marker.setSize(3.5)  # mm — ~1.75 mm of tick on each side of the line
        marker.setColor(QColor(60, 60, 60))
        marker.setStrokeColor(QColor(60, 60, 60))
        marker.setStrokeWidth(0.3)

        marker.setDataDefinedProperty(
            QgsSymbolLayer.PropertyAngle,
            QgsProperty.fromField("rotation_perp"),
        )

        symbol = QgsMarkerSymbol()
        symbol.changeSymbolLayer(0, marker)
        layer.renderer().setSymbol(symbol)
        layer.triggerRepaint()
    except Exception as exc:  # pragma: no cover — styling failures aren't fatal
        print(f"[align2qgis] station marker styling skipped: {exc}")


def _apply_rotated_labels(
    layer: QgsVectorLayer,
    *,
    field: str,
    font_size: int,
    color: tuple[int, int, int],
    bold: bool = False,
    y_offset: float = 1.5,
) -> None:
    """Enable text labels rotated by the ``rotation`` field of ``layer``.

    Wraps each optional API call separately so a missing enum in one QGIS
    version doesn't take the whole labeling step down — an earlier
    revision wrapped the entire body in one ``try`` and a missing
    ``QgsPalLayerSettings.LabelRotation`` left the layer with no labels
    at all.
    """
    from qgis.core import (
        QgsPalLayerSettings,
        QgsProperty,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )
    from qgis.PyQt.QtGui import QColor, QFont
    from qgis.PyQt.QtWidgets import QApplication

    text = QgsTextFormat()
    # QFont() defaults to "MS Shell Dlg 2" on Qt 6, an alias QGIS 4 can't
    # render — use the app font (a real resolved family) instead.
    font = QFont(QApplication.font())
    font.setPointSize(font_size)
    font.setBold(bold)
    text.setFont(font)
    text.setColor(QColor(*color))

    pal = QgsPalLayerSettings()
    pal.fieldName = field
    pal.enabled = True
    pal.setFormat(text)

    try:
        placement = getattr(QgsPalLayerSettings, "Placement", QgsPalLayerSettings)
        pal.placement = getattr(placement, "OverPoint", QgsPalLayerSettings.OverPoint)
    except (AttributeError, TypeError) as exc:
        print(f"[align2qgis] label placement skipped: {exc}")
    try:
        pal.quadOffset = QgsPalLayerSettings.QuadrantAbove
    except AttributeError as exc:
        print(f"[align2qgis] label quadrant offset skipped: {exc}")
    pal.yOffset = y_offset

    try:
        rotation_prop = getattr(
            getattr(QgsPalLayerSettings, "Property", QgsPalLayerSettings),
            "LabelRotation",
            None,
        )
        if rotation_prop is None:
            rotation_prop = QgsPalLayerSettings.LabelRotation
        pal.dataDefinedProperties().setProperty(
            rotation_prop, QgsProperty.fromField("rotation")
        )
    except (AttributeError, TypeError) as exc:
        print(f"[align2qgis] label rotation binding skipped: {exc}")

    layer.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    layer.setLabelsEnabled(True)
