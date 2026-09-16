"""In-memory progress for polled background work. Single-process only: another worker sees none of it.

Finished entries linger until expiry, since a fast job can finish before the first poll."""

import threading
import time

DEFAULT_TTL_SECONDS = 60 * 60


class ProgressRegistry[K, V]:
    """Expiry tracks set() calls only — mutating a stored value does not keep it alive."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[K, tuple[float, V]] = {}
        # The sweep is a read-then-delete across threads, which the GIL alone doesn't make atomic.
        self._lock = threading.Lock()

    def set(self, key: K, value: V) -> None:
        with self._lock:
            self._sweep()
            self._entries[key] = (time.monotonic(), value)

    def get(self, key: K, default: V | None = None) -> V | None:
        with self._lock:
            self._sweep()
            entry = self._entries.get(key)
            return entry[1] if entry is not None else default

    def discard(self, key: K) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def _sweep(self) -> None:
        # Caller holds the lock.
        cutoff = time.monotonic() - self._ttl_seconds
        stale = [key for key, (written_at, _) in self._entries.items() if written_at < cutoff]
        for key in stale:
            del self._entries[key]
