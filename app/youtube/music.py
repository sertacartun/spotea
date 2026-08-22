"""YouTube Music as a source for songs, playlists, charts and artists.

A sibling of search.py, not a replacement for it. The difference is the
transport: search.py drives yt-dlp, which loads and parses youtube.com
*pages*; everything here goes through ytmusicapi to YouTube Music's own
InnerTube JSON endpoint. Same company, different service, different results
— and that last part is the whole reason this module exists. Measured live:
a song search on youtube.com returns a title and a video id and nothing
else, while the same query here comes back with the artist, the album, the
duration and the release year already attached, because YouTube Music
indexes tracks rather than videos.

**Deliberately not used for podcasts, and not used for channel search.**
That is a measured decision, not an oversight. YouTube Music models a
podcast as an "MPSP…" playlist rather than a channel, which is the wrong
identity for an app whose entire sync path is a channel's RSS artist — and its
matching is worse besides: searching it for "The Diary of a CEO" put a
reupload channel whose episodes have a few hundred views ahead of the real
19M-subscriber one that youtube.com finds first. Podcast discovery and
channel search stay on search.py. See the module docstring there.

Two things about the ytmusicapi surface are worth knowing before touching
this file:

  * **Never pass `language`.** `YTMusic(language="tr")` does not fail, it
    returns *nothing* — songs, artists and albums all come back as empty
    lists while videos still work. The parser locates each result section by
    matching its translated header, and the Turkish locale ships `albümler`
    but leaves `songs` untranslated, so the match silently misses. An empty
    list is a plausible-looking answer, which is what makes it expensive.
    `location` is unaffected and safe.
  * Counts arrive as display strings ("1.8M"), not numbers — see
    _parse_count. Because the language above is always English, so is their
    formatting.

Failures are flattened the same way search.py flattens them: every function
here backs a browse surface where "found nothing" is a normal outcome the UI
already renders, so nothing raises. The catch is deliberately broad —
ytmusicapi surfaces network errors, its own YTMusicError, and plain KeyError
when YouTube reshapes a response, and none of those should turn Explore into
a 500.
"""

import logging
import re
import threading
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

# Square art is rendered into cards roughly 200 CSS pixels wide and into the
# detail panel's hero at about twice that. 544 is a size the CDN already
# serves for album art (it is the largest YouTube Music itself asks for), so
# it costs nothing extra to standardise on. The alternative is what the API
# hands over untouched: a 60-pixel thumbnail, which is a visibly blurry card.
COVER_SIZE = 544

# YouTube Music's "global" chart, used when no country is asked for. Real
# country codes ("TR", "DE") select that country's charts instead.
GLOBAL_CHART_COUNTRY = "ZZ"

# A mood or genre shelf comes back with well over a hundred playlists in one
# response (133 for "Autumn", measured) — one shelf's worth is what gets
# rendered, so cap it here rather than serialising the rest into a cache row.
MOOD_PLAYLIST_LIMIT = 24

# The chart's artist list runs to 40. Same reasoning.
CHART_ARTIST_LIMIT = 12

# And the same cap on the chart playlists. It no longer binds now that only
# one playlist per country survives the filter below, but it is cheap
# insurance against a country that starts reporting several.
CHART_PLAYLIST_LIMIT = 12

# The only chart playlist worth a tile. Measured 2026-08-21 across US, GB and
# AU, a country's chart carries three or four:
#
#   "Trending 20 <Country>"
#   "Daily Top Music Videos - <Country>"
#   "Top 100 Music Videos <Country>"
#   "Top 100 Live Performances - <Country>"   (US and GB; absent in AU)
#
# The last three are *video* charts — the same songs ranked by their official
# music video's view count, plus live sets — which is not what this app is
# for. Trending is the one that answers "what is happening right now", and
# it is also the only one whose id is an OLAK5uy_… playlist rather than the
# PL4fGSI1pDJn… the video charts share.
#
# Matched on the title prefix, which is safe here only because this client is
# never constructed with a `language` (see the module docstring for why it
# must not be): YouTube Music answers in English regardless of the country
# asked for, so "Trending 20 Australia" is not localised.
CHART_TRENDING_PREFIX = "Trending"

# How many tracks an artist page lists. Set above what YouTube Music will
# ever hand over, so in practice it caps nothing and every artist fits on
# the one page the remote detail panel has (there is no pagination there —
# see services/remote_detail._base_context).
#
# That's affordable because the ceiling is low and fixed. A "Top songs"
# playlist stops at 150 no matter whose it is: measured across Taylor
# Swift, Drake, Frank Sinatra, Tarkan and J.S. Bach — a composer with
# thousands of works — all five reported exactly 150. It is a cap on that
# surface rather than a catalogue size; a full discography lives in the
# albums and singles sections, which is a per-album traversal this app
# doesn't make. With the artist's music videos merged in, ~160 rows is the
# worst case.
#
# Unlike search.py's PLAYLIST_ITEM_LIMIT (50), this bounds a *render* and
# not a *fetch*: YouTube Music returns the whole list in the same response,
# so a lower number here would only throw away tracks already in hand.
ARTIST_TRACK_LIMIT = 200

# How many songs the artist *profile* previews before "See all" hands over
# to the full list. Ten is a section, not a page — YouTube Music itself
# shows five, which reads as a teaser rather than something you'd browse.
#
# They come from the same Top songs playlist the full list does, not from
# the preview the artist page carries: those five entries have no duration
# at all (measured — `duration` and `duration_seconds` are both absent),
# and a song row with no duration is exactly the wart this section is meant
# to be free of.
ARTIST_PREVIEW_SONGS = 10

# How many releases each shelf shows. The artist page hands over ten of
# each without being asked; the rest sit behind a browse id this app
# doesn't follow (see ArtistRelease).
ARTIST_RELEASE_LIMIT = 10


# YTMusic wraps a requests.Session, which is not documented as thread-safe,
# and services/recommendations.py runs its searches across a thread pool.
# One client per thread rather than one shared behind a lock: constructing
# one costs zero network requests (it builds the session and reads a bundled
# locale file, nothing more), so the "expensive singleton" reasoning that
# would justify sharing simply does not apply here.
_local = threading.local()


def _client() -> YTMusic:
    client = getattr(_local, "client", None)
    if client is None:
        # No `language` — see the module docstring. No `location` either:
        # left empty, YouTube Music infers one from the request's own IP,
        # which for a self-hosted app is exactly the right answer and needs
        # no setting.
        client = YTMusic()
        _local.client = client
    return client


def _call(description: str, method: str, *args, level: int = logging.WARNING, **kwargs):
    """One ytmusicapi call, with failure flattened to None. See the module
    docstring for why the catch is this broad.

    `level` is for the one caller whose failures are routine rather than
    surprising: asking whether a channel is an artist (see fetch_artist)
    fails for every channel that isn't one, and a traceback per podcast
    opened is noise that trains you to ignore the log. Anything above INFO
    keeps the traceback, since at that point it's a real fault.
    """
    try:
        return getattr(_client(), method)(*args, **kwargs)
    except Exception:
        logger.log(level, "YouTube Music %s failed", description, exc_info=level > logging.INFO)
        return None


def _search(query: str, filter: str, limit: int) -> list[dict]:
    results = _call(f"search ({filter})", "search", query, filter=filter, limit=limit) or []
    if not results:
        # Worth a line in the log rather than silence: an empty result for a
        # query that youtube.com would answer is the exact signature of the
        # `language` trap in the module docstring, and of YouTube changing a
        # section header out from under the parser.
        logger.info("YouTube Music %s search for %r returned nothing", filter, query)
    return results[:limit]


_COUNT_SUFFIXES = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_count(text: str | None) -> int | None:
    """"1.8M" as 1800000. YouTube Music reports subscriber and view counts
    only as the string it would print, while ChannelSearchResult carries a
    number (which is what the client formats for itself). Anything that
    doesn't parse becomes None — a card with no count reads fine, a card
    with a wrong one doesn't."""
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
    """The largest reported cover, asked for at COVER_SIZE — the raw remote
    URL, not yet proxied. Every candidate here is a real URL YouTube Music
    itself reported, unlike yt-dlp's speculative maxresdefault.jpg guess
    (which 404s), so the last (largest) one is always usable.

    Used directly only where the caller does its own proxying downstream
    (an artist's `avatar_url`, passed to cached_avatar_or_hotlink by
    remote_detail.py once a local copy is worth checking for). Everywhere
    else, see _proxied_cover_url below.
    """
    if not thumbnails:
        return None
    return absolute_thumbnail_url(cover_url_at_size(thumbnails[-1].get("url"), COVER_SIZE))


def _proxied_cover_url(thumbnails: list[dict] | None) -> str | None:
    """A song, playlist or release cover, ready to render.

    Unlike a followed artist's avatar, none of these ever earn a local copy
    (see cached_avatar_or_hotlink's docstring on why not — the same 92%
    orphan-file problem applies here at a much larger scale, since a track
    or an album is browsed far more often than it's followed). So this
    always goes through /image-proxy rather than checking for one on disk
    first — hotlinking Google's CDN directly from these cards used to fail
    silently and often: Chrome's ORB rejects a real share of yt3.ggpht.com
    responses outright (see download_avatar's docstring), and any cover
    that happened to arrive on lh3.googleusercontent.com instead — which
    absolute_thumbnail_url only rewrites away from for the yt3 host — was
    hard-blocked by this app's own img-src CSP, which never allowed that
    host at all.
    """
    url = _cover_url(thumbnails)
    return proxied_image_url(url) if url else None


def _artist_names(item: dict) -> tuple[str | None, str | None]:
    """A track's artists as a display string, plus the channel to attach it
    to.

    The channel is the first artist's own id, which for a song is the
    auto-generated "<Artist> - Topic" channel rather than the artist's
    official channel. That is the right choice *here* even though the Topic
    channel is not what anyone wants to follow: it arrives free in this same
    response, it is a real UC id with a working RSS artist, and all it does is
    give the preview row a placeholder artist to hang off (see
    routers/explore.py's add_video_batch). Following is a separate action and
    resolves the official channel properly — see fetch_artist.
    """
    artists = [artist for artist in item.get("artists") or [] if artist.get("name")]
    if not artists:
        return None, None
    channel_id = next(
        (
            artist["id"]
            for artist in artists
            if artist.get("id") and CHANNEL_ID_RE.match(artist["id"])
        ),
        None,
    )
    return ", ".join(artist["name"] for artist in artists), channel_id


def _song_result(item: dict) -> VideoSearchResult | None:
    """One search/artist entry as a VideoSearchResult.

    Reuses search.py's dataclass rather than introducing a music-shaped one,
    which is what keeps this module a drop-in swap: routers, schemas, the
    recommendations cache payload and the client's card renderer all keep
    speaking the shape they already speak.
    """
    video_id = item.get("videoId")
    if not video_id or not VIDEO_ID_RE.match(video_id):
        return None

    title = item.get("title")
    if not title:
        return None

    channel_title, channel_id = _artist_names(item)
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=_proxied_cover_url(item.get("thumbnails")),
        duration_seconds=item.get("duration_seconds"),
        channel_title=channel_title,
        channel_id=channel_id,
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
        # `author` is the publisher ("YouTube Music" for the curated ones);
        # `description` is the line YouTube Music itself prints under a mood
        # playlist, and it is the more useful of the two — "Taylor Swift,
        # Lewis Capaldi, Olivia Rodrigo" says more about a playlist called
        # "Fall Hits" than its publisher's name does. Whichever is present.
        channel_title=item.get("author") or item.get("description"),
    )


def _playlist_results(items: list[dict] | None) -> list[PlaylistSearchResult]:
    results = (_playlist_result(item) for item in items or [])
    return [result for result in results if result is not None]


def _artist_result(item: dict) -> ChannelSearchResult | None:
    """A chart or related-artist entry as a ChannelSearchResult.

    `browseId` here is the artist's YouTube Music browse id, which for an
    artist with an official channel *is* that channel's UC id — the same
    thing search.py's channel results carry, so these can share a shelf.
    Entries whose browse id isn't a channel id (a rare "MPLA…" artist page
    with no channel behind it) are dropped rather than rendered as a card
    that leads nowhere.
    """
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
    """Explore's song search. The counterpart of search.py's search_videos,
    and the reason this module exists — see its docstring."""
    return _song_results(_search(query, "songs", limit))


# YouTube Music labels every entry with what kind of recording it is.
# "ATV" is the audio track — the song as it appears on its album, with square
# cover art and, usually, timed lyrics. "OMV" is the official music video:
# a different recording of the same song, with a 16:9 video still for a cover
# and no album to hang it off.
SONG_VIDEO_TYPE = "MUSIC_VIDEO_TYPE_ATV"

# How many candidates find_song_version looks at. The song version is
# normally the first result; the extra four are there for the case where a
# live take or a sped-up upload outranks it.
SONG_MATCH_CANDIDATES = 5

# Punctuation, and the bracketed asides a music video's title carries that
# its song's doesn't (and the other way round): "(Official Video)",
# "(feat. …)", "[Explicit]".
_TITLE_NOISE_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _match_key(text: str | None) -> str:
    """A title or artist name reduced to what two spellings of the same thing
    have in common — lowercase words, no punctuation, no bracketed aside."""
    if not text:
        return ""
    return " ".join(_NON_WORD_RE.sub(" ", _TITLE_NOISE_RE.sub(" ", text.lower())).split())


# Words that, inside a title's bracketed aside, mean this is a *different
# recording* of the song rather than a different spelling of it. _match_key
# throws brackets away — which is what lets "Sunflower (Spider-Man: Into the
# Spider-Verse)" match "Sunflower" — and that same rule makes "Die With A
# Smile (Instrumental)" look like an equally good answer for "Die With A
# Smile". It isn't: swapping a track for its karaoke version is a worse
# outcome than leaving the music video alone.
#
# Deliberately not exhaustive and deliberately not applied to the whole
# title: "(Cardi B Version)" and "(Taylor's Version)" are the song, and
# guessing at every possible qualifier would start rejecting those.
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
    """True when a title's brackets mark it as a different take."""
    for aside in re.findall(r"\((.*?)\)|\[(.*?)\]", title or ""):
        words = set(_match_key(" ".join(part for part in aside if part)).split())
        if words & _OTHER_RECORDING_WORDS:
            return True
    return False


# Words a *video* title carries that its song's title doesn't — the
# uploader's own labelling. Only ever consulted for the words left over
# around an already-matched song title (see _leftover_explained), never to
# rewrite a title outright: "Video Games" is a song's name and "Official
# Video" is decoration, and only their position tells the two apart.
#
# Left exactly as the 117-track measurement in find_song_version ran it
# rather than tidied afterwards — that run is the evidence it produces no
# wrong match, and trimming the list would invalidate it.
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

# What a video title uses to set the song's name apart from the artist
# credit in front of it and the "Official MV" tail behind it. Quotes carry
# the most weight: in "KATSEYE (캣츠아이) 'Hootie Frutti' Official MV"
# nothing but the quotes says which two words are the song.
_TITLE_SEGMENT_RE = re.compile("[-–—|/'\"“”‘’()\\[\\]:,.]+")


def _title_segments(title: str | None) -> set[str]:
    """The video title's delimiter-separated parts, each through _match_key."""
    parts = (_match_key(part) for part in _TITLE_SEGMENT_RE.split(title or ""))
    return {part for part in parts if part}


def _contains_run(words: list[str], run: list[str]) -> bool:
    """Whether `run` appears in `words` as consecutive whole words. Whole
    words rather than a substring, so "art" does not match "Artist"."""
    span = len(run)
    if not span or span > len(words):
        return False
    return any(words[index : index + span] == run for index in range(len(words) - span + 1))


def _leftover_explained(words: list[str], run: list[str], allowed: set[str]) -> bool:
    """Whether everything in `words` other than one occurrence of `run` is
    accounted for: filler, a year, or part of an artist's name."""
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
    """Whether a song's title is this video's song title wearing the
    uploader's decoration.

    Bare containment is not enough on its own, and the measurement says so:
    alone it matched "Legends" against "VonOff1700 - Hood Legends (Official
    Video)", a different song by the same artist. One of two stronger things
    has to hold — either the song's title is one of the video title's
    delimited parts ("… 'Hootie Frutti' Official MV"), or every word left
    over once it is removed is decoration ("Video Games Performance Edit,
    HD, Closed Captioned").
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
    """Whether a candidate is by the artist this video is by.

    Three ways, strongest first, because the two things we know about the
    artist fail in opposite cases:

    * **The channel id.** YouTube Music gives one artist different display
      names in different responses — a playlist entry says "Marie Ulven"
      where search says "girl in red" — but both carry the same
      UCmNtyqQl03eWyvikCMbO3fA. That is an identity rather than a guess, and
      it makes a stage name and a legal name the same artist for free.
    * **The name**, for entries carrying no id to compare.
    * **A credited name inside the video's own title.** When a label uploads
      the video the stored artist is the *label* ("HYBE LABELS") and the real
      artist appears only in the title ("KATSEYE (캣츠아이) 'Animal'
      Official MV"). Corroboration rather than looseness: a wrong song's
      artist does not turn up in this video's title.

    Always against the entry's artist *list*, never against
    VideoSearchResult.channel_title, which is every credited artist joined
    into one string — comparing a lead artist ("ROSÉ") against the joined
    form ("ROSÉ, Bruno Mars") failed for every collaboration.
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
    """The album version of a track that arrived as a music video, or None.

    YouTube Music's curated and mood playlists are *video* playlists almost
    end to end — measured across three of them: 3 of 200, 3 of 96 and 2 of
    200 entries were songs, the rest music videos. A video entry has no album
    behind it, so it carries a 16:9 video still where every other cover in
    this app is square album art, and it usually has no lyrics either. That
    is the whole of "why do playlist covers look wrong".

    There is no counterpart field to follow. ytmusicapi does parse one, but
    only when the response carries a playlistPanelVideoWrapperRenderer —
    YouTube Music's own song/video switcher, which a signed-out client never
    receives. Ours is signed out (logged_in: 0) and the field came back None
    in both directions, checked. Searching for the song is what works.

    **Two rounds of measurement, and the second one moved the design.** Run
    over every track of a chart and of a mood playlist (117 music videos),
    matching on exact title equality alone against the same with the
    fallbacks below:

    | list                      | videos | exact only | with fallbacks |
    |---------------------------|--------|------------|----------------|
    | Trending 20 United States | 17     | 5  (29%)   | 12 (71%)       |
    | Fall Hits                 | 100    | 96 (96%)   | 100 (100%)     |

    Playlists were already fine; **charts were the broken case**, because the
    two carry different titles. A playlist entry has a clean song title
    ("Bel Air"); a chart entry has the raw uploaded video title ("KATSEYE
    (캣츠아이) 'Hootie Frutti' Official MV"). _match_key drops bracketed
    asides, so "(Official Video)" costs nothing while a bare "Official MV"
    defeats the whole match.

    All 17 chart decisions were then checked by hand: the 12 matches are
    right, and the 5 misses *should* miss — three of those songs exist on
    YouTube Music only as some other account's re-upload, one is not there at
    all, and one is a cover whose search returns the original (Cazzu's "Si
    Una Vez" finds Selena's). Loosening the artist check to catch them would
    mean playing the wrong recording, which is the one outcome worth
    avoiding: a search is a guess, and a music video beats the wrong song.
    Duration stays out of it — a video with a long intro runs 35 seconds
    past its song and is still the same track.

    Five candidates is enough: asking for ten changed no result out of 117.
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
        # Held back rather than returned, and the longest wins: a title that
        # accounts for more of the video's wording is the more specific
        # answer ("Hood Legends" over "Legends"). An exact match anywhere in
        # the list still beats every one of them, which is why this returns
        # only after the loop.
        if (
            _song_title_nested(video_words, song_key, segments, credited | {wanted_artist})
            and len(song_key.split()) > nested_words
        ):
            nested, nested_words = result, len(song_key.split())
    return nested


def search_playlists(query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[PlaylistSearchResult]:
    """Ready-made playlists for a query, preferring YouTube Music's own
    curated ones over user-made lists.

    Two filters rather than one because they answer differently: for
    "turkish rock", `featured_playlists` returns "Turkish Rock Legends" —
    curated, square cover art, actually about the genre — while `playlists`
    returns twenty community uploads of varying quality. The community
    search only runs when the curated one came up short, so the usual cost
    is still a single request.
    """
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
    """Explore's artist search.

    Replaced a yt-dlp search of youtube.com's channel tab, which had to filter
    "<Artist> - Topic" containers back out of its own results by name. This
    one is asking the music catalogue for musicians, so there is nothing to
    filter: measured live, "tarkan" returns Tarkan first out of 13.

    The results carry no subscriber count (measured: the field is absent), so
    cards built from these print a name and nothing under it. The count is on
    the artist's own page for anyone who opens it.
    """
    results = (_artist_result(item) for item in _search(query, "artists", limit))
    return [result for result in results if result is not None]


def fetch_playlist(playlist_id: str, limit: int = PLAYLIST_ITEM_LIMIT) -> PlaylistDetail:
    """One playlist's tracks. Empty items when there's no such playlist.

    Preferred over the yt-dlp flat read this replaced for the same reason
    fetch_release is: one request either way, but this one answers with real
    durations and square cover art rather than a video still. Measured live
    on a chart playlist: 100 of 100 tracks, every one with a duration, in
    0.47s.
    """
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
    """What one country's chart page holds: its Trending playlist (an
    ordinary playlist once opened, so the existing remote-playlist panel
    renders it with no special casing) and the artists currently charting
    there.

    `playlists` is a list rather than a single entry because fetch_charts_for
    blends several countries into one shelf; per country it holds exactly one
    (see CHART_TRENDING_PREFIX for what the other chart playlists were and
    why they are dropped).
    """

    playlists: list[PlaylistSearchResult]
    artists: list[ChannelSearchResult]


def fetch_charts(
    country: str = GLOBAL_CHART_COUNTRY, artist_limit: int = CHART_ARTIST_LIMIT
) -> Charts:
    """One country's charts. Both shelves come out of a single request —
    YouTube Music returns them together, and asking twice would double the
    cost of a shelf pair nobody edits.

    Only the country's Trending playlist is kept; the video charts alongside
    it are dropped (see CHART_TRENDING_PREFIX).
    """
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
    """Several countries' charts as one shelf pair, taken a rank at a time.

    One country is the ordinary case and costs one request; this is what
    makes more than one possible without the result being "country A's
    chart, then country B's" stuck end to end.

    **Why this exists rather than a filter.** The global chart ("ZZ") is
    heavily weighted toward the most populous music markets — measured on
    2026-08-21, nine of its top twenty artists were Indian playback singers
    and the top five were all of them. There is no way to filter that: a
    chart artist comes back as `browseId, rank, subscribers, thumbnails,
    title, trend` and nothing else, so nothing in the response says which
    market it charted in. Script detection on the name doesn't work either —
    they are Latin-script ("Alka Yagnik") like everything else. Naming the
    countries you want is the only mechanism the API actually supports.

    Round-robin by rank, not concatenation: taking each country's #1, then
    each country's #2, gives a shelf where every listed country is
    represented near the top rather than the first one owning the whole
    thing. Deduped by browse id, since the same artist charts in several
    places and the second listing is not worth a tile.
    """
    if len(countries) == 1:
        return fetch_charts(countries[0], artist_limit)

    per_country = [fetch_charts(country, artist_limit) for country in countries]
    return Charts(
        # One Trending playlist per country now (see CHART_TRENDING_PREFIX),
        # so this is a concatenation in country order and the round-robin is
        # doing nothing that a flat list wouldn't. Kept anyway: it is the
        # same call as the artists below, it still dedupes, and it is what
        # keeps the shelf honest if a country ever reports two.
        playlists=_round_robin(
            [charts.playlists for charts in per_country], "playlist_id"
        )[:CHART_PLAYLIST_LIMIT],
        artists=_round_robin([charts.artists for charts in per_country], "channel_id")[
            :artist_limit
        ],
    )


def _round_robin(lists: list[list], identity: str) -> list:
    """One from each list, then the next from each, until they run out.

    Deduped by `identity`, since the same artist charts in several countries
    and the second listing is not worth a tile.
    """
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
    """One entry of YouTube Music's mood/genre browse menu. `params` is an
    opaque token from that same response — the only way to ask for the
    category's playlists, and not something that can be constructed."""

    title: str
    params: str
    section: str


# YouTube Music's mood menu has two sections and only one of them is usable.
# Measured 2026-08-19 across all 40 categories: 25 of them raise a parse
# error from inside ytmusicapi, and they are every single entry under
# "Genres" ("Rock", "Jazz", "Latin", …) plus one mood ("Family"). Those
# grids mix plain videos in among the playlists, and the library's playlist
# parser reads a browse id off a title run those video items don't have —
# so it isn't a bad category, it's a shape its parser doesn't handle, and
# nothing short of reimplementing that parser works around it here.
# Defaulting to the section that works keeps the shelf reliable; pass
# section=None to get everything back once upstream can parse it.
MOOD_SECTION = "Moods & moments"


def fetch_mood_categories(section: str | None = MOOD_SECTION) -> list[MoodCategory]:
    """The moods (and, asked for, genres) YouTube Music currently offers,
    flattened out of its two sections but keeping which one each came from,
    so a caller can present them apart."""
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
    """One mood or genre's playlists. `params` comes from
    fetch_mood_categories and is opaque — see MoodCategory."""
    return _playlist_results(_call("mood playlists", "get_mood_playlists", params))[:limit]


@dataclass
class ArtistRelease:
    """One album or single off an artist's page.

    `browse_id` is an "MPREb_…" release id rather than a playlist, and it
    is all a card needs: fetch_release opens either kind with it. Albums
    also report an `audioPlaylistId` here and singles don't, which is why
    that isn't the identifier this uses — one id that works for both beats
    two paths where one of them is sometimes absent.

    `year` is the only date YouTube Music reports on this surface. There is
    no month or day, which is why "new" here can only ever mean "this
    year".
    """

    browse_id: str
    title: str
    year: str | None
    kind: str
    cover_url: str | None


@dataclass
class ArtistProfile:
    """An artist page as YouTube Music knows it.

    `browse_id` is how YouTube Music addresses the artist, and it accepts
    more than one id for the same person: the Topic-channel id that song
    results and chart entries carry, *and* the artist's official channel id
    — verified live, both open Shirin David's page and both report the same
    `channelId` back. That second half is what lets an ordinary channel
    result open an artist page (see services/remote_detail.py).

    `channel_id` is the artist's **official** YouTube channel — the one a
    person browses. `topic_channel_id` is the auto-generated one their music
    is published to, and the one this app follows; see _topic_channel_id.
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
    # How many tracks the artist has in total, which is not always how many
    # are in `tracks` — see ARTIST_TRACK_LIMIT.
    track_count: int = 0
    # The rest of what the artist page carries. All of it arrives in the
    # same response as the fields above, so a profile that renders every
    # one of these costs exactly what a bare track list costs.
    albums: list[ArtistRelease] = field(default_factory=list)
    singles: list[ArtistRelease] = field(default_factory=list)
    related: list[ChannelSearchResult] = field(default_factory=list)


def _artist_songs(songs: dict, all_songs: bool) -> tuple[list[dict], int | None]:
    """The song entries to list for an artist, and how many there are in
    total when YouTube Music says so.

    The page itself only previews five, with a "Top songs" playlist behind a
    browse id for the rest — 56 for a mid-size artist, 150 for a long career,
    measured. A five-track artist page is not worth opening, so the playlist
    is what gets listed and the preview is only the fallback for an artist
    who has no such playlist. Costs a second request, which is why
    `all_songs` exists: a caller that only wants the ids off the page header
    shouldn't pay it.

    `limit` bounds how many continuation requests get made, not the list —
    ytmusicapi reads whole pages and stops once it has enough, so asking for
    100 still returned all 150. And the list that comes back can be shorter
    than the playlist claims, since a few entries fail to parse. Both are
    why the count travels separately: the caller can't infer "there are
    more" from the length it was handed.
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
    """One of the artist page's release shelves. Entries with no browse id
    are dropped rather than rendered as a card that leads nowhere, the same
    way _artist_result drops artists with no channel behind them."""
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
                # Singles report their own ("Single", and "EP" for some);
                # albums report nothing, so the shelf they came from is the
                # only thing that knows.
                kind=item.get("type") or kind,
            )
        )
    return releases[:ARTIST_RELEASE_LIMIT]


def _topic_channel_id(songs: list[dict], name: str) -> str | None:
    """The artist's own "<Artist> - Topic" channel, read off their tracks.

    Every song credits its artists by id, and for the artist whose page this
    is, that id is their Topic channel — the auto-generated one a label
    uploads licensed audio to. It is the only channel that carries their
    music and nothing else, which is what makes it the right thing to
    follow: an official channel's artist also carries vlogs and interviews,
    and those are not what someone following an artist in a music app is
    asking for.

    Matched by name rather than taken from the first credit, because a
    collaboration lists the other artist first. Measured on Shirin David and
    Sezen Aksu: the name match finds exactly one id, credited on 56 of 56
    and 150 of 150 tracks respectively, and its artist is titled "<Artist> -
    Topic". Costs nothing — this is the response the page already returned.

    Case-insensitively, because the two halves of that match come from
    different places and do not always agree: Usher's page is headed
    "USHER" while every one of his tracks credits "Usher", and an exact
    comparison quietly returned nothing for him.
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
    """Follows a VEVO channel through to the artist page that has the music
    on it.

    A label's VEVO channel is a video channel, and YouTube Music answers for
    one with an artist page that knows the right name and carries no songs
    at all — no preview, no "Top songs" playlist behind it, nothing to read
    a Topic channel off (measured on Travis Scott, 50 Cent, Snoop Dogg and
    Beyoncé: all four came back with a name and zero songs). What it does
    carry is a `channelId` pointing somewhere else, and asking again with
    that returns the real page, songs and all — 4 of 4.

    That difference is the whole trigger, so the second request only ever
    happens on a page that had nothing to offer anyway. An artist asked for
    by their own id gets their own id back and never reaches this.
    """
    redirect = artist.get("channelId")
    if (artist.get("songs") or {}).get("results"):
        return artist, browse_id
    if not redirect or redirect == browse_id or not CHANNEL_ID_RE.match(redirect):
        return artist, browse_id

    followed = _call("redirected artist", "get_artist", redirect, level=logging.INFO)
    if not followed or not followed.get("name"):
        return artist, browse_id
    # The id travels with it: this is the one that reopens the profile, and
    # the VEVO id would reopen the songless page this just escaped.
    return followed, redirect


def _related_artists(section: dict | None) -> list[ChannelSearchResult]:
    results = (_artist_result(item) for item in (section or {}).get("results") or [])
    return [result for result in results if result is not None]


def fetch_artist(browse_id: str, all_songs: bool = True) -> ArtistProfile | None:
    """One artist's page, or None if YouTube Music has no such artist.

    None is also what an ordinary, non-musical channel gets: asked for one,
    YouTube Music answers with a page this parser can't read (KeyError on
    'musicImmersiveHeaderRenderer', measured on both a podcast and a tech
    channel), which _call flattens like any other failure. That makes this
    function safe to *try* on any channel id and let the answer decide — see
    services/remote_detail.py.

    `tracks` is the artist's songs and **not** the videos section of the
    same page, which used to be merged in here. That merge earned its keep
    when this only had the page's five-song preview to work with; against
    the full song list it adds almost nothing and costs twice. Measured on
    Drake and on Shirin David: 8 of the 10 videos were already in the song
    list under a different id — YouTube Music holds separate ids for a
    release's audio track (MUSIC_VIDEO_TYPE_ATV) and its official video
    (…_OMV), so deduplicating by video id can't see they're the same song —
    and a video entry carries no duration at all (its keys are title,
    videoId, artists, playlistId, thumbnails, views), so every one of them
    rendered as a duration-less row at the bottom of the panel.

    An artist with no songs at all comes back with nothing playable, which
    sends the panel to their channel listing (see
    services/remote_detail.py) — where their videos are, in full. A VEVO
    channel looks exactly like that case and isn't: see _redirected_artist,
    which is why `browse_id` on the returned profile is not always the one
    that was asked for.

    Everything else on the page — albums, singles, videos, related artists
    — comes back too, off this same response. The profile renders all of
    them and pays nothing extra for it; a caller that only wants the track
    list can ignore them.
    """
    artist = _call("artist", "get_artist", browse_id, level=logging.INFO)
    if not artist or not artist.get("name"):
        return None

    # A VEVO channel lands on a page with the right name and no music on it;
    # this is what walks from there to the page that has it.
    artist, browse_id = _redirected_artist(artist, browse_id)

    songs, reported_count = _artist_songs(artist.get("songs") or {}, all_songs)
    tracks: list[VideoSearchResult] = []
    seen: set[str] = set()
    for track in _song_results(songs):
        if track.video_id in seen:
            continue
        seen.add(track.video_id)
        tracks.append(track)

    # What the artist has, against what this page will show. The reported
    # count is the larger of the two whenever entries fail to parse, which
    # happens routinely — Drake's playlist reports 150 tracks and yields
    # 143, Bach's 147. It's what lets the panel say "first 143 of 150"
    # rather than implying 143 is all there is.
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
    """One album or single, opened. The same shape either way — YouTube
    Music answers a one-track single and a fourteen-track album with the
    identical structure, so nothing here has to care which it is."""

    title: str
    year: str | None
    kind: str
    cover_url: str | None
    artist_names: str | None
    tracks: list[VideoSearchResult]


def fetch_release(browse_id: str) -> ReleaseDetail | None:
    """An album or single's tracks, or None if there's no such release.

    Used for both, and deliberately in preference to opening an album's
    `audioPlaylistId` through the yt-dlp playlist path: it costs the same
    one request and answers better, with real durations and the square
    cover art rather than a video still.
    """
    release = _call("release", "get_album", browse_id)
    if not release or not release.get("title"):
        return None

    tracks = _song_results(release.get("tracks"))
    if not tracks:
        return None

    # A track entry inside an album/single response carries no thumbnail of
    # its own — measured live on a 14-track album, every single one came
    # back with thumbnails: None, since the whole release shares one cover
    # rather than each track having its own. Without this, every row in an
    # opened album rendered with no image at all. cover_url is already
    # proxied (see _proxied_cover_url), so no extra wrapping needed here.
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
    """One line of timed lyrics, in milliseconds from the start of the track."""

    text: str
    start_ms: int
    end_ms: int


@dataclass
class TimedLyrics:
    lines: list[LyricLine]
    source: str | None


def fetch_timed_lyrics(video_id: str) -> TimedLyrics | None:
    """This track's lyrics with timestamps, or None if it hasn't got any.

    Two requests, not one, and there is no cheaper route: get_lyrics needs an
    "MPLYt…" browse id, and the only place that id is published is the watch
    playlist for the video. `limit=1` keeps the second half of that response
    (a radio queue nobody here wants) from being built.

    Measured over 21 tracks before this was written, because the numbers
    decide how it should be used:

      * timestamps: 6/21. Plain untimed lyrics: **0/21**. Where YouTube Music
        has lyrics at all it appears to have timed ones, so there is no
        "timed, else plain" ladder to climb — a track either has this or has
        nothing, which is why this returns one type and None.
      * coverage splits by what the row is. Proper song entries (what a
        followed artist's sync produces) came back 4/9; "(Official Music
        Video)"-style uploads 2/12. Lyrics attach to the song in YouTube
        Music's catalogue, not to a video of it.

    So roughly two thirds of a mixed library has none, which is why callers
    cache the None as hard as they cache a hit (see services/lyrics.py) and
    why nothing fetches this until someone asks to read it.
    """
    watch = _call("watch playlist (for lyrics)", "get_watch_playlist", videoId=video_id, limit=1)
    browse_id = (watch or {}).get("lyrics")
    if not browse_id:
        return None

    result = _call("lyrics", "get_lyrics", browse_id, timestamps=True)
    if not result or not result.get("hasTimestamps"):
        # Untimed lyrics are treated as no lyrics rather than rendered as a
        # block of text: the panel exists to follow along, and a static wall
        # that never moves while the track plays reads as broken. It also
        # never actually happened in the sample above.
        return None

    lines = [
        LyricLine(text=line.text, start_ms=line.start_time, end_ms=line.end_time)
        for line in result["lyrics"]
    ]
    if not lines:
        return None
    return TimedLyrics(lines=lines, source=result.get("source"))
