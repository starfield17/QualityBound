from __future__ import annotations

import argparse
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.config import app_icon_path, app_root, bundle_root, ensure_runtime_layout
from core.i18n import TranslationCatalog
from gui.gui_mainwindow import MainWindow


def _default_catalog() -> TranslationCatalog:
    return TranslationCatalog(
        bundle_dir=bundle_root() / "config" / "i18n",
        translations_dir=app_root() / "translations",
    )


def run_gui(argv: list[str] | None = None) -> int:
    ensure_runtime_layout()
    catalog = _default_catalog()
    for diagnostic in catalog.diagnostics():
        print(
            f"i18n warning ({diagnostic.locale}): {diagnostic.message}",
            file=sys.stderr,
        )
    parser = argparse.ArgumentParser(description="QualityBound GUI")
    parser.add_argument(
        "--lang",
        choices=[info.code for info in catalog.languages()],
        help="Language pack to load",
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1] + (argv or []))
    icon_path = app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(repo_root=app_root(), language=args.lang, catalog=catalog)
    window.show()
    return app.exec()
