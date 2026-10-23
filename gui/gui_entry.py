from __future__ import annotations

import argparse
import sys

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from core.config import app_icon_path, app_root, ensure_runtime_layout, load_app_config
from core.i18n import LANGUAGES, Text, Translator
from gui.gui_mainwindow import MainWindow


def run_gui(argv: list[str] | None = None) -> int:
    ensure_runtime_layout()
    language_parser = argparse.ArgumentParser(add_help=False)
    language_parser.add_argument("--lang", choices=[info.code for info in LANGUAGES])
    language_args, _ = language_parser.parse_known_args(argv)
    tr = Translator(language_args.lang or load_app_config().get("language", "en"))
    parser = argparse.ArgumentParser(description="QualityBound GUI", add_help=False)
    options = parser.add_argument_group(tr.t(Text(en='Options', zh_cn='选项', ja='オプション')))
    options.add_argument(
        "-h", "--help", action="help",
        help=tr.t(Text(en='Show this help message and exit', zh_cn='显示此帮助信息并退出', ja='このヘルプを表示して終了します')),
    )
    options.add_argument(
        "--lang",
        choices=[info.code for info in LANGUAGES],
        help=tr.t(Text(en='Display language', zh_cn='显示语言', ja='表示言語')),
    )
    args = parser.parse_args(argv)

    app = QApplication(sys.argv[:1] + (argv or []))
    icon_path = app_icon_path()
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
    window = MainWindow(repo_root=app_root(), language=args.lang)
    try:
        window.show()
        return app.exec()
    finally:
        # app.exit() and exceptions can bypass closeEvent. Join workers before
        # PySide destroys their QThreads during application/interpreter cleanup.
        window.shutdown()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
