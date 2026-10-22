from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Translator
from gui.qt_optionals import maybe_none


class PresetManagerDialog(QDialog):
    def __init__(
        self,
        tr: Translator,
        preset_names: list[str],
        default_preset_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = tr
        self.default_preset_name = default_preset_name
        self.selected_action: dict[str, str] | None = None
        self._preset_names: list[str] = []

        self._build_ui()
        self.set_preset_names(preset_names, default_preset_name)
        self.apply_translations(tr)

    def _build_ui(self) -> None:
        self.resize(520, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.info_label = QLabel()
        self.status_label = QLabel()
        self.status_label.setObjectName("setupSummary")
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
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self.load_button.clicked.connect(self._request_load)
        self.save_current_button.clicked.connect(self._request_save)
        self.delete_button.clicked.connect(self._request_delete)
        self.default_button.clicked.connect(self._request_set_default)
        self.close_button.clicked.connect(self.reject)
        self.list_widget.currentItemChanged.connect(lambda *_args: self._update_button_state())
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._request_load())

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.presets"))
        self.load_button.setText(self.translator.t("gui.button.load_preset"))
        self.save_current_button.setText(self.translator.t("gui.button.save_current_as_preset"))
        self.delete_button.setText(self.translator.t("gui.button.delete_preset"))
        self.default_button.setText(self.translator.t("gui.button.set_default_preset"))
        self.close_button.setText(self.translator.t("gui.button.close"))
        self._update_info_label()
        self._update_button_state()

    def set_preset_names(self, preset_names: list[str], default_preset_name: str) -> None:
        previous = self._selected_preset_name()
        self._preset_names = list(preset_names)
        self.default_preset_name = default_preset_name
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        marker = self.translator.t("gui.label.default_preset_marker")
        for name in preset_names:
            label = f"{name} ({marker})" if name == default_preset_name else name
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, name)
            self.list_widget.addItem(item)
        self.list_widget.blockSignals(False)
        row = self._row_for_name(previous)
        if row < 0 and preset_names:
            row = 0
        if row >= 0:
            self.list_widget.setCurrentRow(row)
        self._update_info_label()
        self._update_button_state()

    def _row_for_name(self, name: str) -> int:
        for row in range(self.list_widget.count()):
            item = maybe_none(self.list_widget.item(row))
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == name:
                return row
        return -1

    def _selected_preset_name(self) -> str:
        item = maybe_none(self.list_widget.currentItem())
        if item is None:
            return ""
        name = item.data(Qt.ItemDataRole.UserRole)
        return name if isinstance(name, str) else ""

    def _update_info_label(self) -> None:
        default_text = self.default_preset_name or self.translator.t("gui.label.none")
        self.info_label.setText(self.translator.t("gui.label.default_preset", name=default_text))

    def _update_button_state(self) -> None:
        has_selection = bool(self._selected_preset_name())
        for button in (self.load_button, self.delete_button, self.default_button):
            button.setEnabled(has_selection)
        self.save_current_button.setEnabled(True)

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
        name = name.strip()
        if name in self._preset_names:
            answer = QMessageBox.question(
                self,
                self.translator.t("gui.message.warning"),
                self.translator.t("gui.message.overwrite_preset_confirm", name=name),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.selected_action = {"action": "save", "name": name}
        self.accept()

    def _request_delete(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        answer = QMessageBox.question(
            self,
            self.translator.t("gui.message.warning"),
            self.translator.t("gui.message.delete_preset_confirm", name=name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.selected_action = {"action": "delete", "name": name}
        self.accept()

    def _request_set_default(self) -> None:
        name = self._selected_preset_name()
        if not name:
            return
        self.selected_action = {"action": "set_default", "name": name}
        self.accept()
