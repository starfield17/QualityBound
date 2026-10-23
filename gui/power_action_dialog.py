from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Text, Translator
from core.media import PostEncodeAction


LABEL_POST_ENCODE_ACTION = Text(
    en='After queue',
    zh_cn='队列结束后',
)


POST_ENCODE_ACTION_TEXT: dict[PostEncodeAction, Text] = {
    PostEncodeAction.DO_NOTHING: Text(en='Do nothing', zh_cn='无操作'),
    PostEncodeAction.SLEEP: Text(en='Sleep system', zh_cn='系统睡眠'),
    PostEncodeAction.SHUTDOWN: Text(en='Shut down system', zh_cn='系统关机'),
    PostEncodeAction.QUIT: Text(en='Quit app', zh_cn='退出程序'),
}


def post_encode_action_text(action: PostEncodeAction) -> Text:
    return POST_ENCODE_ACTION_TEXT.get(action, POST_ENCODE_ACTION_TEXT[PostEncodeAction.DO_NOTHING])


class PowerActionCountdownDialog(QDialog):
    def __init__(
        self,
        tr: Translator,
        action: PostEncodeAction,
        timeout_sec: int = 30,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = tr
        self.action = action
        self.remaining_sec = timeout_sec
        self.total_sec = timeout_sec

        self.setWindowTitle(self.translator.t(Text(en='Action on Queue Completion', zh_cn='转码完成自动操作')))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)
        self.resize(440, 180)

        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        QApplication.beep()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        self.message_label = QLabel()
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)
        font = self.message_label.font()
        font.setPointSize(font.pointSize() + 1)
        font.setBold(True)
        self.message_label.setFont(font)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, self.total_sec)
        self.progress_bar.setValue(self.remaining_sec)
        self.progress_bar.setTextVisible(False)

        button_layout = QHBoxLayout()
        button_layout.setSpacing(12)

        self.cancel_button = QPushButton(self.translator.t(Text(en='Cancel', zh_cn='取消')))
        self.cancel_button.setDefault(True)
        self.execute_button = QPushButton(self.translator.t(Text(en='Execute Now', zh_cn='立即执行')))

        button_layout.addStretch(1)
        button_layout.addWidget(self.cancel_button)
        button_layout.addWidget(self.execute_button)

        layout.addWidget(self.message_label)
        layout.addWidget(self.progress_bar)
        layout.addLayout(button_layout)

        self.cancel_button.clicked.connect(self.reject)
        self.execute_button.clicked.connect(self.accept)

        self._update_text()

    def _update_text(self) -> None:
        action_name = self.translator.t(post_encode_action_text(self.action))
        self.message_label.setText(
            self.translator.t(Text(en='Batch encoding finished. The system will {action} in {seconds} seconds.', zh_cn='批处理转码已完成。系统将在 {seconds} 秒后自动执行【{action}】。'), action=action_name, seconds=self.remaining_sec)
        )
        self.progress_bar.setValue(self.remaining_sec)

    def _on_tick(self) -> None:
        self.remaining_sec -= 1
        if self.remaining_sec <= 0:
            self._timer.stop()
            self.accept()
        else:
            self._update_text()

    def reject(self) -> None:
        self._timer.stop()
        super().reject()

    def accept(self) -> None:
        self._timer.stop()
        super().accept()
