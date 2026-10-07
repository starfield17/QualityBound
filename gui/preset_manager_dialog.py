from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from core.i18n import Translator


class PresetManagerDialog(QDialog):
    def __init__(self, tr: Translator, preset_names: list[str], default_preset_name: str, parent=None) -> None:
        super().__init__(parent)
        self.translator = tr
        self.default_preset_name = default_preset_name
        self.selected_action: dict[str, str] | None = None

        self._build_ui()
        self.set_preset_names(preset_names, default_preset_name)
        self.apply_translations(tr)

    def _build_ui(self) -> None:
        self.resize(480, 420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.info_label = QLabel()
        self.list_widget = QListWidget()

        buttons = QHBoxLayout()
        self.load_button = QPushButton()
        self.save_current_button = QPushButton()
        self.delete_button = QPushButton()
        self.default_button = QPushButton()
        self.close_button = QPushButton()
        buttons.addWidget(self.load_button)
        buttons.addWidget(self.save_current_button)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(self.default_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)

        layout.addWidget(self.info_label)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(buttons)

        self.load_button.clicked.connect(self._request_load)
        self.save_current_button.clicked.connect(self._request_save)
        self.delete_button.clicked.connect(self._request_delete)
        self.default_button.clicked.connect(self._request_set_default)
        self.close_button.clicked.connect(self.reject)

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.presets"))
        self.load_button.setText(self.translator.t("gui.button.load_preset"))
        self.save_current_button.setText(self.translator.t("gui.button.save_current_as_preset"))
        self.delete_button.setText(self.translator.t("gui.button.delete_preset"))
        self.default_button.setText(self.translator.t("gui.button.set_default_preset"))
        self.close_button.setText(self.translator.t("gui.button.close"))
        self._update_info_label()

    def set_preset_names(self, preset_names: list[str], default_preset_name: str) -> None:
        self.default_preset_name = default_preset_name
        self.list_widget.clear()
        for name in preset_names:
            label = name
            if name == default_preset_name:
                label = f"{name} ({self.translator.t('gui.label.default_preset_marker')})"
            self.list_widget.addItem(label)
        if preset_names:
            self.list_widget.setCurrentRow(0)
        self._update_info_label()

    def _selected_preset_name(self) -> str:
        item = self.list_widget.currentItem()
        if item is None:
            return ""
        text = item.text()
        suffix = f" ({self.translator.t('gui.label.default_preset_marker')})"
        if text.endswith(suffix):
            return text[: -len(suffix)]
        return text

    def _update_info_label(self) -> None:
        default_text = self.default_preset_name or self.translator.t("gui.label.none")
        self.info_label.setText(self.translator.t("gui.label.default_preset", name=default_text))

    def _request_load(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        self.selected_action = {"action": "load", "name": name}
        self.accept()

    def _request_save(self) -> None:
        name, ok = QInputDialog.getText(
            self,
            self.translator.t("gui.button.save_current_as_preset"),
            self.translator.t("gui.message.enter_preset_name"),
        )
        if not ok or not name.strip():
            return
        self.selected_action = {"action": "save", "name": name.strip()}
        self.accept()

    def _request_delete(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        self.selected_action = {"action": "delete", "name": name}
        self.accept()

    def _request_set_default(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        self.selected_action = {"action": "set_default", "name": name}
        self.accept()
