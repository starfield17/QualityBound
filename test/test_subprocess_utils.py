from __future__ import annotations

import io
import subprocess
import unittest
from unittest.mock import patch

from core.encoding.process import _start_command_process
from core.ffmpeg.probe import _run_command
from core.ffmpeg.subprocess import (
    hidden_popen_kwargs,
    hidden_process_creationflags,
    noninteractive_run_kwargs,
)
from gui.gui_workers import _safe_console_print


class SubprocessPolicyTestCase(unittest.TestCase):
    def test_non_windows_run_kwargs_use_devnull_without_creation_flag(self) -> None:
        with patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=0):
            kwargs = noninteractive_run_kwargs()

        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("creationflags", kwargs)

    def test_windows_run_kwargs_use_devnull_and_create_no_window(self) -> None:
        creationflags = 0x08000000
        with patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags):
            kwargs = noninteractive_run_kwargs()

        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["creationflags"], creationflags)

    def test_hidden_process_creationflags_reads_windows_constant(self) -> None:
        creationflags = 0x08000000
        with (
            patch("core.ffmpeg.subprocess.os.name", "nt"),
            patch.object(subprocess, "CREATE_NO_WINDOW", creationflags, create=True),
        ):
            self.assertEqual(hidden_process_creationflags(), creationflags)

    def test_non_windows_popen_kwargs_are_empty(self) -> None:
        with patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=0):
            self.assertEqual(hidden_popen_kwargs(), {})

    def test_windows_popen_kwargs_contain_creation_flag(self) -> None:
        creationflags = 0x08000000
        with patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags):
            self.assertEqual(hidden_popen_kwargs(), {"creationflags": creationflags})


class SubprocessCallSiteTestCase(unittest.TestCase):
    def test_probe_command_uses_noninteractive_stdin(self) -> None:
        with patch("core.ffmpeg.probe.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 0
            popen.return_value.poll.return_value = 0
            result = _run_command(["ffprobe"])

        self.assertEqual(result.returncode, 0)
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_probe_command_forwards_windows_creation_flag(self) -> None:
        creationflags = 0x08000000
        with (
            patch("core.ffmpeg.subprocess.hidden_process_creationflags", return_value=creationflags),
            patch("core.ffmpeg.probe.subprocess.Popen") as popen,
        ):
            popen.return_value.wait.return_value = 0
            popen.return_value.poll.return_value = 0
            _run_command(["ffprobe"])

        self.assertEqual(popen.call_args.kwargs["creationflags"], creationflags)

    def test_encoding_process_keeps_pipe_stdin_and_forwards_windows_flag(self) -> None:
        creationflags = 0x08000000
        with (
            patch("core.encoding.process.hidden_popen_kwargs", return_value={"creationflags": creationflags}),
            patch("core.encoding.process.subprocess.Popen") as popen,
        ):
            _start_command_process(["ffmpeg"])

        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.PIPE)
        self.assertEqual(kwargs["creationflags"], creationflags)


class SafeConsolePrintTestCase(unittest.TestCase):
    def test_none_stdout_is_ignored(self) -> None:
        with patch("gui.gui_workers.sys.stdout", None):
            _safe_console_print("message")

    def test_broken_stdout_is_ignored(self) -> None:
        class BrokenStream:
            def write(self, _message: str) -> int:
                raise OSError("invalid handle")

            def flush(self) -> None:
                raise OSError("invalid handle")

        with patch("gui.gui_workers.sys.stdout", BrokenStream()):
            _safe_console_print("message")

    def test_stdout_flush_failure_is_ignored(self) -> None:
        class FlushFailingStream:
            def __init__(self) -> None:
                self.value = ""

            def write(self, message: str) -> int:
                self.value += message
                return len(message)

            def flush(self) -> None:
                raise OSError("invalid handle")

        stream = FlushFailingStream()
        with patch("gui.gui_workers.sys.stdout", stream):
            _safe_console_print("message")

        self.assertEqual(stream.value, "message\n")

    def test_valid_stdout_receives_message(self) -> None:
        stream = io.StringIO()
        with patch("gui.gui_workers.sys.stdout", stream):
            _safe_console_print("message")

        self.assertEqual(stream.getvalue(), "message\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
