from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
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

from core.i18n import Text, Translator
from gui.qt_optionals import maybe_none
from gui.window_geometry import clamped_window_size


BUTTON_SAVE_CURRENT_AS_PRESET = Text(
    en='Save Current As…',
    zh_cn='当前设置另存为…',
    ja='現在の設定に名前を付けて保存…',
)

MESSAGE_WARNING = Text(
    en='Warning',
    zh_cn='警告',
    ja='警告',
)


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
        self.resize(clamped_window_size(560, 440))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.info_label = QLabel()
        self.info_label.setWordWrap(True)
        self.list_widget = QListWidget()

        buttons = QGridLayout()
        self.load_button = QPushButton()
        self.save_current_button = QPushButton()
        self.delete_button = QPushButton()
        self.default_button = QPushButton()
        self.close_button = QPushButton()
        buttons.addWidget(self.load_button, 0, 0)
        buttons.addWidget(self.save_current_button, 0, 1)
        buttons.addWidget(self.delete_button, 1, 0)
        buttons.addWidget(self.default_button, 1, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        footer.addWidget(self.close_button)

        layout.addWidget(self.info_label)
        layout.addWidget(self.list_widget, 1)
        layout.addLayout(buttons)
        layout.addLayout(footer)

        self.load_button.clicked.connect(self._request_load)
        self.save_current_button.clicked.connect(self._request_save)
        self.delete_button.clicked.connect(self._request_delete)
        self.default_button.clicked.connect(self._request_set_default)
        self.close_button.clicked.connect(self.reject)
        self.list_widget.currentItemChanged.connect(self._on_current_item_changed)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)

    def _on_current_item_changed(
        self,
        _current: QListWidgetItem | None,
        _previous: QListWidgetItem | None,
    ) -> None:
        self._update_button_state()

    def _on_item_double_clicked(self, _item: QListWidgetItem) -> None:
        self._request_load()

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t(Text(en='Preset Manager', zh_cn='预设管理', ja='プリセットマネージャー')))
        self.load_button.setText(self.translator.t(Text(en='Load', zh_cn='加载', ja='読み込み')))
        self.save_current_button.setText(self.translator.t(BUTTON_SAVE_CURRENT_AS_PRESET))
        self.delete_button.setText(self.translator.t(Text(en='Delete Preset', zh_cn='删除预设', ja='プリセットを削除')))
        self.default_button.setText(self.translator.t(Text(en='Set Default', zh_cn='设为默认', ja='デフォルトに設定')))
        self.close_button.setText(self.translator.t(Text(en='Close', zh_cn='关闭', ja='閉じる')))
        self._update_info_label()
        self._update_button_state()

    def set_preset_names(self, preset_names: list[str], default_preset_name: str) -> None:
        previous = self._selected_preset_name()
        self._preset_names = list(preset_names)
        self.default_preset_name = default_preset_name
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        marker = self.translator.t(Text(en='default', zh_cn='默认', ja='デフォルト'))
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
        default_text = self.default_preset_name or self.translator.t(Text(en='none', zh_cn='无', ja='なし'))
        self.info_label.setText(self.translator.t(Text(en='Default preset: {name}', zh_cn='默认预设：{name}', ja='デフォルトプリセット: {name}'), name=default_text))

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
            self.translator.t(BUTTON_SAVE_CURRENT_AS_PRESET),
            self.translator.t(Text(en='Enter a preset name:', zh_cn='请输入预设名称：', ja='プリセット名を入力:')),
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        if name in self._preset_names:
            answer = QMessageBox.question(
                self,
                self.translator.t(MESSAGE_WARNING),
                self.translator.t(Text(en="Preset '{name}' already exists. Overwrite it?", zh_cn='预设“{name}”已存在，覆盖吗？', ja="プリセット '{name}' は既に存在します。上書きしますか？"), name=name),
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
            self.translator.t(MESSAGE_WARNING),
            self.translator.t(Text(en="Delete preset '{name}'?", zh_cn='删除预设“{name}”？', ja="プリセット '{name}' を削除しますか？"), name=name),
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
