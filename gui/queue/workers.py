from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QThread, Signal

from core.encoding import prepare_encode_requests
from core.config import bundle_root
from core.i18n import TranslationCatalog, Translator
from core.ffmpeg import find_binary, probe_media_info, discover_ffmpeg_tools
from core.media import collect_video_files
from core.models import (
    EncodeRequest,
    OperationCancelledError,
    VideoFileItem,
)
from gui.queue.state import QueueItemRecord, QueueSourceDraft, create_draft_record


class QueueIntakeWorker(QThread):
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal(str)
    log = Signal(str)
    problem = Signal(str)
    progress = Signal(object)

    def __init__(self, input_path: Path | None, recursive: bool, ffprobe_path: str | None,
                 files: Iterable[VideoFileItem] | None = None, *, ffmpeg_path: str | None = None,
                 paths: Iterable[Path] | None = None, translator: Translator | None = None) -> None:
        super().__init__()
        self.input_path, self.recursive, self.ffprobe_path = input_path, recursive, ffprobe_path
        self.files = list(files) if files is not None else None
        self.ffmpeg_path = ffmpeg_path
        self.paths = list(paths) if paths is not None else None
        self.translator = translator or TranslationCatalog(bundle_dir=bundle_root() / "config/i18n").translator("en")
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            sources: list[tuple[VideoFileItem, Path | None]] = []
            if self.files is not None:
                sources = [(item, None) for item in self.files]
            else:
                for path in self.paths if self.paths is not None else [self.input_path or Path()]:
                    self.progress.emit({"stage": "planning", "state": "scanning", "file_path": str(path)})
                    try:
                        items = collect_video_files(path, self.recursive, self.problem.emit, cancel_check=self._cancel_event.is_set)
                    except OperationCancelledError:
                        raise
                    except (OSError, ValueError, RuntimeError) as exc:
                        if self.paths is None:
                            raise
                        self.problem.emit(self.translator.t("gui.log.intake_failed", file=str(path), error=str(exc)))
                        continue
                    sources.extend((item, path if path.is_dir() else None) for item in items)
            if self._cancel_event.is_set():
                raise OperationCancelledError(self.translator.t("gui.log.intake_cancelled"))
            if self.ffmpeg_path is not None:
                _ffmpeg, probe = discover_ffmpeg_tools(self.ffmpeg_path, self.ffprobe_path)
            else:
                probe = find_binary(self.ffprobe_path, "ffprobe")
            records: list[QueueItemRecord] = []
            for index, (item, input_root) in enumerate(sources):
                self.progress.emit({"stage": "planning", "state": "probing", "file_name": item.path.name,
                                    "percent": index * 100 / max(1, len(sources))})
                if self._cancel_event.is_set():
                    raise OperationCancelledError(self.translator.t("gui.log.intake_cancelled"))
                draft = QueueSourceDraft(item, input_root)
                error = None
                try:
                    draft.media_info = probe_media_info(probe, item.path, cancel_check=self._cancel_event.is_set)
                except OperationCancelledError:
                    raise
                except Exception as exc:
                    error = str(exc)
                    self.problem.emit(self.translator.t("gui.log.intake_failed", file=item.path.name, error=error))
                records.append(create_draft_record(draft, error))
            if self._cancel_event.is_set():
                raise OperationCancelledError(self.translator.t("gui.log.intake_cancelled"))
            self.completed.emit(records)
        except OperationCancelledError:
            self.cancelled.emit(self.translator.t("gui.log.intake_cancelled"))
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
