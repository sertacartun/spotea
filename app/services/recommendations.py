"""Explore's browse shelves: interest playlists, followed-artist songs/similar artists, charts and moods.

Searches are slow and rate-limited on an unauthenticated IP, so batches are cached per user,
interests are sampled per run, and only one build runs at a time.
"""

import json
import logging
import random
import threading
from dataclasses import asdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.interests import interests_signature, parse_interests
from app.models import Artist, Content, RecommendationCache, User
from app.timeutil import is_stale, utcnow
from app.youtube import music
from app.youtube.music import fetch_charts_for, fetch_mood_categories
from app.youtube.music import search_playlists as search_music_playlists

logger = logging.getLogger(__name__)

# A cached batch is rebuilt when the interests or PAYLOAD_VERSION change, or once a refresh boundary passes
# (00:00 and 12:00 UTC, see timeutil.last_refresh_boundary).

# Each sampled interest costs one search; this bounds a run's request count.
INTERESTS_PER_RUN = 3

RESULTS_PER_SHELF = 12

# Part of the cache signature: bump when the stored batch shape changes so old rows rebuild.
PAYLOAD_VERSION = "v6"

# Playlists only: artist/song search on free-text genres returns filler channels and
# instrumentals, so those shelves come from followed artists instead.
_SEARCHERS = {
    "playlists": search_music_playlists,
}

# Two interests in one genre often return the same playlist.
_IDENTITY_FIELDS = {"playlists": "playlist_id"}

# Not about data races: a crude brake on concurrent searches.
_build_lock = threading.Lock()


def empty_batch() -> dict:
    return {
        "interests_used": [],
        "playlists": [],
        "charts": [],
        "chart_artists": [],
        "moods": [],
    }


def _charts_shelves() -> dict:
    """This week's charts, one request per configured country; shares the batch cache."""
    charts = fetch_charts_for(settings.chart_countries)
    return {
        "charts": [asdict(playlist) for playlist in charts.playlists],
        "chart_artists": [asdict(artist) for artist in charts.artists],
    }


def _mood_categories() -> dict:
    """Every YouTube Music mood category (one request); playlists are fetched only when one is opened."""
    return {"moods": [asdict(category) for category in fetch_mood_categories()]}


_BROWSE_BUILDERS = (_charts_shelves, _mood_categories)


def _sample(interests: list[str]) -> list[str]:
    """Random rather than the first few, so rebuilds work their way around a long list."""
    if len(interests) <= INTERESTS_PER_RUN:
        return list(interests)
    return random.sample(interests, INTERESTS_PER_RUN)


def _interleave(per_interest: list[list[dict]], identity_field: str) -> list[dict]:
    """Merge per-interest results round-robin, deduplicated and capped."""
    merged: list[dict] = []
    seen: set[str] = set()
    for rank in range(max((len(results) for results in per_interest), default=0)):
        for results in per_interest:
            if rank >= len(results):
                continue
            identity = results[rank][identity_field]
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(results[rank])
            if len(merged) == RESULTS_PER_SHELF:
                return merged
    return merged


def build_batch(interests: list[str]) -> dict:
    """Run the searches for a fresh batch; pure, no database or caching."""
    sampled = _sample(interests)
    jobs = [(kind, interest) for interest in sampled for kind in _SEARCHERS]

    batch = empty_batch()
    # Submitted first so they overlap with the searches instead of queueing behind them.
    browsing = [music.pool.submit(build) for build in _BROWSE_BUILDERS]
    results = list(music.pool.map(lambda job: _SEARCHERS[job[0]](job[1]), jobs))
    for shelves in browsing:
        batch.update(shelves.result())

    by_kind: dict[str, list[list[dict]]] = {kind: [] for kind in _SEARCHERS}
    for (kind, _), found in zip(jobs, results, strict=True):
        by_kind[kind].append([asdict(item) for item in found])

    batch["interests_used"] = sampled
    for kind, per_interest in by_kind.items():
        batch[kind] = _interleave(per_interest, _IDENTITY_FIELDS[kind])

    logger.info(
        "Built recommendations for %s: %d playlists, "
        "%d charts, %d charting artists, %d moods",
        ", ".join(sampled) or "no interests",
        len(batch["playlists"]),
        len(batch["charts"]),
        len(batch["chart_artists"]),
        len(batch["moods"]),
    )
    return batch


def _cached_payload(cache: RecommendationCache | None, signature: str) -> dict | None:
    """The stored batch for these interests regardless of age; None if absent, for others, or corrupt."""
    if cache is None or cache.interests_signature != signature:
        return None
    try:
        return json.loads(cache.payload)
    except json.JSONDecodeError:
        return None


def _cached_batch(cache: RecommendationCache | None, signature: str) -> tuple[dict, datetime] | None:
    """The cached batch if still usable, else None."""
    if cache is None or is_stale(cache.generated_at):
        return None
    payload = _cached_payload(cache, signature)
    return None if payload is None else (payload, cache.generated_at)


# Searches flatten YouTube failures to empty lists, so an all-empty build usually means an outage.
_FETCHED_SHELVES = ("playlists", "charts", "chart_artists", "moods")


def _came_back_empty(batch: dict) -> bool:
    return not any(batch[shelf] for shelf in _FETCHED_SHELVES)


def _merge_from_followed(db: Session, user: User, column, exclude_ids: set[str], identity_field: str) -> list[dict]:
    """Merge every followed artist's stored JSON list from `column`, deduped and capped.

    Not cached in the batch: it is a cheap query over data the profile already has.
    """
    rows = db.query(column).filter(
        Artist.user_id == user.id, Artist.followed.is_(True), column.isnot(None)
    )

    per_artist: list[list[dict]] = []
    for (raw,) in rows:
        try:
            candidates = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(candidates, list):
            per_artist.append(candidates)

    # Round-robin so no single artist fills the shelf; flat, no weighting, on purpose.
    seen = set(exclude_ids)
    merged: list[dict] = []
    for position in range(max((len(items) for items in per_artist), default=0)):
        for items in per_artist:
            if position >= len(items):
                continue
            candidate = items[position]
            identity = candidate.get(identity_field)
            if not identity or identity in seen:
                continue
            seen.add(identity)
            merged.append(candidate)
            if len(merged) == RESULTS_PER_SHELF:
                return merged
    return merged


def _similar_to_followed(db: Session, user: User, exclude_ids: set[str]) -> list[dict]:
    """Followed artists' "fans also like" lists; no network call."""
    return _merge_from_followed(db, user, Artist.related_artists, exclude_ids, "channel_id")


def _songs_from_followed(db: Session, user: User, exclude_ids: set[str]) -> list[dict]:
    """Followed artists' page-preview songs (ordered by popularity, not release date); no network call."""
    return _merge_from_followed(db, user, Artist.top_tracks, exclude_ids, "video_id")


def _drop_already_in_library(db: Session, user: User, batch: dict) -> dict:
    """Filter out artists and videos already in the library, at read time.

    Done on every read, not in build_batch, because a cached batch outlives later follows.
    Playlists aren't filtered: search results don't expose enough to match them reliably.
    """
    owned_video_ids = {
        video_id for (video_id,) in db.query(Content.video_id).filter(Content.user_id == user.id)
    }
    # Both ids: search results carry the browse id, chart entries either.
    followed_ids = {
        value
        for row in db.query(Artist.channel_id, Artist.browse_id).filter(
            Artist.user_id == user.id, Artist.followed.is_(True)
        )
        for value in row
        if value is not None
    }

    return {
        **batch,
        "videos": _songs_from_followed(db, user, owned_video_ids),
        "chart_artists": [
            c for c in batch["chart_artists"] if c["channel_id"] not in followed_ids
        ],
        "similar_artists": _similar_to_followed(db, user, followed_ids),
    }


def get_recommendations(db: Session, user: User) -> tuple[dict, datetime]:
    """The current batch (rebuilt if missing or stale), filtered against the library."""
    batch, generated_at = _get_or_build_batch(db, user)
    return _drop_already_in_library(db, user, batch), generated_at


def _cache_signature(interests: list[str]) -> str:
    return f"{PAYLOAD_VERSION}:{interests_signature(interests)}"


def _get_or_build_batch(db: Session, user: User) -> tuple[dict, datetime]:
    """The caching/locking half of get_recommendations, unfiltered."""
    interests = parse_interests(user.interests)
    signature = _cache_signature(interests)
    cached = _cached_batch(user.recommendation_cache, signature)
    if cached:
        return cached

    with _build_lock:
        # Ends the read transaction so the re-read sees a batch committed while waiting;
        # otherwise SQLite keeps serving this session's older snapshot.
        db.rollback()
        cache = db.get(RecommendationCache, user.id)
        cached = _cached_batch(cache, signature)
        if cached:
            return cached

        batch = build_batch(interests)
        generated_at = utcnow()
        previous = _cached_payload(cache, signature)
        if cache is None:
            db.add(
                RecommendationCache(
                    user_id=user.id,
                    interests_signature=signature,
                    payload=json.dumps(batch),
                    generated_at=generated_at,
                )
            )
        elif previous is not None and _came_back_empty(batch) and not _came_back_empty(previous):
            # Keep the last good batch until the next boundary instead of blanking Explore.
            logger.warning("Recommendations came back empty; keeping the previous batch")
            batch = previous
            cache.generated_at = generated_at
        else:
            cache.interests_signature = signature
            cache.payload = json.dumps(batch)
            cache.generated_at = generated_at
        db.commit()

    return batch, generated_at
