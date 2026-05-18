"""Module-level constants shared across the plugin.

Kept in a tiny module so layer builders, styling, cache, and the main
plugin class can import them without depending on each other.
"""
from __future__ import annotations

PLUGIN_NAME = "Align2QGIS"

# Custom properties tagged onto layers and group nodes so Re-apply and the
# profile dock can resolve "which source LandXML did this come from?" days
# later. These survive project save/load.
PROP_SOURCE_PATH = "align2qgis/source_path"
PROP_CRS = "align2qgis/crs"

# Layer-name prefixes used inside GeoPackage + memory layer names. Kept as
# constants so the dock's "this is a Segments layer" / "this is a Stations
# layer" matching stays in lockstep with whatever the layer builders emit.
ALIGNMENT_LAYER_PREFIX = "Alignments"
SEGMENTS_LAYER_PREFIX = "Segments"
STATIONS_LAYER_PREFIX = "Stations"
DIMENSIONS_LAYER_PREFIX = "Dimensions"
