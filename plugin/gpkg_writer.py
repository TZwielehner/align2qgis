"""Persist memory layers into a GeoPackage with append semantics.

Each call writes the four canonical tables (Alignments / Segments /
Stations / Dimensions). If a table already exists in the file, rows whose
``source_file`` column equals the current import's basename are deleted
first, then the new rows are appended — so re-importing the same LandXML
replaces just its contribution and leaves rows from other imports alone.

Schema migration: old per-file tables (``align_<stem>`` etc.) from earlier
plugin versions are left intact. A warning is printed to the QGIS Python
console; the user is expected to clean them up manually if desired.
"""
from __future__ import annotations

import os

from qgis.core import (
    QgsFeature,
    QgsProject,
    QgsVectorFileWriter,
    QgsVectorLayer,
)

from .constants import SOURCE_FILE_FIELD

_LEGACY_PREFIXES = ("align_", "segments_", "stations_", "dims_")


def write_layers_to_gpkg(
    layers_with_names: list[tuple[QgsVectorLayer, str]],
    gpkg_path: str,
    *,
    source_file: str = "",
) -> list[tuple[str, str, str]]:
    """Write each ``(layer, gpkg_layer_name)`` into ``gpkg_path``.

    For each canonical layer name: if the GPKG table doesn't exist, create
    it from the layer schema; if it does, delete rows with the matching
    ``source_file`` value, then append the new rows.

    Returns ``[(gpkg_path, gpkg_layer_name, display_name), …]`` so callers
    can re-load the persisted layers as GPKG-backed vectors.
    """
    # Treat empty placeholder files (size 0) as new — QGIS' "Save As" dialog
    # often touches the path before the writer runs, leaving a 0-byte file.
    is_new_file = (
        not os.path.exists(gpkg_path) or os.path.getsize(gpkg_path) == 0
    )
    if not is_new_file:
        _warn_on_legacy_tables(gpkg_path)
    written: list[tuple[str, str, str]] = []

    for layer, name in layers_with_names:
        table_exists = (not is_new_file) and _table_exists(gpkg_path, name)
        if not table_exists:
            _create_table(layer, name, gpkg_path, create_file=is_new_file)
            is_new_file = False
        else:
            if source_file:
                _delete_by_source_file(gpkg_path, name, source_file)
            _append_features(layer, gpkg_path, name)
        written.append((gpkg_path, name, layer.name()))
    return written


def _table_exists(gpkg_path: str, name: str) -> bool:
    probe = QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
    return probe.isValid()


def _create_table(
    layer: QgsVectorLayer, name: str, gpkg_path: str, *, create_file: bool,
) -> None:
    transform_context = QgsProject.instance().transformContext()
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = name
    opts.fileEncoding = "UTF-8"
    opts.actionOnExistingFile = (
        QgsVectorFileWriter.CreateOrOverwriteFile
        if create_file
        else QgsVectorFileWriter.CreateOrOverwriteLayer
    )
    err, msg = QgsVectorFileWriter.writeAsVectorFormatV2(
        layer, gpkg_path, transform_context, opts
    )
    if err != QgsVectorFileWriter.NoError:
        raise IOError(f"GPKG write failed for layer '{name}': {msg}")


def _delete_by_source_file(gpkg_path: str, name: str, source_file: str) -> None:
    layer = QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
    if not layer.isValid():
        return
    idx = layer.fields().indexOf(SOURCE_FILE_FIELD)
    if idx < 0:
        return
    victims = [
        f.id() for f in layer.getFeatures()
        if f.attribute(idx) == source_file
    ]
    if not victims:
        return
    layer.startEditing()
    layer.dataProvider().deleteFeatures(victims)
    layer.commitChanges()


def _append_features(layer: QgsVectorLayer, gpkg_path: str, name: str) -> None:
    """Append ``layer``'s features into the existing GPKG table by matching
    field names. Fields in the source layer that don't exist in the target
    are dropped; missing target fields stay NULL.
    """
    target = QgsVectorLayer(f"{gpkg_path}|layername={name}", name, "ogr")
    if not target.isValid():
        raise IOError(f"GPKG layer '{name}' could not be reopened for append")
    target_fields = target.fields()
    src_fields = layer.fields()
    new_feats: list[QgsFeature] = []
    for sf in layer.getFeatures():
        nf = QgsFeature(target_fields)
        nf.setGeometry(sf.geometry())
        for i in range(target_fields.count()):
            fname = target_fields.at(i).name()
            src_idx = src_fields.indexOf(fname)
            if src_idx >= 0:
                nf.setAttribute(i, sf.attribute(src_idx))
        new_feats.append(nf)
    if not new_feats:
        return
    target.startEditing()
    target.dataProvider().addFeatures(new_feats)
    if not target.commitChanges():
        raise IOError(
            f"GPKG append failed for layer '{name}': "
            f"{'; '.join(target.commitErrors())}"
        )


def _warn_on_legacy_tables(gpkg_path: str) -> None:
    """Print a one-shot console warning if the GPKG contains old per-file
    tables. We never touch them — the user keeps full control over migration.
    """
    try:
        from osgeo import ogr  # type: ignore[import-not-found]
    except ImportError:
        return
    # ogr.Open raises RuntimeError on Windows for files that exist but
    # aren't a recognised driver (empty placeholder, wrong extension);
    # the warning is purely informational, so swallow and return.
    try:
        ds = ogr.Open(gpkg_path)
    except RuntimeError:
        return
    if ds is None:
        return
    legacy: list[str] = []
    for i in range(ds.GetLayerCount()):
        layer_name = ds.GetLayerByIndex(i).GetName()
        if any(layer_name.startswith(p) for p in _LEGACY_PREFIXES):
            legacy.append(layer_name)
    if legacy:
        print(
            "[align2qgis] GeoPackage contains legacy per-file tables "
            f"({', '.join(legacy)}); left untouched. Delete them manually "
            "if you no longer need them."
        )
