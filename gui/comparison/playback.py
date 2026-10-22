"""External-FFmpeg playback, with Qt used only for images and PCM audio output.

Both video decoders share one presentation clock. Pipe backpressure bounds
decoded data: video workers hold one scheduled frame and audio holds <=0.5s.
There is no QMediaPlayer or Qt FFmpeg backend in this playback path.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from typing import IO

from PySide6.QtCore import QCoreApplication, QIODevice, QObject, QThread, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioDevice, QAudioFormat, QAudioSink, QMediaDevices

from core.ffmpeg import noninteractive_run_kwargs, terminate_process
from gui.comparison.decode import ComparisonMedia, DecodeCancelled
from gui.qt_optionals import maybe_none


class PlaybackClock:
    def __init__(self, position: float) -> None:
        self.position = position
        self.started = threading.Event()
        self._epoch = 0.0
        self.audio_master = False
        self._audio_seconds = 0.0

    def start(self) -> None:
        self._epoch = time.monotonic()
        self.started.set()

    def now(self) -> float:
        if self.audio_master:
            return self.position + self._audio_seconds
        return self.position + (time.monotonic() - self._epoch if self.started.is_set() else 0)

    def update_audio(self, seconds: float) -> None:
        self._audio_seconds = max(self._audio_seconds, seconds)

    def wait_until(self, position: float, cancelled: threading.Event) -> bool:
        while not self.started.wait(0.02):
            if cancelled.is_set():
                return False
        while not cancelled.is_set():
            remaining = position - self.now()
            if remaining <= 0:
                return True
            cancelled.wait(min(remaining, 0.005))
        return False


def read_ppm(stream: IO[bytes]) -> QImage | None:
    """Read one FFmpeg P6 frame without reading ahead into the next frame."""
    magic = stream.readline()
    if not magic:
        return None
    if magic.strip() != b"P6":
        raise ValueError("Invalid FFmpeg video pipe header")
    dimensions = stream.readline()
    width, height = (int(value) for value in dimensions.split())
    maximum = stream.readline()
    size = width * height * 3
    if maximum.strip() != b"255" or min(width, height) <= 0 or size > 512 * 1024 * 1024:
        raise ValueError("Unsupported FFmpeg video pipe dimensions")
    pixels = stream.read(size)
    if len(pixels) != size:
        raise ValueError("Incomplete FFmpeg video frame")
    image = QImage.fromData(magic + dimensions + maximum + pixels)
    if image.isNull():
        raise ValueError("Invalid FFmpeg video frame")
    return image


class PipeWorker(QThread):
    failed = Signal(str)

    def __init__(self, command: list[str], parent: QObject) -> None:
        super().__init__(parent)
        self.command = command
        self.cancelled = threading.Event()
        self._done = threading.Event()
        self.errors: deque[str] = deque(maxlen=20)

    def cancel(self) -> None:
        self.cancelled.set()

    def _stderr_line(self, line: str) -> None:
        self.errors.append(line)

    def _consume(self, stream: IO[bytes]) -> None:
        raise NotImplementedError

    def run(self) -> None:
        try:
            with subprocess.Popen[bytes](self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         **noninteractive_run_kwargs()) as process:
                def read_errors() -> None:
                    assert process.stderr is not None
                    for line in process.stderr:
                        self._stderr_line(line.decode("utf-8", errors="replace"))

                def watch_cancel() -> None:
                    while not self._done.wait(0.05):
                        if self.cancelled.is_set():
                            terminate_process(process)
                            return

                reader = threading.Thread(target=read_errors, daemon=True)
                watcher = threading.Thread(target=watch_cancel, daemon=True)
                reader.start()
                watcher.start()
                try:
                    assert process.stdout is not None
                    self._consume(process.stdout)
                    if self.cancelled.is_set():
                        raise DecodeCancelled()
                    process.wait(timeout=5)
                    reader.join(timeout=1)
                    if process.returncode:
                        raise ValueError("".join(self.errors)[-2000:])
                finally:
                    terminate_process(process)
                    self._done.set()
                    reader.join(timeout=1)
                    watcher.join(timeout=1)
        except DecodeCancelled:
            pass
        except Exception as exc:
            if not self.cancelled.is_set():
                self.failed.emit(str(exc))


class VideoPlaybackWorker(PipeWorker):
    prepared = Signal(object, float)
    frame_ready = Signal(object, float)

    def __init__(self, ffmpeg: Path, path: Path, position: float, offset: float,
                 clock: PlaybackClock, parent: QObject) -> None:
        super().__init__([
            str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info",
            "-copyts", "-start_at_zero", "-ss", f"{position:.6f}", "-i", str(path),
            "-map", "0:v:0", "-an", "-sn", "-vf", "scale=round(iw*sar):ih,setsar=1,showinfo=checksum=0",
            "-fps_mode", "passthrough", "-c:v", "ppm", "-pix_fmt", "rgb24", "-f", "image2pipe", "pipe:1",
        ], parent)
        self.clock = clock
        self.offset = offset
        self.timestamps: queue.Queue[float] = queue.Queue()
        self.presented = threading.Event()

    def _stderr_line(self, line: str) -> None:
        match = re.search(r"\bpts_time:([-+\d.eE]+)", line)
        if match and "showinfo" in line:
            self.timestamps.put(float(match.group(1)))
        else:
            super()._stderr_line(line)

    def _timestamp(self) -> float:
        deadline = time.monotonic() + 60
        while not self.cancelled.is_set():
            try:
                return self.timestamps.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("No FFmpeg frame timestamp within 60 seconds")
        raise DecodeCancelled()

    def _consume(self, stream: IO[bytes]) -> None:
        first = True
        lag_started: float | None = None
        while not self.cancelled.is_set():
            image = read_ppm(stream)
            if image is None:
                if first:
                    raise ValueError("No decoded video frames")
                break
            timestamp = self._timestamp()
            if first:
                self.prepared.emit(image, timestamp)
                first = False
            if not self.clock.wait_until(timestamp - self.offset, self.cancelled):
                return
            lateness = self.clock.now() - (timestamp - self.offset)
            if lateness > 0.080:
                if lag_started is None:
                    lag_started = time.monotonic()
                elif time.monotonic() - lag_started > 2:
                    raise ValueError("FFmpeg video decoding cannot keep up with playback")
                continue
            lag_started = None
            self.presented.clear()
            self.frame_ready.emit(image, timestamp)
            # At most one queued GUI frame per worker, even if the UI is busy.
            while not self.presented.wait(0.02):
                if self.cancelled.is_set():
                    return


class PcmBuffer:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self._data = bytearray()
        self._condition = threading.Condition()

    def put(self, data: bytes, cancelled: threading.Event) -> bool:
        with self._condition:
            while len(self._data) + len(data) > self.capacity and not cancelled.is_set():
                self._condition.wait(0.05)
            if cancelled.is_set():
                return False
            self._data.extend(data)
            return True

    def take(self, count: int) -> bytes:
        with self._condition:
            result = bytes(self._data[:count])
            del self._data[:count]
            self._condition.notify_all()
            return result

    def put_back(self, data: bytes) -> None:
        with self._condition:
            self._data[:0] = data


class AudioPlaybackWorker(PipeWorker):
    prepared = Signal()

    def __init__(self, ffmpeg: Path, path: Path, position: float, duration: float, format: QAudioFormat,
                 buffer: PcmBuffer, parent: QObject) -> None:
        sample = {
            QAudioFormat.SampleFormat.UInt8: "u8", QAudioFormat.SampleFormat.Int16: "s16le",
            QAudioFormat.SampleFormat.Int32: "s32le", QAudioFormat.SampleFormat.Float: "f32le",
        }[format.sampleFormat()]
        super().__init__([
            str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{position:.6f}",
            "-i", str(path), "-map", "0:a:0", "-vn", "-sn", "-ar", str(format.sampleRate()),
            "-ac", str(format.channelCount()), "-af", "aresample=async=1:first_pts=0,apad",
            "-t", f"{duration:.6f}", "-f", sample, "pipe:1",
        ], parent)
        self.buffer = buffer

    def _consume(self, stream: IO[bytes]) -> None:
        first = True
        while not self.cancelled.is_set():
            data = stream.read(4096)
            if not data:
                if first:
                    self.prepared.emit()
                break
            if not self.buffer.put(data, self.cancelled):
                return
            if first:
                self.prepared.emit()
                first = False


def audio_output_device() -> QAudioDevice:
    """Initialize Qt's audio-only fallback without loading any codec plugin.

    QAudioSink is native PCM output, but Qt otherwise initializes a media codec
    backend when enumerating devices. The fallback supports the audio APIs on
    all desktop targets. Restore plugin paths immediately for other Qt widgets.
    Packaged builds also exclude the multimedia codec plugins entirely.
    """
    paths = QCoreApplication.libraryPaths()
    try:
        QCoreApplication.setLibraryPaths([])
        return QMediaDevices.defaultAudioOutput()
    finally:
        QCoreApplication.setLibraryPaths(paths)


class PlaybackSession(QObject):
    frame_ready = Signal(int, object, float)
    failed = Signal(str)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.clock = PlaybackClock(0)
        self.workers: list[PipeWorker] = []
        self.sink: QAudioSink | None = None
        self.device: QIODevice | None = None
        self.buffer: PcmBuffer | None = None
        self._generation = 0
        self._video_ready: set[int] = set()
        self._audio_ready = True
        self.audio_available = True

    def start(self, media: ComparisonMedia, position: float, offset: float, audio_side: int) -> None:
        self.stop()
        generation = self._generation
        self.clock = PlaybackClock(position)
        self._video_ready.clear()
        self._audio_ready = True
        self.audio_available = True
        if audio_side in (0, 1) and media.details[audio_side].has_audio:
            device = audio_output_device()
            if device.isNull():
                self.audio_available = False
            else:
                format = QAudioFormat()
                format.setSampleRate(48000)
                format.setChannelCount(2)
                format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
                if not device.isFormatSupported(format):
                    format = device.preferredFormat()
                self.sink = QAudioSink(device, format, self)
                self.sink.setBufferSize(format.bytesForDuration(80_000))
                self.buffer = PcmBuffer(max(8192, format.bytesForDuration(500_000)))
                end = min(media.details[0].duration, media.details[1].duration - offset)
                audio = AudioPlaybackWorker(media.ffmpeg, media.paths[audio_side],
                                            position + (offset if audio_side else 0), end - position,
                                            format, self.buffer, self)
                self.clock.audio_master = True
                self._audio_ready = False

                def audio_prepared() -> None:
                    if generation == self._generation:
                        self._audio_ready = True
                        self._maybe_start()

                audio.prepared.connect(audio_prepared)

                def audio_failed(error: str) -> None:
                    if generation == self._generation:
                        self.failed.emit(error)

                audio.failed.connect(audio_failed)
                self.workers.append(audio)
        for side in (0, 1):
            worker = VideoPlaybackWorker(media.ffmpeg, media.paths[side],
                                         position + (offset if side else 0), offset if side else 0, self.clock, self)

            def prepared(image: object, timestamp: float, side: int = side) -> None:
                if generation == self._generation:
                    self._video_ready.add(side)
                    self._maybe_start()

            def frame(image: object, timestamp: float, side: int = side,
                      worker: VideoPlaybackWorker = worker) -> None:
                try:
                    if (generation == self._generation
                            and self.clock.now() - (timestamp - (offset if side else 0)) <= 0.080):
                        self.frame_ready.emit(side, image, timestamp)
                finally:
                    worker.presented.set()

            def failed(error: str, generation: int = generation) -> None:
                if generation == self._generation:
                    self.failed.emit(error)

            worker.prepared.connect(prepared)
            worker.frame_ready.connect(frame)
            worker.failed.connect(failed)
            self.workers.append(worker)
        for worker in self.workers:
            worker.start()

    def _maybe_start(self) -> None:
        if self._video_ready == {0, 1} and self._audio_ready and not self.clock.started.is_set():
            if self.sink is not None:
                self.device = maybe_none(self.sink.start())
                if self.device is None:
                    self.failed.emit("Cannot open PCM audio output")
                    return
            self.clock.start()

    def tick(self) -> float:
        if self.sink is not None and self.device is not None and self.buffer is not None:
            data = self.buffer.take(max(0, self.sink.bytesFree()))
            if data:
                written = self.device.write(data)
                if written < 0:
                    self.failed.emit("Cannot write PCM audio output")
                    return self.clock.now()
                elif written < len(data):
                    self.buffer.put_back(data[written:])
            self.clock.update_audio(self.sink.processedUSecs() / 1_000_000)
        return self.clock.now()

    def stop(self) -> None:
        self._generation += 1
        if self.sink is not None:
            self.sink.stop()
            self.sink.deleteLater()
            self.sink = None
        self.device = None
        self.buffer = None
        for worker in self.workers:
            worker.cancel()
        for worker in self.workers:
            worker.wait()
            worker.deleteLater()
        self.workers.clear()
