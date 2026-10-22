from __future__ import annotations

import sys
import threading

from PySide6.QtCore import QThread, Signal

from core.ffmpeg import ensure_encoder_capabilities, find_binary
from core.models import (
    VmafBackend,
    VmafRuntimeSupport,
)
from core.smart import VMAF_PRODUCTION_MODELS, probe_vmaf_runtime


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
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def _emit_log(self, message: str) -> None:
        self.log.emit(message)
        _safe_console_print(message)

    def run(self) -> None:
        try:
            if self._cancelled():
                return
            ffmpeg = find_binary(self.ffmpeg_path, "ffmpeg")
            capabilities = ensure_encoder_capabilities(
                ffmpeg,
                force_refresh=self.force_refresh,
                progress_callback=self._emit_log,
            )
            if self._cancelled():
                return
            vmaf_models: list[VmafRuntimeSupport] = []
            for model in VMAF_PRODUCTION_MODELS:
                if self._cancelled():
                    return
                vmaf_models.append(probe_vmaf_runtime(ffmpeg, model, VmafBackend.CPU))
            vmaf_errors = [
                f"{support.model}: {support.error_message}"
                for support in vmaf_models
                if not support.runnable and support.error_message
            ]
            capabilities = dict(capabilities)
            if self._cancelled():
                return
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
