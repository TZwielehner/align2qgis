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
