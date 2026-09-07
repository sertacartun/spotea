"""Explore's browse shelves: playlists picked from the interests listed in
Settings; songs and similar artists picked from who's actually followed;
plus the charts and the full list of mood/genre categories, which belong
to nobody in particular.

Playlists is the one shelf still built from plain search, not a recommender
model — an interest is a free-text phrase, and a result is what YouTube
search returns for it, which covers "tell me what you like" turning
straight into a query. Songs and Artists used to work the same way, keyed
on that same free text, and both went badly for anything that wasn't
literally a song title or an artist's name: an interest was routinely a
genre or a mood instead ("Hip Hop", not "Drake" or "SICKO MODE"), and
YouTube Music's artist search in particular answers that kind of query with
beatmaker/compilation channels, not real artists (measured live). Both were
replaced by shelves built from artists actually followed instead of typed
text — similar_artists and songs from their own page previews (see
_similar_to_followed and _songs_from_followed) — which are the two shelves
here that *are* a real signal about this profile, not a search. The charts
and mood shelves answer the remaining case: a library that has said nothing
about itself yet and followed nobody, which until charts/moods existed had
an empty Explore tab and a nag.

The interesting part is therefore not the ranking, it's the request budget.
One batch is several live searches, each of them seconds long, against a
service that rate-limits an unauthenticated residential IP. So:

  * a batch is cached in the database (models.RecommendationCache), keyed by
    what the interests hashed to, and reused until it expires or the interests
    change — opening the Explore tab costs nothing;
  * "expires" means the same interval the user already chose for background
    artist refreshes (Settings → Artist updates), rather than a second cadence
    nobody asked for. There is deliberately no separate refresh control:
    recommendations go stale on that interval, when the interest list is
    edited, and when the app-wide Refresh button is pressed — the same three
    moments everything else on the page does;
  * a run samples INTERESTS_PER_RUN of the interests rather than
    searching all of them, which bounds the cost of a run regardless of how
    many interests are listed (and makes "Refresh" surface different corners
    of the list, which is the behaviour you want anyway);
  * only one run happens at a time process-wide, and a run that finds a fresh
    batch already waiting reuses it instead of repeating the work.
"""

import json
import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime

from sqlalchemy.orm import Session

from app.config import settings
from app.interests import interests_signature, parse_interests
from app.models import Artist, Content, RecommendationCache, User
from app.timeutil import utcnow
from app.youtube.music import fetch_charts_for, fetch_mood_categories
from app.youtube.music import search_playlists as search_music_playlists

logger = logging.getLogger(__name__)

# There is deliberately no age limit on a cached batch. It used to expire on
# the interval Settings offered for artist refreshes, so that "how often does
# this app go and look at YouTube again" stayed one setting rather than two —
# and then that setting was removed, because a library only has to be checked
# when someone opens the app for the first time or asks for it outright. The
# same rule reaches here: a batch is rebuilt when there isn't one, when the
# interests it was built from have changed, when PAYLOAD_VERSION moves, and
# when the Refresh button forces it. Nothing else expires it, and nothing
# goes near YouTube on a clock.

# Interests sampled per run. Each one costs one search (playlists — see
# _SEARCHERS), so this is the knob that decides a run's request count —
# three searches, run in parallel, is well under a second of wall time.
INTERESTS_PER_RUN = 3

# Wide enough for every sampled interest's one search at once, plus the two
# browse jobs (see _BROWSE_BUILDERS) that don't depend on interests at all.
# Nothing else here overlaps.
_POOL_SIZE = INTERESTS_PER_RUN * 1 + 2

# Per shelf, after merging every sampled interest's results. Roughly a shelf
# and a half of horizontal scrolling, which is as far as anyone browses one.
RESULTS_PER_SHELF = 12

# Bumped whenever the shape of a stored batch changes. It rides along on the
# cached signature, so every existing row stops matching and rebuilds on its
# own — which is what keeps this module free to add a shelf without a
# migration, and without a reader having to defend against a payload written
# by an older version of itself.
PAYLOAD_VERSION = "v5"

# No artist or song search here any more — an interest is free text, and it
# was routinely a genre or a mood rather than an artist's name or a song
# title ("Hip Hop", not "Drake" or "SICKO MODE"). YouTube Music's artist
# search answers that kind of query with beatmaker/compilation channels, not
# real artists (measured live: searching "Hip Hop" returned "Hip hop beats",
# "Oldschool Hip-Hop Instrumentalist"); its song search does better but is
# still inconsistent the same way (the same query's top results mixed real
# hits like "SICKO MODE" in with a nameless "Aggressive Fight Epic Hip Hop
# Motivation Music #3 (Instrumental Mix)"). See _similar_to_followed and
# _songs_from_followed for what replaced both: YouTube Music's own data off
# artists actually followed, which needs no search at all.
_SEARCHERS = {
    "playlists": search_music_playlists,
}

# Result key -> the field that identifies one result, for cross-interest
# deduplication. Two interests in the same genre routinely return the same
# playlist; showing it twice in one shelf reads as a bug.
_IDENTITY_FIELDS = {"playlists": "playlist_id"}

# Serializes batch building process-wide. Not about data races (each run
# writes only its own row) — it's a second, cruder brake on how many
# searches can be in flight at once.
_build_lock = threading.Lock()


def empty_batch() -> dict:
    """Every shelf, empty. The starting point a build fills in, and what a
    run that came back with nothing at all looks like."""
    return {
        "interests_used": [],
        "playlists": [],
        "charts": [],
        "chart_artists": [],
        "moods": [],
    }


def _charts_shelves() -> dict:
    """The chart pair, which has nothing to do with anyone's interests — it
    is what everyone in the charted countries is listening to this week.
    Included in the batch rather than served from a route of its own so it
    shares the cache, the TTL and the refresh button that already exist for
    the shelves beside it; the cost of that is one duplicated copy per user,
    which is a few kilobytes of JSON against a whole second endpoint.

    Costs one request per configured country (see config.chart_countries),
    which the batch's 30-minute TTL is what makes affordable."""
    charts = fetch_charts_for(settings.chart_countries)
    return {
        "charts": [asdict(playlist) for playlist in charts.playlists],
        "chart_artists": [asdict(artist) for artist in charts.artists],
    }


def _mood_categories() -> dict:
    """Every one of YouTube Music's moods (not genres — see
    fetch_mood_categories' MOOD_SECTION on why only that section is safe to
    list), for Explore's own "Moods & genres" browse row.

    All of them rather than one at random: the user picks which to open, so
    this has to be the full list, not a sample — the point of showing it at
    all is letting someone see "Sad" is an option before they'd think to ask
    for it. Still bounded to one request: this lists categories, not their
    playlists, and a category's playlists are only fetched once someone
    actually opens it (see remote_detail.remote_mood_context).
    """
    return {"moods": [asdict(category) for category in fetch_mood_categories()]}


# Shelves built without reference to the interest list, so they fill in even
# for a library that has listed none.
_BROWSE_BUILDERS = (_charts_shelves, _mood_categories)


def _sample(interests: list[str]) -> list[str]:
    """Which interests this run searches on. Random rather than "the first
    few" so that refreshing works its way around a long list instead of
    rebuilding the same batch."""
    if len(interests) <= INTERESTS_PER_RUN:
        return list(interests)
    return random.sample(interests, INTERESTS_PER_RUN)


def _interleave(per_interest: list[list[dict]], identity_field: str) -> list[dict]:
    """Merges one shelf's per-interest result lists round-robin, so the front
    of the shelf represents every sampled interest rather than exhausting the
    first one. Deduplicated by identity, and capped."""
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
    """Runs the searches for a fresh batch. Pure — no database, no caching —
    so the caching policy above stays in one place (get_recommendations).

    A library with no interests still gets a batch: the interest searches
    are simply skipped and the charts and mood shelves are the whole of it.
    That is the case Explore used to have nothing at all to show.
    """
    sampled = _sample(interests)
    jobs = [(kind, interest) for interest in sampled for kind in _SEARCHERS]

    batch = empty_batch()
    with ThreadPoolExecutor(max_workers=_POOL_SIZE) as pool:
        # Submitted before the interest searches so they overlap with them
        # rather than queueing behind a full pool.
        browsing = [pool.submit(build) for build in _BROWSE_BUILDERS]
        results = list(pool.map(lambda job: _SEARCHERS[job[0]](job[1]), jobs))
        for shelves in browsing:
            batch.update(shelves.result())

    # Rebuilt into {kind: [per-interest result lists]} in the sampled order,
    # which is what _interleave's round-robin walks across.
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


def _cached_batch(
    cache: RecommendationCache | None, signature: str, *, not_before: datetime | None
) -> tuple[dict, datetime] | None:
    """The cached batch if this caller can use it, else None.

    `not_before` is the oldest build this caller accepts, and it's the only
    thing that separates a plain read from a refresh: an ordinary read passes
    None and will take a batch of any age, while a refresh only accepts one
    built after it started — which can only be a batch another request built
    while this one waited for the lock.
    """
    if cache is None or cache.interests_signature != signature:
        return None
    if not_before is not None and cache.generated_at < not_before:
        return None
    try:
        return json.loads(cache.payload), cache.generated_at
    except json.JSONDecodeError:
        # A payload written by an older version of this module (or truncated
        # somehow) is a cache miss, not a 500.
        return None


def _merge_from_followed(db: Session, user: User, column, exclude_ids: set[str], identity_field: str) -> list[dict]:
    """Shared machinery behind _similar_to_followed and _songs_from_followed:
    merge every followed artist's own stored JSON list (whichever column is
    asked for), deduped by identity_field, capped at RESULTS_PER_SHELF, and
    interleaved a position at a time so no one artist can own the shelf.

    Both callers get the interleaving, on purpose: they are the same shelf
    shape with a different column behind them, and "dominated by one artist"
    is the same complaint in either.

    Deliberately not part of `batch`/build_batch: unlike the searches and
    the browse builders, this costs nothing but a query over data this
    profile already has, so there's no reason to cache it, sample it, or
    make a library with nothing followed wait for a rebuild to see it stay
    empty — it just is empty, same as _drop_already_in_library's filtering
    is always fresh regardless of the batch's own cache state.

    A profile with nothing followed gets nothing here at all — no seeded
    default, unlike the old interest-based shelves this replaced. That's
    the point: both callers only ever reflect artists actually followed.
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

    # Round-robin, not one artist at a time.
    #
    # This used to walk each artist's list to exhaustion and return the
    # moment it had RESULTS_PER_SHELF, which meant the first artist's list
    # filled the shelf on its own and every artist after them was never
    # read at all. With a dozen slots and lists this long, "the first
    # artist" was in practice the only artist — the reported symptom being
    # that the shelf is dominated by whoever was followed most recently.
    #
    # Flat, deliberately: no weighting by play count or recency. The
    # complaint is that one artist owns the shelf, and one from each in turn
    # is the whole of the fix. A weight would soften that rather than remove
    # it, and would need a reason to prefer any particular curve.
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
    """Every followed artist's own "fans also like" list (see
    Artist.related_artists, refreshed on every sync alongside
    monthly_listeners — no network call happens here), merged and deduped.
    """
    return _merge_from_followed(db, user, Artist.related_artists, exclude_ids, "channel_id")


def _songs_from_followed(db: Session, user: User, exclude_ids: set[str]) -> list[dict]:
    """Every followed artist's own page-preview songs (see Artist.top_tracks,
    refreshed on every sync alongside monthly_listeners/related_artists —
    no network call happens here), merged and deduped.

    Replaces the interest-based Songs shelf (see _SEARCHERS' comment on
    why): a genre or mood typed as free text found real songs
    inconsistently, mixing real hits in with nameless instrumental filler.
    This can't do that — every result here is a real song by an artist this
    profile actually follows — at the cost of not necessarily being their
    newest: a sync's all_songs=False call reads the artist page's own
    preview, which YouTube Music orders by popularity, not release date.
    """
    return _merge_from_followed(db, user, Artist.top_tracks, exclude_ids, "video_id")


def _drop_already_in_library(db: Session, user: User, batch: dict) -> dict:
    """Recommending something already in the library adds nothing — a
    followed artist or an added video isn't new. Applied here, at read
    time, against every batch (freshly built or cached) rather than inside
    build_batch: a cached batch can outlive what's added to the library
    after it was built, and re-checking on every read is what makes
    following a recommended artist make it disappear from the shelf on the
    very next load, rather than waiting for the batch to expire and rebuild.
    Playlists aren't filtered — YouTube's search results don't expose enough
    to match one against the library reliably.

    The charting-artists and similar-artists shelves both get the same
    rule: each is a list of artists to follow, and one already followed
    isn't. videos and similar_artists are computed here too (see
    _songs_from_followed and _similar_to_followed) rather than in `batch`
    — piggybacking on the owned_video_ids/followed_ids queries below rather
    than two more of their own.
    """
    owned_video_ids = {
        video_id for (video_id,) in db.query(Content.video_id).filter(Content.user_id == user.id)
    }
    # Both the Topic channel a follow is keyed by and the browse id an
    # artist's page is addressed by: a search result carries the browse id,
    # a chart entry can carry either, and a shelf that still offers someone
    # already in the library is the bug this exists to stop.
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


def get_recommendations(
    db: Session, user: User, *, force: bool = False
) -> tuple[dict, datetime]:
    """The current batch plus when it was built, rebuilding only if
    it's missing, built from different interests, or `force`d — then
    filtered against the current library (see
    _drop_already_in_library), always fresh regardless of whether the batch
    itself came from cache.

    There is always a batch, including with no interests listed: the
    charts and mood shelves don't depend on any (see build_batch), so
    generated_at is never None.
    """
    batch, generated_at = _get_or_build_batch(db, user, force=force)
    return _drop_already_in_library(db, user, batch), generated_at


def _cache_signature(interests: list[str]) -> str:
    """What a cached row has to match to be usable: the interests it was
    built from *and* the payload shape this version of the module writes.
    See PAYLOAD_VERSION."""
    return f"{PAYLOAD_VERSION}:{interests_signature(interests)}"


def _get_or_build_batch(
    db: Session, user: User, *, force: bool
) -> tuple[dict, datetime]:
    """The caching/locking half of get_recommendations, unfiltered — split
    out so the library filter above wraps every return path (three of them,
    below) from a single point instead of needing to be threaded through
    each one."""
    interests = parse_interests(user.interests)

    # Read before the lock, so a refresh that waits behind another one can
    # still tell "built while I waited" from "already there when I arrived".
    started = utcnow()
    signature = _cache_signature(interests)
    if not force:
        cached = _cached_batch(user.recommendation_cache, signature, not_before=None)
        if cached:
            return cached

    with _build_lock:
        # Ends this request's read transaction before looking again, so the
        # re-read can actually see a batch another request committed while
        # this one was queued on the lock — without it SQLite keeps serving
        # this session's older snapshot. Safe to roll back: nothing of this
        # request's own is pending at this point.
        db.rollback()
        cache = db.get(RecommendationCache, user.id)
        cached = _cached_batch(cache, signature, not_before=started if force else None)
        if cached:
            return cached

        batch = build_batch(interests)
        generated_at = utcnow()
        if cache is None:
            db.add(
                RecommendationCache(
                    user_id=user.id,
                    interests_signature=signature,
                    payload=json.dumps(batch),
                    generated_at=generated_at,
                )
            )
        else:
            cache.interests_signature = signature
            cache.payload = json.dumps(batch)
            cache.generated_at = generated_at
        db.commit()

    return batch, generated_at
