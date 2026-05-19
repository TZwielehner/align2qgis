"""Curvature + vertical-profile diagram dock with interactive scrub slider.

Reads segments from a ``segments_<stem>`` layer that the import flow
creates, plus an optional densified (station, elevation) profile. Plots
both, colour-coded by segment kind, and highlights the slider position
with a red vertical line on each subplot. Falls back to a text-only
summary when matplotlib isn't available in the QGIS install.

Slider scrubbing is the hot path. To stay smooth at 100 000 steps we only
re-plot the static layers (segment colours, profile line, axes) when
``set_alignment`` / ``clear`` is called; on every slider tick we update
just the cached red ``axvline`` handles and the text panel.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QSlider,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsVectorLayer

_SLIDER_STEPS = 100000  # int range; sub-decimetre resolution on a 10 km alignment

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as _Canvas
    _HAS_MPL = True
except ImportError:
    try:
        from matplotlib.figure import Figure  # type: ignore[no-redef]
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as _Canvas
        _HAS_MPL = True
    except ImportError:
        Figure = None  # type: ignore[assignment]
        _Canvas = None  # type: ignore[assignment]
        _HAS_MPL = False


@dataclass
class _Segment:
    kind: str
    sta_start: float
    sta_end: float
    curvature_start: float
    curvature_end: float
    radius_start: float | None


_KIND_COLORS = {
    "line": "#7a7a7a",
    "curve": "#c14040",
    "spiral": "#3a8fc1",
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Align2QgisProfileDock(QDockWidget):
    """Floating/dock widget showing curvature + vertical profile of an alignment."""

    alignmentSelected = pyqtSignal(str)  # emits the chosen source_path

    def __init__(self, parent=None) -> None:
        super().__init__("Align2QGIS — Alignment Profile", parent)
        self._segments: list[_Segment] = []
        self._seg_starts: list[float] = []
        self._vert_stations: list[float] = []
        self._vert_elevs: list[float] = []
        self._station_range_cached: tuple[float, float] | None = None
        self._highlight_sta: float | None = None
        self._title: str = ""
        # Cached matplotlib artist handles. We mutate their data on slider
        # scrub instead of re-plotting the segment lines.
        self._curv_vline = None
        self._vert_vline = None
        self._curv_text = None
        self._vert_text = None

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(4, 4, 4, 4)

        combo_row = QHBoxLayout()
        combo_row.addWidget(QLabel("Alignment:"))
        self.alignment_combo = QComboBox()
        self.alignment_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.alignment_combo.currentIndexChanged.connect(self._on_combo_changed)
        combo_row.addWidget(self.alignment_combo, 1)
        layout.addLayout(combo_row)

        if _HAS_MPL:
            self.figure = Figure(figsize=(5, 4.4), tight_layout=True)
            self.canvas = _Canvas(self.figure)
            self.ax_curv, self.ax_vert = self.figure.subplots(
                2, 1, sharex=True, gridspec_kw={"height_ratios": [1, 1]}
            )
            layout.addWidget(self.canvas)
        else:
            self.figure = None
            self.canvas = None
            self.ax_curv = None
            self.ax_vert = None
            layout.addWidget(
                QLabel(
                    "matplotlib not available — install it via OSGeo4W / pip "
                    "to enable the plots. Text panel below still works."
                )
            )

        slider_row = QHBoxLayout()
        self.station_label = QLabel("Station: —")
        self.station_label.setMinimumWidth(140)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, _SLIDER_STEPS)
        self.slider.setValue(0)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider_changed)
        slider_row.addWidget(self.station_label)
        slider_row.addWidget(self.slider, 1)
        layout.addLayout(slider_row)

        self.info_label = QLabel("Select an Align2QGIS alignment or station.")
        self.info_label.setWordWrap(True)
        self.info_label.setMinimumHeight(48)
        layout.addWidget(self.info_label)

        self.setWidget(container)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def set_alignment(
        self,
        segments_layer: QgsVectorLayer | None,
        vert_profile: list[tuple[float, float]] | None = None,
        title: str = "",
    ) -> None:
        """Populate from a ``segments_<stem>`` layer + optional vertical profile."""
        self._segments = []
        self._title = title
        if segments_layer is not None and segments_layer.isValid():
            for feat in segments_layer.getFeatures():
                try:
                    seg = _Segment(
                        kind=str(feat["kind"]),
                        sta_start=_safe_float(feat["sta_start"]),
                        sta_end=_safe_float(feat["sta_end"]),
                        curvature_start=_safe_float(feat["curvature_start"]),
                        curvature_end=_safe_float(feat["curvature_end"]),
                        radius_start=_optional_float(feat["radius_start"]),
                    )
                except KeyError:
                    continue
                self._segments.append(seg)
            self._segments.sort(key=lambda s: s.sta_start)
        self._seg_starts = [s.sta_start for s in self._segments]

        pairs = sorted(vert_profile or [])
        self._vert_stations = [s for s, _ in pairs]
        self._vert_elevs = [e for _, e in pairs]

        self._station_range_cached = self._compute_station_range()
        self._highlight_sta = None
        self._sync_slider_range()
        self._redraw_static()
        self._update_highlight()

    def set_highlight_station(self, station_m: float | None) -> None:
        self._highlight_sta = station_m
        if station_m is not None:
            self._set_slider_to_station(station_m)
        self._update_highlight()

    def clear(self) -> None:
        self._segments = []
        self._seg_starts = []
        self._vert_stations = []
        self._vert_elevs = []
        self._station_range_cached = None
        self._highlight_sta = None
        self._title = ""
        self._sync_slider_range()
        self._redraw_static()
        self._update_highlight()

    def set_alignments_available(self, items: list[tuple[str, str]]) -> None:
        """Refresh the alignment-picker combo. ``items`` is ``[(source_path, label), …]``.

        Signals are blocked while we repopulate so we don't trigger a
        spurious ``alignmentSelected`` when the plugin pushes a new list
        in response to a layer added/removed in the project.
        """
        self.alignment_combo.blockSignals(True)
        current = self.alignment_combo.currentData()
        self.alignment_combo.clear()
        for source_path, label in items:
            self.alignment_combo.addItem(label, source_path)
        # Try to re-select what was previously active.
        if current is not None:
            for i in range(self.alignment_combo.count()):
                if self.alignment_combo.itemData(i) == current:
                    self.alignment_combo.setCurrentIndex(i)
                    break
        self.alignment_combo.blockSignals(False)

    def select_alignment(self, source_path: str) -> None:
        """Programmatically set the combo without re-emitting the signal."""
        for i in range(self.alignment_combo.count()):
            if self.alignment_combo.itemData(i) == source_path:
                self.alignment_combo.blockSignals(True)
                self.alignment_combo.setCurrentIndex(i)
                self.alignment_combo.blockSignals(False)
                return

    def _on_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        source = self.alignment_combo.itemData(index)
        if source:
            self.alignmentSelected.emit(source)

    # ------------------------------------------------------------------
    # Slider plumbing
    # ------------------------------------------------------------------
    def _compute_station_range(self) -> tuple[float, float] | None:
        candidates_lo = []
        candidates_hi = []
        if self._segments:
            candidates_lo.append(self._segments[0].sta_start)
            candidates_hi.append(max(s.sta_end for s in self._segments))
        if self._vert_stations:
            candidates_lo.append(self._vert_stations[0])
            candidates_hi.append(self._vert_stations[-1])
        if not candidates_lo:
            return None
        lo, hi = min(candidates_lo), max(candidates_hi)
        return (lo, hi) if hi > lo else None

    def _sync_slider_range(self) -> None:
        rng = self._station_range_cached
        self.slider.blockSignals(True)
        self.slider.setEnabled(rng is not None)
        self.slider.setValue(0)
        self.slider.blockSignals(False)

    def _set_slider_to_station(self, station: float) -> None:
        rng = self._station_range_cached
        if rng is None:
            return
        lo, hi = rng
        val = int(round((station - lo) / (hi - lo) * _SLIDER_STEPS))
        val = max(0, min(_SLIDER_STEPS, val))
        self.slider.blockSignals(True)
        self.slider.setValue(val)
        self.slider.blockSignals(False)

    def _station_from_slider(self, val: int) -> float | None:
        rng = self._station_range_cached
        if rng is None:
            return None
        lo, hi = rng
        return lo + (val / _SLIDER_STEPS) * (hi - lo)

    def _on_slider_changed(self, val: int) -> None:
        sta = self._station_from_slider(val)
        if sta is None:
            return
        self._highlight_sta = sta
        self._update_highlight()

    # ------------------------------------------------------------------
    # Drawing — static (cold path) vs highlight (slider-scrub hot path)
    # ------------------------------------------------------------------
    def _redraw_static(self) -> None:
        """Re-plot segment lines + axis labels. Slow; only run on data change."""
        if self.ax_curv is None or self.ax_vert is None:
            return
        self._draw_curvature(self.ax_curv)
        self._draw_vertical(self.ax_vert)
        # ax.clear() inside the draw helpers removes any previous axvline /
        # text artists, so the cached handles must be invalidated.
        self._curv_vline = None
        self._vert_vline = None
        self._curv_text = None
        self._vert_text = None
        if self._title:
            self.figure.suptitle(self._title, fontsize=9)
        else:
            self.figure.suptitle("")
        self.canvas.draw_idle()

    def _update_highlight(self) -> None:
        """Move (or create) the red highlight lines + stat overlays. Cheap."""
        sta = self._highlight_sta
        if self.ax_curv is not None and self.ax_vert is not None:
            for ax, attr in (
                (self.ax_curv, "_curv_vline"),
                (self.ax_vert, "_vert_vline"),
            ):
                line = getattr(self, attr)
                if sta is None:
                    if line is not None:
                        line.set_visible(False)
                    continue
                if line is None:
                    line = ax.axvline(sta, color="#c00", linewidth=1.5)
                    setattr(self, attr, line)
                else:
                    line.set_xdata([sta, sta])
                    line.set_visible(True)
            self._set_annotation(self.ax_curv, "_curv_text", self._curvature_stat_text(sta))
            self._set_annotation(self.ax_vert, "_vert_text", self._elevation_stat_text(sta))
            self.canvas.draw_idle()
        self._update_text()

    def _set_annotation(self, ax, attr: str, text: str) -> None:
        handle = getattr(self, attr)
        if not text:
            if handle is not None:
                handle.set_visible(False)
            return
        if handle is None:
            handle = ax.text(
                0.98, 0.96, text,
                transform=ax.transAxes, ha="right", va="top",
                fontsize=8,
                bbox=dict(
                    boxstyle="round,pad=0.3",
                    facecolor="white",
                    edgecolor="#888",
                    alpha=0.85,
                ),
            )
            setattr(self, attr, handle)
        else:
            handle.set_text(text)
            handle.set_visible(True)

    def _curvature_stat_text(self, station: float | None) -> str:
        if station is None:
            return ""
        kc = self._curvature_at(station)
        if kc is None:
            return ""
        kind, k = kc
        if abs(k) < 1e-12:
            return f"{kind}\ntangent (R = ∞)"
        return f"{kind}\nR = {1.0 / abs(k):.1f} m\nκ = {k * 1000:.3f} × 10⁻³ /m"

    def _elevation_stat_text(self, station: float | None) -> str:
        if station is None:
            return ""
        elev = self._elevation_at(station)
        if elev is None:
            return ""
        return f"Elev = {elev:.3f} m"

    def _draw_curvature(self, ax) -> None:
        ax.clear()
        if not self._segments:
            return
        # κ scaled ×1000 → easier to read on rail-scale radii.
        for s in self._segments:
            ax.plot(
                [s.sta_start, s.sta_end],
                [s.curvature_start * 1000.0, s.curvature_end * 1000.0],
                color=_KIND_COLORS.get(s.kind, "#000"),
                linewidth=2.0,
            )
        ax.axhline(0, color="#bbb", linewidth=0.5, linestyle="--")
        ax.set_ylabel("κ × 10³  (1/m)")
        ax.grid(True, alpha=0.25)

    def _draw_vertical(self, ax) -> None:
        ax.clear()
        if self._vert_stations:
            # Samples are densified inside vertical curves (parabolic
            # evaluation in landxml_parser.profile_samples), so a plain
            # line plot is faithful — no scatter dots needed.
            ax.plot(
                self._vert_stations,
                self._vert_elevs,
                color="#2a7a4a",
                linewidth=1.6,
            )
            ax.set_ylabel("Elevation (m)")
            ax.grid(True, alpha=0.25)
        else:
            ax.text(
                0.5, 0.5, "No vertical profile in source LandXML",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=8, color="#888",
            )
            ax.set_yticks([])
            ax.grid(False)
        ax.set_xlabel("Station (m)")

    # ------------------------------------------------------------------
    # Text panel + lookups
    # ------------------------------------------------------------------
    def _update_text(self) -> None:
        if not self._segments:
            self.station_label.setText("Station: —")
            self.info_label.setText(
                "No alignment loaded — make an Align2QGIS layer active."
            )
            return
        if self._highlight_sta is None:
            self.station_label.setText("Station: —")
            self.info_label.setText(
                f"{len(self._segments)} segment(s) loaded. "
                "Drag the slider or click a station marker to inspect."
            )
            return
        sta = self._highlight_sta
        self.station_label.setText(f"Station: {sta:.2f} m")

        elev = self._elevation_at(sta)
        elev_line = (
            f"Elevation = {elev:.3f} m"
            if elev is not None
            else "Elevation = — (outside vertical profile range)"
        )

        kc = self._curvature_at(sta)
        if kc is None:
            seg_line = "Outside the horizontal alignment range."
        else:
            kind, k = kc
            if abs(k) < 1e-12:
                geom = "tangent (R = ∞)"
            else:
                geom = f"R = {1.0 / abs(k):.1f} m  (κ = {k * 1000:.3f} × 10⁻³ /m)"
            seg_line = f"{kind}: {geom}"

        self.info_label.setText(f"{seg_line}\n{elev_line}")

    def _elevation_at(self, station: float) -> float | None:
        stations = self._vert_stations
        if not stations:
            return None
        if station < stations[0] - 1e-6 or station > stations[-1] + 1e-6:
            return None
        idx = bisect.bisect_left(stations, station)
        if idx == 0:
            return self._vert_elevs[0]
        if idx >= len(stations):
            return self._vert_elevs[-1]
        a_sta, b_sta = stations[idx - 1], stations[idx]
        a_elev, b_elev = self._vert_elevs[idx - 1], self._vert_elevs[idx]
        if b_sta - a_sta <= 1e-9:
            return a_elev
        t = (station - a_sta) / (b_sta - a_sta)
        return a_elev + t * (b_elev - a_elev)

    def _curvature_at(self, station: float) -> tuple[str, float] | None:
        if not self._segments:
            return None
        # bisect_right gives insertion point AFTER any equal sta_start; the
        # segment containing ``station`` is the one whose sta_start is the
        # largest value <= station, i.e. idx - 1.
        idx = bisect.bisect_right(self._seg_starts, station) - 1
        if idx < 0:
            return None
        s = self._segments[idx]
        if station > s.sta_end + 1e-6:
            return None
        length = s.sta_end - s.sta_start
        if length <= 0:
            return (s.kind, s.curvature_start)
        t = (station - s.sta_start) / length
        k = s.curvature_start + t * (s.curvature_end - s.curvature_start)
        return (s.kind, k)
