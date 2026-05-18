"""Single dialog for the LandXML import workflow.

Replaces the chain of ``QInputDialog`` prompts so users see every option at
once and can write the whole import to a GeoPackage in one round trip.
Settings persist between sessions via ``QSettings`` under ``Align2QGIS/*``.
"""
from __future__ import annotations

from dataclasses import dataclass

from qgis.PyQt.QtCore import QSettings
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


_PREFIX = "Align2QGIS/"


@dataclass
class ImportOptions:
    landxml_path: str
    crs_authid: str
    chainage_interval: float
    create_chainage_layer: bool
    create_dimension_layer: bool
    dim_arcs: bool
    dim_spirals: bool
    dim_tangents: bool
    label_perpendicular: bool  # False = read along tangent, True = across it
    gpkg_path: str  # "" → in-memory layers only


class Align2QgisImportDialog(QDialog):
    def __init__(self, default_crs: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import LandXML alignment")
        self.setMinimumWidth(480)

        settings = QSettings()

        self.path_edit = QLineEdit()
        path_btn = QPushButton("Browse…")
        path_btn.clicked.connect(self._pick_landxml)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(path_btn)

        self.crs_edit = QLineEdit(default_crs)

        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.0, 10000.0)
        self.interval_spin.setDecimals(2)
        self.interval_spin.setValue(
            float(settings.value(_PREFIX + "chainage_interval", 50.0))
        )
        self.interval_spin.setSuffix(" m  (0 = no chainage layer)")

        self.dim_group = QGroupBox("Dimensioning labels")
        self.dim_group.setCheckable(True)
        self.dim_group.setChecked(
            settings.value(_PREFIX + "dim_enabled", True, type=bool)
        )
        self.dim_arcs = QCheckBox("Arc radii (R=…)")
        self.dim_arcs.setChecked(
            settings.value(_PREFIX + "dim_arcs", True, type=bool)
        )
        self.dim_spirals = QCheckBox("Spiral A-values (A=… / L=…)")
        self.dim_spirals.setChecked(
            settings.value(_PREFIX + "dim_spirals", True, type=bool)
        )
        self.dim_tangents = QCheckBox("Tangent lengths (L=…)")
        self.dim_tangents.setChecked(
            settings.value(_PREFIX + "dim_tangents", False, type=bool)
        )
        self.label_perpendicular = QCheckBox(
            "Rotate station/dimension labels perpendicular to alignment"
        )
        self.label_perpendicular.setChecked(
            settings.value(_PREFIX + "label_perpendicular", False, type=bool)
        )
        dim_layout = QVBoxLayout()
        dim_layout.addWidget(self.dim_arcs)
        dim_layout.addWidget(self.dim_spirals)
        dim_layout.addWidget(self.dim_tangents)
        dim_layout.addWidget(self.label_perpendicular)
        self.dim_group.setLayout(dim_layout)

        gpkg_group = QGroupBox("GeoPackage output (optional)")
        self.gpkg_edit = QLineEdit(settings.value(_PREFIX + "gpkg_path", "") or "")
        gpkg_btn = QPushButton("Browse…")
        gpkg_btn.clicked.connect(self._pick_gpkg)
        gpkg_row = QHBoxLayout()
        gpkg_row.addWidget(self.gpkg_edit, 1)
        gpkg_row.addWidget(gpkg_btn)
        gpkg_help = QLabel(
            "Leave empty to keep everything as in-memory layers.\n"
            "If the file already exists, the import adds new layers and "
            "replaces any same-named layer it finds."
        )
        gpkg_help.setWordWrap(True)
        gpkg_layout = QVBoxLayout()
        gpkg_layout.addLayout(gpkg_row)
        gpkg_layout.addWidget(gpkg_help)
        gpkg_group.setLayout(gpkg_layout)

        form = QFormLayout()
        form.addRow("LandXML file", path_row)
        form.addRow("CRS (AuthID)", self.crs_edit)
        form.addRow("Chainage interval", self.interval_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        main = QVBoxLayout()
        main.addLayout(form)
        main.addWidget(self.dim_group)
        main.addWidget(gpkg_group)
        main.addWidget(buttons)
        self.setLayout(main)

    # ------------------------------------------------------------------
    def _pick_landxml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select LandXML file",
            self.path_edit.text(),
            "LandXML (*.xml *.landxml);;All files (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _pick_gpkg(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select or create GeoPackage",
            self.gpkg_edit.text(),
            "GeoPackage (*.gpkg)",
            options=QFileDialog.DontConfirmOverwrite,
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.gpkg_edit.setText(path)

    # ------------------------------------------------------------------
    def options(self) -> ImportOptions:
        return ImportOptions(
            landxml_path=self.path_edit.text().strip(),
            crs_authid=self.crs_edit.text().strip(),
            chainage_interval=self.interval_spin.value(),
            create_chainage_layer=self.interval_spin.value() > 0,
            create_dimension_layer=self.dim_group.isChecked(),
            dim_arcs=self.dim_arcs.isChecked(),
            dim_spirals=self.dim_spirals.isChecked(),
            dim_tangents=self.dim_tangents.isChecked(),
            label_perpendicular=self.label_perpendicular.isChecked(),
            gpkg_path=self.gpkg_edit.text().strip(),
        )

    def persist(self) -> None:
        s = QSettings()
        s.setValue(_PREFIX + "last_crs", self.crs_edit.text().strip())
        s.setValue(_PREFIX + "chainage_interval", self.interval_spin.value())
        s.setValue(_PREFIX + "dim_enabled", self.dim_group.isChecked())
        s.setValue(_PREFIX + "dim_arcs", self.dim_arcs.isChecked())
        s.setValue(_PREFIX + "dim_spirals", self.dim_spirals.isChecked())
        s.setValue(_PREFIX + "dim_tangents", self.dim_tangents.isChecked())
        s.setValue(_PREFIX + "label_perpendicular", self.label_perpendicular.isChecked())
        s.setValue(_PREFIX + "gpkg_path", self.gpkg_edit.text().strip())
