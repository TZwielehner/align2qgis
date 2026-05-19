"""Align2QGIS Processing provider registration."""
from __future__ import annotations

import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .point_from_station import PointFromStationOffsetAlgorithm
from .station_from_point import StationFromPointAlgorithm


class Align2QgisProvider(QgsProcessingProvider):
    """Registers the plugin's algorithms in the QGIS Processing Toolbox.

    Shows up as a top-level "Align2QGIS" group with two children:
    *Station from point* and *Point from station and offset*.
    """

    def id(self) -> str:
        return "align2qgis"

    def name(self) -> str:
        return "Align2QGIS"

    def icon(self) -> QIcon:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icon.png")
        return QIcon(path) if os.path.exists(path) else QIcon()

    def loadAlgorithms(self) -> None:  # noqa: N802
        self.addAlgorithm(StationFromPointAlgorithm())
        self.addAlgorithm(PointFromStationOffsetAlgorithm())
