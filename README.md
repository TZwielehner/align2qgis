# align2qgis

QGIS 3 plugin: import LandXML horizontal alignments (lines, circular arcs,
clothoid spirals) as a native curved-geometry layer.

## What it handles

| LandXML element            | Method                                                       |
| -------------------------- | ------------------------------------------------------------ |
| `<Line>`                   | Straight segment, two points.                                |
| `<Curve rot="cw\|ccw">`    | Exact circular-arc math from `Start`, `Center`, `End`, emitted as a `CircularString` sub-curve. |
| `<Spiral spiType="clothoid">` | Numerical integration of θ(s) = k₀s + (k₁−k₀)s²/(2L), then discretized into a chain of circular arcs (per-arc chord error ≤ 1 cm by default). |
| Anything else              | Skipped with a warning in the QGIS log.                      |

Geometry: each alignment is emitted as a `CompoundCurve` of line +
circular-arc sub-curves, so QGIS' offset / buffer / length operations use
the analytic curve rather than a chord polyline. This matters when
deriving lane edges or parallel features at significant offsets — the
polyline approach produces visible faceting that the arc chain avoids.

Coordinates: LandXML stores points as `Northing Easting [Elevation]`.
The plugin swaps to QGIS axis order (x = Easting, y = Northing) in
`geometry_builder.ne_to_xy`. LandXML files do not embed a CRS, so the plugin
asks for one (`EPSG:25832` is offered by default; for AT/DE rail data this is
often `EPSG:31256`, `EPSG:31287`, or a project-specific Gauß-Krüger zone —
check the survey report).

## Compatibility

Targets QGIS 3.22+ and QGIS 4 (Qt6). The codebase uses the `qgis.PyQt`
shim, `QMetaType.Type` field types, and the `Qgis.*` enum forms
(`Qgis.MessageLevel`, `Qgis.MarkerShape`) so the same source works
against both PyQt5 and PyQt6 builds. `QgsCompoundCurve` instances are
wrapped via `QgsGeometry(cc.clone())` for unambiguous ownership transfer
under QGIS 4's stricter SIP bindings.

## Install in QGIS

1. Locate your QGIS plugin directory:
   - Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins`
   - Linux: `~/.local/share/QGIS/QGIS3/profiles/default/python/plugins`
2. Copy the `plugin/` directory there, renaming it to `align2qgis`:
   ```
   …/python/plugins/align2qgis/
       __init__.py
       align2qgis_plugin.py
       geometry_builder.py
       landxml_parser.py
       metadata.txt
   ```
3. Restart QGIS, open *Plugins → Manage and Install Plugins*, enable
   **Align2QGIS** (it is marked experimental — tick *Show experimental*).
4. Use *Vector → Align2QGIS → Import LandXML alignment…* or the toolbar icon.

## Use

1. Pick the `.xml` file (e.g. `Trassierung_001.xml`).
2. Confirm the CRS (e.g. `EPSG:31256`).
3. A memory layer `Alignments — <filename>` is added, one feature per
   `<Alignment>`. Attributes: `name`, `length_xml`, `length_geom`,
   `sta_start`, `n_segments`.

## CLI sanity-check (no QGIS required)

`tools/dump_alignment.py` parses a LandXML file and prints WKT for each
alignment so you can paste into QGIS via *Layer → Add Layer → Add Delimited
Text Layer* or load with any GIS tool:

```
python3 tools/dump_alignment.py path/to/file.xml > alignments.wkt
```

## Tests

```
python3 -m unittest discover -s tests
# or just:
python3 -c "import tests.test_geometry as t; [getattr(t,n)() for n in dir(t) if n.startswith('test_')]"
```

Tests cover the parser, axis swap, both arc sweep directions, the
infinite-radius spiral edge case, a Fresnel cross-check at the spiral
mid-point, the clothoid → arc-chain discretization (endpoint matching +
per-arc chord error within budget), and the chainage walker.

## Known limits

- `<IrregularLine>`, `<Chain>`, station equations: not handled.
- Clothoid arc-discretization is driven by `|dκ/ds|·h³/24 ≤ ε` with
  ε = 1 cm. The chain's outer endpoints are still pinned to the LandXML
  `<Start>` / `<End>` to absorb integration drift on long spirals — a
  geometrically inconsistent LandXML end will be honoured rather than
  corrected.
- CRS must be supplied by the user; LandXML provides none.
