from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QWidget

from core.i18n import Translator


class NotificationBanner(QFrame):
    """One compact, dismissible message with optional actions."""

    dismissed = Signal()

    def __init__(self, translator: Translator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.translator = translator
        self.setObjectName("banner")
        self.setProperty("severity", "info")
        self.setVisible(False)
        self._actions: list[QPushButton] = []
        self._action_specs: list[tuple[str, Callable[[], None]]] = []

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(8)
        self.text_label = QLabel()
        self.text_label.setObjectName("bannerText")
        self.text_label.setWordWrap(True)
        layout.addWidget(self.text_label, 1)
        self.actions_layout = QHBoxLayout()
        self.actions_layout.setSpacing(6)
        layout.addLayout(self.actions_layout)
        self.dismiss_button = QToolButton()
        self.dismiss_button.setText("×")
        self.dismiss_button.setAutoRaise(True)
        self.dismiss_button.setToolTip("")
        self.dismiss_button.clicked.connect(self._dismiss)
        layout.addWidget(self.dismiss_button)

    def apply_translations(self, translator: Translator) -> None:
        self.translator = translator
        self.dismiss_button.setToolTip(translator.t("gui.banner.dismiss"))

    def show_message(
        self,
        text: str,
        *,
        severity: str = "info",
        actions: list[tuple[str, Callable[[], None]]] | None = None,
        dismissible: bool = True,
        actions_enabled: bool = True,
    ) -> None:
        self.text_label.setText(text)
        if self.property("severity") != severity:
            self.setProperty("severity", severity)
            self.style().unpolish(self)
            self.style().polish(self)
        specs = actions or []
        if specs != self._action_specs:
            for button in self._actions:
                self.actions_layout.removeWidget(button)
                button.hide()
                button.deleteLater()
            self._actions = []
            self._action_specs = list(specs)
            for label, callback in specs:
                button = QPushButton(label)
                button.setAutoDefault(False)
                button.clicked.connect(callback)
                self.actions_layout.addWidget(button)
                self._actions.append(button)
        for button in self._actions:
            button.setEnabled(actions_enabled)
        self.dismiss_button.setVisible(dismissible)
        self.setVisible(True)

    def hide_message(self) -> None:
        self.setVisible(False)

    def _dismiss(self) -> None:
        self.hide_message()
        self.dismissed.emit()
