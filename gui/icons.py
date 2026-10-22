from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_ICONS: dict[str, str] = {
    "add_files": """
        <path d="M6 3h7l4 4v8"/>
        <path d="M13 3v5h5"/>
        <path d="M6 3H5a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h7"/>
        <path d="M18 15v6M15 18h6"/>
    """,
    "add_folder": """
        <path d="M3 7a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>
        <path d="M14 13v6M11 16h6"/>
    """,
    "add_to_queue": """
        <path d="M4 6h10M4 11h10M4 16h7"/>
        <path d="M18 12v8M14 16h8"/>
    """,
    "start": '<path d="M8 5.5 19 12 8 18.5Z" fill="{color}" stroke="{color}"/>',
    "pause": '<path d="M8 5v14M16 5v14" stroke-width="2.4"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="{color}" stroke="{color}"/>',
    "queue": """
        <path d="M9 6h11M9 12h11M9 18h11"/>
        <circle cx="5" cy="6" r="1.3" fill="{color}" stroke="none"/>
        <circle cx="5" cy="12" r="1.3" fill="{color}" stroke="none"/>
        <circle cx="5" cy="18" r="1.3" fill="{color}" stroke="none"/>
    """,
    "log": """
        <path d="M6 3h8l4 4v14H6Z"/>
        <path d="M14 3v5h5M9 12h6M9 16h6"/>
    """,
    "compare": """
        <rect x="3.5" y="5" width="7" height="14" rx="1.2"/>
        <rect x="13.5" y="5" width="7" height="14" rx="1.2"/>
        <path d="M8 12h8"/>
    """,
    "presets": """
        <path d="M6 3h12v18l-6-4-6 4Z"/>
        <path d="M9 8h6"/>
    """,
    "settings": """
        <path d="M4 7h10M18 7h2M4 17h2M10 17h10M4 12h5M13 12h7"/>
        <circle cx="16" cy="7" r="2"/>
        <circle cx="7" cy="12" r="2"/>
        <circle cx="15" cy="17" r="2"/>
    """,
    "about": """
        <circle cx="12" cy="12" r="8.5"/>
        <path d="M12 11v5"/>
        <circle cx="12" cy="8" r="0.9" fill="{color}" stroke="none"/>
    """,
    "open_folder": """
        <path d="M3 7a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z"/>
    """,
    "help": """
        <circle cx="12" cy="12" r="8.5"/>
        <path d="M9.8 9a2.3 2.3 0 1 1 3.3 2.1c-.7.4-1.1.9-1.1 1.7"/>
        <circle cx="12" cy="16.5" r="0.9" fill="{color}" stroke="none"/>
    """,
    "refresh": """
        <path d="M20 11a8 8 0 1 0-2.3 5.7"/>
        <path d="M20 4v7h-7"/>
    """,
    "search": """
        <circle cx="10.5" cy="10.5" r="6"/>
        <path d="m15 15 5 5"/>
    """,
    "close": '<path d="m6 6 12 12M18 6 6 18" stroke-width="2"/>',
}


def _render_pixmap(name: str, color: str, size: int) -> QPixmap:
    body = _ICONS[name].replace("{color}", color)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.7" '
        f'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        renderer.render(painter, QRectF(0, 0, size, size))
    finally:
        painter.end()
    return pixmap


def action_icon(name: str, color: str, disabled_color: str) -> QIcon:
    """Build a small multi-size icon from the internal line-icon set."""

    icon = QIcon()
    for size in (16, 20, 24, 32):
        icon.addPixmap(_render_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(_render_pixmap(name, disabled_color, size), QIcon.Mode.Disabled, QIcon.State.Off)
    return icon
