"""Run unittest with unhandled Python/Qt callback errors included in the result.

Use ``python -m scripts.run_tests discover -s test -p 'test_*.py' -v``.
The hooks cover discovery through final garbage collection and pending Qt work.
No log matching or Qt warning suppression is involved.
"""

from __future__ import annotations

import gc
import sys
import threading
import traceback
import unittest
from contextlib import contextmanager
from types import TracebackType
from typing import Any, Iterator


@contextmanager
def capture_unhandled_errors() -> Iterator[list[str]]:
    errors: list[str] = []
    lock = threading.Lock()
    old_sys_hook = sys.excepthook
    old_thread_hook = threading.excepthook
    old_unraisable_hook = sys.unraisablehook

    def record(kind: str, exc_type: Any, value: Any, tb: TracebackType | None) -> None:
        detail = kind + "\n" + "".join(traceback.format_exception(exc_type, value, tb))
        # Keep formatted text rather than tracebacks: retaining their frames
        # would change the lifetime of the very Qt objects being tested.
        with lock:
            errors.append(detail)

    def sys_hook(exc_type: type[BaseException], value: BaseException, tb: TracebackType | None) -> None:
        record("Unhandled Python/Qt callback exception", exc_type, value, tb)
        old_sys_hook(exc_type, value, tb)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        record("Unhandled thread exception", args.exc_type, args.exc_value, args.exc_traceback)
        old_thread_hook(args)

    def unraisable_hook(args: Any) -> None:
        record("Unraisable exception", args.exc_type, args.exc_value, args.exc_traceback)
        old_unraisable_hook(args)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    sys.unraisablehook = unraisable_hook
    try:
        yield errors
    finally:
        sys.excepthook = old_sys_hook
        threading.excepthook = old_thread_hook
        sys.unraisablehook = old_unraisable_hook


def drain_pending_work() -> None:
    """Observe finalizers and a bounded number of pending Qt event turns."""
    gc.collect()
    if "PySide6.QtCore" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent

    app = QCoreApplication.instance()
    if app is None:
        return
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        gc.collect()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    initial_threads = set(threading.enumerate())
    with capture_unhandled_errors() as errors:
        class CheckedResult(unittest.TextTestResult):
            def stopTestRun(self) -> None:
                drain_pending_work()
                # A worker that outlives its test could raise only after the
                # result is printed and the hooks restored. Tests must finish
                # their own workers before the suite can be called successful.
                for thread in threading.enumerate():
                    if thread not in initial_threads and not thread.daemon:
                        errors.append(f"Test left a non-daemon thread running: {thread.name}")
                for detail in errors:
                    # Add before TextTestRunner prints its summary and checks
                    # wasSuccessful(), so a callback error cannot produce OK.
                    probe = unittest.FunctionTestCase(lambda: None, description="Unhandled asynchronous exception")
                    try:
                        raise RuntimeError(detail)
                    except RuntimeError:
                        self.addError(probe, sys.exc_info())
                super().stopTestRun()

        class CheckedRunner(unittest.TextTestRunner):
            def _makeResult(self) -> CheckedResult:
                return CheckedResult(self.stream, self.descriptions, self.verbosity, durations=self.durations)

        program = unittest.main(
            module=None,
            argv=[sys.argv[0], *(args or ["discover", "-s", "test", "-v"])],
            testRunner=CheckedRunner,
            exit=False,
        )
        if not program.result.wasSuccessful():
            return 1
        return 0 if program.result.testsRun else 5


if __name__ == "__main__":
    raise SystemExit(main())
