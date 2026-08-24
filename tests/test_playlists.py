"""Hand-made playlists: the API, and the two places their rows can be
orphaned from underneath them.

Library's other three lists are filters over `content` (see
page_context.PLAYLIST_KINDS) and so cannot be wrong about what they hold —
they recompute it. These have rows, which is what makes ownership, ordering
and cleanup worth pinning down.
"""

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
        user = User(id=OTHER_USER_ID, email="other@example.com", password_hash="x")
        db_session.add(user)
        db_session.commit()
    return user


def test_creating_and_listing(client):
    res = client.post("/playlists", json={"name": "  Road trip  "})
    assert res.status_code == 201
    # Trimmed at the router — a name with edge whitespace would otherwise be
    # a different name than the one the user believes they typed, and the
    # duplicate check below would never catch its twin.
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
    # 200 rather than 409: "it is in the list" is the outcome the press asked
    # for either way, and only the wording of the confirmation differs.
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

    # The gap in `position` is deliberate — nothing reads the numbers, only
    # their order — but a later append must not land on one already taken.
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
    """Unfollowing an artist deletes their Content rows outright (see
    storage.purge_content). SQLite does not enforce the foreign key unless
    PRAGMA foreign_keys is on, and the ORM cascade runs from Playlist down
    rather than from Content sideways — so without an explicit sweep the row
    goes and its playlist_items stay, pointing at nothing. The list then
    renders a gap and "Play all" queues an id that 404s.
    """
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
    """It renders through the same _detail_panel.html, so it has to supply the
    same keys — a missing one is a silently empty region rather than an
    error."""
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
    # Only this kind carries these, and the panel's two extra controls read
    # them (see _detail_hero.html and _content_row.html).
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
    # The two controls only this kind gets.
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
    # The grid only renders at all once the user follows someone (see
    # _library_grid.html's `{% if artists %}`), so there has to be one.
    _artist(db_session)
    track = _track(db_session, "tile")
    playlist_id = client.post("/playlists", json={"name": "On the tile"}).json()["id"]
    client.post(f"/playlists/{playlist_id}/tracks", json={"content_id": track.id})

    res = client.get("/partials/library")
    assert res.status_code == 200
    assert "On the tile" in res.text
    assert f'href="/#user-playlist/{playlist_id}"' in res.text
    # The count comes off one grouped query, and a tile that disagreed with
    # the list it opens is the bug that made the artist counts a single query.
    assert "1 song" in res.text
    # And the way to make another one sits with them.
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
