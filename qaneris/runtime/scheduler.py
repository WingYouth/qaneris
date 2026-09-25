"""Small bounded in-process scheduler."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor


class ThreadRunScheduler:
    def __init__(self, max_workers: int = 4):
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="qaneris-run")

    def submit(self, run_id: str, work: Callable[[str], None]) -> None:
        self.pool.submit(work, run_id)


class InlineRunScheduler:
    def submit(self, run_id: str, work: Callable[[str], None]) -> None:
        work(run_id)
