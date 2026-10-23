from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class AsyncTestRunnerTestCase(unittest.TestCase):
    def _run_probe(self, body: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "test_probe.py"
            probe.write_text(textwrap.dedent(body), encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-m", "scripts.run_tests", "discover", "-s", directory,
                 "-p", "test_probe.py", "-v"],
                cwd=ROOT,
                env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_normal_suite_succeeds(self) -> None:
        result = self._run_probe("""
            import unittest
            class Probe(unittest.TestCase):
                def test_ok(self):
                    self.assertEqual(1 + 1, 2)
        """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Ran 1 test", result.stderr)

    def test_unhandled_async_errors_fail_the_process(self) -> None:
        probes = {
            "qt_callback": """
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication
                app = QApplication([])
                def fail():
                    raise RuntimeError('async-probe-qt')
                QTimer.singleShot(0, fail)
                app.processEvents()
            """,
            "thread": """
                import threading
                def fail():
                    raise RuntimeError('async-probe-thread')
                thread = threading.Thread(target=fail)
                thread.start()
                thread.join()
            """,
            "destructor": """
                class Broken:
                    def __del__(self):
                        raise RuntimeError('async-probe-destructor')
                instance = Broken()
                del instance
            """,
            "discovery": """
                pass
            """,
            "final_qt_cleanup": """
                from PySide6.QtCore import QTimer
                from PySide6.QtWidgets import QApplication
                global app
                app = QApplication([])
                def fail():
                    raise RuntimeError('async-probe-final')
                QTimer.singleShot(0, fail)
            """,
        }
        for name, body in probes.items():
            with self.subTest(name=name):
                prefix = ""
                if name == "discovery":
                    prefix = "import sys\ntry:\n    raise RuntimeError('async-probe-discovery')\nexcept RuntimeError:\n    sys.excepthook(*sys.exc_info())\n"
                source = prefix + "import unittest\nclass Probe(unittest.TestCase):\n    def test_probe(self):\n"
                source += textwrap.indent(textwrap.dedent(body).strip(), "        ") + "\n"
                result = self._run_probe(source)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("async-probe-", result.stderr)
                self.assertIn("FAILED", result.stderr)

    def test_assertion_failure_still_fails(self) -> None:
        result = self._run_probe("""
            import unittest
            class Probe(unittest.TestCase):
                def test_failure(self):
                    self.assertEqual(1, 2)
        """)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("AssertionError", result.stderr)
