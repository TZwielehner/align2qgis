"""Shared pytest setup for align2qgis tests.

The plugin modules import ``qgis.PyQt`` lazily; pure-geometry tests
(:mod:`test_geometry`, :mod:`test_stationing`, :mod:`test_dimensions`)
don't actually touch Qt at import time and run on any Python.

The smoke-import test below proves the plugin package can be imported
against PyQt6 — protects against the Qt5→Qt6 enum regressions coming
with QGIS 4. Locally PyQt6 is usually absent and this test skips; CI
installs it explicitly so the regression check still runs there.
"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def pyqt6_available() -> bool:
    pytest.importorskip("PyQt6")
    return True
