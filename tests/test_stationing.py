"""Chainage placement tests (no QGIS required)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from plugin.stationing import chainage_points, format_station  # noqa: E402


def _stations_only(rows):
    return [round(r[0], 6) for r in rows]


def test_round_stations_from_zero():
    line = [(0.0, 0.0), (100.0, 0.0)]
    rows = chainage_points(line, sta_start=0.0, interval=25.0)
    # endpoints + interior 25, 50, 75 → start(0), 25, 50, 75, end(100)
    assert _stations_only(rows) == [0.0, 25.0, 50.0, 75.0, 100.0]
    # x positions follow the line exactly
    xs = [round(r[1], 6) for r in rows]
    assert xs == [0.0, 25.0, 50.0, 75.0, 100.0]


def test_first_round_station_after_offset_start():
    # sta_start=23.0, interval=50 → first round station is 50 (absolute multiple)
    line = [(0.0, 0.0), (200.0, 0.0)]
    rows = chainage_points(line, sta_start=23.0, interval=50.0)
    assert _stations_only(rows) == [23.0, 50.0, 100.0, 150.0, 200.0, 223.0]


def test_interval_zero_returns_empty():
    line = [(0.0, 0.0), (100.0, 0.0)]
    assert chainage_points(line, 0.0, 0.0) == []


def test_endpoint_dedup_when_start_on_grid():
    # sta_start=0 already on the 50-grid; we must not emit station 0 twice.
    line = [(0.0, 0.0), (100.0, 0.0)]
    rows = chainage_points(line, sta_start=0.0, interval=50.0)
    assert _stations_only(rows) == [0.0, 50.0, 100.0]


def test_station_on_kinked_polyline():
    # L-shape: 0→100 east, 100→100 then north 50. Length 150.
    line = [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0)]
    rows = chainage_points(line, sta_start=0.0, interval=50.0)
    assert _stations_only(rows) == [0.0, 50.0, 100.0, 150.0]
    # Station 100 sits at the corner; station 150 at the L's far end.
    xs_ys = [(round(r[1], 6), round(r[2], 6)) for r in rows]
    assert xs_ys == [(0.0, 0.0), (50.0, 0.0), (100.0, 0.0), (100.0, 50.0)]


def test_bearing_east_pointing_line_is_zero():
    line = [(0.0, 0.0), (100.0, 0.0)]
    rows = chainage_points(line, sta_start=0.0, interval=50.0)
    bearings = [round(r[3], 3) for r in rows]
    assert bearings == [0.0, 0.0, 0.0]


def test_bearing_diagonal_line_is_45():
    line = [(0.0, 0.0), (100.0, 100.0)]
    rows = chainage_points(line, sta_start=0.0, interval=50.0)
    for r in rows:
        assert abs(r[3] - 45.0) < 1e-3


def test_bearing_westbound_flipped_upright():
    # Raw atan2 for west is ±180°; upright_bearing folds that to 0 so labels
    # don't render upside-down on a westbound alignment.
    line = [(100.0, 0.0), (0.0, 0.0)]
    rows = chainage_points(line, sta_start=0.0, interval=50.0)
    bearings = [round(r[3], 3) for r in rows]
    assert bearings == [0.0, 0.0, 0.0]


def test_format_station_rail_convention():
    assert format_station(0.0) == "0+000.00"
    assert format_station(50.0) == "0+050.00"
    assert format_station(1250.0) == "1+250.00"
    assert format_station(13441.49) == "13+441.49"
