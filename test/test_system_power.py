from __future__ import annotations

import subprocess
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

from core.media import (
    PostEncodeAction,
    SystemPowerResult,
    execute_power_action,
    execute_system_shutdown,
    execute_system_sleep,
    parse_post_encode_action,
    post_encode_action_key,
)


class SystemPowerTestCase(unittest.TestCase):
    def test_parse_post_encode_action(self) -> None:
        self.assertEqual(parse_post_encode_action("none"), PostEncodeAction.DO_NOTHING)
        self.assertEqual(parse_post_encode_action("sleep"), PostEncodeAction.SLEEP)
        self.assertEqual(parse_post_encode_action("shutdown"), PostEncodeAction.SHUTDOWN)
        self.assertEqual(parse_post_encode_action("quit"), PostEncodeAction.QUIT)
        self.assertEqual(parse_post_encode_action("  SLEEP  "), PostEncodeAction.SLEEP)
        self.assertEqual(parse_post_encode_action("invalid_choice"), PostEncodeAction.DO_NOTHING)
        self.assertEqual(parse_post_encode_action(None), PostEncodeAction.DO_NOTHING)
        self.assertEqual(parse_post_encode_action(PostEncodeAction.SHUTDOWN), PostEncodeAction.SHUTDOWN)

    def test_post_encode_action_key(self) -> None:
        self.assertEqual(post_encode_action_key(PostEncodeAction.DO_NOTHING), "gui.power.action.none")
        self.assertEqual(post_encode_action_key(PostEncodeAction.SLEEP), "gui.power.action.sleep")
        self.assertEqual(post_encode_action_key(PostEncodeAction.SHUTDOWN), "gui.power.action.shutdown")
        self.assertEqual(post_encode_action_key(PostEncodeAction.QUIT), "gui.power.action.quit")

    @patch("subprocess.run")
    def test_execute_system_sleep_darwin_pmset_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Sleep triggered", stderr="")
        with patch("sys.platform", "darwin"):
            result = execute_system_sleep()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SLEEP)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.attempted_commands, (("pmset", "sleepnow"),))
            self.assertIsNone(result.error)
            self.assertFalse(result.timed_out)
            mock_run.assert_called_once_with(
                ["pmset", "sleepnow"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_sleep_darwin_fallback_to_applescript(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="pmset permission denied"),
            MagicMock(returncode=0, stdout="AppleScript sleep ok", stderr="")
        ]
        with patch("sys.platform", "darwin"):
            result = execute_system_sleep()
            self.assertTrue(result.success)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.attempted_commands,
                (
                    ("pmset", "sleepnow"),
                    ("osascript", "-e", 'tell application "System Events" to sleep'),
                ),
            )
            self.assertEqual(mock_run.call_count, 2)
            mock_run.assert_called_with(
                ["osascript", "-e", 'tell application "System Events" to sleep'],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_sleep_darwin_both_fail(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="pmset failed"),
            MagicMock(returncode=2, stdout="", stderr="osascript failed"),
        ]
        with patch("sys.platform", "darwin"):
            result = execute_system_sleep()
            self.assertFalse(result.success)
            self.assertEqual(result.returncode, 2)
            self.assertIn("osascript failed", result.error or "")
            self.assertEqual(len(result.attempted_commands), 2)

    @patch("subprocess.run")
    def test_execute_system_shutdown_darwin_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "darwin"):
            result = execute_system_shutdown()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SHUTDOWN)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                result.attempted_commands,
                (("osascript", "-e", 'tell application "System Events" to shut down'),),
            )
            mock_run.assert_called_once_with(
                ["osascript", "-e", 'tell application "System Events" to shut down'],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_shutdown_darwin_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=127, stdout="", stderr="Command not permitted")
        with patch("sys.platform", "darwin"):
            result = execute_system_shutdown()
            self.assertFalse(result.success)
            self.assertEqual(result.returncode, 127)
            self.assertEqual(result.error, "Command not permitted")

    @patch("subprocess.run")
    def test_execute_system_sleep_win32(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "win32"):
            result = execute_system_sleep()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SLEEP)
            mock_run.assert_called_once_with(
                ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_shutdown_win32(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "win32"):
            result = execute_system_shutdown()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SHUTDOWN)
            mock_run.assert_called_once_with(
                ["shutdown", "/s", "/t", "0"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_sleep_linux(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "linux"):
            result = execute_system_sleep()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SLEEP)
            mock_run.assert_called_once_with(
                ["systemctl", "suspend"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    @patch("subprocess.run")
    def test_execute_system_shutdown_linux(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "linux"):
            result = execute_system_shutdown()
            self.assertTrue(result.success)
            self.assertEqual(result.action, PostEncodeAction.SHUTDOWN)
            mock_run.assert_called_once_with(
                ["systemctl", "poweroff"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5.0,
                stdin=subprocess.DEVNULL,
            )

    def test_unsupported_platform(self) -> None:
        with patch("sys.platform", "freebsd"):
            res_sleep = execute_system_sleep()
            self.assertFalse(res_sleep.success)
            self.assertIn("Unsupported platform", res_sleep.error or "")

            res_shutdown = execute_system_shutdown()
            self.assertFalse(res_shutdown.success)
            self.assertIn("Unsupported platform", res_shutdown.error or "")

    @patch("subprocess.run")
    def test_timeout_handling(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["systemctl", "suspend"], timeout=5.0)
        with patch("sys.platform", "linux"):
            result = execute_system_sleep()
            self.assertFalse(result.success)
            self.assertTrue(result.timed_out)
            self.assertIn("timed out", result.error or "")

    @patch("subprocess.run")
    def test_routine_exceptions_do_not_raise(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("Executable not found")
        with patch("sys.platform", "linux"):
            result = execute_system_shutdown()
            self.assertFalse(result.success)
            self.assertIn("not found", result.error or "")

        mock_run.side_effect = PermissionError("Operation not permitted")
        with patch("sys.platform", "linux"):
            result = execute_system_shutdown()
            self.assertFalse(result.success)
            self.assertIn("Permission denied", result.error or "")

    @patch("subprocess.run")
    def test_execute_power_action_dispatcher(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("sys.platform", "darwin"):
            res_sleep = execute_power_action(PostEncodeAction.SLEEP)
            self.assertTrue(res_sleep.success)
            self.assertEqual(res_sleep.action, PostEncodeAction.SLEEP)

            res_shutdown = execute_power_action("shutdown")
            self.assertTrue(res_shutdown.success)
            self.assertEqual(res_shutdown.action, PostEncodeAction.SHUTDOWN)

            res_none = execute_power_action(PostEncodeAction.DO_NOTHING)
            self.assertTrue(res_none.success)
            self.assertEqual(res_none.action, PostEncodeAction.DO_NOTHING)
            self.assertEqual(res_none.attempted_commands, ())

            res_quit = execute_power_action(PostEncodeAction.QUIT)
            self.assertTrue(res_quit.success)
            self.assertEqual(res_quit.action, PostEncodeAction.QUIT)

    def test_system_power_result_immutability(self) -> None:
        result = SystemPowerResult(
            success=True,
            action=PostEncodeAction.SLEEP,
            attempted_commands=(("pmset", "sleepnow"),),
            returncode=0,
        )
        with self.assertRaises(FrozenInstanceError):
            result.success = False  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main(verbosity=2)
