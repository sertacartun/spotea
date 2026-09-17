import logging
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Artist
from app.progress import ProgressRegistry
from app.services.artist_sync import apply_artist_data, fetch_artist_data

logger = logging.getLogger(__name__)


# Terminal phase is "done" rather than removing the entry: a fast sync can finish before the
# client's first poll, which must still tell "finished" apart from "never ran".
sync_progress: ProgressRegistry[int, tuple[str, int, int]] = ProgressRegistry()

# Terminal entries stay readable for a while, so "has an entry" != "is running".
ACTIVE_PHASES = frozenset({"syncing"})


def syncing_artist_ids(feed_ids: Iterable[int]) -> set[int]:
    """Which of `feed_ids` are still syncing; in-memory lookups, no query."""
    return {
        artist_id
        for artist_id in feed_ids
        if (sync_progress.get(artist_id) or ("", 0, 0))[0] in ACTIVE_PHASES
    }


def mark_syncing(artist_id: int) -> None:
    """Put an artist into the syncing state without doing any work.

    Called before POST /artists answers: the background task may not have started when the
    client renders the card, and a card not marked syncing never polls.
    """
    sync_progress.set(artist_id, ("syncing", 0, 0))


def run_initial_sync(artist_id: int, db: Session) -> None:
    artist = db.get(Artist, artist_id)
    if artist is None:
        return

    mark_syncing(artist_id)
    try:
        result = fetch_artist_data(artist.browse_id, artist.avatar_url)
        apply_artist_data(db, artist, result)
    except Exception:
        # Must still clear the phase, or the card says "Fetching uploads…" forever.
        logger.exception("Initial sync failed for artist %s", artist_id)
        sync_progress.set(artist_id, ("done", 0, 0))
        return

    sync_progress.set(artist_id, ("done", 0, 0))


def run_initial_sync_task(artist_id: int) -> None:
    """BackgroundTasks entry point on its own session; the request's get_db session is already closed."""
    with SessionLocal() as db:
        run_initial_sync(artist_id, db)
