"""Following an artist.

Every way of following something goes through here, so this is where "is
this actually an artist?" gets answered — once, on the server. It used to be
the client's answer, arriving as a request field only a client that had
already opened the artist's profile could fill in; the detail panel's Follow
button got it right and nothing else did.

Now it is the *only* answer: a channel YouTube Music can't read as an artist
cannot be followed at all. That is the music-only scope in one rule — the
library holds artists, and a page that isn't one has nothing this app would
sync from it.
"""

from sqlalchemy.orm import Session

from app.models import Artist
from app.youtube.music import fetch_artist
from app.youtube.urls import CHANNEL_ID_RE, extract_channel_id


def get_or_create_placeholder(
    db: Session, channel_id: str, channel_title: str | None, user_id: int
) -> Artist:
    """An Artist row for someone the user hasn't actually followed — exists
    only so a single track added via Explore has somewhere to attach
    (Content.artist_id is required). followed=False keeps it out of Library
    and the background refresh (see content_query.followed_artists) until the
    user follows them for real, which upgrades this same row in place (see
    follow_channel below) instead of creating a duplicate. Keyed by the
    bare channel id, which is what both paths agree on.

    No avatar fetch: a placeholder artist's avatar is never displayed anywhere
    (Library and the channel-hero page are the only avatar consumers, and
    both are followed-only surfaces)."""
    existing = db.query(Artist).filter(Artist.user_id == user_id, Artist.channel_id == channel_id).first()
    if existing:
        return existing

    artist = Artist(user_id=user_id, channel_id=channel_id, name=channel_title, followed=False)
    db.add(artist)
    db.commit()
    db.refresh(artist)
    return artist


class AlreadyFollowingError(Exception):
    def __init__(self, channel_id: str, channel_title: str | None):
        super().__init__(channel_id)
        self.channel_title = channel_title


class NotAnArtistError(Exception):
    """What came back isn't a musician's page, so there is nothing to follow."""


def _resolve_artist(channel_id: str) -> tuple[str, str, str]:
    """The artist key, browse id and display name for an artist, from any id
    that opens their page.

    `get_artist` accepts more than one id for the same person — the Topic
    channel id that song results and chart entries carry, *and* their
    official channel id (measured: both open Shirin David's page and both
    report the same channelId back). Whichever came in, what gets stored is
    the browse id off the page that actually has the music: a VEVO container
    answers with the right name and no songs, and music._redirected_artist
    walks from there to the page that does.

    all_songs=False: this needs the ids and the name off the page header,
    and the "Top songs" playlist behind them is a second request nobody here
    reads.
    """
    artist = fetch_artist(channel_id, all_songs=False)
    if artist is None:
        raise NotAnArtistError("This channel isn't an artist on YouTube Music")

    # The artist's own key stays the "<Artist> - Topic" channel where there is
    # one. Nothing fetches it any more (see services/artist_sync.py), but it
    # is what makes "follow the official channel" and "follow the Topic
    # channel" resolve to the same row rather than two.
    key_channel_id = artist.topic_channel_id or artist.channel_id or artist.browse_id
    return key_channel_id, artist.browse_id, artist.name


def follow_artist(
    db: Session,
    channel_id: str,
    user_id: int,
    sync: bool = True,
) -> tuple[Artist, int]:
    """DB half of following an artist, given a URL that names a channel.

    The artist resolution runs before the duplicate check on purpose, because
    it can change which artist this even is: following an artist's official
    channel when their Topic channel is already followed is a duplicate, and
    only says so once both have been reduced to the same key.

    A matching Artist can already exist with followed=False — a placeholder
    created for a track the user only grabbed one of (see routers/explore.py's
    get_or_create_placeholder), or an artist unfollowed while keeping
    some content. Following now upgrades that row in place rather than
    bouncing the user with "already exists" for a artist they never knowingly
    added.

    `sync=False` stops once the row exists, leaving the first sync to
    services/backfill.run_initial_sync — what a request-serving caller wants,
    since nothing waiting on the response needs it.
    """
    channel_id = extract_channel_id(channel_id)
    if not channel_id or not CHANNEL_ID_RE.match(channel_id):
        raise NotAnArtistError("This doesn't look like a YouTube channel")

    channel_id, browse_id, name = _resolve_artist(channel_id)

    existing = db.query(Artist).filter(Artist.user_id == user_id, Artist.channel_id == channel_id).first()
    if existing and existing.followed:
        raise AlreadyFollowingError(channel_id, existing.name)

    if existing:
        artist = existing
        artist.followed = True
        artist.name = name
        artist.browse_id = browse_id
    else:
        artist = Artist(
            user_id=user_id,
            channel_id=channel_id,
            name=name,
            browse_id=browse_id,
        )
        db.add(artist)
    db.commit()
    db.refresh(artist)

    if not sync:
        return artist, 0

    from app.services.artist_sync import apply_artist_data, fetch_artist_data

    result = fetch_artist_data(artist.browse_id, artist.release_snapshot, artist.avatar_url)
    return artist, apply_artist_data(db, artist, result)


def follow_artist_by_url(
    db: Session,
    channel_url: str,
    user_id: int,
    sync: bool = True,
) -> tuple[Artist, int]:
    """Follow an artist from whatever URL the caller has. The only entry
    point routes use."""
    return follow_artist(db, channel_url.strip(), user_id, sync=sync)
