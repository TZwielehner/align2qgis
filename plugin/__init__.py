"""QGIS plugin entry point. QGIS calls ``classFactory`` to instantiate the plugin."""
from __future__ import annotations


def classFactory(iface):  # noqa: N802 — name required by QGIS
    from .align2qgis_plugin import Align2QgisPlugin

    return Align2QgisPlugin(iface)
