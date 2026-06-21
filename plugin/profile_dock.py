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

Annotations on the elevation plot — PVI markers, grade labels on
tangents, VertCurve length/radius callouts, crest/sag markers, and
station-equation discontinuities — are part of the static layer and are
preserved through the canvas's pan / zoom / vertical-exaggeration UI.
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

from ._utils import optional_float as _optional_float, safe_float as _safe_float
from .alignment_cache import alignments_by_name
from .constants import (
    FIELD_ALIGNMENT,
    FIELD_CURVATURE_END,
    FIELD_CURVATURE_START,
    FIELD_ELEVATION,
    FIELD_KIND,
    FIELD_RADIUS_START,
    FIELD_STA_END,
    FIELD_STA_START,
    FIELD_STATION,
    FIELD_VC_LENGTH,
)
from .landxml_parser import (
    PVI,
    ProfAlign,
    Profile,
    StaEquation,
    VertCurve,
    profile_samples,
)


def _is_real_vertcurve(item) -> bool:
    """A non-degenerate ``<VertCurve>`` item (PVIs and zero-length VCs return False)."""
    return isinstance(item, VertCurve) and item.length > 0

_SLIDER_STEPS = 100000  # int range; sub-decimetre resolution on a 10 km alignment

# Vertical-exaggeration choices the user can pick from the toolbar.
# 1× = autoscale-default. Higher values zoom into the elevation axis
# (y-range divided by the factor, centered on the data midpoint).
_VE_CHOICES = (1.0, 2.0, 5.0, 10.0, 25.0, 50.0)

# Colour palette for the elevation-plot annotations. PVI/profile green
# matches the densified profile line; crest/sag colours follow the rail
# convention (warm = crest, cool = sag); equation marks read as
# neutral guide lines.
_COLOR_PROFILE = "#2a7a4a"
_COLOR_CREST = "#b06b00"
_COLOR_SAG = "#0066a6"
_COLOR_NEUTRAL = "#666"
_COLOR_GUIDE = "#888"

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_qtagg import (
        FigureCanvasQTAgg as _Canvas,
        NavigationToolbar2QT as _Toolbar,
    )
    _HAS_MPL = True
except ImportError:
    try:
        from matplotlib.figure import Figure  # type: ignore[no-redef]
        from matplotlib.backends.backend_qt5agg import (  # type: ignore[no-redef]
            FigureCanvasQTAgg as _Canvas,
            NavigationToolbar2QT as _Toolbar,
        )
        _HAS_MPL = True
    except ImportError:
        Figure = None  # type: ignore[assignment]
        _Canvas = None  # type: ignore[assignment]
        _Toolbar = None  # type: ignore[assignment]
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


def _high_low_point(
    prev_pvi, vc: VertCurve, next_pvi,
) -> tuple[float, float, bool] | None:
    """Crest / sag inside a VertCurve, or ``None`` when the extreme is outside.

    The parabola is ``y(s) = y_bvc + g_back·s + (g_ahead − g_back)/(2L)·s²``;
    its slope vanishes at ``s* = −g_back · L / (g_ahead − g_back)``. Returns
    ``(station, elevation, is_crest)`` when ``0 < s* < L``.
    """
    L = vc.length
    if L <= 0:
        return None
    dx_back = vc.station - prev_pvi.station
    dx_ahead = next_pvi.station - vc.station
    if dx_back <= 0 or dx_ahead <= 0:
        return None
    g_back = (vc.elev - prev_pvi.elev) / dx_back
    g_ahead = (next_pvi.elev - vc.elev) / dx_ahead
    dg = g_ahead - g_back
    if abs(dg) < 1e-9:
        return None
    s_star = -g_back * L / dg
    if s_star <= 0 or s_star >= L:
        return None
    sta_bvc = vc.station - L / 2.0
    elev_bvc = vc.elev - (L / 2.0) * g_back
    sta = sta_bvc + s_star
    elev = elev_bvc + g_back * s_star + dg / (2.0 * L) * s_star * s_star
    return sta, elev, dg < 0


class Align2QgisProfileDock(QDockWidget):
    """Floating/dock widget showing curvature + vertical profile of an alignment."""

    alignmentSelected = pyqtSignal(str)  # emits the chosen alignment name

    def __init__(self, parent=None) -> None:
        super().__init__("Align2QGIS — Alignment Profile", parent)
        self._segments: list[_Segment] = []
        self._seg_starts: list[float] = []
        self._vert_stations: list[float] = []
        self._vert_elevs: list[float] = []
        # Raw PVI / VertCurve items (in display-station space) for the
        # elevation-plot annotations. Distinct from the densified
        # (station, elev) pairs above, which the slider tooltip uses.
        self._pvi_items: list[PVI | VertCurve] = []
        self._equations: list[StaEquation] = []
        self._station_range_cached: tuple[float, float] | None = None
        self._highlight_sta: float | None = None
        self._title: str = ""
        self._vert_exaggeration: float = 1.0
        # Y-axis limits captured on first draw so VE adjustments stay
        # anchored to the data extent, not the previous zoom level.
        self._vert_baseline_ylim: tuple[float, float] | None = None
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
            self.toolbar = _Toolbar(self.canvas, container)
            self.toolbar.setIconSize(self.toolbar.iconSize() * 0.85)
            layout.addWidget(self.toolbar)
            self.ax_curv, self.ax_vert = self.figure.subplots(
                2, 1, sharex=True, gridspec_kw={"height_ratios": [1, 1]}
            )
            layout.addWidget(self.canvas)
            # Mouse-wheel zoom in either subplot, anchored at the cursor.
            self.canvas.mpl_connect("scroll_event", self._on_scroll)
        else:
            self.figure = None
            self.canvas = None
            self.toolbar = None
            self.ax_curv = None
            self.ax_vert = None
            layout.addWidget(
                QLabel(
                    "matplotlib not available — install it via OSGeo4W / pip "
                    "to enable the plots. Text panel below still works."
                )
            )

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("V exag:"))
        self.ve_combo = QComboBox()
        for ve in _VE_CHOICES:
            self.ve_combo.addItem(f"{ve:g}×", ve)
        self.ve_combo.setEnabled(_HAS_MPL)
        self.ve_combo.currentIndexChanged.connect(self._on_ve_changed)
        controls_row.addWidget(self.ve_combo)
        controls_row.addSpacing(12)
        self.station_label = QLabel("Station: —")
        self.station_label.setMinimumWidth(140)
        controls_row.addWidget(self.station_label)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, _SLIDER_STEPS)
        self.slider.setValue(0)
        self.slider.setEnabled(False)
        self.slider.valueChanged.connect(self._on_slider_changed)
        controls_row.addWidget(self.slider, 1)
        layout.addLayout(controls_row)

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
        vert_profile_layer: QgsVectorLayer | None = None,
        alignment_name: str = "",
    ) -> None:
        """Populate from the canonical ``Segments`` and ``VerticalProfile`` layers.

        The Segments layer is filtered to ``alignment_name`` for the
        curvature plot. PVI items and ``<StaEquation>`` discontinuities
        come from the parsed :class:`Alignment` behind the Segments
        layer (resolved via :func:`alignments_by_name`); the
        ``vert_profile_layer`` is consulted only as a fallback when the
        source LandXML can't be reached. The resulting items are
        densified via :func:`profile_samples` so the elevation plot is
        faithful to the parabolic vertical-curve math.
        """
        self._segments = []
        self._title = alignment_name
        if segments_layer is not None and segments_layer.isValid():
            for feat in segments_layer.getFeatures():
                try:
                    if alignment_name and str(feat[FIELD_ALIGNMENT]) != alignment_name:
                        continue
                    seg = _Segment(
                        kind=str(feat[FIELD_KIND]),
                        sta_start=_safe_float(feat[FIELD_STA_START]),
                        sta_end=_safe_float(feat[FIELD_STA_END]),
                        curvature_start=_safe_float(feat[FIELD_CURVATURE_START]),
                        curvature_end=_safe_float(feat[FIELD_CURVATURE_END]),
                        radius_start=_optional_float(feat[FIELD_RADIUS_START]),
                    )
                except KeyError:
                    continue
                self._segments.append(seg)
            self._segments.sort(key=lambda s: s.sta_start)
        self._seg_starts = [s.sta_start for s in self._segments]

        # Prefer the parsed Alignment object — it carries the raw
        # PVI/VertCurve items and StaEquation list directly, avoiding a
        # round-trip through the rendered vertical-profile layer.
        alignment_obj = self._resolve_alignment(segments_layer, alignment_name)
        if alignment_obj is not None:
            self._equations = list(alignment_obj.equations)
            self._pvi_items = self._pvi_items_from_alignment(alignment_obj)
        else:
            self._equations = []
            self._pvi_items = self._extract_pvi_items(vert_profile_layer, alignment_name)
        pairs = profile_samples(
            Profile(name="", alignments=[ProfAlign(name="", elements=list(self._pvi_items))])
        ) if self._pvi_items else []
        self._vert_stations = [s for s, _ in pairs]
        self._vert_elevs = [e for _, e in pairs]

        self._station_range_cached = self._compute_station_range()
        self._highlight_sta = None
        self._vert_baseline_ylim = None  # recompute on next draw for the new data
        self._sync_slider_range()
        self._redraw_static()
        self._update_highlight()

    @staticmethod
    def _extract_pvi_items(
        layer: QgsVectorLayer | None, alignment_name: str,
    ) -> list[PVI | VertCurve]:
        """Read PVI rows from the vertical-profile layer and rebuild the items list."""
        if layer is None or not layer.isValid():
            return []
        items: list[PVI | VertCurve] = []
        for feat in layer.getFeatures():
            try:
                if alignment_name and str(feat[FIELD_ALIGNMENT]) != alignment_name:
                    continue
                sta = _safe_float(feat[FIELD_STATION])
                elev = _safe_float(feat[FIELD_ELEVATION])
                vc_len = _safe_float(feat[FIELD_VC_LENGTH])
                kind = str(feat[FIELD_KIND] or "pvi")
            except KeyError:
                continue
            if kind == "pvi" or vc_len <= 0:
                items.append(PVI(station=sta, elev=elev))
            else:
                items.append(VertCurve(station=sta, elev=elev, length=vc_len))
        items.sort(key=lambda x: x.station)
        return items

    @staticmethod
    def _resolve_alignment(
        segments_layer: QgsVectorLayer | None, alignment_name: str,
    ):
        """Return the parsed :class:`Alignment` for ``alignment_name``, or ``None``."""
        if segments_layer is None or not segments_layer.isValid() or not alignment_name:
            return None
        return alignments_by_name(segments_layer).get(alignment_name)

    @staticmethod
    def _pvi_items_from_alignment(alignment) -> list[PVI | VertCurve]:
        """Flatten every ``<ProfAlign>``'s elements into one station-sorted list."""
        profile = getattr(alignment, "profile", None)
        if profile is None:
            return []
        items: list[PVI | VertCurve] = []
        for prof_align in profile.alignments:
            items.extend(prof_align.elements)
        items.sort(key=lambda x: x.station)
        return items

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
        self._pvi_items = []
        self._equations = []
        self._station_range_cached = None
        self._highlight_sta = None
        self._title = ""
        self._vert_baseline_ylim = None
        self._sync_slider_range()
        self._redraw_static()
        self._update_highlight()

    def set_alignments_available(self, items: list[tuple[str, str]]) -> None:
        """Refresh the alignment-picker combo. ``items`` is ``[(alignment_name, label), …]``.

        Signals are blocked while we repopulate so we don't trigger a
        spurious ``alignmentSelected`` when the plugin pushes a new list.
        """
        self.alignment_combo.blockSignals(True)
        current = self.alignment_combo.currentData()
        self.alignment_combo.clear()
        for alignment_name, label in items:
            self.alignment_combo.addItem(label, alignment_name)
        if current is not None:
            for i in range(self.alignment_combo.count()):
                if self.alignment_combo.itemData(i) == current:
                    self.alignment_combo.setCurrentIndex(i)
                    break
        self.alignment_combo.blockSignals(False)

    def select_alignment(self, alignment_name: str) -> None:
        """Programmatically set the combo without re-emitting the signal."""
        for i in range(self.alignment_combo.count()):
            if self.alignment_combo.itemData(i) == alignment_name:
                self.alignment_combo.blockSignals(True)
                self.alignment_combo.setCurrentIndex(i)
                self.alignment_combo.blockSignals(False)
                return

    def _on_combo_changed(self, index: int) -> None:
        if index < 0:
            return
        name = self.alignment_combo.itemData(index)
        if name:
            self.alignmentSelected.emit(name)

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
        sr = self._station_range_cached
        if sr is None:
            self.slider.setEnabled(False)
            return
        self.slider.setEnabled(True)

    def _set_slider_to_station(self, station: float) -> None:
        sr = self._station_range_cached
        if sr is None:
            return
        lo, hi = sr
        if hi <= lo:
            return
        t = (station - lo) / (hi - lo)
        val = max(0, min(_SLIDER_STEPS, int(round(t * _SLIDER_STEPS))))
        self.slider.blockSignals(True)
        self.slider.setValue(val)
        self.slider.blockSignals(False)

    def _station_from_slider(self, val: int) -> float | None:
        sr = self._station_range_cached
        if sr is None:
            return None
        lo, hi = sr
        t = val / _SLIDER_STEPS
        return lo + t * (hi - lo)

    def _on_slider_changed(self, val: int) -> None:
        sta = self._station_from_slider(val)
        if sta is None:
            return
        self._highlight_sta = sta
        self._update_highlight()

    # ------------------------------------------------------------------
    # Vertical exaggeration / wheel zoom
    # ------------------------------------------------------------------
    def _on_ve_changed(self, index: int) -> None:
        if index < 0:
            return
        ve = self.ve_combo.itemData(index)
        if ve is None:
            return
        self._vert_exaggeration = float(ve)
        self._apply_vert_exaggeration()
        if self.canvas is not None:
            self.canvas.draw_idle()

    def _apply_vert_exaggeration(self) -> None:
        """Resize the elevation y-axis around its midpoint by ``1 / VE``.

        "1×" here means the autoscale-default range fits the data with a
        small margin; higher VE values zoom into the elevation axis so
        small grade changes become visible on rail-style alignments where
        the elevation variation is hundreds of times smaller than the
        plan length.
        """
        if self.ax_vert is None or self._vert_baseline_ylim is None:
            return
        lo, hi = self._vert_baseline_ylim
        mid = 0.5 * (lo + hi)
        half = 0.5 * (hi - lo) / max(self._vert_exaggeration, 1e-6)
        self.ax_vert.set_ylim(mid - half, mid + half)

    def _on_scroll(self, event) -> None:
        """Mouse-wheel zoom anchored at the cursor in whichever subplot is active."""
        if event.inaxes is None or self.canvas is None:
            return
        ax = event.inaxes
        scale = 1.0 / 1.2 if event.button == "up" else 1.2
        xdata = event.xdata
        ydata = event.ydata
        if xdata is None or ydata is None:
            return
        xlo, xhi = ax.get_xlim()
        ylo, yhi = ax.get_ylim()
        new_x_range = (xhi - xlo) * scale
        new_y_range = (yhi - ylo) * scale
        relx = (xhi - xdata) / (xhi - xlo) if xhi > xlo else 0.5
        rely = (yhi - ydata) / (yhi - ylo) if yhi > ylo else 0.5
        ax.set_xlim(xdata - new_x_range * (1 - relx), xdata + new_x_range * relx)
        ax.set_ylim(ydata - new_y_range * (1 - rely), ydata + new_y_range * rely)
        self.canvas.draw_idle()

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
        # Capture the autoscaled y-range so the VE control has a stable
        # baseline that doesn't drift with manual zoom.
        if self._vert_stations:
            self._vert_baseline_ylim = self.ax_vert.get_ylim()
            self._apply_vert_exaggeration()
        else:
            self._vert_baseline_ylim = None
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
        if not self._vert_stations:
            ax.text(
                0.5, 0.5, "No vertical profile in source LandXML",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=8, color="#888",
            )
            ax.set_yticks([])
            ax.grid(False)
            ax.set_xlabel("Station (m)")
            return
        # Samples are densified inside vertical curves (parabolic evaluation
        # in landxml_parser.profile_samples), so a plain line plot is
        # faithful — no scatter dots needed.
        ax.plot(
            self._vert_stations,
            self._vert_elevs,
            color=_COLOR_PROFILE,
            linewidth=1.6,
        )
        ax.set_ylabel("Elevation (m)")
        ax.grid(True, alpha=0.25)
        ax.set_xlabel("Station (m)")
        self._draw_pvi_annotations(ax)
        self._draw_equation_marks(ax)

    def _draw_pvi_annotations(self, ax) -> None:
        """Static annotations on the elevation plot.

        Adds four layers, each tied to one pass over ``_pvi_items``:
        PVI dots labelled with (station, elev), grade-percent labels
        centred on each tangent run, ``L = / R =`` callouts under every
        VertCurve PVI, and crest / sag diamonds at the parabola extremum
        when it falls strictly inside the curve. The PVI dot sits at the
        LandXML vertex elevation, not on the parabola — the offset is
        useful for surveyors comparing the design vertex to the smoothed
        through-curve elevation.
        """
        items = self._pvi_items
        if not items:
            return
        for it in items:
            ax.plot(it.station, it.elev, "o", markersize=4, color=_COLOR_PROFILE,
                    markeredgecolor="white", markeredgewidth=0.6, zorder=4)
            ax.annotate(
                f"{it.station:,.0f}\n{it.elev:.2f}",
                xy=(it.station, it.elev),
                xytext=(0, 8), textcoords="offset points",
                ha="center", va="bottom", fontsize=7, color=_COLOR_PROFILE,
                zorder=4,
            )
        for a, b in zip(items, items[1:]):
            dx = b.station - a.station
            if dx <= 0:
                continue
            grade_pct = (b.elev - a.elev) / dx * 100.0
            mid_sta = 0.5 * (a.station + b.station)
            mid_elev = 0.5 * (a.elev + b.elev)
            ax.annotate(
                f"{grade_pct:+.2f} %",
                xy=(mid_sta, mid_elev),
                xytext=(0, -10), textcoords="offset points",
                ha="center", va="top", fontsize=7, color="#444",
                bbox=dict(boxstyle="round,pad=0.15", facecolor="white",
                          edgecolor="none", alpha=0.7),
                zorder=3,
            )
        for it in items:
            if not _is_real_vertcurve(it):
                continue
            label = f"L = {it.length:g} m"
            if it.radius is not None and it.radius > 0:
                label += f"\nR = {it.radius:g} m"
            ax.annotate(
                label,
                xy=(it.station, it.elev),
                xytext=(0, -22), textcoords="offset points",
                ha="center", va="top", fontsize=7, color=_COLOR_NEUTRAL,
                zorder=3,
            )
        for i, it in enumerate(items):
            if not _is_real_vertcurve(it):
                continue
            if i == 0 or i == len(items) - 1:
                continue
            extreme = _high_low_point(items[i - 1], it, items[i + 1])
            if extreme is None:
                continue
            sta, elev, is_crest = extreme
            color = _COLOR_CREST if is_crest else _COLOR_SAG
            ax.plot(sta, elev, marker="D", markersize=5, color=color,
                    markeredgecolor="white", markeredgewidth=0.6, zorder=5)
            ax.annotate(
                f"{'crest' if is_crest else 'sag'}\n{sta:,.0f} / {elev:.2f}",
                xy=(sta, elev),
                xytext=(6, 0), textcoords="offset points",
                ha="left", va="center", fontsize=7, color=color,
                zorder=5,
            )

    def _draw_equation_marks(self, ax) -> None:
        """Dashed vertical + top-axis label at each ``<StaEquation>`` point."""
        if not self._equations:
            return
        for eq in self._equations:
            ax.axvline(eq.sta_back, color=_COLOR_GUIDE, linestyle="--",
                       linewidth=0.8, alpha=0.7, zorder=2)
            ax.annotate(
                f"{eq.sta_back:,.0f} → {eq.sta_ahead:,.0f}",
                xy=(eq.sta_back, 1.0), xycoords=("data", "axes fraction"),
                xytext=(2, -2), textcoords="offset points",
                ha="left", va="top", fontsize=7, color=_COLOR_NEUTRAL,
                rotation=90, zorder=2,
            )

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
            return s.kind, s.curvature_start
        t = (station - s.sta_start) / length
        k = s.curvature_start + (s.curvature_end - s.curvature_start) * t
        return s.kind, k
