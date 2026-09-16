import asyncio
import contextlib
import logging

from app.database import SessionLocal
from app.storage import sweep_cache, sweep_orphans, sweep_stale_previews

logger = logging.getLogger(__name__)

# Owned here so /health can check is_alive.
_task: asyncio.Task | None = None

# Pause after a failure so a persistent error doesn't become a tight traceback loop.
ERROR_BACKOFF_SECONDS = 60

TICK_SECONDS = 15 * 60


def _sweep_disk() -> None:
    with SessionLocal() as db:
        sweep_orphans(db)
        sweep_stale_previews(db)
        sweep_cache(db)


async def run_scheduler() -> None:
    """Guarded: an unhandled error would kill this task silently while /health stayed green."""
    while True:
        # CancelledError is a BaseException, so shutdown passes through.
        try:
            await asyncio.to_thread(_sweep_disk)
        except Exception:
            logger.exception("Sweep cycle failed; retrying in %ds", ERROR_BACKOFF_SECONDS)
            await asyncio.sleep(ERROR_BACKOFF_SECONDS)
            continue
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    global _task
    _task = asyncio.create_task(run_scheduler())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None


def is_alive() -> bool:
    """False means cleanup stopped for good; /health reports it so compose restarts the container."""
    return _task is not None and not _task.done()
