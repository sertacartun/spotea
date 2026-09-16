"""Following an artist, syncing what they release, and unfollowing. Nothing here goes online."""

import json
import logging
from datetime import datetime

import pytest

import app.routers.artists as artists_router
import app.services.artist_follow as artist_follow_module
import app.services.artist_sync as artist_sync
import app.services.initial_sync as initial_sync_module
from app.models import Artist, Content, User
from app.services.artist_follow import NotAnArtistError, follow_artist
from app.services.artist_sync import ArtistFetchResult, apply_artist_data
from app.youtube.models import ChannelSearchResult, VideoSearchResult
from app.youtube.music import ArtistProfile, ArtistRelease, ReleaseDetail

USER_ID = 1

TOPIC_ID = "UCDdTH-sn8qG64wK5ChFDQ4Q"
OFFICIAL_ID = "UC5ZkRnYd3__WBBGnAnWO9Cg"


def _release(browse_id="MPREb_aaaaaaaaaaa", title="A Single", year="2026"):
    return ArtistRelease(
        browse_id=browse_id, title=title, year=year, kind="Single", cover_url=None
    )


def _artist(
    *,
    topic_channel_id=TOPIC_ID,
    browse_id=OFFICIAL_ID,
    albums=(),
    singles=(),
    monthly_listeners=None,
    related=(),
    tracks=(),
):
    return ArtistProfile(
        browse_id=browse_id,
        channel_id=OFFICIAL_ID,
        topic_channel_id=topic_channel_id,
        name="Shirin David",
        description=None,
        subscriber_count=1_000_000,
        monthly_listeners=monthly_listeners,
        avatar_url=None,
        tracks=list(tracks),
        track_count=0,
        albums=list(albums),
        singles=list(singles),
        related=list(related),
    )


def _track(video_id="trackaaaaaa", title="A Track"):
    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=None,
        duration_seconds=200,
        channel_title="Shirin David",
        channel_id=TOPIC_ID,
    )


def _stub_artist_lookup(monkeypatch, profile, calls=None):
    def fake(browse_id, all_songs=True):
        if calls is not None:
            calls.append((browse_id, all_songs))
        return profile

    monkeypatch.setattr(artist_follow_module, "fetch_artist", fake)


def test_following_a_musicians_own_channel_keys_on_their_topic_channel(db_session, monkeypatch):
    """Official and Topic channel ids must resolve to one row."""
    _stub_artist_lookup(monkeypatch, _artist())

    artist, _ = follow_artist(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert artist.channel_id == TOPIC_ID
    assert artist.browse_id == OFFICIAL_ID
    assert artist.name == "Shirin David"


def test_the_card_is_titled_with_the_artists_name(db_session, monkeypatch):
    _stub_artist_lookup(monkeypatch, _artist())

    artist, _ = follow_artist(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert artist.name == "Shirin David"


def test_a_channel_that_is_not_an_artist_cannot_be_followed(db_session, monkeypatch):
    _stub_artist_lookup(monkeypatch, None)

    with pytest.raises(NotAnArtistError):
        follow_artist(
            db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
        )

    assert db_session.query(Artist).count() == 0


def test_a_url_with_no_channel_in_it_never_reaches_youtube_music(db_session, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("must not ask YouTube Music about a URL with no channel id")

    monkeypatch.setattr(artist_follow_module, "fetch_artist", explode)

    with pytest.raises(NotAnArtistError):
        follow_artist(db_session, "https://www.youtube.com/playlist?list=PLx", USER_ID, sync=False)


def test_a_non_artist_channel_is_a_400_not_a_500(client, monkeypatch):
    monkeypatch.setattr(artist_follow_module, "fetch_artist", lambda browse_id, all_songs=True: None)

    res = client.post("/artists", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"})

    assert res.status_code == 400


def test_following_the_channel_of_an_artist_already_followed_is_a_duplicate(db_session, monkeypatch, client):
    """Artist resolution runs before the duplicate check, so both ids collide."""
    _stub_artist_lookup(monkeypatch, _artist())
    follow_artist(db_session, f"https://www.youtube.com/channel/{TOPIC_ID}", USER_ID, sync=False)

    monkeypatch.setattr(
        "app.services.artist_follow.fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    res = client.post("/artists", json={"channel_url": f"https://www.youtube.com/channel/{OFFICIAL_ID}"})

    assert res.status_code == 409
    assert db_session.query(Artist).count() == 1


def test_following_a_previously_previewed_artist_upgrades_the_placeholder(db_session, monkeypatch):
    """An Explore placeholder (followed=False) is upgraded in place, not rejected as a duplicate."""
    placeholder = Artist(user_id=USER_ID, channel_id=TOPIC_ID, followed=False)
    db_session.add(placeholder)
    db_session.commit()
    _stub_artist_lookup(monkeypatch, _artist())

    artist, _ = follow_artist(
        db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False
    )

    assert artist.id == placeholder.id
    assert artist.followed is True
    assert db_session.query(Artist).count() == 1


def test_the_track_list_is_not_paid_for_on_a_follow(db_session, monkeypatch):
    calls = []
    _stub_artist_lookup(monkeypatch, _artist(), calls=calls)

    follow_artist(db_session, f"https://www.youtube.com/channel/{OFFICIAL_ID}", USER_ID, sync=False)

    assert calls == [(OFFICIAL_ID, False)]


def _followed(db_session, **kwargs):
    defaults = {
        "user_id": USER_ID,
        "channel_id": TOPIC_ID,
        "name": "Shirin David",
        "browse_id": OFFICIAL_ID,
    }
    defaults.update(kwargs)
    artist = Artist(**defaults)
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist


def test_a_first_sync_records_the_catalogue_without_importing_it(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(singles=[_release(), _release("MPREb_bbbbbbbbbbb")]),
    )

    def explode(browse_id):
        raise AssertionError("a first sync must not open any release")

    monkeypatch.setattr(artist_sync, "fetch_release", explode)

    artist = _followed(db_session)
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    new_count = apply_artist_data(db_session, artist, result)

    assert new_count == 0
    assert db_session.query(Content).count() == 0
    # The whole release is stored: Home's "New releases" shelf renders from it without fetching.
    stored = json.loads(artist.release_snapshot)
    assert [entry["browse_id"] for entry in stored] == ["MPREb_aaaaaaaaaaa", "MPREb_bbbbbbbbbbb"]
    assert stored[0]["title"] == "A Single"
    assert stored[0]["year"] == "2026"


def test_a_first_sync_still_records_monthly_listeners(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(monthly_listeners="1.91M")
    )

    artist = _followed(db_session)
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    assert artist.monthly_listeners == "1.91M"


def test_monthly_listeners_is_refreshed_on_every_sync(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(monthly_listeners="2.4M")
    )

    artist = _followed(db_session, release_snapshot="[]")
    artist.monthly_listeners = "1.91M"
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    assert artist.monthly_listeners == "2.4M"


def _related(channel_id, title):
    return ChannelSearchResult(
        channel_id=channel_id,
        title=title,
        thumbnail_url=None,
        subscriber_count=None,
        channel_url=f"https://www.youtube.com/channel/{channel_id}",
    )


def test_a_first_sync_still_records_related_artists(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(
            related=[_related("UCrelatedaaaaaaaaaaaaaaa", "Related One")]
        ),
    )

    artist = _followed(db_session)
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    stored = json.loads(artist.related_artists)
    assert [r["title"] for r in stored] == ["Related One"]


def test_related_artists_is_refreshed_on_every_sync(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(
            related=[_related("UCrelatedbbbbbbbbbbbbbbb", "Related Two")]
        ),
    )

    artist = _followed(db_session, release_snapshot="[]")
    artist.related_artists = json.dumps([{"channel_id": "UCstale", "title": "Stale"}])
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    stored = json.loads(artist.related_artists)
    assert [r["title"] for r in stored] == ["Related Two"]


def test_an_artist_with_no_related_artists_clears_a_stale_list(db_session, monkeypatch):
    monkeypatch.setattr(artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(related=[]))

    artist = _followed(db_session, release_snapshot="[]")
    artist.related_artists = json.dumps([{"channel_id": "UCstale", "title": "Stale"}])
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    assert json.loads(artist.related_artists) == []


def test_a_first_sync_still_records_top_tracks(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(tracks=[_track("trackaaaaaa1", "Popular Song")]),
    )

    artist = _followed(db_session)
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    stored = json.loads(artist.top_tracks)
    assert [t["title"] for t in stored] == ["Popular Song"]


def test_top_tracks_is_refreshed_on_every_sync(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(tracks=[_track("trackbbbbbbb2", "New Preview")]),
    )

    artist = _followed(db_session, release_snapshot="[]")
    artist.top_tracks = json.dumps([{"video_id": "stale", "title": "Stale"}])
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    stored = json.loads(artist.top_tracks)
    assert [t["title"] for t in stored] == ["New Preview"]


def test_a_later_sync_imports_only_what_appeared_since(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(
            singles=[_release("MPREb_new00000000"), _release("MPREb_aaaaaaaaaaa")]
        ),
    )
    opened = []

    def fake_release(browse_id):
        opened.append(browse_id)
        return ReleaseDetail(
            title="New Single", year="2026", kind="Single", cover_url=None,
            artist_names="Shirin David", tracks=[_track("newtrack001")],
        )

    monkeypatch.setattr(artist_sync, "fetch_release", fake_release)

    artist = _followed(db_session, release_snapshot='["MPREb_aaaaaaaaaaa"]')
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    new_count = apply_artist_data(db_session, artist, result)

    assert opened == ["MPREb_new00000000"], "an already-known release was opened again"
    assert new_count == 1
    row = db_session.query(Content).one()
    assert row.video_id == "newtrack001"
    assert row.duration_seconds == 200, "the duration has to survive — RSS never carried one"
    assert row.published_at is not None


def test_a_release_that_will_not_open_is_retried_next_time(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(singles=[_release("MPREb_broken00000")]),
    )
    monkeypatch.setattr(artist_sync, "fetch_release", lambda browse_id: None)

    artist = _followed(db_session, release_snapshot="[]")
    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    assert artist.release_snapshot == "[]"


def test_a_track_already_in_the_library_is_not_inserted_twice(db_session, monkeypatch):
    """Content's (user_id, video_id) is unique across artists, e.g. a collaboration."""
    other = _followed(db_session, channel_id="https://example.com/other", browse_id="UCother")
    db_session.add(
        Content(artist_id=other.id, user_id=USER_ID, video_id="shared00001", title="Already here")
    )
    db_session.commit()

    monkeypatch.setattr(
        artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(singles=[_release()])
    )
    monkeypatch.setattr(
        artist_sync,
        "fetch_release",
        lambda browse_id: ReleaseDetail(
            title="Feature", year="2026", kind="Single", cover_url=None, artist_names="x",
            tracks=[_track("shared00001"), _track("brandnew001")],
        ),
    )

    artist = _followed(db_session, channel_id="UCanother0000000000000")
    result = artist_sync.fetch_artist_data(artist.browse_id, "[]", None)
    new_count = apply_artist_data(db_session, artist, result)

    assert new_count == 1
    assert db_session.query(Content).count() == 2


def test_an_unreadable_artist_page_is_a_skip_not_a_failure(db_session, monkeypatch, caplog):
    monkeypatch.setattr(artist_sync, "fetch_artist", lambda browse_id, all_songs=True: None)

    artist = _followed(db_session, release_snapshot="[]")
    with caplog.at_level(logging.WARNING):
        result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)

    assert result.ok is False
    assert apply_artist_data(db_session, artist, result) == 0
    assert "no page to read" in caplog.text


def test_refresh_isolates_one_failing_artist(db_session, monkeypatch):
    good = _followed(db_session, channel_id="https://example.com/good")
    bad = _followed(db_session, channel_id="https://example.com/bad", browse_id="UCbad")

    monkeypatch.setattr(
        artist_sync,
        "fetch_artist_data",
        lambda browse_id, snapshot, avatar_url: ArtistFetchResult(ok=True, releases=[]),
    )

    real_apply = artist_sync.apply_artist_data

    def flaky(db, artist, result):
        if artist.id == bad.id:
            raise RuntimeError("boom")
        return real_apply(db, artist, result)

    monkeypatch.setattr(artist_sync, "apply_artist_data", flaky)

    artist_sync.refresh_feeds(db_session, [bad, good])

    db_session.expire_all()
    assert db_session.get(Artist, good.id).release_snapshot == "[]", "the good artist was skipped too"


def test_a_feed_with_no_artist_behind_it_is_skipped(db_session, monkeypatch):
    placeholder = _followed(db_session, browse_id=None, followed=False)

    def explode(*args, **kwargs):
        raise AssertionError("a placeholder artist must not be synced")

    monkeypatch.setattr(artist_sync, "fetch_artist_data", explode)

    assert artist_sync.refresh_feeds(db_session, [placeholder]) == 0


def _seed_feed_with_content(db_session, **content_kwargs):
    artist = Artist(user_id=USER_ID, channel_id="https://example.com/unfollow-me", name="Unfollow Me")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    defaults = {"status": "not_downloaded"}
    defaults.update(content_kwargs)
    content = Content(
        artist_id=artist.id, user_id=USER_ID, video_id="untouched1", title="Untouched video", **defaults
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return artist, content


def test_unfollowing_an_artist_with_no_engaged_content_deletes_it_entirely(client, db_session):
    artist, _content = _seed_feed_with_content(db_session)

    res = client.delete(f"/artists/{artist.id}")

    assert res.status_code == 204
    assert db_session.query(Artist).filter(Artist.id == artist.id).first() is None
    assert db_session.query(Content).filter(Content.artist_id == artist.id).count() == 0


def test_unfollowing_keeps_downloaded_content_and_downgrades_the_feed(client, db_session):
    artist, content = _seed_feed_with_content(db_session, status="ready", file_path=None)

    res = client.delete(f"/artists/{artist.id}")
    # client's request runs on its own Session; drop db_session's cached attributes.
    db_session.expire_all()

    assert res.status_code == 204
    kept_feed = db_session.query(Artist).filter(Artist.id == artist.id).first()
    assert kept_feed is not None
    assert kept_feed.followed is False
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


def test_unfollowing_keeps_recently_played_content(client, db_session):
    artist, content = _seed_feed_with_content(db_session, last_played_at=datetime(2026, 1, 1))

    res = client.delete(f"/artists/{artist.id}")

    assert res.status_code == 204
    assert db_session.query(Artist).filter(Artist.id == artist.id).first() is not None
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


def test_unfollowing_keeps_favorited_content(client, db_session):
    artist, content = _seed_feed_with_content(db_session, is_favorite=True)

    res = client.delete(f"/artists/{artist.id}")

    assert res.status_code == 204
    assert db_session.query(Artist).filter(Artist.id == artist.id).first() is not None
    assert db_session.query(Content).filter(Content.id == content.id).first() is not None


def _stub_initial_fetch(monkeypatch, spy=None):
    def fake(browse_id, snapshot, avatar_url):
        if spy:
            spy()
        return ArtistFetchResult(ok=True, releases=[])

    monkeypatch.setattr(initial_sync_module, "fetch_artist_data", fake)


def test_a_new_feed_says_it_is_filling_in_before_it_fetches_anything(db_session, monkeypatch):
    """Otherwise the card shows "0 songs" while the fetch runs."""
    seen: list[set[int]] = []
    artist = _followed(db_session)
    initial_sync_module.mark_syncing(artist.id)
    _stub_initial_fetch(monkeypatch, spy=lambda: seen.append(initial_sync_module.syncing_artist_ids([artist.id])))

    initial_sync_module.run_initial_sync(artist.id, db_session)

    assert seen == [{artist.id}]
    initial_sync_module.sync_progress.discard(artist.id)


def test_a_failed_initial_sync_does_not_leave_the_card_stuck(db_session, monkeypatch):
    artist = _followed(db_session)

    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(initial_sync_module, "fetch_artist_data", explode)

    initial_sync_module.run_initial_sync(artist.id, db_session)

    assert initial_sync_module.sync_progress.get(artist.id)[0] == "done"
    initial_sync_module.sync_progress.discard(artist.id)


def test_adding_a_feed_answers_before_it_fetches_anything(client, monkeypatch):
    fetched: list[str] = []
    scheduled: list[int] = []

    monkeypatch.setattr(
        artist_follow_module, "fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    monkeypatch.setattr(
        artist_sync, "fetch_artist_data",
        lambda browse_id, snapshot, avatar_url: fetched.append(browse_id) or ArtistFetchResult(ok=True),
    )
    monkeypatch.setattr(artists_router, "run_initial_sync_task", lambda artist_id: scheduled.append(artist_id))

    res = client.post("/artists", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"})

    assert res.status_code == 201
    assert fetched == [], "the route fetched the catalogue before answering"
    assert scheduled == [res.json()["artist"]["id"]]


def test_the_card_is_already_filling_in_when_the_response_lands(client, monkeypatch):
    """The background task isn't guaranteed to start before the client's first fragment request."""
    monkeypatch.setattr(
        artist_follow_module, "fetch_artist", lambda browse_id, all_songs=True: _artist()
    )
    monkeypatch.setattr(artists_router, "run_initial_sync_task", lambda artist_id: None)

    artist_id = client.post(
        "/artists", json={"channel_url": f"https://www.youtube.com/channel/{TOPIC_ID}"}
    ).json()["artist"]["id"]

    try:
        assert client.get("/artists/syncing").json() == [artist_id]
    finally:
        initial_sync_module.sync_progress.discard(artist_id)


def test_backfilling_lists_only_this_users_running_syncs(client, db_session):
    mine = Artist(user_id=USER_ID, channel_id="https://example.com/mine", name="Mine")
    other_user = User(username="someone-else", password_hash="x")
    db_session.add_all([mine, other_user])
    db_session.commit()
    theirs = Artist(
        user_id=other_user.id, channel_id="https://example.com/theirs", name="Theirs"
    )
    db_session.add(theirs)
    db_session.commit()

    initial_sync_module.sync_progress.set(mine.id, ("syncing", 0, 0))
    initial_sync_module.sync_progress.set(theirs.id, ("syncing", 0, 0))
    try:
        assert client.get("/artists/syncing").json() == [mine.id]

        # A finished sync keeps its entry for a while (see progress.py): an entry is not "running".
        initial_sync_module.sync_progress.set(mine.id, ("done", 0, 0))
        assert client.get("/artists/syncing").json() == []
    finally:
        initial_sync_module.sync_progress.discard(mine.id)
        initial_sync_module.sync_progress.discard(theirs.id)


def test_library_marks_a_feed_that_is_still_being_fetched(client, db_session):
    artist = Artist(
        user_id=USER_ID, channel_id="https://example.com/preparing", name="Still Filling In"
    )
    db_session.add(artist)
    db_session.commit()

    initial_sync_module.sync_progress.set(artist.id, ("syncing", 0, 0))
    try:
        body = client.get("/partials/library").text
        assert 'data-preparing="true"' in body
        assert "Fetching releases" in body
    finally:
        initial_sync_module.sync_progress.discard(artist.id)

    body = client.get("/partials/library").text
    assert "data-preparing" not in body, "the card kept saying it was fetching after the sync ended"


def test_an_old_bare_id_snapshot_is_not_treated_as_unseen(db_session, monkeypatch):
    """The legacy bare-id snapshot must still be read, or every release looks new after upgrade."""
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(singles=[_release(), _release("MPREb_bbbbbbbbbbb")]),
    )

    def explode(browse_id):
        raise AssertionError("a release already in the snapshot must not be opened")

    monkeypatch.setattr(artist_sync, "fetch_release", explode)

    artist = _followed(db_session)
    artist.release_snapshot = '["MPREb_aaaaaaaaaaa", "MPREb_bbbbbbbbbbb"]'
    db_session.commit()

    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    new_count = apply_artist_data(db_session, artist, result)

    assert new_count == 0
    assert db_session.query(Content).count() == 0


def test_an_old_snapshot_is_rewritten_in_the_new_shape(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync, "fetch_artist", lambda browse_id, all_songs=True: _artist(singles=[_release()])
    )
    artist = _followed(db_session)
    artist.release_snapshot = '["MPREb_aaaaaaaaaaa"]'
    db_session.commit()

    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    assert json.loads(artist.release_snapshot) == [
        {
            "browse_id": "MPREb_aaaaaaaaaaa",
            "title": "A Single",
            "year": "2026",
            "kind": "Single",
            "cover_url": None,
        }
    ]


def test_snapshot_readers_survive_junk():
    from app.services.artist_sync import snapshot_release_ids, snapshot_releases

    for junk in (None, "", "not json", "{}", "[1, 2]"):
        assert snapshot_releases(junk) == []
        assert snapshot_release_ids(junk) == set()


def test_a_release_that_wont_open_stays_out_of_the_snapshot(db_session, monkeypatch):
    monkeypatch.setattr(
        artist_sync,
        "fetch_artist",
        lambda browse_id, all_songs=True: _artist(
            singles=[_release(), _release("MPREb_bbbbbbbbbbb", title="Broken")]
        ),
    )
    monkeypatch.setattr(
        artist_sync,
        "fetch_release",
        lambda browse_id: None
        if browse_id == "MPREb_bbbbbbbbbbb"
        else ReleaseDetail(
            title="A Single", year="2026", kind="Single", cover_url=None,
            artist_names="An Artist", tracks=[_track("newtrack001")],
        ),
    )

    artist = _followed(db_session)
    artist.release_snapshot = "[]"
    db_session.commit()

    result = artist_sync.fetch_artist_data(artist.browse_id, artist.release_snapshot, None)
    apply_artist_data(db_session, artist, result)

    stored = [entry["browse_id"] for entry in json.loads(artist.release_snapshot)]
    assert stored == ["MPREb_aaaaaaaaaaa"]
