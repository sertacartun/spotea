"""Refreshing a library's new releases when the app is opened, one refresh per user at a time."""

import logging
import threading

from app.content_query import followed_artists
from app.database import SessionLocal
from app.models import User
from app.services.artist_sync import refresh_feeds
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

# Process-local is enough: the app runs single-worker by design (see tests/test_single_worker_guard.py).
_in_flight: set[int] = set()
_lock = threading.Lock()


def is_due(user: User) -> bool:
    """Only true for a library never checked; afterwards only the Refresh button checks."""
    return user.refreshed_at is None


def refresh_if_due(user_id: int) -> None:
    """Refresh this user's artists if still due and not already in flight.

    Runs as a BackgroundTask; re-checks due-ness because another tab may have refreshed meanwhile.
    """
    with _lock:
        if user_id in _in_flight:
            return
        _in_flight.add(user_id)
    try:
        with SessionLocal() as db:
            user = db.get(User, user_id)
            if user is None or not is_due(user):
                return
            artists = followed_artists(db, user_id=user_id).all()
            new_count = refresh_feeds(db, artists)
            # Stamped even with no artists, or an empty library is due on every page load.
            user.refreshed_at = utcnow()
            db.commit()
            if new_count:
                logger.info(
                    "Refresh on open added %d new item(s) across %d artist(s) for user %d",
                    new_count,
                    len(artists),
                    user_id,
                )
    except Exception:
        # The page is already sent; a failure must not take the task runner down with it.
        logger.exception("Refresh on open failed for user %d", user_id)
    finally:
        with _lock:
            _in_flight.discard(user_id)


def queue_due_refresh(background_tasks, user: User) -> None:
    """Queue a refresh behind this response if the user is due; refresh_if_due re-checks."""
    if is_due(user):
        background_tasks.add_task(refresh_if_due, user.id)
