"""Hand-made playlists: the API, and cleanup of rows orphaned underneath them."""

from app.models import Artist, Content, Playlist, PlaylistItem, User
from app.page_context import user_playlist_detail_context, user_playlist_ids
from app.storage import purge_content

USER_ID = 1
OTHER_USER_ID = 2


def _artist(db_session, user_id=USER_ID):
    artist = (
        db_session.query(Artist).filter(Artist.user_id == user_id).first()
        or Artist(user_id=user_id, channel_id=f"https://example.com/a{user_id}", name="A")
    )
    if artist.id is None:
        db_session.add(artist)
        db_session.commit()
        db_session.refresh(artist)
    return artist


def _track(db_session, video_id, *, user_id=USER_ID):
    content = Content(
        artist_id=_artist(db_session, user_id).id,
        user_id=user_id,
        video_id=video_id,
        title=f"Track {video_id}",
        status="ready",
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return content


def _second_user(db_session):
    user = db_session.query(User).filter(User.id == OTHER_USER_ID).one_or_none()
    if user is None:
        user = User(id=OTHER_USER_ID, username="other", password_hash="x")
        db_session.add(user)
        db_session.commit()
    return user


def test_creating_and_listing(client):
    res = client.post("/playlists", json={"name": "  Road trip  "})
    assert res.status_code == 201
    # Trimmed so the duplicate check catches an edge-whitespace twin.
    assert res.json()["name"] == "Road trip"
    assert res.json()["track_count"] == 0

    listed = client.get("/playlists").json()
    assert [p["name"] for p in listed] == ["Road trip"]
    # Not asked about any particular track, so it must not claim to know.
    assert listed[0]["contains"] is None


def test_a_duplicate_name_is_a_conflict_not_a_second_list(client):
    assert client.post("/playlists", json={"name": "Gym"}).status_code == 201
    res = client.post("/playlists", json={"name": "gym"})
    assert res.status_code == 409
    assert len(client.get("/playlists").json()) == 1


def test_a_blank_name_is_refused(client):
    assert client.post("/playlists", json={"name": "   "}).status_code == 422
    assert client.post("/playlists", json={"name": ""}).status_code == 422


def test_adding_a_track_then_seeing_it_in_the_list(client, db_session):
    track = _track(db_session, "aaa")
    playlist_id = client.post("/playlists", json={"name": "Mix"}).json()["id"]

    res = client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})
    assert res.status_code == 200
    assert res.json()["status"] == "added"

    listed = client.get(f"/playlists?content_id={track.id}").json()
    assert listed[0]["track_count"] == 1
    assert listed[0]["contains"] is True


def test_adding_the_same_track_twice_is_reported_not_duplicated(client, db_session):
    track = _track(db_session, "bbb")
    playlist_id = client.post("/playlists", json={"name": "Mix"}).json()["id"]

    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})
    res = client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})
    # 200, not 409: the track being in the list is the outcome either way.
    assert res.status_code == 200
    assert res.json()["status"] == "duplicate"
    assert client.get("/playlists").json()[0]["track_count"] == 1


def test_order_is_the_order_they_were_added(client, db_session):
    tracks = [_track(db_session, f"o{i}") for i in range(3)]
    playlist_id = client.post("/playlists", json={"name": "Ordered"}).json()["id"]
    for track in reversed(tracks):
        client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    assert user_playlist_ids(db_session, USER_ID, playlist_id) == [t.id for t in reversed(tracks)]


def test_a_removal_does_not_renumber_the_rest(client, db_session):
    tracks = [_track(db_session, f"r{i}") for i in range(3)]
    playlist_id = client.post("/playlists", json={"name": "Gappy"}).json()["id"]
    for track in tracks:
        client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    client.delete(f"/playlists/{playlist_id}/tracks/{tracks[1].id}")

    # Gaps in position are fine, but a later append must not reuse a taken one.
    remaining = user_playlist_ids(db_session, USER_ID, playlist_id)
    assert remaining == [tracks[0].id, tracks[2].id]

    extra = _track(db_session, "r9")
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": extra.id})
    assert user_playlist_ids(db_session, USER_ID, playlist_id) == [
        tracks[0].id,
        tracks[2].id,
        extra.id,
    ]


def test_removing_a_track_that_is_not_in_the_list(client, db_session):
    track = _track(db_session, "ccc")
    playlist_id = client.post("/playlists", json={"name": "Mix"}).json()["id"]
    assert client.delete(f"/playlists/{playlist_id}/tracks/{track.id}").status_code == 404


def test_deleting_a_playlist_keeps_the_songs(client, db_session):
    track = _track(db_session, "ddd")
    playlist_id = client.post("/playlists", json={"name": "Temp"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    assert client.delete(f"/playlists/{playlist_id}").status_code == 200
    assert client.get("/playlists").json() == []
    # The items go with the list; the library does not.
    assert db_session.query(PlaylistItem).count() == 0
    assert db_session.query(Content).filter(Content.id == track.id).one_or_none() is not None


def test_purging_a_track_takes_it_out_of_every_playlist(client, db_session):
    """Content rows are deleted outright and SQLite doesn't cascade sideways, so items need a sweep."""
    track = _track(db_session, "eee")
    playlist_id = client.post("/playlists", json={"name": "Holder"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})
    assert db_session.query(PlaylistItem).count() == 1

    purge_content(db_session, db_session.query(Content).filter(Content.id == track.id).one())
    db_session.commit()

    assert db_session.query(PlaylistItem).count() == 0
    assert user_playlist_ids(db_session, USER_ID, playlist_id) == []


def test_another_users_playlist_is_not_reachable(client, db_session):
    _second_user(db_session)
    theirs = Playlist(user_id=OTHER_USER_ID, name="Not yours")
    db_session.add(theirs)
    db_session.commit()
    db_session.refresh(theirs)

    assert client.get("/playlists").json() == []
    assert client.delete(f"/playlists/{theirs.id}").status_code == 404
    assert (
        client.post(f"/playlists/{theirs.id}/tracks", json={"content_id": 1}).status_code == 404
    )
    assert user_playlist_ids(db_session, USER_ID, theirs.id) is None


def test_another_users_track_cannot_be_added(client, db_session):
    _second_user(db_session)
    theirs = _track(db_session, "fff", user_id=OTHER_USER_ID)
    playlist_id = client.post("/playlists", json={"name": "Mine"}).json()["id"]

    res = client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": theirs.id})
    assert res.status_code == 404


def test_the_detail_context_matches_the_pinned_lists_shape(client, db_session):
    """Rendered through _detail_panel.html, so it must supply the same keys."""
    track = _track(db_session, "ggg")
    playlist_id = client.post("/playlists", json={"name": "Shaped"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    context = user_playlist_detail_context(db_session, USER_ID, playlist_id, page=1)
    for key in (
        "kind",
        "artist",
        "title",
        "empty_message",
        "empty_help",
        "empty_cta",
        "empty_cta_href",
        "video_count",
        "content",
        "page",
        "total_pages",
        "start_index",
        "base_url",
    ):
        assert key in context, f"missing {key}"
    assert context["kind"] == "user-playlist"
    assert context["video_count"] == 1
    assert context["base_url"] == f"/#user-playlist/{playlist_id}"
    # Only this kind carries these (see _detail_hero.html and _content_row.html).
    assert context["playlist_id"] == playlist_id


def test_the_detail_context_is_none_for_someone_elses(db_session):
    _second_user(db_session)
    theirs = Playlist(user_id=OTHER_USER_ID, name="Theirs")
    db_session.add(theirs)
    db_session.commit()
    assert user_playlist_detail_context(db_session, USER_ID, theirs.id, page=1) is None


def test_the_detail_fragment_renders_the_rows(client, db_session):
    track = _track(db_session, "hhh")
    playlist_id = client.post("/playlists", json={"name": "Rendered"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    res = client.get(f"/partials/detail/user-playlist/{playlist_id}")
    assert res.status_code == 200
    assert 'data-target="detail-panel"' in res.text
    assert "Track hhh" in res.text
    assert "delete-playlist-btn" in res.text
    assert 'class="track-remove"' in res.text


def test_the_detail_fragment_404s_for_an_unknown_playlist(client):
    assert client.get("/partials/detail/user-playlist/9999").status_code == 404


def test_play_all_queues_the_stored_order(client, db_session):
    tracks = [_track(db_session, f"q{i}") for i in range(3)]
    playlist_id = client.post("/playlists", json={"name": "Queued"}).json()["id"]
    for track in reversed(tracks):
        client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    res = client.get(f"/content/queue/user-playlist/{playlist_id}")
    assert res.status_code == 200
    assert res.json()["ids"] == [t.id for t in reversed(tracks)]


def test_play_all_404s_for_an_unknown_playlist(client):
    assert client.get("/content/queue/user-playlist/9999").status_code == 404


def test_library_renders_a_tile_per_playlist(client, db_session):
    # The grid only renders once the user follows someone.
    _artist(db_session)
    track = _track(db_session, "tile")
    playlist_id = client.post("/playlists", json={"name": "On the tile"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    res = client.get("/partials/library")
    assert res.status_code == 200
    assert "On the tile" in res.text
    assert f'href="/#user-playlist/{playlist_id}"' in res.text
    assert "1 song" in res.text
    assert 'id="new-playlist-btn"' in res.text


def test_pagination_clamps_to_the_pages_that_exist(client, db_session):
    track = _track(db_session, "iii")
    playlist_id = client.post("/playlists", json={"name": "Short"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    context = user_playlist_detail_context(db_session, USER_ID, playlist_id, page=99)
    assert context["page"] == 1
    assert context["total_pages"] == 1
    assert len(context["content"]) == 1


def test_an_empty_playlist_gets_one_page_not_zero(client, db_session):
    playlist_id = client.post("/playlists", json={"name": "Empty"}).json()["id"]
    context = user_playlist_detail_context(db_session, USER_ID, playlist_id, page=1)
    # total_pages of 0 would make _pagination.html render "Page 1 of 0".
    assert context["total_pages"] == 1
    assert context["content"] == []
    assert context["video_count"] == 0
