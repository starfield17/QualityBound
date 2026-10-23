from __future__ import annotations

import argparse
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.config import app_icon_path, app_root, ensure_runtime_layout
from core.i18n import LANGUAGES
from gui.gui_mainwindow import MainWindow


def run_gui(argv: list[str] | None = None) -> int:
    ensure_runtime_layout()
    parser = argparse.ArgumentParser(description="QualityBound GUI")
    parser.add_argument(
        "--lang",
        choices=[info.code for info in LANGUAGES],
        help="Display language",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1] + (argv or []))
    icon_path = app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(repo_root=app_root(), language=args.lang)
    window.show()
    return app.exec()
