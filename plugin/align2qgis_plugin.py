"""QGIS plugin orchestrator.

Wires the QGIS menu actions, the profile dock, and the layer-tree group
together. The actual work is delegated to the focused modules:

* :mod:`.layers`        — build the four output layers from parsed alignments.
* :mod:`.styling`       — label rotation, tick marker, dimension label colour.
* :mod:`.cache`         — keep parsed alignments + densified profile in RAM.
* :mod:`.import_dialog` — single dialog that collects every option.
* :mod:`.gpkg_writer`   — persist memory layers into a GeoPackage.
* :mod:`.profile_dock`  — interactive curvature + vertical profile widget.
"""
from __future__ import annotations

import os
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
    QgsMapLayer,
    QgsProject,
    QgsVectorLayer,
)

from . import cache
from .constants import (
    ALIGNMENT_LAYER_PREFIX,
    DIMENSIONS_LAYER_PREFIX,
    PLUGIN_NAME,
    PROP_CRS,
    PROP_SOURCE_PATH,
    SEGMENTS_LAYER_PREFIX,
    STATIONS_LAYER_PREFIX,
)
from .gpkg_writer import write_layers_to_gpkg
from .import_dialog import Align2QgisImportDialog, ImportOptions
from .landxml_parser import inspect_landxml, parse_alignments
from .layers import (
    build_alignment_layer,
    build_chainage_layer,
    build_dimension_layer,
    build_segment_layer,
    safe_name,
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
        self._load_alignment_into_dock(source)

    def _on_dock_alignment_picked(self, source_path: str) -> None:
        if source_path:
            self._load_alignment_into_dock(source_path)

    def _load_alignment_into_dock(self, source_path: str) -> None:
        """Wire the dock's segment + station + vertical-profile state to
        ``source_path``. Common back-end for the active-layer flow and the
        combo-box picker.
        """
        if self.profile_dock is None:
            return
        seg_layer, stn_layer = self._find_dock_layers(source_path)
        self.profile_dock.set_alignment(
            seg_layer,
            vert_profile=cache.vert_samples(source_path),
            title=os.path.basename(source_path),
        )
        self.profile_dock.select_alignment(source_path)
        self._connect_station_layer(stn_layer)

    def _find_dock_layers(
        self, source_path: str
    ) -> tuple[QgsVectorLayer | None, QgsVectorLayer | None]:
        seg_layer: QgsVectorLayer | None = None
        stn_layer: QgsVectorLayer | None = None
        for ml in QgsProject.instance().mapLayers().values():
            if ml.customProperty(PROP_SOURCE_PATH, "") != source_path:
                continue
            name = ml.name()
            if name.startswith(SEGMENTS_LAYER_PREFIX):
                seg_layer = ml
            elif name.startswith(STATIONS_LAYER_PREFIX):
                stn_layer = ml
        return seg_layer, stn_layer

    def _available_alignments(self) -> list[tuple[str, str]]:
        seen: dict[str, str] = {}
        for ml in QgsProject.instance().mapLayers().values():
            source = ml.customProperty(PROP_SOURCE_PATH, "")
            if source and source not in seen:
                seen[source] = os.path.basename(source)
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
        alignments = self._parse_or_error(opts.landxml_path)
        if alignments is None:
            return

        replaced = self._purge_existing(opts.landxml_path)
        cache.remember(opts.landxml_path, alignments)

        base = os.path.basename(opts.landxml_path)
        stem = safe_name(os.path.splitext(base)[0])
        layers_to_register = self._build_layers(alignments, opts, base, stem)

        project = QgsProject.instance()
        root = project.layerTreeRoot()
        group = root.insertGroup(0, base)
        group.setCustomProperty(PROP_SOURCE_PATH, opts.landxml_path)
        group.setCustomProperty(PROP_CRS, opts.crs_authid)

        try:
            target = self._register_layers(project, group, layers_to_register, opts)
        except IOError as exc:
            QMessageBox.critical(
                self.iface.mainWindow(), PLUGIN_NAME,
                f"GeoPackage write failed:\n{exc}",
            )
            root.removeChildNode(group)
            return

        self._refresh_dock_combo()

        align_layer = layers_to_register[0][0]
        n_align = align_layer.featureCount()
        n_layers = len(layers_to_register)
        action = "Re-applied" if replaced else "Imported"
        msg = (
            f"{action} {n_align} alignment(s) from {base} → "
            f"{n_layers} layer(s) ({target})"
        )
        self.iface.messageBar().pushMessage(
            PLUGIN_NAME, msg, level=Qgis.MessageLevel.Info, duration=6
        )

    def _parse_or_error(self, path: str):
        try:
            with open(path, "rb") as fh:
                xml_bytes = fh.read()
            alignments = parse_alignments(xml_bytes)
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
        return alignments

    def _build_layers(
        self, alignments, opts: ImportOptions, base: str, stem: str
    ) -> list[tuple[QgsVectorLayer, str, str]]:
        """Return ``[(layer, gpkg_name, kind), …]`` for everything to register."""
        crs = opts.crs_authid
        tag = lambda layer: tag_layer(layer, opts.landxml_path, crs)  # noqa: E731

        out: list[tuple[QgsVectorLayer, str, str]] = [(
            tag(build_alignment_layer(alignments, f"{ALIGNMENT_LAYER_PREFIX} — {base}", crs)),
            f"align_{stem}",
            "alignment",
        )]

        seg_layer = tag(build_segment_layer(
            alignments, f"{SEGMENTS_LAYER_PREFIX} — {base}", crs
        ))
        if seg_layer.featureCount() > 0:
            out.append((seg_layer, f"segments_{stem}", "segments"))

        if opts.chainage_interval > 0:
            stn_layer = tag(build_chainage_layer(
                alignments,
                f"{STATIONS_LAYER_PREFIX} ({opts.chainage_interval:g} m) — {base}",
                crs, opts.chainage_interval,
                perpendicular=opts.label_perpendicular,
            ))
            if stn_layer.featureCount() > 0:
                out.append((stn_layer, f"stations_{stem}", "chainage"))

        if opts.create_dimension_layer and (
            opts.dim_arcs or opts.dim_spirals or opts.dim_tangents
        ):
            dim_layer = tag(build_dimension_layer(
                alignments, f"{DIMENSIONS_LAYER_PREFIX} — {base}", crs,
                arcs=opts.dim_arcs, spirals=opts.dim_spirals, tangents=opts.dim_tangents,
                perpendicular=opts.label_perpendicular,
            ))
            if dim_layer.featureCount() > 0:
                out.append((dim_layer, f"dims_{stem}", "dimensions"))

        return out

    def _register_layers(
        self, project, group, layers_to_register, opts: ImportOptions
    ) -> str:
        """Drop everything into the project + group, optionally to GPKG.

        Returns a short human label for the destination (used in the
        pushMessage summary).
        """
        if opts.gpkg_path:
            pairs = [(layer, name) for layer, name, _ in layers_to_register]
            written = write_layers_to_gpkg(pairs, opts.gpkg_path)
            kinds = {gpkg_name: kind for _, gpkg_name, kind in layers_to_register}
            for gpkg_path, gpkg_name, display in written:
                gpkg_layer = QgsVectorLayer(
                    f"{gpkg_path}|layername={gpkg_name}", display, "ogr"
                )
                if not gpkg_layer.isValid():
                    continue
                tag_layer(gpkg_layer, opts.landxml_path, opts.crs_authid)
                project.addMapLayer(gpkg_layer, False)
                group.addLayer(gpkg_layer)
                self._apply_styling(gpkg_layer, kinds.get(gpkg_name, ""))
            return opts.gpkg_path

        for layer, _, kind in layers_to_register:
            project.addMapLayer(layer, False)
            group.addLayer(layer)
            self._apply_styling(layer, kind)
        return "in-memory layers"

    @staticmethod
    def _apply_styling(layer: QgsVectorLayer, kind: str) -> None:
        if kind == "chainage":
            apply_station_labels(layer)
            apply_station_symbol(layer)
        elif kind == "dimensions":
            apply_dimension_labels(layer)

    def _purge_existing(self, source_path: str) -> int:
        """Remove every plugin-managed layer + its group whose source
        matches ``source_path``. Keeps :meth:`_process` idempotent so
        Re-apply replaces instead of stacking duplicates.
        """
        project = QgsProject.instance()
        victims = [
            layer.id()
            for layer in project.mapLayers().values()
            if layer.customProperty(PROP_SOURCE_PATH, "") == source_path
        ]
        for layer_id in victims:
            project.removeMapLayer(layer_id)

        root = project.layerTreeRoot()
        for grp in list(root.findGroups()):
            if grp.customProperty(PROP_SOURCE_PATH, "") == source_path:
                root.removeChildNode(grp)
        cache.forget(source_path)
        return len(victims)
