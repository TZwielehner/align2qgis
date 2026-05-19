"""QGIS plugin orchestrator.

Wires the QGIS menu actions, the profile dock, and the layer-tree group
together. The actual work is delegated to the focused modules:

* :mod:`.layers`        — build the six output layers from parsed alignments.
* :mod:`.styling`       — label rotation, tick marker, dimension label colour.
* :mod:`.import_dialog` — single dialog that collects every option.
* :mod:`.gpkg_writer`   — persist memory layers into a GeoPackage.
* :mod:`.profile_dock`  — interactive curvature + vertical profile widget.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.PyQt.QtWidgets import (
    QDialog,
    QFileDialog,
    QMessageBox,
)
from qgis.core import (
    Qgis,
    QgsFeature,
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
)

from .constants import (
    ALIGNMENT_LAYER_PREFIX,
    CHAINAGE_LABEL_LAYER,
    CROSS_SECTIONS_LAYER,
    DIMENSIONS_LAYER_PREFIX,
    PLUGIN_NAME,
    PROP_CRS,
    PROP_SOURCE_PATH,
    SEGMENTS_LAYER_PREFIX,
    SOURCE_FILE_FIELD,
    STATIONS_LAYER_PREFIX,
    VERTICAL_PROFILE_LAYER,
)
from .gpkg_writer import write_layers_to_gpkg
from .import_dialog import Align2QgisImportDialog, ImportOptions
from .landxml_parser import inspect_landxml, parse_alignments_with_meta, parse_cross_sections
from .layers import (
    build_alignment_layer,
    build_chainage_label_layer,
    build_cross_sections_layer,
    build_dimension_layer,
    build_segment_layer,
    build_stations_layer,
    build_vertical_profile_layer,
    tag_layer,
)
from .profile_dock import Align2QgisProfileDock
from .styling import (
    apply_dimension_labels,
    apply_station_labels,
    apply_station_symbol,
)

if TYPE_CHECKING:
    from qgis.gui import QgisInterface


_SUPPORTED_INSPECT_TAGS = {"Alignment", "Profile"}

# Layer-kind tags used by _apply_styling to pick the right symbology / labels.
_KIND_ALIGNMENT = "alignment"
_KIND_SEGMENTS = "segments"
_KIND_STATIONS = "stations"
_KIND_DIMENSIONS = "dimensions"
_KIND_CHAINAGE = "chainage"
_KIND_VERTICAL_PROFILE = "vertical_profile"
_KIND_CROSS_SECTIONS = "cross_sections"


class Align2QgisPlugin:
    def __init__(self, iface: "QgisInterface") -> None:
        self.iface = iface
        self.import_action: QAction | None = None
        self.reapply_action: QAction | None = None
        self.inspect_action: QAction | None = None
        self.profile_action: QAction | None = None
        self.profile_dock: Align2QgisProfileDock | None = None
        self._dock_station_layer: QgsVectorLayer | None = None

    # ------------------------------------------------------------------
    # QGIS lifecycle
    # ------------------------------------------------------------------
    def initGui(self) -> None:  # noqa: N802
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        self.import_action = self._make_action(
            "Import LandXML alignment…", self.run, icon=icon, on_toolbar=True
        )
        self.reapply_action = self._make_action(
            "Re-apply settings to active alignment layer…", self.run_reapply
        )
        self.inspect_action = self._make_action(
            "Inspect LandXML file…", self.run_inspect
        )

        self.profile_dock = Align2QgisProfileDock(self.iface.mainWindow())
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.profile_dock)
        self.profile_dock.hide()
        self.profile_dock.alignmentSelected.connect(self._on_dock_alignment_picked)

        self.profile_action = self._make_action(
            "Show curvature profile dock", self._toggle_profile_dock,
            checkable=True,
        )
        self.profile_dock.visibilityChanged.connect(self.profile_action.setChecked)

        self.iface.currentLayerChanged.connect(self._on_active_layer_changed)

    def unload(self) -> None:
        try:
            self.iface.currentLayerChanged.disconnect(self._on_active_layer_changed)
        except (TypeError, RuntimeError):
            pass
        self._disconnect_station_layer()
        if self.profile_dock is not None:
            self.iface.removeDockWidget(self.profile_dock)
            self.profile_dock.deleteLater()
            self.profile_dock = None
        for action in (
            self.import_action,
            self.reapply_action,
            self.inspect_action,
            self.profile_action,
        ):
            if action is None:
                continue
            self.iface.removePluginVectorMenu(PLUGIN_NAME, action)
            self.iface.removeToolBarIcon(action)
        self.import_action = None
        self.reapply_action = None
        self.inspect_action = None
        self.profile_action = None

    def _make_action(
        self, label, callback, *, icon=None, checkable=False, on_toolbar=False
    ) -> QAction:
        action = QAction(icon, label, self.iface.mainWindow()) if icon else QAction(
            label, self.iface.mainWindow()
        )
        if checkable:
            action.setCheckable(True)
            action.toggled.connect(callback)
        else:
            action.triggered.connect(callback)
        self.iface.addPluginToVectorMenu(PLUGIN_NAME, action)
        if on_toolbar:
            self.iface.addToolBarIcon(action)
        return action

    # ------------------------------------------------------------------
    # Profile dock wiring
    # ------------------------------------------------------------------
    def _toggle_profile_dock(self, checked: bool) -> None:
        if self.profile_dock is None:
            return
        self.profile_dock.setVisible(checked)
        if checked:
            self._refresh_profile_for_active_layer()

    def _on_active_layer_changed(self, layer) -> None:
        if self.profile_dock is None or not self.profile_dock.isVisible():
            return
        self._refresh_profile_for_active_layer()

    def _refresh_profile_for_active_layer(self) -> None:
        if self.profile_dock is None:
            return
        self._refresh_dock_combo()
        layer = self.iface.activeLayer()
        source = (
            layer.customProperty(PROP_SOURCE_PATH, "")
            if isinstance(layer, QgsMapLayer)
            else ""
        )
        if not source:
            self.profile_dock.clear()
            self._disconnect_station_layer()
            return
        # Resolve a concrete alignment name from the Alignments layer for this source.
        align_name = self._first_alignment_name_for_source(source)
        if align_name:
            self._load_alignment_into_dock(align_name)
        else:
            self.profile_dock.clear()
            self._disconnect_station_layer()

    def _first_alignment_name_for_source(self, source_path: str) -> str | None:
        """Return the first alignment name whose source_file matches the basename."""
        aligns_layer = self._find_named_layer(QgsProject.instance(), ALIGNMENT_LAYER_PREFIX)
        if aligns_layer is None:
            return None
        base = os.path.basename(source_path)
        for feat in aligns_layer.getFeatures():
            if str(feat[SOURCE_FILE_FIELD] or "") == base:
                name = str(feat["name"] or "")
                if name:
                    return name
        return None

    def _on_dock_alignment_picked(self, alignment_name: str) -> None:
        if alignment_name:
            self._load_alignment_into_dock(alignment_name)

    def _load_alignment_into_dock(self, alignment_name: str) -> None:
        """Wire the dock to the chosen alignment name, reading from project layers."""
        if self.profile_dock is None:
            return
        seg_layer, stn_layer, vp_layer = self._find_dock_layers()
        self.profile_dock.set_alignment(
            seg_layer,
            vert_profile_layer=vp_layer,
            alignment_name=alignment_name,
        )
        self.profile_dock.select_alignment(alignment_name)
        self._connect_station_layer(stn_layer)

    def _find_dock_layers(
        self,
    ) -> tuple[QgsVectorLayer | None, QgsVectorLayer | None, QgsVectorLayer | None]:
        """Locate the canonical Segments, Stations, and VerticalProfile layers."""
        seg_layer: QgsVectorLayer | None = None
        stn_layer: QgsVectorLayer | None = None
        vp_layer: QgsVectorLayer | None = None
        for ml in QgsProject.instance().mapLayers().values():
            name = ml.name()
            if name == SEGMENTS_LAYER_PREFIX:
                seg_layer = ml
            elif name == STATIONS_LAYER_PREFIX:
                stn_layer = ml
            elif name == VERTICAL_PROFILE_LAYER:
                vp_layer = ml
        return seg_layer, stn_layer, vp_layer

    def _available_alignments(self) -> list[tuple[str, str]]:
        """Return ``[(alignment_name, label), …]`` from the Alignments layer."""
        aligns_layer = self._find_named_layer(QgsProject.instance(), ALIGNMENT_LAYER_PREFIX)
        if aligns_layer is None:
            return []
        seen: dict[str, str] = {}
        for feat in aligns_layer.getFeatures():
            name = str(feat["name"] or "")
            source = str(feat[SOURCE_FILE_FIELD] or "")
            if name and name not in seen:
                seen[name] = f"{name} — {source}" if source else name
        return sorted(seen.items(), key=lambda kv: kv[1])

    def _refresh_dock_combo(self) -> None:
        if self.profile_dock is None:
            return
        self.profile_dock.set_alignments_available(self._available_alignments())

    def _connect_station_layer(self, layer: QgsVectorLayer | None) -> None:
        if self._dock_station_layer is layer:
            return
        self._disconnect_station_layer()
        if layer is None:
            return
        layer.selectionChanged.connect(self._on_station_selection_changed)
        self._dock_station_layer = layer

    def _disconnect_station_layer(self) -> None:
        if self._dock_station_layer is None:
            return
        try:
            self._dock_station_layer.selectionChanged.disconnect(
                self._on_station_selection_changed
            )
        except (TypeError, RuntimeError):
            pass
        self._dock_station_layer = None

    def _on_station_selection_changed(self, *_args) -> None:
        if self.profile_dock is None or self._dock_station_layer is None:
            return
        selected = self._dock_station_layer.selectedFeatures()
        if not selected:
            self.profile_dock.set_highlight_station(None)
            return
        try:
            sta = float(selected[0]["station"])
        except (KeyError, TypeError, ValueError):
            self.profile_dock.set_highlight_station(None)
            return
        self.profile_dock.set_highlight_station(sta)

    # ------------------------------------------------------------------
    # User-triggered actions
    # ------------------------------------------------------------------
    def run(self) -> None:
        from qgis.PyQt.QtCore import QSettings

        project_crs = QgsProject.instance().crs()
        default_auth = (
            QSettings().value("Align2QGIS/last_crs", "")
            or (project_crs.authid() if project_crs.isValid() else "EPSG:25832")
        )
        opts = self._collect_options(default_auth)
        if opts is None:
            return
        if not opts.landxml_path or not opts.crs_authid:
            QMessageBox.warning(
                self.iface.mainWindow(),
                PLUGIN_NAME,
                "Need both a LandXML file and a CRS to import.",
            )
            return
        self._process(opts)

    def run_reapply(self) -> None:
        """Re-run station / dimension / segment generation against the
        currently-active alignment layer, re-parsing its source LandXML.
        """
        layer = self.iface.activeLayer()
        source_path = layer.customProperty(PROP_SOURCE_PATH, "") if layer is not None else ""
        if not source_path:
            QMessageBox.information(
                self.iface.mainWindow(),
                PLUGIN_NAME,
                "Select an Align2QGIS alignment layer first, then re-run this "
                "action. (Alignment layers carry a hidden source-path tag.)",
            )
            return
        crs_authid = layer.customProperty(PROP_CRS, "") or "EPSG:25832"
        opts = self._collect_options(crs_authid, preset_path=source_path)
        if opts is None:
            return
        if not opts.landxml_path or not opts.crs_authid:
            return
        self._process(opts)

    def run_inspect(self) -> None:
        """List every LandXML element type the file contains.

        Quick way to answer "what's in my file" without running an import.
        """
        path, _ = QFileDialog.getOpenFileName(
            self.iface.mainWindow(),
            "Inspect LandXML file",
            "",
            "LandXML (*.xml *.landxml);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                counts = inspect_landxml(fh.read())
        except Exception as exc:
            QMessageBox.critical(
                self.iface.mainWindow(),
                PLUGIN_NAME,
                f"Failed to read LandXML:\n{exc}",
            )
            return

        rows = []
        for tag, count in counts.items():
            marker = " (imported)" if tag in _SUPPORTED_INSPECT_TAGS and count > 0 else ""
            rows.append(f"  {tag}: {count}{marker}")
        QMessageBox.information(
            self.iface.mainWindow(),
            f"{PLUGIN_NAME} — {os.path.basename(path)}",
            "Element counts:\n\n" + "\n".join(rows) + "\n\n"
            "Currently imported: Alignment (horizontal + chainage + dimensions"
            " + segments) and Profile (vertical, shown in the profile dock).\n"
            "Other element types are present in the file but not yet imported;"
            " ask if you'd like one wired up.",
        )

    # ------------------------------------------------------------------
    # Import pipeline
    # ------------------------------------------------------------------
    def _collect_options(
        self, default_crs: str, preset_path: str = ""
    ) -> ImportOptions | None:
        dlg = Align2QgisImportDialog(default_crs, parent=self.iface.mainWindow())
        if preset_path:
            dlg.path_edit.setText(preset_path)
            dlg.crs_edit.setText(default_crs)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        dlg.persist()
        return dlg.options()

    def _process(self, opts: ImportOptions) -> None:
        parsed = self._parse_or_error(opts.landxml_path)
        if parsed is None:
            return
        alignments, meta = parsed

        base = os.path.basename(opts.landxml_path)
        replaced = self._purge_existing(opts.landxml_path)

        try:
            with open(opts.landxml_path, "rb") as fh:
                xml_bytes = fh.read()
            cross_sections = parse_cross_sections(xml_bytes)
        except Exception:
            cross_sections = []

        # Build fresh memory layers from this import. Each carries the
        # source_file column so the per-import purge can find them later.
        imported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_layers = self._build_layers(
            alignments, cross_sections, opts, base, imported_at, meta,
        )

        project = QgsProject.instance()
        try:
            target = self._register_layers(project, new_layers, opts)
        except IOError as exc:
            QMessageBox.critical(
                self.iface.mainWindow(), PLUGIN_NAME,
                f"GeoPackage write failed:\n{exc}",
            )
            return

        self._refresh_dock_combo()

        n_align = len(alignments)
        action = "Re-applied" if replaced else "Imported"
        msg = (
            f"{action} {n_align} alignment(s) from {base} → "
            f"{len(new_layers)} layer(s) ({target})"
        )
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, msg, level=Qgis.MessageLevel.Info, duration=6
        )

    def _parse_or_error(self, path: str):
        try:
            with open(path, "rb") as fh:
                xml_bytes = fh.read()
            alignments, meta = parse_alignments_with_meta(xml_bytes)
        except Exception as exc:  # broad: surface parse errors to user
            QMessageBox.critical(
                self.iface.mainWindow(), PLUGIN_NAME,
                f"Failed to parse LandXML:\n{exc}",
            )
            return None
        if not alignments:
            QMessageBox.information(
                self.iface.mainWindow(), PLUGIN_NAME,
                "No <Alignment> elements found in file.",
            )
            return None
        return alignments, meta

    def _build_layers(
        self, alignments, cross_sections, opts: ImportOptions, base: str,
        imported_at: str, meta,
    ) -> list[tuple[QgsVectorLayer, str, str, bool]]:
        """Return ``[(layer, canonical_name, kind, persist_to_gpkg), …]``.

        ``persist_to_gpkg`` is False for the auxiliary in-memory chainage
        label layer; everything else gets written to the GeoPackage.
        """
        crs = opts.crs_authid
        tag = lambda layer: tag_layer(layer, opts.landxml_path, crs)  # noqa: E731

        out: list[tuple[QgsVectorLayer, str, str, bool]] = [(
            tag(build_alignment_layer(
                alignments, ALIGNMENT_LAYER_PREFIX, crs,
                source_file=base, source_path=opts.landxml_path,
                imported_at=imported_at, metadata=meta,
            )),
            ALIGNMENT_LAYER_PREFIX,
            _KIND_ALIGNMENT,
            True,
        )]

        seg_layer = tag(build_segment_layer(
            alignments, SEGMENTS_LAYER_PREFIX, crs, source_file=base,
        ))
        if seg_layer.featureCount() > 0:
            out.append((seg_layer, SEGMENTS_LAYER_PREFIX, _KIND_SEGMENTS, True))

        # Stations table = defining stations only (segment endpoints + start).
        stn_layer = tag(build_stations_layer(
            alignments, STATIONS_LAYER_PREFIX, crs,
            perpendicular=opts.label_perpendicular,
            source_file=base,
        ))
        if stn_layer.featureCount() > 0:
            out.append((stn_layer, STATIONS_LAYER_PREFIX, _KIND_STATIONS, True))

        # Interval chainage labels live on a separate in-memory point layer.
        if opts.chainage_interval > 0:
            chain_layer = tag(build_chainage_label_layer(
                alignments, CHAINAGE_LABEL_LAYER, crs, opts.chainage_interval,
                perpendicular=opts.label_perpendicular,
            ))
            if chain_layer.featureCount() > 0:
                out.append((chain_layer, CHAINAGE_LABEL_LAYER, _KIND_CHAINAGE, False))

        if opts.create_dimension_layer and (
            opts.dim_arcs or opts.dim_spirals or opts.dim_tangents
        ):
            dim_layer = tag(build_dimension_layer(
                alignments, DIMENSIONS_LAYER_PREFIX, crs,
                arcs=opts.dim_arcs, spirals=opts.dim_spirals, tangents=opts.dim_tangents,
                perpendicular=opts.label_perpendicular,
                source_file=base,
            ))
            if dim_layer.featureCount() > 0:
                out.append((dim_layer, DIMENSIONS_LAYER_PREFIX, _KIND_DIMENSIONS, True))

        # Always persist VerticalProfile and CrossSections (may be empty layers).
        vp_layer = tag(build_vertical_profile_layer(
            alignments, VERTICAL_PROFILE_LAYER, crs, source_file=base,
        ))
        out.append((vp_layer, VERTICAL_PROFILE_LAYER, _KIND_VERTICAL_PROFILE, True))

        xs_layer = tag(build_cross_sections_layer(
            cross_sections, alignments, CROSS_SECTIONS_LAYER, crs, source_file=base,
        ))
        out.append((xs_layer, CROSS_SECTIONS_LAYER, _KIND_CROSS_SECTIONS, True))

        return out

    def _register_layers(
        self, project, new_layers, opts: ImportOptions,
    ) -> str:
        """Append features into the canonical-named project layers; create
        them if they don't exist yet. Optionally also persist to GPKG.

        Returns a short human label for the destination (used in the
        pushMessage summary).
        """
        root = project.layerTreeRoot()
        group = root.findGroup(PLUGIN_NAME) or root.insertGroup(0, PLUGIN_NAME)

        if opts.gpkg_path:
            persisted = [
                (layer, name) for layer, name, _, persist in new_layers if persist
            ]
            base = os.path.basename(opts.landxml_path)
            write_layers_to_gpkg(persisted, opts.gpkg_path, source_file=base)

        for layer, name, kind, persist in new_layers:
            existing = self._find_named_layer(project, name)
            if existing is None:
                if persist and opts.gpkg_path:
                    gpkg_layer = QgsVectorLayer(
                        f"{opts.gpkg_path}|layername={name}", name, "ogr"
                    )
                    if gpkg_layer.isValid():
                        tag_layer(gpkg_layer, opts.landxml_path, opts.crs_authid)
                        project.addMapLayer(gpkg_layer, False)
                        group.addLayer(gpkg_layer)
                        self._apply_styling(gpkg_layer, kind, opts)
                        continue
                project.addMapLayer(layer, False)
                group.addLayer(layer)
                self._apply_styling(layer, kind, opts)
            else:
                # Layer already exists in project — append the freshly-built
                # features into it. Memory layers and GPKG-backed layers both
                # support dataProvider().addFeatures().
                self._append_features(existing, layer)
                self._apply_styling(existing, kind, opts)

        return opts.gpkg_path or "in-memory layers"

    @staticmethod
    def _find_named_layer(project, name: str) -> QgsVectorLayer | None:
        for ml in project.mapLayers().values():
            if isinstance(ml, QgsVectorLayer) and ml.name() == name:
                return ml
        return None

    @staticmethod
    def _append_features(target: QgsVectorLayer, source: QgsVectorLayer) -> None:
        """Copy every feature from ``source`` into ``target`` by attribute
        name. Field set differences are tolerated — missing fields stay NULL.
        """
        target_fields = target.fields()
        src_fields = source.fields()
        new_feats: list[QgsFeature] = []
        for sf in source.getFeatures():
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
        was_editing = target.isEditable()
        if not was_editing:
            target.startEditing()
        target.dataProvider().addFeatures(new_feats)
        if not was_editing:
            target.commitChanges()
        target.updateExtents()
        target.triggerRepaint()

    @staticmethod
    def _apply_styling(layer: QgsVectorLayer, kind: str, opts: ImportOptions) -> None:
        if kind in (_KIND_STATIONS, _KIND_CHAINAGE):
            apply_station_labels(layer)
            apply_station_symbol(layer)
        elif kind == _KIND_DIMENSIONS:
            apply_dimension_labels(layer)

    def _purge_existing(self, source_path: str) -> int:
        """Delete rows whose ``source_file`` matches ``source_path``'s basename
        from each canonical-named layer in the project. Idempotent: missing
        layers are skipped silently.

        Returns the number of layers from which rows were deleted.
        """
        project = QgsProject.instance()
        base = os.path.basename(source_path)
        touched = 0
        for layer_name in (
            ALIGNMENT_LAYER_PREFIX, SEGMENTS_LAYER_PREFIX,
            STATIONS_LAYER_PREFIX, DIMENSIONS_LAYER_PREFIX,
            CHAINAGE_LABEL_LAYER, VERTICAL_PROFILE_LAYER, CROSS_SECTIONS_LAYER,
        ):
            layer = self._find_named_layer(project, layer_name)
            if layer is None:
                continue
            field_idx = layer.fields().indexOf(SOURCE_FILE_FIELD)
            if field_idx < 0:
                continue
            victims = [
                f.id() for f in layer.getFeatures()
                if f.attribute(field_idx) == base
            ]
            if not victims:
                continue
            was_editing = layer.isEditable()
            if not was_editing:
                layer.startEditing()
            layer.dataProvider().deleteFeatures(victims)
            if not was_editing:
                layer.commitChanges()
            layer.updateExtents()
            layer.triggerRepaint()
            touched += 1
        return touched
