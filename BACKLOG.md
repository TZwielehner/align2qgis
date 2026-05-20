# Backlog / deferred cleanups

Findings from `/simplify` passes that were intentionally deferred — not bugs,
but pickup-able quality wins for a future sweep. Add new entries here rather
than scattering TODO comments through the source.

## Geometry / parser

- **`_grade_between` (landxml_parser.py) duplicates the grade math in
  `profile_samples`.** Both functions compute the back/ahead tangent
  slope at a profile item from neighbour stations. Unifying them would
  pull `_grade_between` (or an equivalent) into `profile_samples`'s
  inner loop. Skipped during the 6+1 cleanup because the math is short
  and well-isolated; sharing it touches a hot path that's already
  tested against the densified ground truth.

- **Spiral projection cost in `_project_to_spiral_seg`.** Each call
  evaluates `_spiral_pose` ~74 times (24 coarse sweeps + ~50 golden-
  section steps), and each pose call integrates the clothoid from 0.
  Acceptable for typical batches (10–100 input points × handful of
  alignments). Memoize per-segment if batch projection becomes a real
  bottleneck — e.g. cache a dense (s, x, y) lookup table on the
  alignment when the projection algorithm runs.

## Layers / processing

- **No constants for layer geometry-type strings.** `"CompoundCurve"`,
  `"CompoundCurveZ"`, `"Point"`, `"PointZ"` appear at the
  `_new_memory_layer` call sites. Skipped — these are QGIS WKT type
  tokens, not project concepts, and adding constants for four strings
  used at four sites is over-engineering. Reconsider only if a new
  layer type joins them.

- **Projection helpers return a 5-tuple instead of a dataclass.**
  `(s_local, signed_offset, foot_x, foot_y, residual)` is consistent
  across `_project_to_line_piece` / `_project_to_curve_seg` /
  `_project_to_spiral_seg`. A `ProjectionStep` dataclass would make
  call sites self-documenting. Skipped because the tuple shape is
  private to `alignment_project_point`; introduce the dataclass if a
  new caller needs to interpret these fields directly.

## Profile dock

- **Layer field-name constants.** `profile_dock.py` reads
  ``feat["alignment"]``, ``feat["sta_start"]``, ``feat["station"]``,
  ``feat["vc_length"]``, etc. as raw strings; the same names are
  declared inside ``layers.py``'s ``QgsField`` definitions. Centralising
  them in `constants.py` (or a new `field_names.py`) would let both
  the producers and consumers share one source of truth. Skipped —
  only ~10 strings used at one read site each, churn outweighs the
  win until a field name actually changes.

- **`_safe_float` / `_optional_float` to a shared utils module.**
  These tiny coercion helpers in `profile_dock.py` would fit naturally
  in a `plugin/_utils.py`. Skipped — no other module currently needs
  them, and introducing a utils module just for two functions invites
  bloat. Promote if a second caller appears.

- **`_KIND_COLORS` to `styling.py`.** Segment-kind palette
  (line/curve/spiral) is hard-coded inside the dock; `styling.py` owns
  label/marker styling for the same kinds. Unifying would centralise
  the palette but cross-couples the dock to QGIS-only styling. Skipped
  — the dock's matplotlib colour space is independent from QGIS
  symbol colours, so a separate constant is honest.

- **Split `_draw_pvi_annotations` into four methods.** Each section
  (PVI dots, grade labels, VC callouts, crest/sag) is tight enough
  that four single-call helpers would add friction without improving
  readability. Skipped; revisit if any section grows.

- **`_label_point` annotation helper.** Several `ax.annotate(...)`
  calls share boilerplate but the per-call params (`xytext`, `ha/va`,
  `bbox`, `color`, `zorder`) vary enough that the helper would have a
  long signature for marginal saving. Skipped.

- **`_high_low_point` ↔ `_grade_between` consolidation.**
  `_high_low_point` (`profile_dock.py`) recomputes back/ahead grades
  via inline divisions; `landxml_parser._grade_between` does the same.
  Signatures don't align (the dock function takes prev/vc/next
  objects, the parser helper takes a list + indices) — bridging them
  needs a wrapper that wouldn't save lines. Skipped.
