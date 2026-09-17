"""Explore's interest-based recommendations; every YouTube search is monkeypatched out."""

import json
from datetime import timedelta

import pytest

from app.models import Artist, Content, RecommendationCache, User
from app.services import recommendations as rec
from app.timeutil import is_stale, last_refresh_boundary, utcnow
from app.youtube.models import ChannelSearchResult, PlaylistSearchResult

USER_ID = 1


@pytest.fixture(autouse=True)
def _reset_interests(db_session):
    """The default profile persists between tests, so its interests are cleared by hand."""
    yield
    profile = db_session.get(User, USER_ID)
    profile.interests = None
    db_session.commit()


def _channel(channel_id):
    return ChannelSearchResult(
        channel_id=channel_id,
        title=f"Channel {channel_id}",
        thumbnail_url=None,
        subscriber_count=10,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
    )


def _playlist(playlist_id):
    return PlaylistSearchResult(
        playlist_id=playlist_id,
        title=f"Playlist {playlist_id}",
        thumbnail_url=None,
        channel_title="Ch",
    )


@pytest.fixture(autouse=True)
def _no_browse_shelves(monkeypatch):
    """Charts and mood categories are fetched regardless of interests, so they're stubbed for every test."""
    monkeypatch.setattr(rec, "_BROWSE_BUILDERS", ())


@pytest.fixture
def fake_browse(monkeypatch):
    def install(*, charts=(), chart_artists=(), moods=()):
        monkeypatch.setattr(
            rec,
            "_BROWSE_BUILDERS",
            (
                lambda: {
                    "charts": [_playlist(p).__dict__ for p in charts],
                    "chart_artists": [_channel(c).__dict__ for c in chart_artists],
                },
                lambda: {"moods": list(moods)},
            ),
        )

    return install


@pytest.fixture
def fake_search(monkeypatch):
    calls = []

    def make(kind, factory):
        def search(query):
            calls.append((kind, query))
            return [factory(f"{query}-{i}") for i in range(3)]

        return search

    searchers = {
        "playlists": make("playlists", _playlist),
    }
    monkeypatch.setattr(rec, "_SEARCHERS", searchers)
    return calls


def _set_interests(db_session, *interests):
    profile = db_session.get(User, USER_ID)
    profile.interests = "\n".join(interests)
    db_session.commit()
    return profile


def test_no_interests_means_no_interest_searches(client, db_session, fake_search):
    body = client.get("/recommendations").json()

    assert body["interests"] == []
    assert body["interests_used"] == []
    assert (body["videos"], body["playlists"]) == ([], [])
    assert fake_search == []


def test_a_profile_with_no_interests_still_gets_the_charts(client, db_session, fake_browse):
    fake_browse(
        charts=["top-40"],
        chart_artists=["UCchart"],
        moods=[{"title": "Chill", "params": "abc123", "section": "Moods & moments", "slug": "chill"}],
    )

    body = client.get("/recommendations").json()

    assert [p["playlist_id"] for p in body["charts"]] == ["top-40"]
    assert [c["channel_id"] for c in body["chart_artists"]] == ["UCchart"]
    assert [m["title"] for m in body["moods"]] == ["Chill"]
    assert body["generated_at"] is not None


def test_mood_categories_lists_every_one_not_just_a_sample(monkeypatch):
    from app.youtube.music import MoodCategory

    categories = [MoodCategory(title=f"Mood {i}", params=f"p{i}", section="Moods & moments", slug=f"mood-{i}") for i in range(14)]
    monkeypatch.setattr(rec, "fetch_mood_categories", lambda: categories)

    result = rec._mood_categories()

    assert [m["title"] for m in result["moods"]] == [c.title for c in categories]
    assert all("params" in m and "slug" in m and "section" in m for m in result["moods"])


def test_a_charting_artist_already_followed_is_dropped(client, db_session, fake_browse):
    fake_browse(chart_artists=["UCfollowed", "UCnew"])
    db_session.add(
        Artist(
            user_id=USER_ID,
            channel_id="UCfollowed",
            name="Already Followed",
            followed=True,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [c["channel_id"] for c in body["chart_artists"]] == ["UCnew"]


def _related_dict(channel_id, title):
    """A related-artist entry with every field RecommendationsOut requires."""
    return {
        "channel_id": channel_id,
        "title": title,
        "thumbnail_url": None,
        "subscriber_count": None,
        "channel_url": f"https://www.youtube.com/channel/{channel_id}",
    }


def _followed_with_related(db_session, channel_id, *related):
    artist = Artist(
        user_id=USER_ID,
        channel_id=channel_id,
        name=f"Followed {channel_id}",
        followed=True,
        related_artists=json.dumps(list(related)),
    )
    db_session.add(artist)
    db_session.commit()
    return artist


def test_similar_artists_is_empty_with_nothing_followed(client, db_session, fake_browse):
    fake_browse()

    body = client.get("/recommendations").json()

    assert body["similar_artists"] == []


def test_similar_artists_merges_across_followed_artists(client, db_session, fake_browse):
    fake_browse()
    _followed_with_related(db_session, "UCfollowed1", _related_dict("UCsimilar1", "Similar One"))
    _followed_with_related(db_session, "UCfollowed2", _related_dict("UCsimilar2", "Similar Two"))

    body = client.get("/recommendations").json()

    assert {a["channel_id"] for a in body["similar_artists"]} == {"UCsimilar1", "UCsimilar2"}


def test_similar_artists_deduplicates_a_shared_recommendation(client, db_session, fake_browse):
    fake_browse()
    _followed_with_related(db_session, "UCfollowed1", _related_dict("UCshared", "Shared"))
    _followed_with_related(db_session, "UCfollowed2", _related_dict("UCshared", "Shared"))

    body = client.get("/recommendations").json()

    assert [a["channel_id"] for a in body["similar_artists"]] == ["UCshared"]


def test_similar_artists_excludes_one_already_followed(client, db_session, fake_browse):
    fake_browse()
    db_session.add(Artist(user_id=USER_ID, channel_id="UCalreadyfollowed", name="Already", followed=True))
    _followed_with_related(db_session, "UCfollowed1", _related_dict("UCalreadyfollowed", "Already"))

    body = client.get("/recommendations").json()

    assert body["similar_artists"] == []


def test_similar_artists_ignores_a_malformed_stored_list(client, db_session, fake_browse):
    fake_browse()
    artist = Artist(user_id=USER_ID, channel_id="UCbad", name="Bad", followed=True)
    artist.related_artists = "not json"
    db_session.add(artist)
    db_session.commit()

    body = client.get("/recommendations").json()

    assert body["similar_artists"] == []


def _track_dict(video_id, title):
    """A top-track entry with every field RecommendationsOut requires."""
    return {
        "video_id": video_id,
        "title": title,
        "thumbnail_url": None,
        "duration_seconds": 200,
        "channel_title": "An Artist",
        "channel_id": "UCartist",
    }


def _followed_with_tracks(db_session, channel_id, *tracks):
    artist = Artist(
        user_id=USER_ID,
        channel_id=channel_id,
        name=f"Followed {channel_id}",
        followed=True,
        top_tracks=json.dumps(list(tracks)),
    )
    db_session.add(artist)
    db_session.commit()
    return artist


def test_songs_is_empty_with_nothing_followed(client, db_session, fake_browse):
    fake_browse()

    body = client.get("/recommendations").json()

    assert body["videos"] == []


def test_songs_merges_across_followed_artists(client, db_session, fake_browse):
    fake_browse()
    _followed_with_tracks(db_session, "UCfollowed1", _track_dict("videoaaaaaa", "Song One"))
    _followed_with_tracks(db_session, "UCfollowed2", _track_dict("videobbbbbb", "Song Two"))

    body = client.get("/recommendations").json()

    assert {v["video_id"] for v in body["videos"]} == {"videoaaaaaa", "videobbbbbb"}


def test_a_video_already_in_the_library_is_dropped_from_the_batch(client, db_session, fake_browse):
    fake_browse()
    artist = _followed_with_tracks(
        db_session,
        "UCfollowed1",
        _track_dict("alreadyowned", "Already Have This"),
        _track_dict("videocccccc", "Not Owned Yet"),
    )
    db_session.add(
        Content(artist_id=artist.id, user_id=USER_ID, video_id="alreadyowned", title="Already Have This")
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [v["video_id"] for v in body["videos"]] == ["videocccccc"]


def test_an_unfollowed_placeholder_artist_does_not_hide_a_chart_artist(client, db_session, fake_browse):
    """followed=False is an Explore placeholder, not a real subscription."""
    fake_browse(chart_artists=["UCchart"])
    db_session.add(
        Artist(user_id=USER_ID, channel_id="UCchart", name="Just A Preview", followed=False)
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert [c["channel_id"] for c in body["chart_artists"]] == ["UCchart"]


def test_the_library_filter_is_reapplied_on_every_read_even_from_cache(
    client, db_session, fake_search, fake_browse
):
    """The library filter is not baked into the cached payload."""
    fake_browse(chart_artists=["UCchart"])
    client.get("/recommendations")
    fake_search.clear()

    db_session.add(
        Artist(
            user_id=USER_ID,
            channel_id="UCchart",
            name="Followed After The Batch Was Built",
            followed=True,
        )
    )
    db_session.commit()

    body = client.get("/recommendations").json()

    assert fake_search == []  # still served from cache, not rebuilt
    assert "UCchart" not in [c["channel_id"] for c in body["chart_artists"]]


def test_a_first_request_builds_a_batch_from_the_interests(client, db_session, fake_search):
    _set_interests(db_session, "jazz")

    body = client.get("/recommendations").json()

    assert body["interests"] == ["jazz"]
    assert body["interests_used"] == ["jazz"]
    assert body["generated_at"] is not None
    assert [p["playlist_id"] for p in body["playlists"]] == ["jazz-0", "jazz-1", "jazz-2"]
    assert sorted(fake_search) == [("playlists", "jazz")]


def test_a_second_request_is_served_from_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz")

    first = client.get("/recommendations").json()
    fake_search.clear()
    second = client.get("/recommendations").json()

    assert second == first
    assert fake_search == []


def test_editing_the_interests_invalidates_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    client.put("/settings", json={"interests": ["funk"]})
    body = client.get("/recommendations").json()

    assert body["interests_used"] == ["funk"]
    assert [q for _, q in fake_search] == ["funk"]


def test_reordering_the_interests_does_not_invalidate_the_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz", "funk")
    client.get("/recommendations")
    fake_search.clear()

    client.put("/settings", json={"interests": ["funk", "jazz"]})
    client.get("/recommendations")

    assert fake_search == []


def _age_batch(db_session, generated_at):
    cache = db_session.get(RecommendationCache, USER_ID)
    cache.generated_at = generated_at
    db_session.commit()


def test_a_batch_built_in_this_window_is_served_from_cache(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    _age_batch(db_session, last_refresh_boundary() + timedelta(seconds=1))

    client.get("/recommendations")
    assert fake_search == []


def test_a_batch_from_before_the_last_boundary_is_rebuilt(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    _age_batch(db_session, last_refresh_boundary() - timedelta(seconds=1))

    client.get("/recommendations")
    assert [q for _, q in fake_search] == ["jazz"]
    db_session.expire_all()
    assert not is_stale(db_session.get(RecommendationCache, USER_ID).generated_at)


def test_an_empty_rebuild_keeps_the_previous_batch_until_the_next_boundary(
    client, db_session, fake_search, monkeypatch, caplog
):
    """Searches flatten YouTube failures to empty lists; an outage must not blank Explore."""
    _set_interests(db_session, "jazz")
    before = client.get("/recommendations").json()["playlists"]
    assert before

    _age_batch(db_session, last_refresh_boundary() - timedelta(seconds=1))
    monkeypatch.setattr(rec, "_SEARCHERS", {"playlists": lambda query: []})

    assert client.get("/recommendations").json()["playlists"] == before
    assert "keeping the previous batch" in caplog.text
    # Stamped, so the next request doesn't hit YouTube again inside the same window.
    db_session.expire_all()
    assert not is_stale(db_session.get(RecommendationCache, USER_ID).generated_at)


def test_an_empty_rebuild_for_new_interests_does_not_bring_back_the_old_ones(
    client, db_session, fake_search, monkeypatch
):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")

    monkeypatch.setattr(rec, "_SEARCHERS", {"playlists": lambda query: []})
    client.put("/settings", json={"interests": ["funk"]})

    body = client.get("/recommendations").json()
    assert body["playlists"] == []
    assert body["interests_used"] == ["funk"]


def test_an_old_batch_is_still_rebuilt_when_the_interests_change(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    cache = db_session.get(RecommendationCache, USER_ID)
    cache.generated_at = utcnow() - timedelta(days=365)
    db_session.commit()

    client.put("/settings", json={"interests": ["funk"]})
    client.get("/recommendations")

    assert fake_search != []


def test_only_a_sample_of_a_long_interest_list_is_searched(client, db_session, fake_search):
    _set_interests(db_session, *[f"tag{i}" for i in range(10)])

    body = client.get("/recommendations").json()

    assert len(body["interests_used"]) == rec.INTERESTS_PER_RUN
    assert set(body["interests_used"]) <= {f"tag{i}" for i in range(10)}
    # One search per sampled interest, no more.
    assert len(fake_search) == rec.INTERESTS_PER_RUN
    # The full list is still reported so Explore can say what it's working from.
    assert len(body["interests"]) == 10


def test_results_from_several_interests_are_interleaved(client, db_session, fake_search):
    _set_interests(db_session, "a", "b")

    playlists = [p["playlist_id"] for p in client.get("/recommendations").json()["playlists"]]

    # Round-robin, so the front of the shelf represents both interests.
    assert playlists[:4] == ["a-0", "b-0", "a-1", "b-1"]


def test_a_result_two_interests_share_is_only_listed_once(client, db_session, monkeypatch):
    monkeypatch.setattr(
        rec,
        "_SEARCHERS",
        {
            "playlists": lambda query: [_playlist("shared"), _playlist(f"{query}-own")],
        },
    )
    _set_interests(db_session, "a", "b")

    playlists = [p["playlist_id"] for p in client.get("/recommendations").json()["playlists"]]

    assert playlists == ["shared", "a-own", "b-own"]


def test_each_shelf_is_capped(client, db_session, monkeypatch):
    many = [_playlist(f"p{i}") for i in range(rec.RESULTS_PER_SHELF + 20)]
    monkeypatch.setattr(rec, "_SEARCHERS", {"playlists": lambda query: many})
    _set_interests(db_session, "jazz")

    assert len(client.get("/recommendations").json()["playlists"]) == rec.RESULTS_PER_SHELF


def test_a_failing_search_does_not_sink_the_batch(client, db_session, monkeypatch):
    # search_* flatten yt-dlp failures to an empty list; that must mean an empty shelf, not an error.
    monkeypatch.setattr(rec, "_SEARCHERS", {"playlists": lambda query: []})
    _set_interests(db_session, "jazz")

    body = client.get("/recommendations").json()

    assert body["playlists"] == []
    assert body["interests_used"] == ["jazz"]


def test_a_corrupt_cached_payload_is_treated_as_a_miss(client, db_session, fake_search):
    _set_interests(db_session, "jazz")
    client.get("/recommendations")
    fake_search.clear()

    cache = db_session.get(RecommendationCache, USER_ID)
    cache.payload = "{not json"
    db_session.commit()

    assert client.get("/recommendations").json()["interests_used"] == ["jazz"]
    assert fake_search != []


def test_recommendation_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/recommendations", follow_redirects=False).status_code == 303


def test_similar_artists_interleaves_instead_of_draining_one_artist(client, db_session, fake_browse):
    fake_browse()
    _followed_with_related(
        db_session, "UCfirst", *[_related_dict(f"UCfirst{i}", f"First {i}") for i in range(20)]
    )
    _followed_with_related(
        db_session, "UCsecond", *[_related_dict(f"UCsecond{i}", f"Second {i}") for i in range(20)]
    )

    body = client.get("/recommendations").json()
    ids = [a["channel_id"] for a in body["similar_artists"]]

    assert ids, "the shelf came back empty"
    assert any(i.startswith("UCsecond") for i in ids), "the second artist never got a slot"
    # Strictly alternating, because both lists are longer than the shelf.
    assert [i.startswith("UCfirst") for i in ids] == [i % 2 == 0 for i in range(len(ids))]


def test_the_songs_shelf_interleaves_too(client, db_session, fake_browse):
    fake_browse()
    _followed_with_tracks(
        db_session, "UCfirst", *[_track_dict(f"firstvid{i:03d}", f"First {i}") for i in range(20)]
    )
    _followed_with_tracks(
        db_session, "UCsecond", *[_track_dict(f"secondvid{i:02d}", f"Second {i}") for i in range(20)]
    )

    body = client.get("/recommendations").json()
    titles = [v["title"] for v in body["videos"]]

    assert titles
    assert any(t.startswith("Second") for t in titles), "the second artist never got a slot"


def test_a_single_followed_artist_still_fills_the_shelf(client, db_session, fake_browse):
    fake_browse()
    _followed_with_related(
        db_session, "UConly", *[_related_dict(f"UConly{i}", f"Only {i}") for i in range(20)]
    )

    body = client.get("/recommendations").json()

    assert len(body["similar_artists"]) == 12
