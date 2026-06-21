# Backlog / deferred cleanups

Findings from `/simplify` passes that were intentionally deferred — not bugs,
but pickup-able quality wins for a future sweep. Add new entries here rather
than scattering TODO comments through the source.

## Decided against (kept as a record so they aren't re-proposed)

- **`_KIND_COLORS` → `styling.py`.** The dock's segment-kind palette is
  matplotlib hex strings for an independent widget plot; `styling.py`
  speaks QGIS map symbology (`QColor` RGB tuples) and has no kind→colour
  map to unify with. Moving it would cross-couple two unrelated rendering
  systems and imply a shared palette that doesn't exist. Keeping the
  dock's palette local is the honest split.

- **`_high_low_point` ↔ `_grade_between` consolidation.**
  `_high_low_point` (`profile_dock.py`) computes back/ahead grades from
  prev/vc/next PVI objects and must bail out (`return None`) when a
  neighbour spacing is non-positive; `landxml_parser._grade_between`
  takes a list + indices and returns `0.0` in that case. The differing
  degenerate-case semantics mean a bridge would need a wrapper that
  saves no lines and obscures the early-exit.
