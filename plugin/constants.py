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
VERTICAL_PROFILE_LAYER = "VerticalProfile"
CROSS_SECTIONS_LAYER = "CrossSections"
CROSS_SECTION_SURFACES_LAYER = "CrossSectionSurfaces"
CG_POINTS_LAYER = "CgPoints"

# In-memory only — interval chainage labels along the Alignments layer.
# Not persisted to the GeoPackage; rebuilt on every import.
CHAINAGE_LABEL_LAYER = "Chainage (interval)"

# Column that distinguishes rows from different LandXML imports inside the
# shared canonical tables. Re-import semantics delete-by-source then append.
SOURCE_FILE_FIELD = "source_file"

# Attribute names that cross the producer/consumer boundary: ``layers.py``
# declares them in QgsField schemas and ``profile_dock.py`` reads them back
# off the features. Centralised so a rename can't silently desync the two
# sides. Fields with no cross-module reader stay as literals at their single
# definition site — only the shared subset is promoted here.
FIELD_ALIGNMENT = "alignment"
FIELD_KIND = "kind"
FIELD_STA_START = "sta_start"
FIELD_STA_END = "sta_end"
FIELD_CURVATURE_START = "curvature_start"
FIELD_CURVATURE_END = "curvature_end"
FIELD_RADIUS_START = "radius_start"
FIELD_STATION = "station"
FIELD_ELEVATION = "elevation"
FIELD_VC_LENGTH = "vc_length"

# Every canonical layer name the plugin emits — the CRS-conflict probe and
# the per-import purge both iterate this. Single source of truth so they
# can't drift when new entity layers are added. Defined last so every
# referenced name is already bound at module load.
CANONICAL_LAYERS: tuple[str, ...] = (
    ALIGNMENT_LAYER_PREFIX, SEGMENTS_LAYER_PREFIX,
    STATIONS_LAYER_PREFIX, DIMENSIONS_LAYER_PREFIX,
    CHAINAGE_LABEL_LAYER, VERTICAL_PROFILE_LAYER, CROSS_SECTIONS_LAYER,
    CROSS_SECTION_SURFACES_LAYER, CG_POINTS_LAYER,
)
