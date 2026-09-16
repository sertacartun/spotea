"""A few-at-a-time, paced download queue for whole lists.

Not FastAPI BackgroundTasks: those run in series per response and one failure skips the
rest. Threads, not asyncio tasks: enqueuers are sync route handlers on the threadpool.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable

logger = logging.getLogger(__name__)

# A yt-dlp fetch is ~1.5s, so three at once makes a 40-song album a minute or so; more
# would be a burst from one residential IP, which is what YouTube answers with 403s.
WORKERS = 3

# Per worker, after each track: keeps the three from firing back to back all day.
GAP_SECONDS = 1.0


class DownloadQueue:
    def __init__(
        self,
        handler: Callable[[int], None],
        workers: int = WORKERS,
        gap_seconds: float = GAP_SECONDS,
    ) -> None:
        self._handler = handler
        self._workers = workers
        self._gap_seconds = gap_seconds
        self._pending: deque[int] = deque()
        # Pending plus running, so a poll mid-download doesn't enqueue it again.
        self._queued: set[int] = set()
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._threads: list[threading.Thread] = []

    def enqueue(self, ids: Iterable[int]) -> None:
        with self._lock:
            for item in ids:
                if item in self._queued:
                    continue
                self._queued.add(item)
                self._pending.append(item)
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            while self._pending and len(self._threads) < self._workers:
                thread = threading.Thread(target=self._run, name="download-queue", daemon=True)
                thread.start()
                self._threads.append(thread)
            self._changed.notify_all()

    def is_queued(self, item: int) -> bool:
        with self._lock:
            return item in self._queued

    def wait_until_idle(self, timeout: float) -> bool:
        """For tests: True once nothing is pending or running."""
        with self._lock:
            return self._changed.wait_for(lambda: not self._queued, timeout=timeout)

    def _run(self) -> None:
        while True:
            with self._lock:
                self._changed.wait_for(lambda: self._pending)
                item = self._pending.popleft()
            try:
                self._handler(item)
            except Exception:
                # One bad track must not stall the rest of the list.
                logger.exception("Queued download %s failed", item)
            finally:
                with self._lock:
                    self._queued.discard(item)
                    self._changed.notify_all()
            if self._gap_seconds:
                time.sleep(self._gap_seconds)
