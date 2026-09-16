"""Explore's remote playlist detail panel and POST /explore/tracks/batch; fetches are faked."""

from pathlib import Path

import pytest

from app.models import Artist, Content
from app.youtube.models import PlaylistDetail, VideoSearchResult

PLAYLIST_ID = "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm"
CHANNEL_ID = "UCYLY-BIq0sSOdNXGm1FPR-w"
OTHER_CHANNEL_ID = "UCR5wZcXtOUka8jTA57flzMg"


def _track(video_id, channel_id=CHANNEL_ID):
    return VideoSearchResult(
        video_id=video_id,
        title="Track",
        thumbnail_url="https://i.ytimg.com/vi/x/hq720.jpg",
        duration_seconds=241,
        channel_title="Some Channel",
        channel_id=channel_id,
    )


@pytest.fixture
def fake_playlist(monkeypatch):
    requested = []

    def fetch(playlist_id):
        requested.append(playlist_id)
        return PlaylistDetail(
            playlist_id=playlist_id,
            title="Best Of Turkish Rock",
            video_count=120,
            items=[_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb", OTHER_CHANNEL_ID)],
        )

    monkeypatch.setattr("app.services.remote_detail.fetch_playlist", fetch)
    return requested


def test_a_remote_playlist_renders_the_detail_panel(client, fake_playlist):
    res = client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}")

    assert res.status_code == 200
    # The same panel a followed channel gets, not a second kind of page.
    assert "track-list" in res.text
    assert "detail-play-all" in res.text
    assert "Best Of Turkish Rock" in res.text
    assert "First 2 of 120 tracks" in res.text
    assert fake_playlist == [PLAYLIST_ID]


def test_a_remote_row_carries_what_the_batch_endpoint_needs(client, fake_playlist):
    text = client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").text

    assert 'data-video-id="aaaaaaaaaaa"' in text
    assert f'data-channel-id="{CHANNEL_ID}"' in text
    # Nothing here has a Content row yet, so there is no /#player/{id} to point at.
    assert "/#player/" not in text
    assert "data-content-id" not in text


def test_a_remote_playlist_has_no_pagination(client, fake_playlist):
    # One flat fetch, and no cheap way to ask YouTube for "page 2".
    assert "pagination" not in client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").text


def test_browsing_stores_nothing(client, db_session, fake_playlist):
    client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}")

    # Rows appear only when playback starts (POST /explore/tracks/batch).
    assert db_session.query(Artist).count() == 0
    assert db_session.query(Content).count() == 0


@pytest.mark.parametrize("playlist_id", ["short", "dQw4w9WgXcQ", "has spaces!"])
def test_a_non_playlist_id_is_rejected_without_being_fetched(client, fake_playlist, playlist_id):
    assert client.get(f"/partials/detail/yt-playlist/{playlist_id}").status_code == 404
    assert fake_playlist == []


def test_an_unreadable_playlist_is_a_404(client, monkeypatch):
    # fetch_playlist flattens every failure into an empty result.
    monkeypatch.setattr(
        "app.services.remote_detail.fetch_playlist",
        lambda playlist_id: PlaylistDetail(
            playlist_id=playlist_id, title=None, video_count=None, items=[]
        ),
    )

    assert client.get(f"/partials/detail/yt-playlist/{PLAYLIST_ID}").status_code == 404


def test_remote_detail_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    paths = (f"/partials/detail/yt-playlist/{PLAYLIST_ID}",)
    with TestClient(app) as anonymous:
        for path in paths:
            assert anonymous.get(path, follow_redirects=False).status_code == 303


def _batch_item(video_id, channel_id=CHANNEL_ID):
    return {
        "video_id": video_id,
        "channel_id": channel_id,
        "title": f"Track {video_id}",
        "thumbnail_url": "https://i.ytimg.com/vi/x/hq720.jpg",
        "duration_seconds": 200,
        "channel_title": "Some Channel",
    }


def test_a_tracks_credit_is_kept_on_the_track_not_on_its_artist(client, db_session):
    """Storing a collaboration's credit on the shared artist row renamed the artist for all its tracks."""
    featured = {**_batch_item("aaaaaaaaaaa"), "artist_credit": "Baby Keem, Kendrick Lamar"}
    solo = _batch_item("bbbbbbbbbbb")

    res = client.post("/explore/tracks/batch", json={"items": [featured, solo]})
    assert res.status_code == 201

    rows = {content.video_id: content for content in db_session.query(Content)}
    assert rows["aaaaaaaaaaa"].artist_credit == "Baby Keem, Kendrick Lamar"
    # No credit of its own, so the artist row answers for it.
    assert rows["bbbbbbbbbbb"].artist_credit is None

    artist = db_session.query(Artist).filter(Artist.channel_id == CHANNEL_ID).one()
    assert artist.name == "Some Channel"


def test_a_credit_is_what_a_track_displays_and_the_artist_name_is_the_fallback(client, db_session):
    """The rule lives on Content.display_artist."""
    client.post(
        "/explore/tracks/batch",
        json={
            "items": [
                {**_batch_item("aaaaaaaaaaa"), "artist_credit": "Baby Keem, Kendrick Lamar"},
                _batch_item("bbbbbbbbbbb"),
            ]
        },
    )

    rows = {content.video_id: content for content in db_session.query(Content)}
    assert rows["aaaaaaaaaaa"].display_artist == "Baby Keem, Kendrick Lamar"
    assert rows["bbbbbbbbbbb"].display_artist == "Some Channel"

    payload = client.get(f"/content/{rows['aaaaaaaaaaa'].id}").json()
    assert payload["channel_title"] == "Baby Keem, Kendrick Lamar"
    payload = client.get(f"/content/{rows['bbbbbbbbbbb'].id}").json()
    assert payload["channel_title"] == "Some Channel"


def test_batch_creates_preview_rows_in_the_order_given(client, db_session):
    res = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", OTHER_CHANNEL_ID)]},
    )

    assert res.status_code == 201
    ids = res.json()["content_ids"]
    rows = {content.id: content for content in db_session.query(Content)}
    # Order in equals order out — the caller uses it directly as the queue.
    assert [rows[i].video_id for i in ids] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert all(rows[i].is_preview for i in ids)


def test_batch_attaches_each_row_to_its_own_placeholder_feed(client, db_session):
    client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", OTHER_CHANNEL_ID)]},
    )

    artists = db_session.query(Artist).all()
    assert len(artists) == 2
    assert all(artist.followed is False for artist in artists)
    assert {artist.channel_id for artist in artists} == {CHANNEL_ID, OTHER_CHANNEL_ID}


def test_batch_reuses_one_placeholder_feed_across_a_channels_tracks(client, db_session):
    client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    )

    assert db_session.query(Artist).count() == 1


def test_batch_reuses_rows_that_already_exist(client, db_session):
    first = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()
    second = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    ).json()

    assert second["content_ids"][0] == first["content_ids"][0]
    assert db_session.query(Content).count() == 2


def test_batch_handles_the_same_video_listed_twice(client, db_session):
    # Playlists contain duplicates; the unique (user_id, video_id) forces one row.
    ids = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("aaaaaaaaaaa")]},
    ).json()["content_ids"]

    assert ids[0] == ids[1]
    assert db_session.query(Content).count() == 1


def test_batch_makes_no_network_calls(client):
    """Every field needed came back with the listing, so one synchronous request covers fifty tracks."""
    source = Path("app/routers/explore.py").read_text()
    network_imports = [
        line
        for line in source.splitlines()
        if line.startswith(("from app.youtube.music", "from app.youtube.extract"))
    ]
    assert network_imports == ["from app.youtube.music import search_artists, search_songs"], (
        "the add routes reach YouTube again — every field they need already "
        f"came back with the listing. Found: {network_imports}"
    )

    res = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]})
    assert res.status_code == 201


def test_batch_drops_items_with_an_unusable_channel_id(client, db_session):
    ids = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb", "nonsense")]},
    ).json()["content_ids"]

    assert len(ids) == 1
    assert db_session.query(Content).count() == 1


def test_batch_with_nothing_usable_is_a_400(client):
    res = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa", "nope")]})
    assert res.status_code == 400


def test_batch_leaves_a_followed_channels_own_content_alone(client, db_session):
    artist = Artist(user_id=1, channel_id=CHANNEL_ID, name="Duman")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    existing = Content(artist_id=artist.id, user_id=1, video_id="aaaaaaaaaaa", title="Already here")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)
    existing_id = existing.id

    ids = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}).json()[
        "content_ids"
    ]

    db_session.expire_all()
    assert ids == [existing_id]
    # Reused, not turned back into a preview, re-titled, or duplicated.
    reused = db_session.get(Content, existing_id)
    assert reused.is_preview is False
    assert reused.title == "Already here"
    assert db_session.query(Artist).count() == 1


def test_batch_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.post(
            "/explore/tracks/batch",
            json={"items": [_batch_item("aaaaaaaaaaa")]},
            follow_redirects=False,
        )
        assert res.status_code == 303


RELEASE_ID = "MPREb_aaaaaaaaaaa"


@pytest.fixture
def fake_release(monkeypatch):
    """Installs a release; `tracks` decides which of the route's two answers comes back."""
    from app.youtube.music import ReleaseDetail

    holder = {"tracks": [_track("aaaaaaaaaaa")]}
    calls = []

    def fetch(browse_id):
        calls.append(browse_id)
        return ReleaseDetail(
            title="Miss Jamaica",
            year="2026",
            kind="Single",
            cover_url="https://lh3.googleusercontent.com/cover",
            artist_names="Jimmy Cliff",
            tracks=holder["tracks"],
        )

    monkeypatch.setattr("app.services.remote_detail.fetch_release", fetch)
    return holder, calls


def test_a_one_track_release_answers_with_the_track_instead_of_a_panel(client, fake_release):
    holder, _ = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa")]

    res = client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/json")
    # Keyed like _remote_track_row.html's dataset, so playRemoteVideo takes it unchanged.
    assert res.json() == {
        "videoId": "aaaaaaaaaaa",
        "title": "Track",
        "channelId": CHANNEL_ID,
        "thumbnailUrl": "https://i.ytimg.com/vi/x/hq720.jpg",
        "durationSeconds": "241",
        "channelTitle": "Some Channel",
    }


def test_a_two_track_release_still_opens_the_panel(client, fake_release):
    """"Single" is a label, not a track count; plenty of Singles have two or three tracks."""
    holder, _ = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa"), _track("bbbbbbbbbbb")]

    res = client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert "track-list" in res.text
    assert "Miss Jamaica" in res.text


def test_deciding_costs_exactly_one_fetch(client, fake_release):
    """The track count and video id arrive together, so there is no probe-then-open."""
    holder, calls = fake_release
    holder["tracks"] = [_track("aaaaaaaaaaa")]

    client.get(f"/partials/detail/yt-release/{RELEASE_ID}")

    assert calls == [RELEASE_ID]


def test_a_release_id_is_validated_before_anything_is_fetched(client, fake_release):
    _, calls = fake_release

    res = client.get("/partials/detail/yt-release/not-a-release-id")

    assert res.status_code == 404
    assert calls == []


def test_a_second_batch_racing_the_first_reuses_its_rows(client, db_session, monkeypatch):
    """The competing rows land between this request's read and its insert: the real race window."""
    from app.routers import explore as explore_router

    real_insert = explore_router._insert_batch
    calls = []

    def racing_insert(db, user_id, items):
        calls.append(1)
        if len(calls) == 1:
            from sqlalchemy.exc import IntegrityError

            from app.database import SessionLocal

            other = SessionLocal()
            try:
                other.add(
                    Artist(user_id=user_id, channel_id=CHANNEL_ID, name="Some Channel", followed=False)
                )
                other.commit()
            finally:
                other.close()
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed: artists"))
        return real_insert(db, user_id, items)

    monkeypatch.setattr(explore_router, "_insert_batch", racing_insert)

    res = client.post(
        "/explore/tracks/batch",
        json={"items": [_batch_item("aaaaaaaaaaa"), _batch_item("bbbbbbbbbbb")]},
    )

    assert res.status_code == 201
    assert len(calls) == 2, "the first attempt has to have collided and been retried"
    # One artist, not two — the retry found the one the other request made.
    assert db_session.query(Artist).filter(Artist.channel_id == CHANNEL_ID).count() == 1
    assert len(res.json()["content_ids"]) == 2


def test_a_batch_that_keeps_colliding_answers_409_not_500(client, monkeypatch):
    """A 500's body is plain text, so the client can't show a `detail`."""
    from sqlalchemy.exc import IntegrityError

    from app.routers import explore as explore_router

    def always_collide(db, user_id, items):
        raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(explore_router, "_insert_batch", always_collide)

    res = client.post("/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]})

    assert res.status_code == 409
    assert "detail" in res.json(), "the client reads data.detail — a 500 gives it nothing"


def _swap(db_session, content, song_video_id="songvideo11"):
    """What POST /content/{id}/song-version does to a row."""
    from app.models import SwappedVideo

    db_session.add(SwappedVideo(user_id=1, video_id=content.video_id, content_id=content.id))
    content.video_id = song_video_id
    db_session.commit()


def test_a_swapped_row_is_found_by_the_id_the_playlist_still_shows(client, db_session):
    ids = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}
    ).json()["content_ids"]
    row = db_session.get(Content, ids[0])
    _swap(db_session, row)

    again = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}
    ).json()["content_ids"]

    assert again == ids, "the same row, not a second one"
    assert db_session.query(Content).count() == 1


def test_a_swapped_row_is_found_by_the_single_add_too(client, db_session):
    """Explore's own search results name the video as well."""
    from app.models import Artist

    artist = Artist(user_id=1, channel_id=CHANNEL_ID, name="Some Channel", followed=False)
    db_session.add(artist)
    db_session.commit()
    row = Content(artist_id=artist.id, user_id=1, video_id="aaaaaaaaaaa", title="T", is_preview=True)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    _swap(db_session, row)

    res = client.post(
        "/explore/tracks",
        json={
            "video_id": "aaaaaaaaaaa",
            "channel_id": CHANNEL_ID,
            "title": "T",
            "thumbnail_url": None,
            "duration_seconds": 100,
            "channel_title": "Some Channel",
        },
    )

    assert res.json()["content_id"] == row.id
    assert db_session.query(Content).count() == 1


def test_deleting_the_row_takes_its_mapping_with_it(client, db_session):
    """ON DELETE CASCADE only fires because app/database.py enables SQLite foreign keys."""
    from app.models import SwappedVideo

    ids = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}
    ).json()["content_ids"]
    row = db_session.get(Content, ids[0])
    _swap(db_session, row)
    assert db_session.query(SwappedVideo).count() == 1

    db_session.delete(row)
    db_session.commit()

    assert db_session.query(SwappedVideo).count() == 0

    # And the next start builds a fresh row rather than resolving to nothing.
    again = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}
    ).json()["content_ids"]
    assert db_session.get(Content, again[0]).video_id == "aaaaaaaaaaa"


def test_another_users_swap_is_not_visible(client, db_session):
    from app.models import SwappedVideo

    ids = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("aaaaaaaaaaa")]}
    ).json()["content_ids"]
    row = db_session.get(Content, ids[0])
    # The mapping exists, but it belongs to somebody else.
    from app.auth import hash_password
    from app.models import User

    other = User(username="other", password_hash=hash_password("x"))
    db_session.add(other)
    db_session.commit()
    db_session.add(SwappedVideo(user_id=other.id, video_id="bbbbbbbbbbb", content_id=row.id))
    db_session.commit()

    again = client.post(
        "/explore/tracks/batch", json={"items": [_batch_item("bbbbbbbbbbb")]}
    ).json()["content_ids"]

    assert again[0] != row.id, "another user's mapping must not resolve here"
    assert db_session.get(Content, again[0]).video_id == "bbbbbbbbbbb"
