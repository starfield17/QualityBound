"""Independent A/B playback and timestamp-based paused-frame inspection."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QDragEnterEvent,
    QDropEvent,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QSlider,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.i18n import Text, Translator
from core.media import VIDEO_EXTENSIONS
from gui.comparison.decode import (ComparisonMedia, DecodeCancelled, FramePair,
                                   ToolRunner, decode_pair, inspect_pair, preview_pair)
from gui.comparison.state import ComparisonPhase, ComparisonTimeline, PlaybackIntent
from gui.comparison.playback import PlaybackSession
from gui.window_geometry import clamped_window_size
from gui.icons import set_button_icon


COMPARE_AUDIO_HINT = Text(
    en="Choose which file's audio track to hear.",
    zh_cn='选择要播放哪个文件的音轨。',
    ja='再生する音声トラックのファイルを選択します。',
)

COMPARE_ERROR = Text(
    en='Comparison failed: {error}. Check the files/tools, or select normal synchronization if only precise inspection failed.',
    zh_cn='对比失败：{error}。请检查文件和工具；如果仅精确检查失败，可选择普通同步。',
    ja='比較に失敗しました: {error}。ファイルとツールを確認するか、精密検査のみが失敗した場合は通常同期を選択してください。',
)

COMPARE_HELP = Text(
    en='Space: play/pause · Tab / T: switch A/B in single view · ←/→: seek 5 seconds (the timeline keeps its arrow keys). Click or drag the timeline to seek both videos. Precise inspection aligns paused frames by timestamp; playback shares one presentation clock. All decoding uses the selected external FFmpeg.\nA frame: , / .   B frame: Shift+, / Shift+.   Editors and menus keep their own keys. Shift+Tab moves focus backwards.',
    zh_cn='Space：播放/暂停 · Tab / T：单画面切换 A/B · ←/→：跳转 5 秒（进度条保留自身方向键）。点击或拖动进度条同步定位两个视频。精确检查按时间戳定位暂停画面；播放共用一个时钟。全部解码使用所选外部 FFmpeg。\nA 逐帧：, / .；B 逐帧：Shift+, / Shift+.。编辑控件和菜单保留自身按键；Shift+Tab 向前一个控件移动焦点。',
    ja='Space: 再生/一時停止 · Tab / T: 単一ビューでA/B切り替え · ←/→: 5秒シーク（タイムラインは独自の矢印キーを維持）。クリックまたはドラッグして両方の動画を同期シーク。精密検査はタイムスタンプで整列し、再生時は共通クロックを使用します。デコードは選択された外部FFmpegを使用します。\nAフレーム: , / .   Bフレーム: Shift+, / Shift+.   編集とメニューは独自のキーを維持。Shift+Tabで前のコントロールに移動します。',
)

COMPARE_HELP_TOGGLE = Text(
    en='Help',
    zh_cn='帮助',
    ja='ヘルプ',
)

COMPARE_MODE_HINT = Text(
    en='Choose side-by-side or a single A/B view.',
    zh_cn='选择并排显示或单个 A/B 视图。',
    ja='並列表示または単一A/Bビューを選択します。',
)

COMPARE_OFFSET = Text(
    en='B offset:',
    zh_cn='B 偏移：',
    ja='B オフセット:',
)

COMPARE_OFFSET_HINT = Text(
    en='B time = A time + offset. Positive values skip the beginning of B. Frame buttons use actual B frame timestamps.',
    zh_cn='B 时间 = A 时间 + 偏移。正值跳过 B 的片头。帧微调按钮使用 B 的实际帧时间戳。',
    ja='B時間 = A時間 + オフセット。正の値はBの開始部分をスキップします。フレームボタンはBの実際のフレームタイムスタンプを使用します。',
)

COMPARE_PLACEHOLDER = Text(
    en='Open or drop a video',
    zh_cn='打开或拖入视频',
    ja='動画を開くかドロップ',
)

COMPARE_PRECISION_HINT = Text(
    en='Precise inspection aligns paused frames by timestamp; normal mode uses the shared playback clock.',
    zh_cn='精确检查按时间戳对齐暂停帧；普通模式使用共享播放时钟。',
    ja='精密検査は一時停止フレームをタイムスタンプで整列します。通常モードは共有再生クロックを使用します。',
)

COMPARE_SCRUBBING = Text(
    en='Seeking both videos…',
    zh_cn='正在同步定位两个视频…',
    ja='両方の動画をシーク中…',
)

COMPARE_TITLE = Text(
    en='Video comparison',
    zh_cn='视频对比',
    ja='動画比較',
)

COMPARE_WAITING_OTHER = Text(
    en='Ready. Open video {side} to compare.',
    zh_cn='已就绪。请打开视频 {side} 以开始对比。',
    ja='準備完了。比較する動画 {side} を開いてください。',
)


class ComparisonWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[ToolRunner], object], parent: QWidget) -> None:
        super().__init__(parent)
        self.runner = ToolRunner()
        self.task = task

    def run(self) -> None:
        try:
            result = self.task(self.runner)
            if not self.runner.cancelled.is_set():
                self.completed.emit(result)
        except DecodeCancelled:
            pass
        except Exception as exc:
            if not self.runner.cancelled.is_set():
                self.failed.emit(str(exc))


class SeekSlider(QSlider):
    """Absolute click and drag seeking with the standard slider signals."""
    def __init__(self, parent: QWidget) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)

    def _set_mouse_position(self, event: QMouseEvent) -> None:
        handle = self.style().pixelMetric(QStyle.PixelMetric.PM_SliderLength)
        width = max(1, self.width() - handle)
        position = max(0, min(width, round(event.position().x() - handle / 2)))
        self.setValue(QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), position, width,
            self.layoutDirection() == Qt.LayoutDirection.RightToLeft,
        ))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self._set_mouse_position(event)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.isSliderDown():
            self._set_mouse_position(event)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self._set_mouse_position(event)
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class FrameCanvas(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.image = QImage()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = (self.width() - size.width()) // 2, (self.height() - size.height()) // 2
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(QRectF(x, y, size.width(), size.height()), self.image)


class VideoPane(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)
        host = QWidget(self)
        self.stack = QStackedLayout(host)
        self.canvas = FrameCanvas(host)
        self.placeholder = QLabel(host)
        self.placeholder.setObjectName("comparePlaceholder")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.placeholder)
        self.stack.setCurrentWidget(self.placeholder)
        layout.addWidget(host, 1)
        self.setMinimumSize(160, 150)

    def show_frame(self, image: QImage) -> None:
        self.canvas.image = image
        self.canvas.update()
        self.stack.setCurrentWidget(self.canvas if not image.isNull() else self.placeholder)

def display_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    return f"{hours:02}:{minutes:02}:{remainder // 1000:02}.{remainder % 1000:03}"


class ComparisonWindow(QWidget):
    """Owns one comparison session; callers supply paths and tools, never queue state."""
    closed = Signal(object)

    def __init__(self, translator: Translator, parent: QWidget | None = None,
                 *, ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.translator = translator
        self.ffmpeg_setting = ffmpeg
        self.ffprobe_setting = ffprobe
        self.paths: list[Path | None] = [None, None]
        self.media: ComparisonMedia | None = None
        self.phase = ComparisonPhase.EMPTY
        self.position = 0.0
        self.timestamps: list[float | None] = [None, None]
        self._generation = 0
        self._jobs: list[ComparisonWorker] = []
        self._intent = PlaybackIntent.PAUSED
        self._layout_signature: tuple[int, bool] | None = None
        self._selected_side = 0
        self.setObjectName("comparisonWindow")
        self.setAcceptDrops(True)
        self.resize(clamped_window_size(1120, 700, minimum_width=720, minimum_height=480))
        self._build_ui()
        self.playback = PlaybackSession(self)
        self.playback.frame_ready.connect(self._playback_frame)
        self.playback.failed.connect(self._decode_error)
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(100)
        self.preview_timer.timeout.connect(self._preview_seek)
        self._shortcuts: list[QShortcut] = []
        for key, callback in [("Space", self.toggle_play), ("T", self.toggle_side), ("Tab", self.toggle_side),
                              ("Left", lambda: self.seek(self.position - 5)),
                              ("Right", lambda: self.seek(self.position + 5)),
                              (",", lambda: self.step_frame(-1)), (".", lambda: self.step_frame(1)),
                              ("Shift+,", lambda: self.step_frame(-1, side=1)),
                              ("Shift+.", lambda: self.step_frame(1, side=1))]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setAutoRepeat(key not in {"Space", "T", "Tab"})
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._update_shortcuts)
        self.apply_translations(translator)
        self._set_audio()
        self._update_controls()

    def _build_ui(self) -> None:
        root = self.root_layout = QVBoxLayout(self)
        # A row's previous minimum must not prevent the resize that reflows it.
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumSize(clamped_window_size(720, 480))
        files = QHBoxLayout()
        self.open_buttons = [QPushButton(self), QPushButton(self)]
        for side, button in enumerate(self.open_buttons):
            button.clicked.connect(lambda _checked=False, side=side: self._choose_file(side))
            files.addWidget(button)
        files.addStretch(1)
        root.addLayout(files)
        self.options_layout = QGridLayout()
        self.options_layout.setHorizontalSpacing(6)
        self.options_layout.setVerticalSpacing(6)
        self.mode = QComboBox(self)
        self.mode.currentIndexChanged.connect(self._update_layout)
        self.precision = QComboBox(self)
        self.precision.currentIndexChanged.connect(self._precision_changed)
        self.audio = QComboBox(self)
        self.audio.currentIndexChanged.connect(self._set_audio)
        self.switch_controls = QWidget(self)
        switch_layout = QHBoxLayout(self.switch_controls)
        switch_layout.setContentsMargins(0, 0, 0, 0)
        self.switch_button = QPushButton(self.switch_controls)
        self.switch_button.clicked.connect(self.toggle_side)
        self.help_button = QToolButton(self.switch_controls)
        set_button_icon(self.help_button, "help")
        self.help_button.setCheckable(True)
        self.help_button.toggled.connect(self._toggle_help)
        switch_layout.addWidget(self.switch_button)
        switch_layout.addWidget(self.help_button)
        self._option_widgets = (self.mode, self.precision, self.audio, self.switch_controls)
        root.addLayout(self.options_layout)
        self.panes = [VideoPane(self), VideoPane(self)]
        self.picture_layout = QHBoxLayout()
        for pane in self.panes:
            self.picture_layout.addWidget(pane, 1)
        root.addLayout(self.picture_layout, 1)
        self.slider = SeekSlider(self)
        self.slider.setSingleStep(1000)
        self.slider.setPageStep(5000)
        self.slider.sliderPressed.connect(self._scrub_started)
        self.slider.valueChanged.connect(self._scrub_moved)
        self.slider.sliderReleased.connect(self._scrub_finished)
        timeline_row = QHBoxLayout()
        timeline_row.addWidget(self.slider, 1)
        self.time_label = QLabel(self)
        timeline_row.addWidget(self.time_label)
        root.addLayout(timeline_row)
        self.controls_layout = QGridLayout()
        self.controls_layout.setHorizontalSpacing(6)
        self.controls_layout.setVerticalSpacing(6)
        self.transport_controls = QWidget(self)
        transport = QHBoxLayout(self.transport_controls)
        transport.setContentsMargins(0, 0, 0, 0)
        self.play_button = QPushButton(self.transport_controls)
        self.play_button.clicked.connect(self.toggle_play)
        self.previous_button = QPushButton(self.transport_controls)
        self.next_button = QPushButton(self.transport_controls)
        self.previous_button.clicked.connect(lambda: self.step_frame(-1))
        self.next_button.clicked.connect(lambda: self.step_frame(1))
        for button in (self.play_button, self.previous_button, self.next_button):
            transport.addWidget(button)
        transport.addStretch()
        self.offset_controls = QWidget(self)
        controls = QHBoxLayout(self.offset_controls)
        controls.setContentsMargins(0, 0, 0, 0)
        self.offset_label = QLabel(self.offset_controls)
        controls.addWidget(self.offset_label)
        self.offset = QDoubleSpinBox(self.offset_controls)
        self.offset.setRange(-86_400_000, 86_400_000)
        self.offset.setDecimals(3)
        self.offset.setSingleStep(1)
        self.offset.setSuffix(" ms")
        self.offset.valueChanged.connect(self._offset_changed)
        controls.addWidget(self.offset)
        self.offset_previous = QPushButton(self.offset_controls)
        self.offset_next = QPushButton(self.offset_controls)
        self.offset_previous.clicked.connect(lambda: self.step_frame(-1, side=1))
        self.offset_next.clicked.connect(lambda: self.step_frame(1, side=1))
        controls.addWidget(self.offset_previous)
        controls.addWidget(self.offset_next)
        root.addLayout(self.controls_layout)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.help_scroll = QScrollArea(self)
        self.help_scroll.setWidgetResizable(True)
        self.help_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.help_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.help_scroll.setMaximumHeight(84)
        self.help_label = QLabel(self.help_scroll)
        self.help_label.setWordWrap(True)
        self.help_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.help_scroll.setWidget(self.help_label)
        self.help_label.hide()
        self.help_scroll.hide()
        root.addWidget(self.help_scroll)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_controls()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in {QEvent.Type.FontChange, QEvent.Type.StyleChange}:
            self._reflow_controls()

    def _reflow_controls(self) -> None:
        if not hasattr(self, 'help_scroll'):
            return
        margins = self.root_layout.contentsMargins()
        available = max(1, self.width() - margins.left() - margins.right())
        widest = max(widget.sizeHint().width() for widget in self._option_widgets)
        columns = max(1, min(len(self._option_widgets), (available + 6) // (widest + 6)))
        stacked = (self.transport_controls.minimumSizeHint().width()
                   + self.offset_controls.minimumSizeHint().width() + 6 > available)
        signature = (columns, stacked)
        if self._layout_signature == signature:
            return
        self._layout_signature = signature
        for column in range(len(self._option_widgets)):
            self.options_layout.setColumnStretch(column, int(column < columns))
        for index, widget in enumerate(self._option_widgets):
            self.options_layout.removeWidget(widget)
            self.options_layout.addWidget(widget, index // columns, index % columns)
        for widget in (self.transport_controls, self.offset_controls):
            self.controls_layout.removeWidget(widget)
        self.controls_layout.addWidget(self.transport_controls, 0, 0)
        self.controls_layout.addWidget(self.offset_controls, int(stacked), 0 if stacked else 1)
        self.controls_layout.setColumnStretch(0, 1)
        self.controls_layout.setColumnStretch(1, int(not stacked))

    def apply_translations(self, translator: Translator) -> None:
        self.translator = translator
        t = translator.t
        self.setWindowTitle(t(COMPARE_TITLE))
        for side, button in enumerate(self.open_buttons):
            button.setText(t(Text(en='Open {side}…', zh_cn='打开 {side}…', ja='{side} を開く…'), side="AB"[side]))
        for combo, labels in [
            (self.mode, (t(Text(en='Side by side', zh_cn='并排显示', ja='並列表示')), t(Text(en='Single view · A/B', zh_cn='单画面 · A/B', ja='単一ビュー · A/B')))),
            (self.precision, (t(Text(en='Precise inspection', zh_cn='精确检查', ja='精密検査')), t(Text(en='Normal synchronization', zh_cn='普通同步', ja='通常同期')))),
            (self.audio, (t(Text(en='Audio track A', zh_cn='音轨 A', ja='音声トラック A')), t(Text(en='Audio track B', zh_cn='音轨 B', ja='音声トラック B')), t(Text(en='Muted', zh_cn='静音', ja='ミュート')))),
        ]:
            index = max(0, combo.currentIndex())
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(labels)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self.switch_button.setText(t(Text(en='Switch A/B · Tab / T', zh_cn='切换 A/B · Tab / T', ja='A/B 切り替え · Tab / T')))
        self.previous_button.setText(t(Text(en='A −1 frame', zh_cn='A −1 帧', ja='A −1 フレーム')))
        self.next_button.setText(t(Text(en='A +1 frame', zh_cn='A +1 帧', ja='A +1 フレーム')))
        self.offset_previous.setText(t(Text(en='B −1 frame', zh_cn='B −1 帧', ja='B −1 フレーム')))
        self.offset_next.setText(t(Text(en='B +1 frame', zh_cn='B +1 帧', ja='B +1 フレーム')))
        self.offset_label.setText(t(COMPARE_OFFSET))
        self.offset.setToolTip(t(COMPARE_OFFSET_HINT))
        self.help_label.setText(t(COMPARE_HELP))
        self.help_button.setToolTip(t(COMPARE_HELP_TOGGLE))
        self.help_button.setAccessibleName(t(COMPARE_HELP_TOGGLE))
        for control, label in ((self.mode, t(COMPARE_MODE_HINT)), (self.precision, t(COMPARE_PRECISION_HINT)), (self.audio, t(COMPARE_AUDIO_HINT)), (self.slider, t(Text(en='Playback position', zh_cn='播放位置', ja='再生位置'))), (self.offset, t(COMPARE_OFFSET))):
            control.setAccessibleName(label)
        self.mode.setToolTip(t(COMPARE_MODE_HINT))
        self.precision.setToolTip(t(COMPARE_PRECISION_HINT))
        self.audio.setToolTip(t(COMPARE_AUDIO_HINT))
        for side, pane in enumerate(self.panes):
            pane.placeholder.setText(t(COMPARE_WAITING_OTHER, side="AB"[1 - side]) if self.paths[side] is not None and self.media is None else t(COMPARE_PLACEHOLDER))
        self._update_labels()
        self._update_controls()
        self._reflow_controls()

    def _toggle_help(self, visible: bool) -> None:
        self.help_label.setVisible(visible)
        self.help_scroll.setVisible(visible)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
            if paths:
                event.acceptProposedAction()
                return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile() and Path(url.toLocalFile()).is_file()
        ]
        if not paths:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        if len(paths) >= 2:
            self.paths = [paths[0], paths[1]]
        else:
            side = 1 if self.panes[1].geometry().contains(event.position().toPoint()) else 0
            self.paths[side] = paths[0]
        self._update_labels()
        if self.paths[0] is not None and self.paths[1] is not None:
            self.load_paths(self.paths[0], self.paths[1])

    @property
    def timeline(self) -> ComparisonTimeline | None:
        if self.media is None:
            return None
        return ComparisonTimeline(self.media.details[0].duration, self.media.details[1].duration,
                                  self.offset.value() / 1000,
                                  self.media.details[0].start, self.media.details[1].start)

    def _invalidate(self) -> int:
        self._generation += 1
        for job in self._jobs:
            job.runner.cancel()
        return self._generation

    def _launch(self, task: Callable[[ToolRunner], object], completed: Callable[[object], None]) -> None:
        generation = self._invalidate()
        job = ComparisonWorker(task, self)
        self._jobs.append(job)

        def finished_result(result: object) -> None:
            if generation == self._generation:
                completed(result)

        def failed_result(error: str) -> None:
            if generation == self._generation:
                self._decode_error(error)

        job.completed.connect(finished_result)
        job.failed.connect(failed_result)
        job.finished.connect(lambda: self._job_finished(job))
        job.start()

    def _job_finished(self, job: ComparisonWorker) -> None:
        self._jobs.remove(job)
        job.deleteLater()

    def _video_filter(self) -> str:
        extensions = " ".join(f"*{suffix}" for suffix in sorted(VIDEO_EXTENSIONS))
        return f"{self.translator.t(Text(en='Video files', zh_cn='视频文件', ja='動画ファイル'))} ({extensions});;{self.translator.t(Text(en='All files', zh_cn='所有文件', ja='すべてのファイル'))} (*)"

    def _choose_file(self, side: int) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            self.translator.t(Text(en='Open video {side}…', zh_cn='打开视频 {side}…', ja='動画 {side} を開く…'), side="AB"[side]),
            "",
            self._video_filter(),
        )
        if path:
            self.paths[side] = Path(path)
            self._update_labels()
            if self.paths[0] is not None and self.paths[1] is not None:
                self.load_paths(self.paths[0], self.paths[1])

    def load_paths(self, a: Path, b: Path) -> None:
        self._invalidate()
        self.preview_timer.stop()
        self.playback.stop()
        self.paths = [a, b]
        self.media = None
        self.timestamps = [None, None]
        self.position = 0
        self._intent = PlaybackIntent.PAUSED
        self.offset.blockSignals(True)
        self.offset.setValue(0)
        self.offset.blockSignals(False)
        for pane in self.panes:
            pane.show_frame(QImage())
        self.phase = ComparisonPhase.LOADING
        self.status.setText(self.translator.t(Text(en='Inspecting videos…', zh_cn='正在读取视频信息…', ja='動画情報を取得中…')))
        self._update_controls()
        self._update_labels()
        self._launch(lambda runner: inspect_pair((a, b), self.ffmpeg_setting, self.ffprobe_setting, runner),
                     self._loaded)

    def _loaded(self, result: object) -> None:
        if not isinstance(result, ComparisonMedia):
            raise TypeError("Expected comparison media")
        self.media = result
        self.phase = ComparisonPhase.PAUSED
        self.seek(0)

    def _decode_error(self, error: str) -> None:
        error_keys = {
            'FFmpeg video decoding cannot keep up with playback': self.translator.t(Text(en='FFmpeg video decoding cannot keep up with playback', zh_cn='FFmpeg 解码速度无法跟上播放。', ja='FFmpegの動画デコードが再生に追いつきません')),
            'Cannot open PCM audio output': self.translator.t(Text(en='Cannot open PCM audio output', zh_cn='无法打开 PCM 音频输出。', ja='PCM音声出力を開けません')),
            'Cannot write PCM audio output': self.translator.t(Text(en='Cannot write PCM audio output', zh_cn='无法写入 PCM 音频输出。', ja='PCM音声出力に書き込めません')),
            'Invalid decoded frame image': self.translator.t(Text(en='Invalid decoded frame image', zh_cn='解码后的帧图像无效。', ja='デコードされたフレーム画像が無効です')),
            'No FFmpeg frame timestamp within 60 seconds': self.translator.t(Text(en='No FFmpeg frame timestamp within 60 seconds', zh_cn='60 秒内未收到 FFmpeg 帧时间戳。', ja='60秒以内にFFmpegフレームタイムスタンプを受信できませんでした')),
            'Video inspection exceeded 60 seconds': self.translator.t(Text(en='Video inspection exceeded 60 seconds', zh_cn='视频检查超过 60 秒。', ja='動画の確認が60秒を超過しました')),
            'No decoded video frames': self.translator.t(Text(en='No decoded video frames', zh_cn='未解码出视频帧。', ja='動画フレームがデコードされませんでした')),
            'Invalid FFmpeg video pipe header': self.translator.t(Text(en='Invalid FFmpeg video pipe header', zh_cn='FFmpeg 视频管道头部无效。', ja='FFmpeg動画パイプヘッダーが無効です')),
            'Unsupported FFmpeg video pipe dimensions': self.translator.t(Text(en='Unsupported FFmpeg video pipe dimensions', zh_cn='FFmpeg 视频管道尺寸不受支持。', ja='サポートされていないFFmpeg動画パイプ寸法です')),
            'Incomplete FFmpeg video frame': self.translator.t(Text(en='Incomplete FFmpeg video frame', zh_cn='FFmpeg 视频帧不完整。', ja='不完全なFFmpeg動画フレームです')),
            'Invalid FFmpeg video frame': self.translator.t(Text(en='Invalid FFmpeg video frame', zh_cn='FFmpeg 视频帧无效。', ja='無効なFFmpeg動画フレームです')),
            'No decoded video frame': self.translator.t(Text(en='No decoded video frame', zh_cn='未解码出视频帧。', ja='動画フレームがデコードされませんでした')),
            'No timestamped preview frame': self.translator.t(Text(en='No timestamped preview frame', zh_cn='未获得带时间戳的预览帧。', ja='タイムスタンプ付きのプレビューフレームがありません')),
            'No video frames at the requested position': self.translator.t(Text(en='No video frames at the requested position', zh_cn='请求位置没有视频帧。', ja='要求された位置に動画フレームがありません')),
            'Invalid video timing metadata': self.translator.t(Text(en='Invalid video timing metadata', zh_cn='视频时间信息无效。', ja='無効な動画タイミングメタデータです')),
            'Video duration and frame rate must be positive': self.translator.t(Text(en='Video duration and frame rate must be positive', zh_cn='视频时长和帧率必须大于零。', ja='動画の長さとフレームレートは正の値である必要があります')),
        }
        if error in error_keys:
            error = error_keys[error]
        elif error.startswith("No video stream: "):
            error = self.translator.t(Text(en='No video stream: {file}', zh_cn='没有视频流：{file}', ja='映像ストリームがありません: {file}'), file=error.removeprefix("No video stream: "))
        elif error.startswith("No timestamped video frames: "):
            error = self.translator.t(Text(en='No timestamped video frames: {file}', zh_cn='视频帧没有时间戳：{file}', ja='タイムスタンプ付きの動画フレームがありません: {file}'), file=error.removeprefix("No timestamped video frames: "))
        self._invalidate()
        self.preview_timer.stop()
        self.playback.stop()
        self._intent = PlaybackIntent.PAUSED
        self.phase = ComparisonPhase.ERROR
        self.status.setText(self.translator.t(COMPARE_ERROR, error=error))
        self._update_controls()

    def _update_shortcuts(self, *_args: object) -> None:
        focus = QApplication.focusWidget()
        editing = isinstance(focus, (QLineEdit, QAbstractSpinBox, QComboBox))
        popup = QApplication.activePopupWidget() is not None
        for shortcut in self._shortcuts:
            key = shortcut.key().toString()
            shortcut.setEnabled(
                not editing and not popup
                and (key not in {"T", "Tab"} or self.mode.currentIndex() == 1)
                and (key not in {"Left", "Right"} or not isinstance(focus, QSlider))
            )

    def _update_layout(self) -> None:
        single = self.mode.currentIndex() == 1
        for side, pane in enumerate(self.panes):
            pane.setVisible(not single or side == self._selected_side)
        self.switch_button.setEnabled(single)
        self._update_shortcuts()

    def toggle_side(self) -> None:
        if self.mode.currentIndex() == 1:
            self._selected_side = 1 - self._selected_side
            self._update_layout()

    def _set_audio(self) -> None:
        if self.phase == ComparisonPhase.PLAYING:
            self.seek(self.position, resume=True)

    def _update_controls(self) -> None:
        timeline = self.timeline
        ready = (timeline is not None and timeline.available
                 and self.phase not in {ComparisonPhase.LOADING, ComparisonPhase.EMPTY, ComparisonPhase.CLOSED})
        self.slider.setEnabled(ready)
        self.play_button.setEnabled(ready and self.phase != ComparisonPhase.SCRUBBING)
        stepping = ready and self.phase in {ComparisonPhase.PAUSED, ComparisonPhase.ERROR}
        for button in (self.previous_button, self.next_button, self.offset_previous, self.offset_next):
            button.setEnabled(stepping)
        self.offset.setEnabled(self.media is not None and self.phase != ComparisonPhase.CLOSED)
        self.play_button.setText(self.translator.t(Text(en='Pause · Space', zh_cn='暂停 · Space', ja='一時停止 · Space'))
                                 if self._playback_intent()
                                 else self.translator.t(Text(en='Play · Space', zh_cn='播放 · Space', ja='再生 · Space')))
        if timeline is not None and timeline.available and self.phase != ComparisonPhase.SCRUBBING:
            self.slider.blockSignals(True)
            self.slider.setRange(round(timeline.start * 1000), round(timeline.end * 1000))
            self.slider.setValue(round(self.position * 1000))
            self.slider.blockSignals(False)
        self._update_layout()
        self._reflow_controls()

    def _update_labels(self) -> None:
        for side, pane in enumerate(self.panes):
            if self.media is None:
                pane.placeholder.setText(self.translator.t(COMPARE_WAITING_OTHER, side="AB"[1 - side]) if self.paths[side] is not None else self.translator.t(COMPARE_PLACEHOLDER))
            path = self.paths[side]
            stamp = self.timestamps[side]
            parts = ["AB"[side], path.name if path else "—"]
            if self.media is not None:
                details = self.media.details[side]
                if details.width and details.height:
                    parts.append(f"{details.width}×{details.height}")
                parts.append(f"{details.fps:.2f} fps")
            parts.append(display_time(stamp) if stamp is not None else "—")
            pane.label.setText(" · ".join(parts))
            pane.label.setToolTip(str(path) if path else "")
        timeline = self.timeline
        end = display_time(timeline.end) if timeline else "—"
        self.time_label.setText(f"{display_time(self.position)} / {end}")
        if self.paths[0] is not None and self.paths[1] is not None:
            self.setWindowTitle(
                f"{self.translator.t(COMPARE_TITLE)} · "
                f"{self.paths[0].name} ↔ {self.paths[1].name}"
            )
        else:
            self.setWindowTitle(self.translator.t(COMPARE_TITLE))

    def _playback_frame(self, side: int, image: object, timestamp: float) -> None:
        if self.phase == ComparisonPhase.PLAYING and isinstance(image, QImage):
            self.panes[side].show_frame(image)
            self.timestamps[side] = timestamp
            self._update_labels()

    def _tick(self) -> None:
        if self.phase != ComparisonPhase.PLAYING:
            return
        timeline = self.timeline
        if timeline is None:
            return
        self.position = timeline.clamp(self.playback.tick())
        if self.phase != ComparisonPhase.PLAYING:
            return
        if self.position >= timeline.end - 0.001:
            self.pause()
            return
        self.slider.blockSignals(True)
        self.slider.setValue(round(self.position * 1000))
        self.slider.blockSignals(False)
        self._update_labels()

    def toggle_play(self) -> None:
        if self.phase in {ComparisonPhase.SEEKING, ComparisonPhase.SCRUBBING}:
            self._intent = PlaybackIntent.PAUSED if self._playback_intent() else PlaybackIntent.PLAYING
            self._update_controls()
        elif self.phase == ComparisonPhase.PLAYING:
            self.pause()
        elif self.play_button.isEnabled():
            self._play()

    def _play(self) -> None:
        timeline = self.timeline
        if timeline is None or not timeline.available or self.media is None:
            return
        self._invalidate()
        if self.position >= timeline.end - 0.001:
            self.position = timeline.start
        self._intent = PlaybackIntent.PLAYING
        self.phase = ComparisonPhase.PLAYING
        self.status.setText(self.translator.t(Text(en='Synchronized playback', zh_cn='同步播放中', ja='同期再生中')))
        try:
            self.playback.start(self.media, self.position, timeline.offset, self.audio.currentIndex())
            if not self.playback.audio_available:
                self.status.setText(self.translator.t(Text(en='Synchronized playback · no audio output device is available.', zh_cn='同步播放中 · 没有可用的音频输出设备。', ja='同期再生中 · 利用可能な音声出力デバイスがありません。')))
        except Exception as exc:
            self._decode_error(str(exc))
        self._update_controls()

    def pause(self) -> None:
        timeline = self.timeline
        if self.phase == ComparisonPhase.PLAYING and timeline is not None:
            self.position = timeline.clamp(self.playback.tick())
        self.playback.stop()
        self._intent = PlaybackIntent.PAUSED
        self.phase = ComparisonPhase.PAUSED
        self.seek(self.position)

    def seek(self, position: float, *, resume: bool | None = None) -> None:
        timeline = self.timeline
        if timeline is None or not timeline.available:
            return
        should_resume = self._playback_intent() if resume is None else resume
        self._invalidate()
        self.playback.stop()
        self.position = timeline.clamp(position)
        if self.precision.currentIndex() == 0:
            self._request_frames(resume=should_resume)
        else:
            self._request_preview(resume=should_resume)
        self._update_controls()
        self._update_labels()

    def _request_frames(self, *, resume: bool = False, side: int = 0, direction: int = 0) -> None:
        media = self.media
        if media is None:
            return
        position, offset = self.position, self.offset.value() / 1000
        self._intent = PlaybackIntent.PLAYING if resume else PlaybackIntent.PAUSED
        self.phase = ComparisonPhase.SEEKING
        self.status.setText(self.translator.t(Text(en='Decoding corresponding frames…', zh_cn='正在解码对应时间帧…', ja='対応するフレームをデコード中…')))
        self._update_controls()
        self._launch(lambda runner: decode_pair(media, position, offset, runner, side, direction),
                     lambda result: self._frames_ready(result, side, direction))

    def _frames_ready(self, result: object, side: int, direction: int) -> None:
        if not isinstance(result, FramePair):
            raise TypeError("Expected comparison frame pair")
        images = [QImage.fromData(result.a.png), QImage.fromData(result.b.png)]
        if any(image.isNull() for image in images):
            self._decode_error("Invalid decoded frame image")
            return
        if direction and side == 1:
            self.offset.blockSignals(True)
            self.offset.setValue(result.offset * 1000)
            self.offset.blockSignals(False)
        self.position = result.position
        self.timestamps = [result.a.timestamp, result.b.timestamp]
        for index, image in enumerate(images):
            self.panes[index].show_frame(image)
        self.phase = ComparisonPhase.PAUSED
        self.status.setText(self.translator.t(Text(en='Paused · timestamp-aligned frames', zh_cn='已暂停 · 已按时间戳定位画面', ja='一時停止中 · タイムスタンプ整列フレーム')) if self.precision.currentIndex() == 0
                            else self.translator.t(Text(en='Paused · normal synchronization', zh_cn='已暂停 · 普通同步', ja='一時停止中 · 通常同期')))
        self._update_controls()
        self._update_labels()
        if self._playback_intent():
            self._play()

    def step_frame(self, direction: int, *, side: int = 0) -> None:
        if self.phase not in {ComparisonPhase.PAUSED, ComparisonPhase.ERROR} or self.media is None:
            return
        self.playback.stop()
        self._request_frames(side=side, direction=direction)

    def _precision_changed(self) -> None:
        if self.media is not None and self.phase != ComparisonPhase.PLAYING:
            self.seek(self.position)

    def _playback_intent(self) -> bool:
        return (self.phase == ComparisonPhase.PLAYING
                or self._intent == PlaybackIntent.PLAYING)

    def _offset_changed(self) -> None:
        timeline = self.timeline
        if timeline is None:
            return
        if not timeline.available:
            self._invalidate()
            self.playback.stop()
            self._intent = PlaybackIntent.PAUSED
            self.phase = ComparisonPhase.PAUSED
            self.status.setText(self.translator.t(Text(en='No common playback interval. Adjust the B offset.', zh_cn='没有共同播放区间，请调整 B 偏移。', ja='共通の再生区間がありません。Bのオフセットを調整してください。')))
            self._update_controls()
        else:
            self.seek(self.position)

    def _scrub_started(self) -> None:
        self._intent = PlaybackIntent.PLAYING if self._playback_intent() else PlaybackIntent.PAUSED
        self._invalidate()
        self.playback.stop()
        self.phase = ComparisonPhase.SCRUBBING
        self.status.setText(self.translator.t(COMPARE_SCRUBBING))
        self._update_controls()

    def _scrub_moved(self, value: int) -> None:
        if self.phase != ComparisonPhase.SCRUBBING:
            if self.phase in {ComparisonPhase.PAUSED, ComparisonPhase.PLAYING, ComparisonPhase.ERROR, ComparisonPhase.SEEKING}:
                self.seek(value / 1000)
            return
        self.position = value / 1000
        self._update_labels()
        if not self.preview_timer.isActive():
            self.preview_timer.start()

    def _preview_seek(self) -> None:
        if self.phase == ComparisonPhase.SCRUBBING:
            media = self.media
            if media is not None:
                position, offset = self.position, self.offset.value() / 1000
                self._launch(lambda runner: preview_pair(media, position, offset, runner), self._scrub_preview_ready)

    def _request_preview(self, *, resume: bool) -> None:
        media = self.media
        if media is not None:
            position, offset = self.position, self.offset.value() / 1000
            self._intent = PlaybackIntent.PLAYING if resume else PlaybackIntent.PAUSED
            self.phase = ComparisonPhase.SEEKING
            self.status.setText(self.translator.t(COMPARE_SCRUBBING))
            self._launch(lambda runner: preview_pair(media, position, offset, runner),
                         lambda result: self._frames_ready(result, 0, 0))

    def _scrub_preview_ready(self, result: object) -> None:
        if self.phase == ComparisonPhase.SCRUBBING and isinstance(result, FramePair):
            for side, frame in enumerate((result.a, result.b)):
                image = QImage.fromData(frame.png)
                if not image.isNull():
                    self.panes[side].show_frame(image)
                    self.timestamps[side] = frame.timestamp
            self._update_labels()

    def _scrub_finished(self) -> None:
        if self.phase != ComparisonPhase.SCRUBBING:
            return
        self.preview_timer.stop()
        self.seek(self.slider.value() / 1000)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit(self.saveGeometry())
        self._intent = PlaybackIntent.PAUSED
        self.phase = ComparisonPhase.CLOSED
        self._invalidate()
        self.timer.stop()
        self.preview_timer.stop()
        self.playback.stop()
        # Cancellation is polled every 100ms; any stubborn child is killed by the
        # worker's bounded process cleanup before its QThread is destroyed.
        for job in self._jobs:
            job.wait()
        event.accept()
