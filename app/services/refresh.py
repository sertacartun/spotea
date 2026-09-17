"""Checking a library's followed artists for new releases, at most once per 12-hour UTC window.

There is no Refresh button and no clock: the client asks on open and on return to the foreground,
and this decides whether a check is due (see timeutil.last_refresh_boundary).
"""

import logging
import threading

from app.content_query import followed_artists
from app.database import SessionLocal
from app.models import User
from app.services.artist_sync import sync_artists
from app.timeutil import is_stale, utcnow

logger = logging.getLogger(__name__)

# Process-local is enough: the app runs single-worker by design (see tests/test_single_worker_guard.py).
_user_locks: dict[int, threading.Lock] = {}
_user_locks_guard = threading.Lock()


def _lock_for(user_id: int) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks.setdefault(user_id, threading.Lock())


def is_due(user: User) -> bool:
    return is_stale(user.refreshed_at)


def sync_if_due(user_id: int) -> bool:
    """Sync this user's artists if due; True when the library may have changed since the caller rendered.

    Several tabs and devices ask at once. Only one syncs; the rest wait for it and report its result
    rather than starting their own.
    """
    lock = _lock_for(user_id)
    waited = not lock.acquire(blocking=False)
    if waited:
        lock.acquire()
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user is None or not is_due(user):
                return waited
            try:
                sync_artists(db, followed_artists(db, user_id=user_id).all())
            except Exception:
                db.rollback()
                logger.exception("Release check failed for user %d", user_id)
            # Stamped even on failure or with no artists, so a bad window costs one attempt, not one per open.
            user.refreshed_at = utcnow()
            db.commit()
            return True
    finally:
        lock.release()
