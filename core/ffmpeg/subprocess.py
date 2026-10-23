from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Any

from core.models import OperationCancelledError


def hidden_process_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def noninteractive_run_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
    }

    creationflags = hidden_process_creationflags()
    if creationflags:
        kwargs["creationflags"] = creationflags

    return kwargs


def hidden_popen_kwargs() -> dict[str, Any]:
    creationflags = hidden_process_creationflags()
    return {"creationflags": creationflags} if creationflags else {}


def terminate_process(proc: subprocess.Popen[Any]) -> None:
    """Stop a child process: try SIGTERM, then force-kill if it stays alive."""

    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_cancellable(command: list[str], *, cancel_check: Callable[[], bool] | None = None,
                    **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Keep subprocess.run semantics while allowing a caller to stop its child."""
    if cancel_check is None:
        return subprocess.run(command, **kwargs)
    if cancel_check():
        raise OperationCancelledError("Tool inspection cancelled.")
    check = bool(kwargs.pop("check", False))
    timeout = kwargs.pop("timeout", 60.0)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    deadline = time.monotonic() + (float(timeout) if timeout is not None else 60.0)
    with subprocess.Popen[str](command, **kwargs) as process:
        try:
            while True:
                if cancel_check():
                    raise OperationCancelledError("Tool inspection cancelled.")
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, float(timeout) if timeout is not None else 60.0)
                try:
                    stdout, stderr = process.communicate(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                terminate_process(process)
        if check and process.returncode:
            raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
