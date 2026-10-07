from __future__ import annotations

from contextlib import contextmanager
import os
import threading
from collections.abc import Callable, Generator

from core.models import OperationCancelledError


MAX_ANALYSIS_JOBS = 2
MIN_CPUS_FOR_PARALLEL_ANALYSIS = 8


def analysis_concurrency_limit(*, cpu_count: int | None = None) -> int:
    """How many Smart analyses may run at once.

    Analysis is cheaper than a full encode, but each job still runs CPU VMAF.
    Two jobs are enough to overlap I/O and startup; more would oversubscribe
    the VMAF thread budget. Encode never shares this slot.
    """
    count = int(cpu_count) if cpu_count is not None else (os.cpu_count() or 4)
    if count < 1:
        count = 1
    if count >= MIN_CPUS_FOR_PARALLEL_ANALYSIS:
        return MAX_ANALYSIS_JOBS
    return 1


# One module owns the process-wide resource and its release discipline.  It is
# deliberately independent from Smart workflow code so all analysis paths use
# the same semaphore.
SMART_ANALYSIS_SEMAPHORE = threading.Semaphore(analysis_concurrency_limit())


def acquire_analysis_slot(cancel_check: Callable[[], bool] | None) -> None:
    while not SMART_ANALYSIS_SEMAPHORE.acquire(timeout=0.1):
        if cancel_check is not None and cancel_check():
            raise OperationCancelledError("Smart analysis cancelled.")


@contextmanager
def analysis_slot(cancel_check: Callable[[], bool] | None) -> Generator[None, None, None]:
    """Acquire a cancellable analysis slot and always release it."""
    acquire_analysis_slot(cancel_check)
    try:
        yield
    finally:
        SMART_ANALYSIS_SEMAPHORE.release()
