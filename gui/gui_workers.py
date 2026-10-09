from __future__ import annotations

import copy
import sys
import threading
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from core.encoding import prepare_encode_requests
from core.ffmpeg import ensure_encoder_capabilities, find_binary, probe_media_info, discover_ffmpeg_tools
from core.media import collect_video_files
from core.models import (
    EncodeRequest,
    OperationCancelledError,
    VideoFileItem,
    VmafBackend,
)
from core.smart import VMAF_PRODUCTION_MODELS, probe_vmaf_runtime
from gui.queue_state import QueueSourceDraft, create_draft_record


def _safe_console_print(message: str) -> None:
    stream = sys.stdout
    if stream is None:
        return

    try:
        print(message, file=stream, flush=True)
    except (OSError, ValueError):
        pass


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


class QueueIntakeWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    log = Signal(str)
    progress = Signal(object)

    def __init__(self, input_path: Path | None, recursive: bool, ffprobe_path: str | None,
                 files: Iterable[VideoFileItem] | None = None, *, ffmpeg_path: str | None = None) -> None:
        super().__init__()
        self.input_path, self.recursive, self.ffprobe_path = input_path, recursive, ffprobe_path
        self.files = list(files) if files is not None else None
        self.ffmpeg_path = ffmpeg_path
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            files = self.files if self.files is not None else collect_video_files(
                self.input_path or Path(), self.recursive, self.log.emit)
            if self.ffmpeg_path is not None:
                _ffmpeg, probe = discover_ffmpeg_tools(self.ffmpeg_path, self.ffprobe_path)
            else:
                probe = find_binary(self.ffprobe_path, "ffprobe")
            input_root = self.input_path if self.input_path is not None and self.input_path.is_dir() else None
            records = []
            for item in files:
                if self._cancel_event.is_set():
                    raise OperationCancelledError("Source intake cancelled.")
                draft = QueueSourceDraft(item, input_root)
                error = None
                try:
                    draft.media_info = probe_media_info(probe, item.path, cancel_check=self._cancel_event.is_set)
                except OperationCancelledError:
                    raise
                except Exception as exc:
                    error = str(exc)
                    self.log.emit(f"Cannot read {item.path.name}: {error}")
                records.append(create_draft_record(draft, error))
            if self._cancel_event.is_set():
                raise OperationCancelledError("Source intake cancelled.")
            self.completed.emit(records)
        except OperationCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class QueuePrepareWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    log = Signal(str)
    progress = Signal(object)

    def __init__(self, requests: list[EncodeRequest], workdir: Path,
                 ffmpeg_path: str | None, ffprobe_path: str | None) -> None:
        super().__init__()
        self.requests = copy.deepcopy(requests)
        self.workdir, self.ffmpeg_path, self.ffprobe_path = workdir, ffmpeg_path, ffprobe_path
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:
        try:
            plan = prepare_encode_requests(self.requests, workdir=self.workdir,
                                           ffmpeg_path=self.ffmpeg_path, ffprobe_path=self.ffprobe_path,
                                           progress_callback=self.log.emit, cancel_check=self._cancel_event.is_set)
            self.completed.emit(plan)
        except OperationCancelledError as exc:
            self.cancelled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))
