# align2qgis

QGIS 3 plugin: import LandXML horizontal alignments (lines, circular arcs,
clothoid spirals) as a memory line layer.

## What it handles

| LandXML element            | Method                                                       |
| -------------------------- | ------------------------------------------------------------ |
| `<Line>`                   | Straight segment, two points.                                |
| `<Curve rot="cw\|ccw">`    | Exact circular-arc math from `Start`, `Center`, `End`.       |
| `<Spiral spiType="clothoid">` | Numerical integration of θ(s) = k₀s + (k₁−k₀)s²/(2L). |
| Anything else              | Skipped with a warning in the QGIS log.                      |

Coordinates: LandXML stores points as `Northing Easting [Elevation]`.
The plugin swaps to QGIS axis order (x = Easting, y = Northing) in
`geometry_builder.ne_to_xy`. LandXML files do not embed a CRS, so the plugin
asks for one (`EPSG:25832` is offered by default; for AT/DE rail data this is
often `EPSG:31256`, `EPSG:31287`, or a project-specific Gauß-Krüger zone —
check the survey report).

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

1. Pick the `.xml` file (e.g. `RDB_TPH-740_4200_-_UE_FM_EP_N_Trassierung_001.xml`).
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

7 tests cover the parser, axis swap, both arc sweep directions, the
infinite-radius spiral edge case, and a Fresnel cross-check at the spiral
mid-point.

## Known limits

- No vertical alignment / profile yet — only `<CoordGeom>` is consumed.
- `<IrregularLine>`, `<Chain>`, station equations: not handled.
- The clothoid is integrated with a trapezoidal rule (~0.5 samples/m
  default). Discretization error grows with spiral length; pin endpoints
  is applied automatically so the polyline lands exactly on `<End>`.
- CRS must be supplied by the user; LandXML provides none.
