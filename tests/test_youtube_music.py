"""app/youtube/music.py without the network; response bodies are trimmed live captures."""

import logging
from urllib.parse import quote

import pytest

from app.youtube import music
from app.youtube.urls import cover_url_at_size, is_video_still, playlist_id_from_browse_id


def _proxied(remote_url: str) -> str:
    """The same wrapping _proxied_cover_url applies."""
    return f"/image-proxy?u={quote(remote_url, safe='')}"

SONG = {
    "title": "Biliyorsun",
    "videoId": "_efHZg9D9iE",
    "videoType": "MUSIC_VIDEO_TYPE_ATV",
    "duration": "5:17",
    "duration_seconds": 317,
    "album": {"name": "Ağlamak Güzeldir", "id": "MPREb_3dKYrF4PXHQ"},
    "artists": [{"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"}],
    "thumbnails": [
        {"url": "https://yt3.googleusercontent.com/abc=w60-h60-l90-rj", "width": 60},
        {"url": "https://yt3.googleusercontent.com/abc=w120-h120-l90-rj", "width": 120},
    ],
}

FEATURED_PLAYLIST = {
    "title": "Turkish Rock Legends",
    "author": "YouTube Music",
    "browseId": "VLRDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk",
    "itemCount": 75,
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/def=w226-h226-l90-rj"}],
}

CHART_ARTIST = {
    "title": "BLOK3",
    "browseId": "UCZpmeLoLLb3vmxgscRyLPgw",
    "subscribers": "1.8M",
    "rank": "1",
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/ghi=w120-h120-l90-rj-dcJRaW7REL"}],
}


class FakeYTMusic:
    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        response = self.responses.get(name, [])
        return response(*args, **kwargs) if callable(response) else response

    def search(self, query, **kwargs):
        return self._record("search", query, **kwargs)

    def get_charts(self, country):
        return self._record("get_charts", country)

    def get_mood_categories(self):
        return self._record("get_mood_categories")

    def get_mood_playlists(self, params):
        return self._record("get_mood_playlists", params)

    def get_artist(self, browse_id):
        return self._record("get_artist", browse_id)

    def get_playlist(self, playlist_id, limit=None):
        return self._record("get_playlist", playlist_id, limit=limit)

    def get_album(self, browse_id):
        return self._record("get_album", browse_id)


@pytest.fixture
def client(monkeypatch):
    """Installs a FakeYTMusic so no test in this file can reach the network."""

    def install(**responses):
        fake = FakeYTMusic(**responses)
        monkeypatch.setattr(music, "_client", lambda: fake)
        return fake

    return install


def test_a_song_becomes_a_video_search_result(client):
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.video_id == "_efHZg9D9iE"
    assert result.title == "Biliyorsun"
    assert result.duration_seconds == 317


def test_a_songs_artists_become_its_channel(client):
    """A preview row's placeholder artist hangs off the Topic channel id."""
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.channel_id == "UCNaGLJRPE3ohleIDM7RFtlQ"
    assert result.channel_title == "Sezen Aksu"


def test_several_artists_become_a_credit_beside_the_lead(client):
    """The credit line is separate from the artist, or a collaboration renames the artist."""
    client(
        search=[
            {
                **SONG,
                "artists": [
                    {"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"},
                    {"name": "Sertab Erener", "id": "UCVQJZE7dNPQdKPBPQnPHIQA"},
                ],
            }
        ]
    )

    (result,) = music.search_songs("duet")

    assert result.channel_title == "Sezen Aksu"
    assert result.artist_credit == "Sezen Aksu, Sertab Erener"
    assert result.channel_id == "UCNaGLJRPE3ohleIDM7RFtlQ"


def test_one_artist_gets_no_credit_of_its_own(client):
    """NULL lets Content.display_artist fall back to the artist."""
    client(search=[{**SONG, "artists": [{"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"}]}])

    (result,) = music.search_songs("solo")

    assert result.channel_title == "Sezen Aksu"
    assert result.artist_credit is None


def test_the_lead_name_follows_the_channel_that_was_picked(client):
    """channel_title and channel_id must describe the same person."""
    client(
        search=[
            {
                **SONG,
                "artists": [
                    {"name": "Various Artists", "id": None},
                    {"name": "Sertab Erener", "id": "UCVQJZE7dNPQdKPBPQnPHIQA"},
                ],
            }
        ]
    )

    (result,) = music.search_songs("compilation")

    assert result.channel_id == "UCVQJZE7dNPQdKPBPQnPHIQA"
    assert result.channel_title == "Sertab Erener"
    assert result.artist_credit == "Various Artists, Sertab Erener"


def test_a_compilation_with_no_real_artist_channel_keeps_none(client):
    """"Various Artists" has no id; the batch endpoint refuses None rather than inventing one."""
    client(search=[{**SONG, "artists": [{"name": "Various Artists", "id": None}]}])

    (result,) = music.search_songs("compilation")

    assert result.channel_title == "Various Artists"
    assert result.channel_id is None


def test_cover_art_is_requested_at_a_size_worth_rendering(client):
    """The API reports 60/120px covers; cards render at ~200px."""
    client(search=[SONG])

    (result,) = music.search_songs("sezen aksu")

    assert result.thumbnail_url == _proxied("https://yt3.ggpht.com/abc=w544-h544-l90-rj")


def test_an_entry_with_no_video_id_is_dropped(client):
    client(search=[SONG, {**SONG, "videoId": None}, {**SONG, "videoId": "not-an-id"}])

    assert len(music.search_songs("sezen aksu")) == 1


def test_a_failing_call_is_an_empty_result_not_an_exception(monkeypatch):
    class Exploding:
        def search(self, *args, **kwargs):
            raise RuntimeError("InnerTube said no")

    monkeypatch.setattr(music, "_client", Exploding)

    assert music.search_songs("anything") == []


def test_search_never_passes_a_language(client):
    """YTMusic(language=...) silently returns empty lists, so nothing may set one."""
    fake = client(search=[SONG])

    music.search_songs("sezen aksu")

    (_, _, kwargs), = fake.calls
    assert "language" not in kwargs


def test_a_playlist_browse_id_loses_its_vl_prefix(client):
    """The VL-prefixed id builds a playlist URL that resolves to nothing, yet PLAYLIST_ID_RE accepts it."""
    client(search=[FEATURED_PLAYLIST])

    (result,) = music.search_playlists("turkish rock")

    assert result.playlist_id == "RDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk"
    assert result.title == "Turkish Rock Legends"
    assert result.channel_title == "YouTube Music"


def test_playlist_search_only_falls_back_to_community_lists_when_short(client):
    fake = client(
        search=lambda query, **kwargs: (
            [FEATURED_PLAYLIST] if kwargs["filter"] == "featured_playlists" else []
        )
    )

    music.search_playlists("turkish rock", limit=1)

    assert [kwargs["filter"] for _, _, kwargs in fake.calls] == ["featured_playlists"]


def test_playlist_search_tops_up_from_community_lists_and_deduplicates(client):
    other = {**FEATURED_PLAYLIST, "browseId": "VLPLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"}
    fake = client(
        search=lambda query, **kwargs: (
            [FEATURED_PLAYLIST]
            if kwargs["filter"] == "featured_playlists"
            else [FEATURED_PLAYLIST, other]
        )
    )

    results = music.search_playlists("turkish rock", limit=4)

    assert [kwargs["filter"] for _, _, kwargs in fake.calls] == [
        "featured_playlists",
        "playlists",
    ]
    assert [result.playlist_id for result in results] == [
        "RDCLAK5uy_mq6KpOULj_9zLh4CH3s9IIT_87Tyf9eIk",
        "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm",
    ]


def test_a_mood_playlist_describes_itself_by_who_is_on_it(client):
    client(
        get_mood_playlists=[
            {
                "title": "Fall Hits",
                "playlistId": "RDCLAK5uy_k8d0XHQgAWWSZe7l7tUp0xLmEV_ncPxck",
                "description": "Taylor Swift, Lewis Capaldi",
                "thumbnails": [{"url": "https://yt3.googleusercontent.com/j=w226-h226-l90-rj"}],
            }
        ]
    )

    (result,) = music.fetch_mood_playlists("ggMPOg1uX3JBUDJTM2ZUUVJM")

    assert result.playlist_id == "RDCLAK5uy_k8d0XHQgAWWSZe7l7tUp0xLmEV_ncPxck"
    assert result.channel_title == "Taylor Swift, Lewis Capaldi"


MOOD_MENU = {
    "Moods & moments": [{"title": "Chill", "params": "aaa"}],
    "Genres": [{"title": "Blues", "params": "bbb"}],
}


def test_mood_categories_skip_the_section_that_cannot_be_parsed(client):
    """Every "Genres" entry raises a parse error inside ytmusicapi (music.MOOD_SECTION)."""
    client(get_mood_categories=MOOD_MENU)

    categories = music.fetch_mood_categories()

    assert [(c.title, c.section) for c in categories] == [("Chill", "Moods & moments")]


def test_mood_categories_remember_which_section_they_came_from(client):
    client(get_mood_categories=MOOD_MENU)

    categories = music.fetch_mood_categories(section=None)

    assert [(c.title, c.section) for c in categories] == [
        ("Chill", "Moods & moments"),
        ("Blues", "Genres"),
    ]


CHART_PLAYLIST = {
    "title": "Trending 20 Turkey",
    "playlistId": "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM",
    "thumbnails": [{"url": "https://yt3.googleusercontent.com/k=s192"}],
}


def test_both_chart_shelves_come_from_one_request(client):
    fake = client(get_charts={"videos": [CHART_PLAYLIST], "artists": [CHART_ARTIST]})

    charts = music.fetch_charts("TR")

    assert len(fake.calls) == 1
    (playlist,) = charts.playlists
    assert playlist.playlist_id == "OLAK5uy_mFBgHnPi7PIkt7vlG84rCduzVjFtuHnpM"
    # Chart art uses "=s192" rather than "=w226-h226"; cover_url_at_size handles both.
    assert playlist.thumbnail_url == _proxied("https://yt3.ggpht.com/k=s544")


def test_a_charting_artist_becomes_a_followable_channel(client):
    client(get_charts={"videos": [], "artists": [CHART_ARTIST]})

    (result,) = music.fetch_charts("TR").artists

    assert result.channel_id == "UCZpmeLoLLb3vmxgscRyLPgw"
    assert result.subscriber_count == 1_800_000
    assert result.channel_url == "https://www.youtube.com/channel/UCZpmeLoLLb3vmxgscRyLPgw"


def test_an_artist_without_a_channel_behind_it_is_dropped(client):
    client(get_charts={"videos": [], "artists": [{**CHART_ARTIST, "browseId": "MPLAucbrowseid"}]})

    assert music.fetch_charts("TR").artists == []


def test_a_country_with_no_charts_at_all_is_two_empty_shelves(client):
    client(get_charts=None)

    charts = music.fetch_charts("ZZ")

    assert charts.playlists == []
    assert charts.artists == []


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("1.8M", 1_800_000),
        ("3.19M", 3_190_000),
        ("952K", 952_000),
        ("1.2B", 1_200_000_000),
        ("4,370,252,054 views", 4_370_252_054),
        ("no idea", None),
        (None, None),
    ],
)
def test_display_counts_become_numbers(reported, expected):
    assert music._parse_count(reported) == expected


def test_an_artist_page_resolves_the_official_channel(client):
    """The browse id is the Topic channel; `channelId` is the artist's real one."""
    client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "subscribers": "3.19M",
            "monthlyListeners": "1.9M monthly listeners",
            "description": "Turkish singer, songwriter and producer.",
            "thumbnails": [{"url": "https://lh3.googleusercontent.com/l=w120-h120-p-l90-rj"}],
            "songs": {"results": [SONG]},
            "videos": {"results": []},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ")

    assert artist.channel_id == "UC6OI7Crv96jgra5pwJNDFRQ"
    assert artist.subscriber_count == 3_190_000
    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]


def test_the_videos_section_is_left_out(client):
    """Videos mostly duplicate listed songs and carry no duration."""
    music_video = {"videoId": "3q4cJ1G_on8", "title": "Aşk Dansı", "views": "1.7B"}
    client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "songs": {"results": [SONG]},
            "videos": {"results": [music_video]},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]


def test_the_same_id_is_never_listed_twice(client):
    client(
        get_artist={"name": "Sezen Aksu", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": [SONG, SONG]},
    )

    assert [track.video_id for track in music.fetch_artist("UCx").tracks] == ["_efHZg9D9iE"]


def test_an_artist_page_lists_the_whole_top_songs_playlist(client):
    """The page previews five songs; the full top-songs playlist is what gets listed."""
    deep_cut = {**SONG, "videoId": "3q4cJ1G_on8", "title": "Aşk Dansı"}
    fake = client(
        get_artist={
            "name": "Shirin David",
            "channelId": "UC5ZkRnYd3__WBBGnAnWO9Cg",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
            "videos": {"results": []},
        },
        get_playlist={"title": "Top songs", "tracks": [SONG, deep_cut]},
    )

    artist = music.fetch_artist("UC5ZkRnYd3__WBBGnAnWO9Cg")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE", "3q4cJ1G_on8"]
    assert ("get_playlist", ("VLOLAK5uy_mcACjdxLHv",), {"limit": music.ARTIST_TRACK_LIMIT}) in fake.calls


def test_the_previewed_songs_stand_in_when_the_playlist_cannot_be_read(client):
    """An empty result would fall through to the channel listing (services/remote_detail.py)."""
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
        },
        get_playlist=None,
    )

    artist = music.fetch_artist("UC5ZkRnYd3__WBBGnAnWO9Cg")

    assert [track.video_id for track in artist.tracks] == ["_efHZg9D9iE"]


def test_the_cap_is_above_anything_youtube_music_serves(client):
    """Top-songs playlists stop at 150; the cap bounds a remote list, it isn't expected to bite."""
    songs = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(150)]
    client(
        get_artist={"name": "Sezen Aksu", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": songs, "trackCount": 150},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 150 < music.ARTIST_TRACK_LIMIT
    assert artist.track_count == 150


def test_an_absurd_list_is_still_capped(client):
    tracks = [{**SONG, "videoId": f"_efHZg9{n:04d}"} for n in range(250)]
    client(
        get_artist={"name": "Someone", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 250},
    )

    assert len(music.fetch_artist("UCx").tracks) == music.ARTIST_TRACK_LIMIT


def test_entries_youtube_drops_are_reported_as_missing(client):
    """`track_count` carries the shortfall so the panel says "first 143 of 150"."""
    tracks = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(143)]
    client(
        get_artist={"name": "Drake", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 150},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 143
    assert artist.track_count == 150


def test_a_short_catalogue_is_not_reported_as_truncated(client):
    tracks = [{**SONG, "videoId": f"_efHZg9D{n:03d}"} for n in range(56)]
    client(
        get_artist={"name": "Shirin David", "songs": {"browseId": "VLx", "results": []}},
        get_playlist={"tracks": tracks, "trackCount": 56},
    )

    artist = music.fetch_artist("UCx")

    assert len(artist.tracks) == 56
    assert artist.track_count == 56


def test_all_songs_false_does_not_pay_for_the_track_list(client):
    """A follow only needs the page header, not the track list."""
    fake = client(
        get_artist={
            "name": "Sezen Aksu",
            "channelId": "UC6OI7Crv96jgra5pwJNDFRQ",
            "songs": {"browseId": "VLOLAK5uy_mcACjdxLHv", "results": [SONG]},
        }
    )

    artist = music.fetch_artist("UCNaGLJRPE3ohleIDM7RFtlQ", all_songs=False)

    assert artist.channel_id == "UC6OI7Crv96jgra5pwJNDFRQ"
    assert [call[0] for call in fake.calls] == ["get_artist"]


def test_a_channel_that_is_not_an_artist_is_none(client, caplog):
    """Non-artist channels raise KeyError inside ytmusicapi; that must return None without warning."""

    def raise_key_error(browse_id):
        raise KeyError("musicImmersiveHeaderRenderer")

    client(get_artist=raise_key_error)

    with caplog.at_level(logging.INFO, logger="app.youtube.music"):
        assert music.fetch_artist("UCGq-a57w-aPwyi3pW7XLiHw") is None

    assert [record.levelno for record in caplog.records] == [logging.INFO]


def test_an_artist_page_carries_its_releases(client):
    """Releases come off the same response as the songs."""
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"results": [SONG]},
            "albums": {
                "results": [
                    {
                        "title": "Schlau aber blond",
                        "browseId": "MPREb_HIQTwIoDtEM",
                        "year": "2025",
                        "audioPlaylistId": "OLAK5uy_niuCyuWWZYKv6jIwsWqDkVsYiBq9C_Plg",
                        "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
                    }
                ]
            },
            "singles": {
                "results": [
                    {"title": "Gut Genug", "browseId": "MPREb_5Y3mCZ5XtG3", "year": "2026", "type": "Single"}
                ]
            },
            "related": {"results": [CHART_ARTIST]},
        }
    )

    artist = music.fetch_artist("UCx")

    (album,) = artist.albums
    assert (album.browse_id, album.year, album.kind) == ("MPREb_HIQTwIoDtEM", "2025", "Album")
    assert album.cover_url == _proxied("https://x/c=w544-h544-l90-rj")
    (single,) = artist.singles
    # Singles report their own type; albums report none, so the shelf names it.
    assert single.kind == "Single"
    assert [artist.title for artist in artist.related] == ["BLOK3"]


def test_a_release_with_no_browse_id_is_dropped(client):
    client(
        get_artist={
            "name": "Shirin David",
            "songs": {"results": [SONG]},
            "albums": {"results": [{"title": "Nameless"}, {"browseId": "MPREb_ok"}]},
        }
    )

    assert music.fetch_artist("UCx").albums == []


def test_an_album_and_a_single_open_the_same_way(client):
    """A single and an album share one response structure."""
    client(
        get_album={
            "title": "Schlau aber blond",
            "year": "2025",
            "type": "Album",
            "artists": [{"name": "Shirin David"}],
            "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
            "tracks": [SONG],
        }
    )

    release = music.fetch_release("MPREb_HIQTwIoDtEM")

    assert (release.title, release.year, release.kind) == ("Schlau aber blond", "2025", "Album")
    assert release.artist_names == "Shirin David"
    assert [track.video_id for track in release.tracks] == ["_efHZg9D9iE"]
    assert release.cover_url == _proxied("https://x/c=w544-h544-l90-rj")


def test_a_tracks_missing_thumbnail_falls_back_to_the_album_cover(client):
    """Album tracks carry no thumbnail of their own."""
    client(
        get_album={
            "title": "Schlau aber blond",
            "year": "2025",
            "type": "Album",
            "artists": [{"name": "Shirin David"}],
            "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
            "tracks": [{**SONG, "thumbnails": None}],
        }
    )

    release = music.fetch_release("MPREb_HIQTwIoDtEM")

    (track,) = release.tracks
    assert track.thumbnail_url == release.cover_url == _proxied("https://x/c=w544-h544-l90-rj")


def test_a_tracks_own_thumbnail_is_not_overwritten_by_the_album_cover(client):
    client(
        get_album={
            "title": "Schlau aber blond",
            "year": "2025",
            "type": "Album",
            "artists": [{"name": "Shirin David"}],
            "thumbnails": [{"url": "https://x/c=w226-h226-l90-rj"}],
            "tracks": [SONG],
        }
    )

    release = music.fetch_release("MPREb_HIQTwIoDtEM")

    (track,) = release.tracks
    assert track.thumbnail_url == _proxied("https://yt3.ggpht.com/abc=w544-h544-l90-rj")


@pytest.mark.parametrize("response", [None, {"title": "Gone", "tracks": []}, {"tracks": [SONG]}])
def test_a_release_that_cannot_be_read_is_none(client, response):
    client(get_album=response)

    assert music.fetch_release("MPREb_x") is None


def test_an_unknown_artist_is_none_not_an_empty_profile(client):
    client(get_artist=None)

    assert music.fetch_artist("UCnope") is None


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # YouTube Music's own dialect, with and without a trailing segment.
        ("https://x/a=w60-h60-l90-rj", "https://x/a=w544-h544-l90-rj"),
        ("https://x/a=w120-h120-l90-rj-dcJRaW7REL", "https://x/a=w544-h544-l90-rj-dcJRaW7REL"),
        ("https://x/a=w60-h60-p-l90-rj", "https://x/a=w544-h544-p-l90-rj"),
        # The "=s<n>" dialect, which chart art uses.
        ("https://x/a=s192", "https://x/a=s544"),
        # A video still: signed, not resizable, left alone.
        ("https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc", "https://i.ytimg.com/vi/x/hq720.jpg?sqp=abc"),
        (None, None),
    ],
)
def test_cover_url_at_size_handles_both_size_dialects(url, expected):
    assert cover_url_at_size(url, 544) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # What YouTube Music reports as a music video's cover.
        ("https://i.ytimg.com/vi/1lrFsXkT_rM/hqdefault.jpg?sqp=-oaymwEWCJAD&rs=AMzJ", True),
        # No query, and the webp host variant.
        ("https://i.ytimg.com/vi/1lrFsXkT_rM/mqdefault.jpg", True),
        ("https://i.ytimg.com/vi_webp/1lrFsXkT_rM/hqdefault.webp", True),
        # Square album art: what a song carries (see images.is_music_video).
        ("https://yt3.ggpht.com/abc=w544-h544-l90-rj", False),
        ("https://lh3.googleusercontent.com/abc=w544-h544-l90-rj", False),
        # A locally cached cover.
        ("/thumbnails/1lrFsXkT_rM.jpg", False),
        (None, False),
    ],
)
def test_is_video_still_tells_a_video_frame_from_album_art(url, expected):
    assert is_video_still(url) is expected


@pytest.mark.parametrize(
    ("browse_id", "expected"),
    [
        ("VLPLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm", "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"),
        ("VLRDCLAK5uy_mq6KpOULj", "RDCLAK5uy_mq6KpOULj"),
        # Already unprefixed — the mood shelves report ids this way.
        ("RDCLAK5uy_mq6KpOULj", "RDCLAK5uy_mq6KpOULj"),
        # Too short to be a playlist id once the prefix comes off.
        ("VLPL", None),
        (None, None),
    ],
)
def test_playlist_id_from_browse_id(browse_id, expected):
    assert playlist_id_from_browse_id(browse_id) == expected


VEVO_ID = "UClRx3MMyYUyqOxyEqA5F2nQ"
REAL_ARTIST_ID = "UCtxdfwb9wfkoGocVUAJ-Bmg"
ARTIST_TOPIC_ID = "UCf_gP4AMRSgAfyzbkeS9k4g"

# VEVO channel: the right name, and no songs section at all.
VEVO_PAGE = {"name": "Travis Scott", "channelId": REAL_ARTIST_ID, "songs": {"results": []}}

REAL_ARTIST_PAGE = {
    "name": "Travis Scott",
    "channelId": REAL_ARTIST_ID,
    "songs": {
        "browseId": None,
        "results": [
            {
                "title": "FE!N",
                "videoId": "_efHZg9D9iE",
                "artists": [{"name": "Travis Scott", "id": ARTIST_TOPIC_ID}],
            }
        ],
    },
}


def test_a_vevo_channel_is_followed_through_to_the_real_artist_page(client):
    """A VEVO channel's artist page has a name but no songs, so follow through to the real artist."""
    fake = client(get_artist=lambda browse_id: VEVO_PAGE if browse_id == VEVO_ID else REAL_ARTIST_PAGE)

    profile = music.fetch_artist(VEVO_ID, all_songs=False)

    assert profile.topic_channel_id == ARTIST_TOPIC_ID
    # The redirect carries the real id; the VEVO id would reopen the songless page.
    assert profile.browse_id == REAL_ARTIST_ID
    assert [call[1][0] for call in fake.calls] == [VEVO_ID, REAL_ARTIST_ID]


def test_an_artist_page_with_songs_is_never_asked_for_twice(client):
    """Only a page with nothing to offer pays for the redirect request."""
    fake = client(get_artist=REAL_ARTIST_PAGE)

    music.fetch_artist(REAL_ARTIST_ID, all_songs=False)

    assert len(fake.calls) == 1


def test_a_page_with_no_songs_and_no_redirect_is_left_alone(client):
    """No music and nowhere to redirect: the caller still gets the page."""
    fake = client(get_artist={"name": "Nobody", "channelId": REAL_ARTIST_ID, "songs": {"results": []}})

    profile = music.fetch_artist(REAL_ARTIST_ID, all_songs=False)

    assert profile.topic_channel_id is None
    assert len(fake.calls) == 1


def test_the_topic_channel_is_matched_regardless_of_case(client):
    """Pages can be headed "USHER" while tracks credit "Usher"."""
    client(
        get_artist={
            "name": "USHER",
            "songs": {
                "results": [
                    {
                        "title": "Yeah!",
                        "videoId": "_efHZg9D9iE",
                        "artists": [{"name": "Usher", "id": ARTIST_TOPIC_ID}],
                    }
                ]
            },
        }
    )

    profile = music.fetch_artist("UCaNrhBiXsXIM2epDl_kEzgQ", all_songs=False)

    assert profile.topic_channel_id == ARTIST_TOPIC_ID


def _chart_artist(slug, title):
    """A chart entry with a real 24-character UC browse id; _artist_result drops anything else."""
    return {**CHART_ARTIST, "browseId": f"UC{slug}".ljust(24, "0"), "title": title}


def _country_charts(**by_country):
    """A get_charts stub that answers differently per country code."""

    def respond(country):
        return by_country.get(country, {"videos": [], "artists": []})

    return respond


def test_one_country_still_costs_one_request(client):
    fake = client(get_charts=_country_charts(TR={"videos": [CHART_PLAYLIST], "artists": []}))

    music.fetch_charts_for(["TR"])

    assert len(fake.calls) == 1


def test_charting_artists_are_taken_a_rank_at_a_time(client):
    """Taken a rank at a time, so the first country doesn't own every slot."""
    client(
        get_charts=_country_charts(
            TR={"videos": [], "artists": [
                _chart_artist("tr1", "TR One"),
                _chart_artist("tr2", "TR Two"),
            ]},
            US={"videos": [], "artists": [
                _chart_artist("us1", "US One"),
                _chart_artist("us2", "US Two"),
            ]},
        )
    )

    charts = music.fetch_charts_for(["TR", "US"])

    assert [a.title for a in charts.artists] == ["TR One", "US One", "TR Two", "US Two"]


def test_an_artist_charting_in_two_countries_gets_one_tile(client):
    client(
        get_charts=_country_charts(
            TR={"videos": [], "artists": [_chart_artist("shared", "Shared")]},
            US={"videos": [], "artists": [
                _chart_artist("shared", "Shared"),
                _chart_artist("us1", "US Only"),
            ]},
        )
    )

    charts = music.fetch_charts_for(["TR", "US"])

    assert [a.title for a in charts.artists] == ["Shared", "US Only"]


def test_a_country_with_a_shorter_chart_doesnt_stop_the_others(client):
    client(
        get_charts=_country_charts(
            TR={"videos": [], "artists": [_chart_artist("tr1", "TR One")]},
            US={"videos": [], "artists": [
                _chart_artist("us1", "US One"),
                _chart_artist("us2", "US Two"),
            ]},
        )
    )

    charts = music.fetch_charts_for(["TR", "US"])

    assert [a.title for a in charts.artists] == ["TR One", "US One", "US Two"]


def _country_chart_playlists(country):
    """A country's four chart playlists in the order YouTube Music returns them; Trending isn't first."""
    return [
        {**CHART_PLAYLIST, "playlistId": f"PL4fGSI1pDJnLIVE{country}".ljust(34, "0"),
         "title": f"Top 100 Live Performances - {country}"},
        {**CHART_PLAYLIST, "playlistId": f"OLAK5uy_TREND{country}".ljust(34, "0"),
         "title": f"Trending 20 {country}"},
        {**CHART_PLAYLIST, "playlistId": f"PL4fGSI1pDJnDAILY{country}".ljust(34, "0"),
         "title": f"Daily Top Music Videos - {country}"},
        {**CHART_PLAYLIST, "playlistId": f"PL4fGSI1pDJnTOP{country}".ljust(34, "0"),
         "title": f"Top 100 Music Videos {country}"},
    ]


def test_only_the_trending_playlist_survives(client):
    """The other three are video charts; Trending isn't first, so [0] won't do."""
    client(get_charts={"videos": _country_chart_playlists("Turkey"), "artists": []})

    titles = [p.title for p in music.fetch_charts("TR").playlists]

    assert titles == ["Trending 20 Turkey"]


def test_each_country_contributes_exactly_one_tile(client):
    """One tile per configured country, in order."""
    client(
        get_charts=_country_charts(
            **{
                code: {"videos": _country_chart_playlists(code), "artists": []}
                for code in ("US", "GB", "CA", "AU", "IE", "NZ")
            }
        )
    )

    titles = [
        p.title for p in music.fetch_charts_for(["US", "GB", "CA", "AU", "IE", "NZ"]).playlists
    ]

    assert titles == [
        "Trending 20 US",
        "Trending 20 GB",
        "Trending 20 CA",
        "Trending 20 AU",
        "Trending 20 IE",
        "Trending 20 NZ",
    ]


def test_the_same_chart_playlist_in_two_countries_appears_once(client):
    client(
        get_charts=_country_charts(
            TR={"videos": [CHART_PLAYLIST], "artists": []},
            US={"videos": [CHART_PLAYLIST], "artists": []},
        )
    )

    assert len(music.fetch_charts_for(["TR", "US"]).playlists) == 1


# Curated/mood playlists are almost all music videos, so the player asks for the song instead.

MUSIC_VIDEO = {
    "title": "Biliyorsun",
    "videoId": "abcdefghij1",
    "videoType": "MUSIC_VIDEO_TYPE_OMV",
    "duration_seconds": 352,
    "album": None,
    "artists": [{"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"}],
    "thumbnails": [{"url": "https://i.ytimg.com/vi/abcdefghij1/hqdefault.jpg?sqp=abc"}],
}


def test_a_music_video_resolves_to_its_song(client):
    fake = client(search=[MUSIC_VIDEO, SONG])

    result = music.find_song_version("Biliyorsun", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "_efHZg9D9iE"
    # Square album art, asked for at COVER_SIZE — the whole point of the swap.
    assert "w544-h544" in result.thumbnail_url
    (name, args, kwargs) = fake.calls[0]
    assert name == "search" and kwargs["filter"] == "songs"
    assert args[0] == "Biliyorsun Sezen Aksu"


def test_a_music_video_result_is_never_the_answer(client):
    """The first hit for a video's title is often the video itself."""
    client(search=[MUSIC_VIDEO])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is None


def test_a_different_song_is_not_close_enough(client):
    other = {**SONG, "title": "Firuze"}
    client(search=[other])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is None


def test_a_different_artist_is_not_close_enough(client):
    other = {**SONG, "artists": [{"name": "Someone Else", "id": "UCotherotherother"}]}
    client(search=[other])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is None


def test_bracketed_asides_and_punctuation_do_not_block_a_match(client):
    """The noise real song and video titles differ by, e.g. "(feat. …)", "(Official Video)"."""
    versioned = {**SONG, "title": "Biliyorsun (feat. Someone) [Remastered]"}
    client(search=[versioned])

    result = music.find_song_version("Biliyorsun!", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "_efHZg9D9iE"


def test_duration_is_deliberately_not_part_of_the_match(client):
    """A music video with a long intro is still the same track."""
    client(search=[SONG])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is not None


def test_a_track_with_no_artist_matches_on_title_alone(client):
    client(search=[SONG])

    result = music.find_song_version("Biliyorsun", None)

    assert result is not None


COLLAB_SONG = {
    **SONG,
    "title": "Biliyorsun",
    "videoId": "collabsong1",
    "artists": [
        {"name": "Sezen Aksu", "id": "UCNaGLJRPE3ohleIDM7RFtlQ"},
        {"name": "Sertab Erener", "id": "UCotherotherotherother"},
    ],
}


def test_a_collaboration_matches_on_the_lead_artist(client):
    """The row names one artist while the song credits several."""
    client(search=[COLLAB_SONG])

    result = music.find_song_version("Biliyorsun", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "collabsong1"


def test_a_credited_artist_further_down_the_list_still_counts(client):
    client(search=[COLLAB_SONG])

    assert music.find_song_version("Biliyorsun", "Sertab Erener") is not None


@pytest.mark.parametrize(
    "title",
    [
        "Biliyorsun (Live)",
        "Biliyorsun (Acoustic)",
        "Biliyorsun (Instrumental)",
        "Biliyorsun [Karaoke]",
        "Biliyorsun (Alison Wonderland Remix)",
        "Biliyorsun (Sped Up)",
    ],
)
def test_a_different_recording_is_not_the_song(client, title):
    """_match_key drops brackets, which would otherwise make an instrumental look like the song."""
    client(search=[{**SONG, "title": title}])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is None


def test_asking_for_a_live_version_still_finds_one(client):
    """A row that is itself a live take should resolve to the live song."""
    client(search=[{**SONG, "title": "Biliyorsun (Live)"}])

    assert music.find_song_version("Biliyorsun (Live in Istanbul)", "Sezen Aksu") is not None


def test_a_version_that_is_still_the_song_is_accepted(client):
    """A qualifier like "(Taylor's Version)" is still the song."""
    client(search=[{**SONG, "title": "Biliyorsun (Sertab Version)"}])

    assert music.find_song_version("Biliyorsun", "Sezen Aksu") is not None


# Chart entries carry raw uploaded video titles ("KATSEYE (캣츠아이) 'Hootie Frutti' Official MV").


def test_a_raw_video_title_still_finds_its_song(client):
    """A bare "Official MV" and a leading artist credit survive _match_key."""
    client(search=[SONG])

    result = music.find_song_version("SEZEN AKSU 'Biliyorsun' Official MV", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "_efHZg9D9iE"


def test_noise_outside_brackets_no_longer_blocks_a_match(client):
    client(search=[SONG])

    result = music.find_song_version(
        "Biliyorsun Performance Edit, HD, Closed Captioned", "Sezen Aksu"
    )

    assert result is not None


def test_a_shorter_song_inside_the_title_is_not_the_answer(client):
    """The song title must be a delimited part of the video title, or the leftovers must be decoration."""
    client(search=[{**SONG, "title": "Legends", "videoId": "wrongsong01"}])

    assert music.find_song_version("Sezen Aksu - Hood Legends (Official Video)", "Sezen Aksu") is None


def test_the_longest_qualifying_title_wins(client):
    short = {**SONG, "title": "Biliyorsun", "videoId": "shortsong01"}
    longer = {**SONG, "title": "Biliyorsun Sezen", "videoId": "longsong001"}
    client(search=[short, longer])

    result = music.find_song_version("Biliyorsun Sezen Official Video", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "longsong001"


def test_an_exact_title_beats_a_nested_one_further_down(client):
    nested = {**SONG, "title": "Biliyorsun", "videoId": "nestedsong1"}
    exact = {**SONG, "title": "Biliyorsun Official Video", "videoId": "exactsong01"}
    client(search=[nested, exact])

    result = music.find_song_version("Biliyorsun Official Video", "Sezen Aksu")

    assert result is not None
    assert result.video_id == "exactsong01"


def test_the_same_channel_under_a_different_name_is_the_same_artist(client):
    """YouTube Music gives one channel id different display names across responses."""
    client(search=[SONG])

    result = music.find_song_version("Biliyorsun", "Marie Ulven", "UCNaGLJRPE3ohleIDM7RFtlQ")

    assert result is not None
    assert result.video_id == "_efHZg9D9iE"


def test_a_label_upload_matches_the_artist_named_in_the_title(client):
    """A label upload names the label; the real artist in the title corroborates the match."""
    client(search=[SONG])

    result = music.find_song_version(
        "SEZEN AKSU (셀렌) 'Biliyorsun' Official MV", "HYBE LABELS", "UChybelabels00000000"
    )

    assert result is not None
    assert result.video_id == "_efHZg9D9iE"


def test_a_different_channel_and_name_is_still_not_the_artist(client):
    """With none of the three artist checks holding, there is no match."""
    client(search=[SONG])

    assert music.find_song_version("Biliyorsun", "Someone Else", "UCsomeoneelse0000000") is None
