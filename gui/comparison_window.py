"""Independent A/B playback and timestamp-based paused-frame inspection."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (QCloseEvent, QImage, QKeySequence, QMouseEvent,
                          QPainter, QPaintEvent, QShortcut)
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
                              QLabel, QPushButton, QSlider, QStackedLayout,
                              QStyle, QVBoxLayout, QWidget)

from core.i18n import Translator
from gui.comparison_decode import (ComparisonMedia, DecodeCancelled, FramePair,
                                   ToolRunner, decode_pair, inspect_pair, preview_pair)
from gui.comparison_state import ComparisonPhase, ComparisonTimeline
from gui.comparison_playback import PlaybackSession


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
        self.image = QImage()

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), Qt.GlobalColor.black)
        if not self.image.isNull():
            size = self.image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            x, y = (self.width() - size.width()) // 2, (self.height() - size.height()) // 2
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawImage(x, y, self.image.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))


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
        self.stack.addWidget(self.canvas)
        layout.addWidget(host, 1)
        self.setMinimumSize(160, 150)

    def show_frame(self, image: QImage) -> None:
        self.canvas.image = image
        self.canvas.update()
        self.stack.setCurrentWidget(self.canvas)

def display_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    return f"{hours:02}:{minutes:02}:{remainder // 1000:02}.{remainder % 1000:03}"


class ComparisonWindow(QWidget):
    """Owns one comparison session; callers supply paths and tools, never queue state."""
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
        self._resume_after_scrub = False
        self._resume_after_seek = False
        self._selected_side = 0
        self.resize(1120, 700)
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
        for key, callback in [("Space", self.toggle_play), ("Tab", self.toggle_side),
                              ("Left", lambda: self.seek(self.position - 5)),
                              ("Right", lambda: self.seek(self.position + 5))]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self._shortcuts.append(shortcut)
        self.apply_translations(translator)
        self._set_audio()
        self._update_controls()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        files = QHBoxLayout()
        self.open_buttons = [QPushButton(self), QPushButton(self)]
        for side, button in enumerate(self.open_buttons):
            button.clicked.connect(lambda _checked=False, side=side: self._choose_file(side))
            files.addWidget(button)
        root.addLayout(files)
        options = QHBoxLayout()
        self.mode = QComboBox(self)
        self.mode.currentIndexChanged.connect(self._update_layout)
        self.precision = QComboBox(self)
        self.precision.currentIndexChanged.connect(self._precision_changed)
        self.audio = QComboBox(self)
        self.audio.currentIndexChanged.connect(self._set_audio)
        self.switch_button = QPushButton(self)
        self.switch_button.clicked.connect(self.toggle_side)
        for widget in (self.mode, self.precision, self.audio, self.switch_button):
            options.addWidget(widget)
        root.addLayout(options)
        self.panes = [VideoPane(self), VideoPane(self)]
        self.picture_layout = QHBoxLayout()
        for pane in self.panes:
            self.picture_layout.addWidget(pane, 1)
        root.addLayout(self.picture_layout, 1)
        self.slider = SeekSlider(self)
        self.slider.sliderPressed.connect(self._scrub_started)
        self.slider.valueChanged.connect(self._scrub_moved)
        self.slider.sliderReleased.connect(self._scrub_finished)
        root.addWidget(self.slider)
        self.time_label = QLabel(self)
        root.addWidget(self.time_label)
        controls = QHBoxLayout()
        self.play_button = QPushButton(self)
        self.play_button.clicked.connect(self.toggle_play)
        self.previous_button = QPushButton(self)
        self.next_button = QPushButton(self)
        self.previous_button.clicked.connect(lambda: self.step_frame(-1))
        self.next_button.clicked.connect(lambda: self.step_frame(1))
        for button in (self.play_button, self.previous_button, self.next_button):
            controls.addWidget(button)
        controls.addStretch()
        self.offset_label = QLabel(self)
        controls.addWidget(self.offset_label)
        self.offset = QDoubleSpinBox(self)
        self.offset.setRange(-86_400_000, 86_400_000)
        self.offset.setDecimals(3)
        self.offset.setSingleStep(1)
        self.offset.setSuffix(" ms")
        self.offset.valueChanged.connect(self._offset_changed)
        controls.addWidget(self.offset)
        self.offset_previous = QPushButton(self)
        self.offset_next = QPushButton(self)
        self.offset_previous.clicked.connect(lambda: self.step_frame(-1, side=1))
        self.offset_next.clicked.connect(lambda: self.step_frame(1, side=1))
        controls.addWidget(self.offset_previous)
        controls.addWidget(self.offset_next)
        root.addLayout(controls)
        self.status = QLabel(self)
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.help_label = QLabel(self)
        self.help_label.setWordWrap(True)
        root.addWidget(self.help_label)

    def apply_translations(self, translator: Translator) -> None:
        self.translator = translator
        t = translator.t
        self.setWindowTitle(t("gui.compare.title"))
        for side, button in enumerate(self.open_buttons):
            button.setText(t("gui.compare.open", side="AB"[side]))
        for combo, labels in [
            (self.mode, (t("gui.compare.side_by_side"), t("gui.compare.single"))),
            (self.precision, (t("gui.compare.precise"), t("gui.compare.normal"))),
            (self.audio, (t("gui.compare.audio_a"), t("gui.compare.audio_b"), t("gui.compare.muted"))),
        ]:
            index = max(0, combo.currentIndex())
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(labels)
            combo.setCurrentIndex(index)
            combo.blockSignals(False)
        self.switch_button.setText(t("gui.compare.switch"))
        self.previous_button.setText(t("gui.compare.previous"))
        self.next_button.setText(t("gui.compare.next"))
        self.offset_previous.setText(t("gui.compare.offset_previous"))
        self.offset_next.setText(t("gui.compare.offset_next"))
        self.offset_label.setText(t("gui.compare.offset"))
        self.offset.setToolTip(t("gui.compare.offset_hint"))
        self.help_label.setText(t("gui.compare.help"))
        self._update_labels()
        self._update_controls()

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

    def _choose_file(self, side: int) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, self.translator.t("gui.compare.open", side="AB"[side]))
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
        self._resume_after_seek = False
        self.offset.blockSignals(True)
        self.offset.setValue(0)
        self.offset.blockSignals(False)
        for pane in self.panes:
            pane.show_frame(QImage())
        self.phase = ComparisonPhase.LOADING
        self.status.setText(self.translator.t("gui.compare.loading"))
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
        self._invalidate()
        self.preview_timer.stop()
        self.playback.stop()
        self.phase = ComparisonPhase.ERROR
        self.status.setText(self.translator.t("gui.compare.error", error=error))
        self._update_controls()

    def _update_layout(self) -> None:
        single = self.mode.currentIndex() == 1
        for side, pane in enumerate(self.panes):
            pane.setVisible(not single or side == self._selected_side)
        self.switch_button.setEnabled(single)
        for shortcut in self._shortcuts:
            if shortcut.key().toString() == "Tab":
                shortcut.setEnabled(single)

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
        self.play_button.setText(self.translator.t("gui.compare.pause")
                                 if self.phase == ComparisonPhase.PLAYING
                                 else self.translator.t("gui.compare.play"))
        if timeline is not None and timeline.available and self.phase != ComparisonPhase.SCRUBBING:
            self.slider.blockSignals(True)
            self.slider.setRange(round(timeline.start * 1000), round(timeline.end * 1000))
            self.slider.setValue(round(self.position * 1000))
            self.slider.blockSignals(False)
        self._update_layout()

    def _update_labels(self) -> None:
        for side, pane in enumerate(self.panes):
            path = self.paths[side]
            stamp = self.timestamps[side]
            pane.label.setText(f"{'AB'[side]} · {path.name if path else '—'} · "
                               f"{display_time(stamp) if stamp is not None else '—'}")
            pane.label.setToolTip(str(path) if path else "")
        timeline = self.timeline
        end = display_time(timeline.end) if timeline else "—"
        self.time_label.setText(f"{display_time(self.position)} / {end}")

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
        if self.phase == ComparisonPhase.PLAYING:
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
        self.phase = ComparisonPhase.PLAYING
        self.status.setText(self.translator.t("gui.compare.playing"))
        try:
            self.playback.start(self.media, self.position, timeline.offset, self.audio.currentIndex())
            if not self.playback.audio_available:
                self.status.setText(self.translator.t("gui.compare.audio_unavailable"))
        except Exception as exc:
            self._decode_error(str(exc))
        self._update_controls()

    def pause(self) -> None:
        timeline = self.timeline
        if self.phase == ComparisonPhase.PLAYING and timeline is not None:
            self.position = timeline.clamp(self.playback.tick())
        self.playback.stop()
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
        self._resume_after_seek = resume
        self.phase = ComparisonPhase.SEEKING
        self.status.setText(self.translator.t("gui.compare.aligning"))
        self._update_controls()
        self._launch(lambda runner: decode_pair(media, position, offset, runner, side, direction),
                     lambda result: self._frames_ready(result, resume, side, direction))

    def _frames_ready(self, result: object, resume: bool, side: int, direction: int) -> None:
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
        self.status.setText(self.translator.t("gui.compare.aligned") if self.precision.currentIndex() == 0
                            else self.translator.t("gui.compare.normal_paused"))
        self._update_controls()
        self._update_labels()
        if resume:
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
                or (self.phase == ComparisonPhase.SEEKING and self._resume_after_seek))

    def _offset_changed(self) -> None:
        timeline = self.timeline
        if timeline is None:
            return
        if not timeline.available:
            self._invalidate()
            self.playback.stop()
            self.phase = ComparisonPhase.PAUSED
            self.status.setText(self.translator.t("gui.compare.no_overlap"))
            self._update_controls()
        else:
            self.seek(self.position)

    def _scrub_started(self) -> None:
        self._resume_after_scrub = self._playback_intent()
        self._invalidate()
        self.playback.stop()
        self.phase = ComparisonPhase.SCRUBBING
        self.status.setText(self.translator.t("gui.compare.scrubbing"))
        self._update_controls()

    def _scrub_moved(self, value: int) -> None:
        if self.phase != ComparisonPhase.SCRUBBING:
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
            self._resume_after_seek = resume
            self.phase = ComparisonPhase.SEEKING
            self.status.setText(self.translator.t("gui.compare.scrubbing"))
            self._launch(lambda runner: preview_pair(media, position, offset, runner),
                         lambda result: self._frames_ready(result, resume, 0, 0))

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
        self.seek(self.slider.value() / 1000, resume=self._resume_after_scrub)

    def closeEvent(self, event: QCloseEvent) -> None:
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
