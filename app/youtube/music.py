"""YouTube Music (via ytmusicapi) as a source for songs, playlists, charts and artists.

Never pass `language`: `YTMusic(language="tr")` silently returns empty songs/artists/albums,
because the parser matches translated section headers. Failures flatten to None/empty, never raise.
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from ytmusicapi import YTMusic

from app.images import cached_avatar_or_hotlink, proxied_image_url
from app.youtube.models import (
    PLAYLIST_ITEM_LIMIT,
    SEARCH_RESULT_LIMIT,
    ChannelSearchResult,
    PlaylistDetail,
    PlaylistSearchResult,
    VideoSearchResult,
)
from app.youtube.urls import (
    CHANNEL_ID_RE,
    VIDEO_ID_RE,
    absolute_thumbnail_url,
    cover_url_at_size,
    playlist_id_from_browse_id,
)

logger = logging.getLogger(__name__)

# The largest size YouTube Music itself requests; the API default is a blurry 60px.
COVER_SIZE = 544

# YouTube Music's code for the global chart.
GLOBAL_CHART_COUNTRY = "ZZ"

# A mood shelf returns 100+ playlists; cap before serialising into the cache.
MOOD_PLAYLIST_LIMIT = 24

CHART_ARTIST_LIMIT = 12

CHART_PLAYLIST_LIMIT = 12

# The other chart playlists are music-video/live charts. Prefix matching relies on
# the client never being given a `language`, so titles stay English.
CHART_TRENDING_PREFIX = "Trending"

# Above YouTube Music's fixed 150-song "Top songs" cap (+ merged videos), so it bounds the
# render, not the fetch — the detail panel has no pagination.
ARTIST_TRACK_LIMIT = 200

# Taken from the Top songs playlist, not the artist page's preview, whose entries have no duration.
ARTIST_PREVIEW_SONGS = 10

ARTIST_RELEASE_LIMIT = 10


# YTMusic's requests.Session isn't thread-safe, so one client per thread. Construction is free, but a
# client's first call downloads the music.youtube.com homepage (~375 KB) for a visitor id.
_local = threading.local()

# Unauthenticated requests: a larger burst risks 429s.
POOL_SIZE = 8

# Fan-out work shares this one long-lived pool so its threads, and their warmed-up clients, outlive a
# single sync. A pool per call paid the homepage fetch again in every worker, doubling the requests.
# Work submitted here must not submit to it again, or it can deadlock waiting on its own workers.
pool = ThreadPoolExecutor(max_workers=POOL_SIZE, thread_name_prefix="youtube-music")


def _client() -> YTMusic:
    client = getattr(_local, "client", None)
    if client is None:
        # No `language` (see module docstring); no `location`, so it's inferred from the request IP.
        client = YTMusic()
        _local.client = client
    return client


def _call(description: str, method: str, *args, level: int = logging.WARNING, **kwargs):
    """One ytmusicapi call, any failure flattened to None (network, YTMusicError, KeyError on reshape)."""
    try:
        return getattr(_client(), method)(*args, **kwargs)
    except Exception:
        logger.log(level, "YouTube Music %s failed", description, exc_info=level > logging.INFO)
        return None


def _search(query: str, filter: str, limit: int) -> list[dict]:
    results = _call(f"search ({filter})", "search", query, filter=filter, limit=limit) or []
    if not results:
        # An empty result is the signature of the `language` trap or a changed section header.
        logger.info("YouTube Music %s search for %r returned nothing", filter, query)
    return results[:limit]


_COUNT_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_count(text: str | None) -> int | None:
    """"1.8M" as 1800000; YouTube Music reports counts only as display strings. None if unparseable."""
    if not text:
        return None
    token = text.strip().split()[0].replace(",", "")
    multiplier = _COUNT_SUFFIXES.get(token[-1:].upper())
    if multiplier:
        token = token[:-1]
    try:
        return int(float(token) * (multiplier or 1))
    except ValueError:
        return None


def _cover_url(thumbnails: list[dict] | None) -> str | None:
    """The largest reported cover at COVER_SIZE, as a raw (unproxied) URL."""
    if not thumbnails:
        return None
    return absolute_thumbnail_url(cover_url_at_size(thumbnails[-1].get("url"), COVER_SIZE))


def _proxied_cover_url(thumbnails: list[dict] | None) -> str | None:
    """A cover via /image-proxy: hotlinking hits Chrome ORB, and lh3 hosts are blocked by our CSP."""
    url = _cover_url(thumbnails)
    return proxied_image_url(url) if url else None


def _artist_names(item: dict) -> tuple[str | None, str | None, str | None]:
    """(primary artist name, joined credit or None if single, channel id).

    Name and credit must stay separate: a joined credit would otherwise name the shared Artist row.
    The channel is usually the "<Artist> - Topic" channel — fine for a preview row, not for following.
    """
    artists = [artist for artist in item.get("artists") or [] if artist.get("name")]
    if not artists:
        return None, None, None
    channel_id = next(
        (
            artist["id"]
            for artist in artists
            if artist.get("id") and CHANNEL_ID_RE.match(artist["id"])
        ),
        None,
    )
    # The artist whose channel this is, so the row's name and channel agree.
    primary = artists[0]["name"]
    if channel_id:
        primary = next(
            (artist["name"] for artist in artists if artist.get("id") == channel_id),
            artists[0]["name"],
        )
    credit = ", ".join(artist["name"] for artist in artists) if len(artists) > 1 else None
    return primary, credit, channel_id


def _song_result(item: dict) -> VideoSearchResult | None:
    video_id = item.get("videoId")
    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None

    title = item.get("title")
    if not title:
        return None

    channel_title, artist_credit, channel_id = _artist_names(item)
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=_proxied_cover_url(item.get("thumbnails")),
        duration_seconds=item.get("duration_seconds"),
        channel_title=channel_title,
        channel_id=channel_id,
        artist_credit=artist_credit,
    )


def _song_results(items: list[dict] | None) -> list[VideoSearchResult]:
    results = (_song_result(item) for item in items or [])
    return [result for result in results if result is not None]


def _playlist_result(item: dict) -> PlaylistSearchResult | None:
    playlist_id = playlist_id_from_browse_id(item.get("browseId") or item.get("playlistId"))
    if not playlist_id:
        return None

    return PlaylistSearchResult(
        playlist_id=playlist_id,
        title=item.get("title") or "Untitled playlist",
        thumbnail_url=_proxied_cover_url(item.get("thumbnails")),
        channel_title=item.get("author") or item.get("description"),
    )


def _playlist_results(items: list[dict] | None) -> list[PlaylistSearchResult]:
    results = (_playlist_result(item) for item in items or [])
    return [result for result in results if result is not None]


def _artist_result(item: dict) -> ChannelSearchResult | None:
    """A chart or related-artist entry; dropped when the browse id isn't a UC channel id."""
    browse_id = item.get("browseId")
    if not browse_id or not CHANNEL_ID_RE.match(browse_id):
        return None

    title = item.get("title") or item.get("artist")
    if not title:
        return None

    return ChannelSearchResult(
        channel_id=browse_id,
        title=title,
        thumbnail_url=cached_avatar_or_hotlink(browse_id, _cover_url(item.get("thumbnails"))),
        subscriber_count=_parse_count(item.get("subscribers")),
        channel_url=f"https://www.youtube.com/channel/{browse_id}",
    )


def search_songs(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[VideoSearchResult]:
    return _song_results(_search(query, "songs", limit))


# ATV = the album audio track (square art, lyrics); OMV = the official music video.
SONG_VIDEO_TYPE = "MUSIC_VIDEO_TYPE_ATV"

# Headroom for a live take or sped-up upload outranking the song version.
SONG_MATCH_CANDIDATES = 5

_TITLE_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _match_key(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(_NON_WORD_RE.sub(" ", _TITLE_NOISE_RE.sub(" ", text.lower())).split())


# Bracketed words marking a different recording (_match_key drops brackets, so "(Instrumental)"
# would otherwise match). Not exhaustive on purpose: "(Taylor's Version)" is still the song.
_OTHER_RECORDING_WORDS = frozenset(
    {
        "live",
        "acoustic",
        "instrumental",
        "karaoke",
        "remix",
        "cover",
        "demo",
        "sped",
        "slowed",
        "reverb",
        "8d",
    }
)


def _is_other_recording(title: str | None) -> bool:
    for aside in re.findall(r"\((.*?)\)|\[(.*?)\]", title or ""):
        words = set(_match_key(" ".join(part for part in aside if part)).split())
        if words & _OTHER_RECORDING_WORDS:
            return True
    return False


# Uploader decoration, only consulted for words left over around a matched song title.
# Validated as-is against 117 real tracks; don't trim without re-measuring.
_TITLE_FILLER_WORDS = frozenset(
    {
        "official", "oficial", "officiel", "video", "videos", "music", "mv",
        "pv", "audio", "lyric", "lyrics", "visualizer", "visualiser", "hd",
        "hq", "4k", "closed", "captioned", "caption", "edit", "performance",
        "clip", "teaser", "full", "ver", "explicit", "color", "coded",
        "dance", "practice", "special", "stage", "from", "the", "movie",
        "soundtrack",
    }
)

_YEAR_RE = re.compile(r"^\d{4}$")

# Quotes matter: in "KATSEYE (캣츠아이) 'Hootie Frutti' Official MV" only they delimit the song.
_TITLE_SEGMENT_RE = re.compile("[-–—|/'\"“”‘’()\\[\\]:,.]+")


def _title_segments(title: str | None) -> set[str]:
    parts = (_match_key(part) for part in _TITLE_SEGMENT_RE.split(title or ""))
    return {part for part in parts if part}


def _contains_run(words: list[str], run: list[str]) -> bool:
    """Whether `run` appears in `words` as consecutive whole words (so "art" doesn't match "Artist")."""
    span = len(run)
    if not span or span > len(words):
        return False
    return any(words[index : index + span] == run for index in range(len(words) - span + 1))


def _leftover_explained(words: list[str], run: list[str], allowed: set[str]) -> bool:
    """Whether everything but one occurrence of `run` is filler, a year, or part of an artist's name."""
    span = len(run)
    for start in range(len(words) - span + 1):
        if words[start : start + span] != run:
            continue
        rest = words[:start] + words[start + span :]
        if all(word in allowed or _YEAR_RE.match(word) for word in rest):
            return True
    return False


def _song_title_nested(
    video_words: list[str], song_key: str, segments: set[str], artist_keys: set[str]
) -> bool:
    """Whether a song's title is this video's title plus uploader decoration.

    Bare containment matched "Legends" to "Hood Legends", so it must also be a delimited segment
    or leave only filler words behind.
    """
    song_words = song_key.split()
    if not _contains_run(video_words, song_words):
        return False
    if song_key in segments:
        return True
    allowed = set(_TITLE_FILLER_WORDS)
    for key in artist_keys:
        allowed |= set(key.split())
    return _leftover_explained(video_words, song_words, allowed)


def _same_artist(
    credited: set[str],
    credited_ids: set[str],
    wanted_name: str,
    wanted_channel_id: str | None,
    video_words: list[str],
) -> bool:
    """Whether a candidate is by this video's artist: channel id, then name, then a credit in the title.

    The id catches differing display names; the title check catches label uploads ("HYBE LABELS").
    Compare against the artist list, never a joined credit string — that fails every collaboration.
    """
    if not wanted_name and not wanted_channel_id:
        return True
    if wanted_channel_id and wanted_channel_id in credited_ids:
        return True
    if wanted_name and wanted_name in credited:
        return True
    return any(name and _contains_run(video_words, name.split()) for name in credited)


def find_song_version(
    title: str,
    artist_name: str | None,
    artist_channel_id: str | None = None,
) -> VideoSearchResult | None:
    """The album (ATV) version of a track that arrived as a music video, or None.

    Curated/mood playlists are ~98% music videos (16:9 stills, no lyrics). There's no counterpart
    field for a signed-out client, so search is the only route. Misses are deliberate: a search is a
    guess, and a music video beats the wrong recording. Duration is ignored (intros vary).
    """
    query = f"{title} {artist_name}".strip() if artist_name else title
    if not query:
        return None

    wanted_title = _match_key(title)
    wanted_artist = _match_key(artist_name)
    video_words = wanted_title.split()
    segments = _title_segments(title)

    nested: VideoSearchResult | None = None
    nested_words = 0
    for item in _search(query, "songs", SONG_MATCH_CANDIDATES):
        if item.get("videoType") != SONG_VIDEO_TYPE:
            continue
        result = _song_result(item)
        if result is None:
            continue
        if _is_other_recording(result.title) and not _is_other_recording(title):
            continue

        artists = item.get("artists") or []
        credited = {_match_key(artist.get("name")) for artist in artists if artist.get("name")}
        credited_ids = {artist.get("id") for artist in artists if artist.get("id")}
        if not _same_artist(credited, credited_ids, wanted_artist, artist_channel_id, video_words):
            continue

        song_key = _match_key(result.title)
        if song_key == wanted_title:
            return result
        # Longest nested title wins ("Hood Legends" over "Legends"); an exact match still beats it.
        if (
            _song_title_nested(video_words, song_key, segments, credited | {wanted_artist})
            and len(song_key.split()) > nested_words
        ):
            nested, nested_words = result, len(song_key.split())
    return nested


def search_playlists(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[PlaylistSearchResult]:
    """Playlists for a query: curated `featured_playlists` first, community `playlists` only to fill up."""
    results = _playlist_results(_search(query, "featured_playlists", limit))
    if len(results) >= limit:
        return results[:limit]

    seen = {result.playlist_id for result in results}
    for result in _playlist_results(_search(query, "playlists", limit)):
        if result.playlist_id in seen:
            continue
        results.append(result)
        if len(results) == limit:
            break
    return results


def search_artists(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[ChannelSearchResult]:
    results = (_artist_result(item) for item in _search(query, "artists", limit))
    return [result for result in results if result is not None]


def fetch_playlist(playlist_id: str, limit: int = PLAYLIST_ITEM_LIMIT) -> PlaylistDetail:
    """One playlist's tracks; empty items when there's no such playlist."""
    playlist = _call("playlist", "get_playlist", playlist_id, limit)
    if not playlist:
        return PlaylistDetail(playlist_id=playlist_id, title=None, video_count=None, items=[])

    return PlaylistDetail(
        playlist_id=playlist_id,
        title=playlist.get("title"),
        video_count=playlist.get("trackCount"),
        items=_song_results(playlist.get("tracks")),
    )


@dataclass
class Charts:

    playlists: list[PlaylistSearchResult]
    artists: list[ChannelSearchResult]


def fetch_charts(
    country: str = GLOBAL_CHART_COUNTRY, artist_limit: int = CHART_ARTIST_LIMIT
) -> Charts:
    """One country's Trending playlist and charting artists, from a single request."""
    charts = _call(f"charts ({country})", "get_charts", country) or {}
    artists = (_artist_result(item) for item in charts.get("artists") or [])
    return Charts(
        playlists=[
            playlist
            for playlist in _playlist_results(charts.get("videos"))
            if playlist.title.startswith(CHART_TRENDING_PREFIX)
        ],
        artists=[artist for artist in artists if artist is not None][:artist_limit],
    )


def fetch_charts_for(
    countries: list[str], artist_limit: int = CHART_ARTIST_LIMIT
) -> Charts:
    """Several countries' charts merged round-robin by rank.

    The global chart is dominated by the largest markets and chart artists carry no country field,
    so naming countries is the only filter the API supports.
    """
    if len(countries) == 1:
        return fetch_charts(countries[0], artist_limit)

    per_country = [fetch_charts(country, artist_limit) for country in countries]
    return Charts(
        playlists=_round_robin(
            [charts.playlists for charts in per_country], "playlist_id"
        )[:CHART_PLAYLIST_LIMIT],
        artists=_round_robin([charts.artists for charts in per_country], "channel_id")[
            :artist_limit
        ],
    )


def _round_robin(lists: list[list], identity: str) -> list:
    """One from each list, then the next from each, deduped by `identity`."""
    merged = []
    seen = set()
    for position in range(max((len(items) for items in lists), default=0)):
        for items in lists:
            if position >= len(items):
                continue
            candidate = items[position]
            key = getattr(candidate, identity)
            if key in seen:
                continue
            seen.add(key)
            merged.append(candidate)
    return merged


@dataclass
class MoodCategory:
    """A mood/genre menu entry; `params` is an opaque token, the only way to request its playlists."""

    title: str
    params: str
    section: str


# ytmusicapi's parser raises on every "Genres" category (their grids mix in videos),
# so default to the section that parses. section=None returns everything.
MOOD_SECTION = "Moods & moments"


def fetch_mood_categories(section: str | None = MOOD_SECTION) -> list[MoodCategory]:
    """The moods (and optionally genres) YouTube Music offers, each tagged with its section."""
    sections = _call("mood categories", "get_mood_categories") or {}
    return [
        MoodCategory(title=item["title"], params=item["params"], section=name)
        for name, items in sections.items()
        if section is None or name == section
        for item in items
        if item.get("title") and item.get("params")
    ]


def fetch_mood_playlists(
    params: str, limit: int = MOOD_PLAYLIST_LIMIT
) -> list[PlaylistSearchResult]:
    return _playlist_results(_call("mood playlists", "get_mood_playlists", params))[:limit]


@dataclass
class ArtistRelease:
    """An album or single off an artist's page.

    `browse_id` ("MPREb_…") opens both kinds; `audioPlaylistId` is absent on singles.
    """


    browse_id: str
    title: str
    year: str | None
    kind: str
    cover_url: str | None


@dataclass
class ArtistProfile:
    """An artist page as YouTube Music knows it.

    `browse_id` accepts both the Topic-channel and the official channel id. `channel_id` is the official
    channel; `topic_channel_id` is the auto-generated one this app follows.
    """

    browse_id: str
    channel_id: str | None
    topic_channel_id: str | None
    name: str
    description: str | None
    subscriber_count: int | None
    monthly_listeners: str | None
    avatar_url: str | None
    tracks: list[VideoSearchResult]
    # Can exceed len(tracks): some entries fail to parse.
    track_count: int = 0
    albums: list[ArtistRelease] = field(default_factory=list)
    singles: list[ArtistRelease] = field(default_factory=list)
    related: list[ChannelSearchResult] = field(default_factory=list)


def _artist_songs(songs: dict, all_songs: bool) -> tuple[list[dict], int | None]:
    """An artist's songs from the "Top songs" playlist (the page only previews five), plus reported total.

    Costs a second request, hence `all_songs`. The count travels separately because parse failures
    make the list shorter than the playlist.
    """
    preview = songs.get("results") or []
    browse_id = songs.get("browseId")
    if not all_songs or not browse_id:
        return preview, None

    playlist = _call("artist top songs", "get_playlist", browse_id, limit=ARTIST_TRACK_LIMIT)
    tracks = (playlist or {}).get("tracks")
    if not tracks:
        return preview, None
    return tracks, (playlist or {}).get("trackCount")


def _releases(section: dict | None, kind: str) -> list[ArtistRelease]:
    """One of the artist page's release shelves, dropping entries with no browse id."""
    releases = []
    for item in (section or {}).get("results") or []:
        browse_id, title = item.get("browseId"), item.get("title")
        if not browse_id or not title:
            continue
        releases.append(
            ArtistRelease(
                browse_id=browse_id,
                title=title,
                year=item.get("year"),
                cover_url=_proxied_cover_url(item.get("thumbnails")),
                # Albums report no type; only the shelf they came from knows.
                kind=item.get("type") or kind,
            )
        )
    return releases[:ARTIST_RELEASE_LIMIT]


def _topic_channel_id(songs: list[dict], name: str) -> str | None:
    """The artist's "<Artist> - Topic" channel id (music only, no vlogs), read off their tracks.

    Matched by name, not first credit (collaborations list others first), and case-insensitively
    ("USHER" page vs "Usher" credits).
    """
    wanted = name.casefold()
    for song in songs:
        for artist in song.get("artists") or []:
            if (artist.get("name") or "").casefold() == wanted and CHANNEL_ID_RE.match(
                artist.get("id") or ""
            ):
                return artist["id"]
    return None


def _redirected_artist(artist: dict, browse_id: str) -> tuple[dict, str]:
    """Follow a VEVO channel's songless artist page through its `channelId` to the real one.

    YouTube Music answers a VEVO channel with the right name but no songs; re-asking with channelId works.
    """
    redirect = artist.get("channelId")
    if (artist.get("songs") or {}).get("results"):
        return artist, browse_id
    if not redirect or redirect == browse_id or not CHANNEL_ID_RE.match(redirect):
        return artist, browse_id

    followed = _call("redirected artist", "get_artist", redirect, level=logging.INFO)
    if not followed or not followed.get("name"):
        return artist, browse_id
    return followed, redirect


def _related_artists(section: dict | None) -> list[ChannelSearchResult]:
    results = (_artist_result(item) for item in (section or {}).get("results") or [])
    return [result for result in results if result is not None]


def fetch_artist(browse_id: str, all_songs: bool = True) -> ArtistProfile | None:
    """One artist's page, or None — also for non-music channels, so it's safe to try on any channel id.

    Videos aren't merged into `tracks`: most duplicate songs under a different (OMV) id and have no
    duration. The returned `browse_id` may differ from the one asked for (see _redirected_artist).
    """
    artist = _call("artist", "get_artist", browse_id, level=logging.INFO)
    if not artist or not artist.get("name"):
        return None

    artist, browse_id = _redirected_artist(artist, browse_id)

    songs, reported_count = _artist_songs(artist.get("songs") or {}, all_songs)
    tracks: list[VideoSearchResult] = []
    seen: set[str] = set()
    for track in _song_results(songs):
        if track.video_id in seen:
            continue
        seen.add(track.video_id)
        tracks.append(track)

    # The reported count often exceeds parsed tracks, so the panel can say "first 143 of 150".
    track_count = max(len(tracks), reported_count or 0)
    tracks = tracks[:ARTIST_TRACK_LIMIT]

    channel_id = artist.get("channelId")
    return ArtistProfile(
        browse_id=browse_id,
        channel_id=channel_id if channel_id and CHANNEL_ID_RE.match(channel_id) else None,
        topic_channel_id=_topic_channel_id(songs, artist["name"]),
        name=artist["name"],
        description=artist.get("description"),
        subscriber_count=_parse_count(artist.get("subscribers")),
        monthly_listeners=artist.get("monthlyListeners"),
        avatar_url=_cover_url(artist.get("thumbnails")),
        tracks=tracks,
        track_count=track_count,
        albums=_releases(artist.get("albums"), "Album"),
        singles=_releases(artist.get("singles"), "Single"),
        related=_related_artists(artist.get("related")),
    )


@dataclass
class ReleaseDetail:
    """YouTube Music returns the same structure for albums and singles."""

    title: str
    year: str | None
    kind: str
    cover_url: str | None
    artist_names: str | None
    tracks: list[VideoSearchResult]


def fetch_release(browse_id: str) -> ReleaseDetail | None:
    """An album or single's tracks, or None if there's no such release."""
    release = _call("release", "get_album", browse_id)
    if not release or not release.get("title"):
        return None

    tracks = _song_results(release.get("tracks"))
    if not tracks:
        return None

    # Album track entries carry no thumbnail of their own; they share the release cover.
    cover_url = _proxied_cover_url(release.get("thumbnails"))
    if cover_url:
        tracks = [
            track if track.thumbnail_url else replace(track, thumbnail_url=cover_url)
            for track in tracks
        ]

    artists = [artist.get("name") for artist in release.get("artists") or [] if artist.get("name")]
    return ReleaseDetail(
        title=release["title"],
        year=release.get("year"),
        kind=release.get("type") or "Release",
        cover_url=cover_url,
        artist_names=", ".join(artists) or None,
        tracks=tracks,
    )


@dataclass
class LyricLine:
    """Times in milliseconds from the start of the track."""

    text: str
    start_ms: int
    end_ms: int


@dataclass
class TimedLyrics:
    lines: list[LyricLine]
    source: str | None


def fetch_timed_lyrics(video_id: str) -> TimedLyrics | None:
    """This track's timed lyrics, or None.

    Two requests: the "MPLYt…" lyrics id is only published on the watch playlist. Most tracks have
    none (lyrics attach to songs, not videos), so callers cache the None too.
    """
    watch = _call("watch playlist (for lyrics)", "get_watch_playlist", videoId=video_id, limit=1)
    browse_id = (watch or {}).get("lyrics")
    if not browse_id:
        return None

    result = _call("lyrics", "get_lyrics", browse_id, timestamps=True)
    if not result or not result.get("hasTimestamps"):
        # Untimed lyrics count as none: a static wall of text reads as broken in a follow-along panel.
        return None

    lines = [
        LyricLine(text=line.text, start_ms=line.start_time, end_ms=line.end_time)
        for line in result["lyrics"]
    ]
    if not lines:
        return None
    return TimedLyrics(lines=lines, source=result.get("source"))
