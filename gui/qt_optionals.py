"""One place to state which PySide6 accessors can return None despite their stubs.

PySide6 declares several Qt getters as non-nullable although Qt returns None when
there is nothing to return: ``QMenu.exec()`` when the menu is dismissed,
``QListWidget.currentItem()`` on an empty list, ``QAbstractItemView.model()``
before the first ``setModel()``, ``QApplication.style()`` without a QApplication,
``QGuiApplication.primaryScreen()`` on a headless display, and
``QStandardItemModel.item()``/``QHeaderView`` for a row or widget the model does
not hold. Their guards are load-bearing, so the values pass through this helper to
restore the ``None`` the generated signature omitted. Keeping the widening here
leaves the unnecessary-comparison rule active for code that really is non-optional.
"""

from __future__ import annotations

from typing import TypeVar

_T = TypeVar("_T")


def maybe_none(value: _T) -> _T | None:
    """Return the value unchanged, typed as the nullable result Qt produces."""
    return value
