from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PostEncodeAction(str, Enum):
    DO_NOTHING = "none"
    SLEEP = "sleep"
    SHUTDOWN = "shutdown"
    QUIT = "quit"


POST_ENCODE_ACTION_KEYS: dict[PostEncodeAction, str] = {
    PostEncodeAction.DO_NOTHING: "gui.power.action.none",
    PostEncodeAction.SLEEP: "gui.power.action.sleep",
    PostEncodeAction.SHUTDOWN: "gui.power.action.shutdown",
    PostEncodeAction.QUIT: "gui.power.action.quit",
}

DEFAULT_POWER_TIMEOUT_SEC: float = 5.0


@dataclass(frozen=True, slots=True)
class SystemPowerResult:
    success: bool
    action: PostEncodeAction
    attempted_commands: tuple[tuple[str, ...], ...] = ()
    returncode: int | None = None
    error: str | None = None
    timed_out: bool = False
    stdout: str = ""
    stderr: str = ""


def post_encode_action_key(action: PostEncodeAction) -> str:
    return POST_ENCODE_ACTION_KEYS.get(action, "gui.power.action.none")


def parse_post_encode_action(value: object) -> PostEncodeAction:
    if isinstance(value, PostEncodeAction):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        for action in PostEncodeAction:
            if action.value == normalized:
                return action
    return PostEncodeAction.DO_NOTHING


def _noninteractive_kwargs() -> dict[str, Any]:
    # Mirrors core.ffmpeg.subprocess.noninteractive_run_kwargs; core.media may not
    # depend on core.ffmpeg, so the few lines are repeated here to keep power
    # commands from blocking on stdin or flashing a console window on Windows.
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if creationflags:
            kwargs["creationflags"] = creationflags
    return kwargs


def _run_single_command(
    cmd: list[str],
    timeout_sec: float = DEFAULT_POWER_TIMEOUT_SEC,
) -> tuple[int | None, str, str, str | None, bool]:
    """Execute a single power command safely with timeout and output capture.

    Returns (returncode, stdout, stderr, error_message, timed_out).
    """
    try:
        res = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            **_noninteractive_kwargs(),
        )
        rc = res.returncode
        out = res.stdout or ""
        err = res.stderr or ""
        error_msg = None
        if rc != 0:
            error_msg = (
                err.strip()
                or out.strip()
                or f"Command '{' '.join(cmd)}' failed with exit code {rc}"
            )
        return (rc, out, err, error_msg, False)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or "" if isinstance(exc.stdout, str) else ""
        err = exc.stderr or "" if isinstance(exc.stderr, str) else ""
        return (
            None,
            out,
            err,
            f"Command '{' '.join(cmd)}' timed out after {timeout_sec}s",
            True,
        )
    except FileNotFoundError as exc:
        return (
            None,
            "",
            "",
            f"Command executable not found: {cmd[0]} ({exc})",
            False,
        )
    except PermissionError as exc:
        return (
            None,
            "",
            "",
            f"Permission denied executing command: {cmd[0]} ({exc})",
            False,
        )
    except OSError as exc:
        return (
            None,
            "",
            "",
            f"OS error executing command '{' '.join(cmd)}': {exc}",
            False,
        )
    except Exception as exc:
        return (
            None,
            "",
            "",
            f"Unexpected error executing command '{' '.join(cmd)}': {exc}",
            False,
        )


def execute_system_sleep(timeout_sec: float = DEFAULT_POWER_TIMEOUT_SEC) -> SystemPowerResult:
    """Trigger system sleep across macOS, Windows, and Linux."""
    platform = sys.platform
    action = PostEncodeAction.SLEEP

    if platform == "darwin":
        cmd1 = ["pmset", "sleepnow"]
        rc, out, err, error_msg, timed_out = _run_single_command(cmd1, timeout_sec=timeout_sec)
        if rc == 0:
            return SystemPowerResult(
                success=True,
                action=action,
                attempted_commands=(tuple(cmd1),),
                returncode=0,
                error=None,
                timed_out=False,
                stdout=out,
                stderr=err,
            )

        cmd2 = ["osascript", "-e", 'tell application "System Events" to sleep']
        rc2, out2, err2, error_msg2, timed_out2 = _run_single_command(cmd2, timeout_sec=timeout_sec)
        attempted = (tuple(cmd1), tuple(cmd2))
        if rc2 == 0:
            return SystemPowerResult(
                success=True,
                action=action,
                attempted_commands=attempted,
                returncode=0,
                error=None,
                timed_out=False,
                stdout=out2,
                stderr=err2,
            )
        return SystemPowerResult(
            success=False,
            action=action,
            attempted_commands=attempted,
            returncode=rc2 if rc2 is not None else rc,
            error=error_msg2 or error_msg or "Failed to trigger sleep on macOS via pmset and osascript",
            timed_out=timed_out or timed_out2,
            stdout=out2 or out,
            stderr=err2 or err,
        )

    elif platform == "win32":
        cmd = ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"]
        rc, out, err, error_msg, timed_out = _run_single_command(cmd, timeout_sec=timeout_sec)
        return SystemPowerResult(
            success=(rc == 0),
            action=action,
            attempted_commands=(tuple(cmd),),
            returncode=rc,
            error=error_msg,
            timed_out=timed_out,
            stdout=out,
            stderr=err,
        )

    elif platform.startswith("linux"):
        cmd = ["systemctl", "suspend"]
        rc, out, err, error_msg, timed_out = _run_single_command(cmd, timeout_sec=timeout_sec)
        return SystemPowerResult(
            success=(rc == 0),
            action=action,
            attempted_commands=(tuple(cmd),),
            returncode=rc,
            error=error_msg,
            timed_out=timed_out,
            stdout=out,
            stderr=err,
        )

    else:
        return SystemPowerResult(
            success=False,
            action=action,
            attempted_commands=(),
            returncode=None,
            error=f"Unsupported platform for system sleep: {platform}",
            timed_out=False,
        )


def execute_system_shutdown(timeout_sec: float = DEFAULT_POWER_TIMEOUT_SEC) -> SystemPowerResult:
    """Trigger system shutdown across macOS, Windows, and Linux."""
    platform = sys.platform
    action = PostEncodeAction.SHUTDOWN

    if platform == "darwin":
        cmd = ["osascript", "-e", 'tell application "System Events" to shut down']
        rc, out, err, error_msg, timed_out = _run_single_command(cmd, timeout_sec=timeout_sec)
        return SystemPowerResult(
            success=(rc == 0),
            action=action,
            attempted_commands=(tuple(cmd),),
            returncode=rc,
            error=error_msg,
            timed_out=timed_out,
            stdout=out,
            stderr=err,
        )

    elif platform == "win32":
        cmd = ["shutdown", "/s", "/t", "0"]
        rc, out, err, error_msg, timed_out = _run_single_command(cmd, timeout_sec=timeout_sec)
        return SystemPowerResult(
            success=(rc == 0),
            action=action,
            attempted_commands=(tuple(cmd),),
            returncode=rc,
            error=error_msg,
            timed_out=timed_out,
            stdout=out,
            stderr=err,
        )

    elif platform.startswith("linux"):
        cmd = ["systemctl", "poweroff"]
        rc, out, err, error_msg, timed_out = _run_single_command(cmd, timeout_sec=timeout_sec)
        return SystemPowerResult(
            success=(rc == 0),
            action=action,
            attempted_commands=(tuple(cmd),),
            returncode=rc,
            error=error_msg,
            timed_out=timed_out,
            stdout=out,
            stderr=err,
        )

    else:
        return SystemPowerResult(
            success=False,
            action=action,
            attempted_commands=(),
            returncode=None,
            error=f"Unsupported platform for system shutdown: {platform}",
            timed_out=False,
        )


def execute_power_action(
    action: PostEncodeAction | str,
    timeout_sec: float = DEFAULT_POWER_TIMEOUT_SEC,
) -> SystemPowerResult:
    """Dispatch requested power action and return structured execution result."""
    parsed_action = parse_post_encode_action(action)
    if parsed_action == PostEncodeAction.SLEEP:
        return execute_system_sleep(timeout_sec=timeout_sec)
    if parsed_action == PostEncodeAction.SHUTDOWN:
        return execute_system_shutdown(timeout_sec=timeout_sec)
    return SystemPowerResult(
        success=True,
        action=parsed_action,
        attempted_commands=(),
        returncode=0,
        error=None,
        timed_out=False,
    )
