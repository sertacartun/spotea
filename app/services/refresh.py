"""Deciding when a library goes and looks for new releases.

This used to be a background loop: every five minutes the scheduler asked
which users' own intervals had elapsed and refreshed those, whether or not
anyone was there. That made sense while the app had a feed to keep warm. It
does not any more — there is nothing that goes stale while the tab is
closed, because nothing is shown until someone opens the app. A library of
150 artists was being fetched around the clock so that a page nobody was
looking at could be right.

So the check moved to the moment it matters: opening the app. For a while
that was governed by an interval in Settings — a floor rather than a clock,
"don't go and look again unless it has been at least this long". That
control is gone too. A followed artist's back catalogue does not change
between two openings of the app, and the one thing the interval bought was
that every visitor paid for someone else's staleness on a schedule nobody
chose. What is left is the two moments a check is actually wanted: the first
time a library is opened at all, and whenever Refresh is pressed. Refreshing
goes on happening in the background either way (see queue_due_refresh), so
nothing waits on a page load; the results land in the next render.

The one-at-a-time guard below matters more here than it did in the loop. A
loop ticks once; opening the app happens whenever it happens, on however
many tabs and devices at once, and a refresh of 150 artists takes ten to
fifteen seconds. Without it, three tabs opened together would each start
their own.
"""

import logging
import threading

from app.content_query import followed_artists
from app.database import SessionLocal
from app.models import User
from app.services.artist_sync import refresh_feeds
from app.timeutil import utcnow

logger = logging.getLogger(__name__)

# User ids with a refresh in flight. A process-local set, which is the right
# scope: the thing being protected against is this process starting the same
# work twice, and the app runs single-worker by design (see
# tests/test_single_worker_guard.py).
_in_flight: set[int] = set()
_lock = threading.Lock()


def is_due(user: User) -> bool:
    """Whether opening the app should send this user's library off to look
    for new releases.

    Only ever true once: for a library that has never been checked, which is
    a brand new account, or one upgrading from before this was recorded. Age
    does not make a library due any more — after the first check, going and
    looking is something the Refresh button does and nothing else.

    Still a function rather than an inlined `is None`, because the callers
    are what makes it correct: queue_due_refresh asks before spending a
    background task, and refresh_if_due asks again after the response has
    gone out, by which time another tab may have answered it.
    """
    return user.refreshed_at is None


def refresh_if_due(user_id: int) -> None:
    """Refresh this user's artists, if they are still due by the time this
    runs and nothing else is already doing it.

    Meant to run as a FastAPI BackgroundTask — after the response has gone
    out, on its own session, so the page that triggered it never waits. That
    page therefore renders the *old* data; what this buys is the next one.

    The due check is repeated here rather than trusted from the caller: this
    starts after the response, so another tab's refresh may have finished in
    between, and re-reading is cheaper than the fetch it avoids.
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
            # Stamped even when nothing came back, and even when there are no
            # artists to sync at all — otherwise an empty library is due on
            # every single page load forever.
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
        # One user's failed refresh must not become a 500 on a page that has
        # already been sent, nor take the worker's task runner down with it.
        logger.exception("Refresh on open failed for user %d", user_id)
    finally:
        with _lock:
            _in_flight.discard(user_id)


def queue_due_refresh(background_tasks, user: User) -> None:
    """Queue a refresh behind this response if the user is due for one.

    Checked twice on purpose — cheaply here so that the overwhelmingly common
    case (opening the app again five minutes later) costs one comparison and
    queues nothing, and again inside refresh_if_due where it is authoritative.
    """
    if is_due(user):
        background_tasks.add_task(refresh_if_due, user.id)
