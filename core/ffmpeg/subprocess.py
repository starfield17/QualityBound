from __future__ import annotations

import os
import subprocess
from typing import Any


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
