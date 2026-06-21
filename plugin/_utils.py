"""Small, dependency-free coercion helpers shared across the plugin.

Kept separate from feature-specific modules so any consumer (the profile
dock today, processing or layer code tomorrow) can pull them in without
importing a heavier module just for a float cast.
"""
from __future__ import annotations


def safe_float(value, default: float = 0.0) -> float:
    """Coerce ``value`` to ``float``, falling back to ``default``.

    Tolerates ``None`` and unparseable values (e.g. a NULL QVariant read off
    a feature attribute) by returning ``default`` instead of raising.
    """
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def optional_float(value) -> float | None:
    """Coerce ``value`` to ``float``, or ``None`` when it can't be parsed."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
