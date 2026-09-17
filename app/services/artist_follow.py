"""Following an artist; the server alone decides whether a channel is an artist."""

from sqlalchemy.orm import Session

from app.models import Artist
from app.youtube.music import fetch_artist
from app.youtube.urls import CHANNEL_ID_RE, extract_channel_id


def get_or_create_placeholder(
    db: Session, channel_id: str, channel_title: str | None, user_id: int
) -> Artist:
    """An unfollowed Artist row so an Explore track has somewhere to attach.

    Following later upgrades this same row in place, keyed by the bare channel id.
    """
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
    """The artist key, browse id and name from any id that opens their page.

    Topic and official channel ids both resolve here; the browse id stored is the page that has the music.
    """
    artist = fetch_artist(channel_id, all_songs=False)
    if artist is None:
        raise NotAnArtistError("This channel isn't an artist on YouTube Music")

    # Keyed on the Topic channel where there is one, so following the official and
    # the Topic channel resolve to the same row.
    key_channel_id = artist.topic_channel_id or artist.channel_id or artist.browse_id
    return key_channel_id, artist.browse_id, artist.name


def follow_artist(
    db: Session,
    channel_id: str,
    user_id: int,
    sync: bool = True,
) -> Artist:
    """DB half of following an artist, given a URL that names a channel.

    Resolution runs before the duplicate check: it can reduce two channel ids to one key.
    An existing followed=False row is upgraded in place. `sync=False` leaves the first sync
    to the background.
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
        return artist

    from app.services.artist_sync import apply_artist_data, fetch_artist_data

    apply_artist_data(db, artist, fetch_artist_data(artist.browse_id, artist.avatar_url))
    return artist


def follow_artist_by_url(
    db: Session,
    channel_url: str,
    user_id: int,
    sync: bool = True,
) -> Artist:
    return follow_artist(db, channel_url.strip(), user_id, sync=sync)
