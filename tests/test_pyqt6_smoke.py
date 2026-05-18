"""PyQt6 import-smoke test — protects against Qt5→Qt6 enum regressions.

Locally this skips because PyQt6 isn't installed (QGIS ships its own
``qgis.PyQt`` shim). CI installs PyQt6 and the test runs — catching the
common QGIS 4 transition pitfalls like ``Qt.AlignCenter`` →
``Qt.AlignmentFlag.AlignCenter`` before they bite end users.
"""
from __future__ import annotations

import pytest

PyQt6 = pytest.importorskip("PyQt6")


def test_pyqt6_core_imports() -> None:
    from PyQt6.QtCore import Qt  # noqa: F401

    # Qt6-style fully-qualified enum access.
    assert Qt.AlignmentFlag.AlignCenter is not None
    assert Qt.DockWidgetArea.RightDockWidgetArea is not None


def test_pyqt6_widgets_import() -> None:
    # No QApplication needed — import-only check.
    # QAction moved to QtGui in Qt6; QDialog/QDockWidget stayed in QtWidgets.
    from PyQt6.QtGui import QAction  # noqa: F401
    from PyQt6.QtWidgets import QDialog, QDockWidget  # noqa: F401
