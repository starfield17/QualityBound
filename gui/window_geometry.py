from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QGuiApplication


def clamped_window_size(
    desired_width: int,
    desired_height: int,
    *,
    minimum_width: int = 0,
    minimum_height: int = 0,
    screen_ratio: float = 0.92,
) -> QSize:
    width = max(1, desired_width)
    height = max(1, desired_height)
    min_width = max(0, minimum_width)
    min_height = max(0, minimum_height)

    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        max_width = max(1, int(available.width() * screen_ratio))
        max_height = max(1, int(available.height() * screen_ratio))
        min_width = min(min_width, max_width)
        min_height = min(min_height, max_height)
        width = min(width, max_width)
        height = min(height, max_height)

    return QSize(max(width, min_width), max(height, min_height))
