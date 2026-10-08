from __future__ import annotations

import sys
import subprocess
import threading
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from core.encoding import build_encode_plan, execute_plan
from core.ffmpeg import ensure_encoder_capabilities, find_binary
from core.models import (
    EncodeOptions,
    OperationCancelledError,
    VideoFileItem,
    VmafBackend,
)
from core.media import collect_video_files
from core.progress_events import ProgressEvent
from core.smart import VMAF_PRODUCTION_MODELS, probe_vmaf_runtime


def _safe_console_print(message: str) -> None:
    stream = sys.stdout
    if stream is None:
        return

    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        pass


class ScanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, input_path: Path, recursive: bool) -> None:
        super().__init__()
        self.input_path = input_path
        self.recursive = recursive

    def run(self) -> None:
        try:
            self.completed.emit(collect_video_files(self.input_path, self.recursive))
        except Exception as exc:
            self.failed.emit(str(exc))


class EncoderCapabilityDetectWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    log = Signal(str)

    def __init__(
        self,
        ffmpeg_path: str | None,
        *,
        force_refresh: bool = False,
    ) -> None:
        super().__init__()
        self.ffmpeg_path = ffmpeg_path
        self.force_refresh = force_refresh

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)
        _safe_console_print(message)

    def run(self) -> None:
        try:
            ffmpeg = find_binary(self.ffmpeg_path, "ffmpeg")
            capabilities = ensure_encoder_capabilities(
                ffmpeg,
                force_refresh=self.force_refresh,
                progress_callback=self._emit_log,
            )
            vmaf_models = [
                probe_vmaf_runtime(ffmpeg, model, VmafBackend.CPU)
                for model in VMAF_PRODUCTION_MODELS
            ]
            vmaf_errors = [
                f"{support.model}: {support.error_message}"
                for support in vmaf_models
                if not support.runnable and support.error_message
            ]
            capabilities = dict(capabilities)
            capabilities["vmaf"] = {
                "runnable": all(support.runnable for support in vmaf_models),
                "models": {
                    support.model: {
                        "runnable": support.runnable,
                        "error_message": support.error_message,
                    }
                    for support in vmaf_models
                },
                "backend": VmafBackend.CPU.value,
                "error_message": "; ".join(vmaf_errors) or None,
            }
            self.completed.emit(capabilities)
        except Exception as exc:
            self.failed.emit(str(exc))


class PlanWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    log = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        input_path: Path | None,
        options: EncodeOptions,
        output_dir: Path | None,
        workdir: Path,
        ffmpeg_path: str | None,
        ffprobe_path: str | None,
        files: Iterable[VideoFileItem] | None = None,
    ) -> None:
        super().__init__()
        self.input_path = input_path
        self.options = options
        self.output_dir = output_dir
        self.workdir = workdir
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self.files = list(files) if files is not None else None
        self._cancel_event = threading.Event()

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)
        _safe_console_print(message)

    def _emit_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            plan = build_encode_plan(
                input_path=self.input_path,
                options=self.options,
                output_dir=self.output_dir,
                workdir=self.workdir,
                ffmpeg_path=self.ffmpeg_path,
                ffprobe_path=self.ffprobe_path,
                files=self.files,
                progress_callback=self._emit_log,
                progress_event_callback=self._emit_progress,
                cancel_check=self._cancel_event.is_set,
            )
            self.completed.emit(plan)
        except OperationCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class EncodeWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    log = Signal(str)
    progress = Signal(object)

    def __init__(
        self,
        input_path: Path,
        options: EncodeOptions,
        output_dir: Path | None,
        workdir: Path,
        ffmpeg_path: str | None,
        ffprobe_path: str | None,
    ) -> None:
        super().__init__()
        self.input_path = input_path
        self.options = options
        self.output_dir = output_dir
        self.workdir = workdir
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path
        self._cancel_event = threading.Event()
        self._current_process: subprocess.Popen[str] | None = None

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)
        _safe_console_print(message)

    def _emit_progress(self, event: ProgressEvent) -> None:
        self.progress.emit(event)

    def _set_current_process(self, proc: subprocess.Popen[str] | None) -> None:
        self._current_process = proc

    def cancel(self) -> None:
        self._cancel_event.set()
        if self._current_process is not None:
            try:
                self._current_process.terminate()
            except Exception:
                pass

    def run(self) -> None:
        try:
            plan = build_encode_plan(
                input_path=self.input_path,
                options=self.options,
                output_dir=self.output_dir,
                workdir=self.workdir,
                ffmpeg_path=self.ffmpeg_path,
                ffprobe_path=self.ffprobe_path,
                progress_callback=self._emit_log,
                progress_event_callback=self._emit_progress,
                cancel_check=self._cancel_event.is_set,
            )
            results = execute_plan(
                plan,
                self.workdir,
                log_callback=self._emit_log,
                progress_callback=self._emit_progress,
                cancel_check=self._cancel_event.is_set,
                process_callback=self._set_current_process,
            )
            self.completed.emit((plan, results))
        except OperationCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))
