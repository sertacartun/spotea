"""Explore's search box: each half asks YouTube Music for its own kind only."""

import pytest

from app.routers import explore as explore_router
from app.youtube.models import ChannelSearchResult, VideoSearchResult

SONG = VideoSearchResult(
    video_id="_efHZg9D9iE",
    title="Biliyorsun",
    thumbnail_url="https://yt3.ggpht.com/abc=w544-h544-l90-rj",
    duration_seconds=317,
    channel_title="Sezen Aksu",
    channel_id="UCNaGLJRPE3ohleIDM7RFtlQ",
)

ARTIST = ChannelSearchResult(
    channel_id="UCQm-Fc8TAF3c1hJffBPjxgw",
    title="Tarkan",
    thumbnail_url="/image-proxy?u=x",
    # None, as YouTube Music's artist search actually answers.
    subscriber_count=None,
    channel_url="https://www.youtube.com/channel/UCQm-Fc8TAF3c1hJffBPjxgw",
)


@pytest.fixture
def sources(monkeypatch):
    calls: list[str] = []

    def install(*, songs=(), artists=()):
        def record(name, results):
            def search(query):
                calls.append(name)
                return list(results)

            return search

        monkeypatch.setattr(explore_router, "search_songs", record("songs", songs))
        monkeypatch.setattr(explore_router, "search_artists", record("artists", artists))
        return calls

    return install


def test_songs_come_from_youtube_music(client, sources):
    calls = sources(songs=[SONG], artists=[ARTIST])

    res = client.get("/explore/songs", params={"q": "sezen aksu"})

    assert res.status_code == 200
    assert [row["video_id"] for row in res.json()] == ["_efHZg9D9iE"]
    assert calls == ["songs"]


def test_a_song_row_carries_what_playing_it_needs(client, sources):
    """The row is handed straight back to POST /explore/tracks."""
    sources(songs=[SONG])

    row = client.get("/explore/songs", params={"q": "biliyorsun"}).json()[0]

    assert row["channel_id"] == "UCNaGLJRPE3ohleIDM7RFtlQ"
    assert row["channel_title"] == "Sezen Aksu"
    assert row["duration_seconds"] == 317


def test_a_query_with_no_songs_is_an_empty_list(client, sources):
    calls = sources(songs=[])

    res = client.get("/explore/songs", params={"q": "keynote how we built it"})

    assert res.status_code == 200
    assert res.json() == []
    assert calls == ["songs"]


def test_artist_search_never_reaches_for_the_song_index(client, sources):
    calls = sources(songs=[SONG], artists=[ARTIST])

    res = client.get("/explore/artists", params={"q": "tarkan"})

    assert [row["channel_id"] for row in res.json()] == ["UCQm-Fc8TAF3c1hJffBPjxgw"]
    assert calls == ["artists"]


def test_an_artist_row_survives_a_missing_subscriber_count(client, sources):
    sources(artists=[ARTIST])

    row = client.get("/explore/artists", params={"q": "tarkan"}).json()[0]

    assert row["title"] == "Tarkan"
    assert row["subscriber_count"] is None


@pytest.mark.parametrize("query", ["", "   "])
@pytest.mark.parametrize("path", ["/explore/songs", "/explore/artists"])
def test_an_empty_query_searches_nothing(client, sources, path, query):
    calls = sources(songs=[SONG], artists=[ARTIST])

    res = client.get(path, params={"q": query})

    assert res.json() == []
    assert calls == []
