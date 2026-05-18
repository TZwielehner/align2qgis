"""Persist memory layers into a GeoPackage.

The first layer written to a new file uses ``CreateOrOverwriteFile``; every
later layer in the same call uses ``CreateOrOverwriteLayer`` so existing
layers in the file are preserved unless their name collides — in which
case the colliding layer is replaced, not duplicated. This matches the
behaviour the import dialog promises in its help text.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)


def write_layers_to_gpkg(
    layers_with_names: list[tuple[QgsVectorLayer, str]],
    gpkg_path: str,
) -> list[tuple[str, str, str]]:
    """Write each ``(layer, gpkg_layer_name)`` into ``gpkg_path``.

    Returns ``[(gpkg_path, gpkg_layer_name, display_name), …]`` so the caller
    can re-load the persisted layers as GPKG-backed vectors.
    """
    transform_context = QgsProject.instance().transformContext()
    is_new_file = not os.path.exists(gpkg_path)
    written: list[tuple[str, str, str]] = []
    for layer, name in layers_with_names:
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "GPKG"
        opts.layerName = name
        opts.fileEncoding = "UTF-8"
        opts.actionOnExistingFile = (
            QgsVectorFileWriter.CreateOrOverwriteFile
            if is_new_file
            else QgsVectorFileWriter.CreateOrOverwriteLayer
        )
        err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
            layer, gpkg_path, transform_context, opts
        )
        if err != QgsVectorFileWriter.NoError:
            raise IOError(f"GPKG write failed for layer '{name}': {msg}")
        written.append((gpkg_path, name, layer.name()))
        is_new_file = False
    return written
