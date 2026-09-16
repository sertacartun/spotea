from datetime import datetime, timedelta

from app.content_query import query_content_page
from app.images import proxied_image_url, unproxied_image_url
from app.models import Artist, Content, User

USER_ID = 1
# Must match conftest.py's DEFAULT_USER_ID; conftest can't be imported as a module.
DEFAULT_USER_ID = 1


def _seed(db_session, count=25, is_favorite=False):
    artist = Artist(user_id=USER_ID, channel_id="https://example.com/artist", name="Test Channel")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    now = datetime(2026, 1, 1)
    items = [
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id=f"vid{i:04d}"[:11],
            title=f"Title {count - i:03d}",
            published_at=now - timedelta(days=i),
            duration_seconds=120 + i,
            is_favorite=is_favorite or (i == 0),
            status="not_downloaded",
        )
        for i in range(count)
    ]
    db_session.add_all(items)
    db_session.commit()
    return artist, items


def test_get_single_content_returns_full_shape(client, db_session):
    _feed, items = _seed(db_session, count=3)

    res = client.get(f"/content/{items[0].id}")
    assert res.status_code == 200

    body = res.json()
    assert set(body.keys()) == {
        "id",
        "artist_id",
        "channel_title",
        "artist_page_id",
        "video_id",
        "title",
        "thumbnail_url",
        "artwork",
        "duration_seconds",
        "published_at",
        "status",
        "added_at",
        "is_favorite",
        "is_played",
        "is_unavailable",
        "is_music_video",
    }
    assert body["id"] == items[0].id
    assert body["channel_title"] == "Test Channel"


def test_the_payload_carries_the_cover_at_several_declared_sizes(client, db_session):
    """iOS won't downscale art for the compact player (Dynamic Island), so small sizes must be declared."""
    _feed, items = _seed(db_session, count=1)
    items[0].thumbnail_url = proxied_image_url(
        "https://yt3.ggpht.com/COVER=w544-h544-l90-rj"
    )
    db_session.commit()

    artwork = client.get(f"/content/{items[0].id}").json()["artwork"]

    assert [entry["sizes"] for entry in artwork] == ["96x96", "192x192", "512x512"]
    assert all("COVER" in entry["src"] for entry in artwork)
    assert "w96-h96" in unproxied_image_url(artwork[0]["src"])
    assert "w512-h512" in unproxied_image_url(artwork[-1]["src"])


def test_a_cover_that_cannot_be_resized_is_published_once_without_a_size(client, db_session):
    """A video still ignores the size segment, so identical URLs mustn't claim different sizes."""
    _feed, items = _seed(db_session, count=1)
    items[0].thumbnail_url = None
    db_session.commit()

    artwork = client.get(f"/content/{items[0].id}").json()["artwork"]

    assert len(artwork) == 1
    assert "sizes" not in artwork[0]


def test_the_payload_says_how_to_open_the_artist(client, db_session):
    """The browse id when the artist has one, otherwise the channel id it was created from."""
    artist, items = _seed(db_session, count=1)

    artist.browse_id = "UCbrowseidbrowseid1234"
    db_session.commit()
    assert client.get(f"/content/{items[0].id}").json()["artist_page_id"] == "UCbrowseidbrowseid1234"

    artist.browse_id = None
    db_session.commit()
    assert client.get(f"/content/{items[0].id}").json()["artist_page_id"] == artist.channel_id


def test_the_payload_carries_a_cover_even_when_the_row_has_none(client, db_session):
    """The player sets its artwork from this payload, so the cover fallback must live in the serializer."""
    artist, items = _seed(db_session, count=1)
    track = items[0]
    track.thumbnail_url = None
    db_session.commit()

    body = client.get(f"/content/{track.id}").json()

    assert body["thumbnail_url"], "the player would render a blank square"
    assert f"{track.video_id}%2Fmqdefault.jpg" in body["thumbnail_url"]


def test_a_tracks_own_cover_reaches_the_payload_unchanged(client, db_session):
    artist, items = _seed(db_session, count=1)
    track = items[0]
    track.thumbnail_url = "/thumbnails/kept.jpg"
    db_session.commit()

    assert client.get(f"/content/{track.id}").json()["thumbnail_url"] == "/thumbnails/kept.jpg"


def test_get_single_content_404_for_nonexistent_id(client, db_session):
    res = client.get("/content/999999")
    assert res.status_code == 404


def test_get_single_content_404_for_another_users_content(client, db_session):
    other_user = User(username="other1", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)

    other_feed = Artist(user_id=other_user.id, channel_id="https://example.com/other", name="Other")
    db_session.add(other_feed)
    db_session.commit()
    db_session.refresh(other_feed)

    other_content = Content(
        artist_id=other_feed.id, user_id=other_user.id, video_id="otheruser01", title="Not yours"
    )
    db_session.add(other_content)
    db_session.commit()
    db_session.refresh(other_content)

    res = client.get(f"/content/{other_content.id}")
    assert res.status_code == 404


def test_playlist_queue_matches_its_detail_panel(client, db_session):
    artist, items = _seed(db_session, count=25)
    ids = client.get("/content/queue/playlist/favorites").json()["ids"]
    # _seed favorites exactly one row (i == 0, the newest).
    favorites, _page, _total_pages = query_content_page(db_session, USER_ID, filter="__favorites__")
    assert ids == [item.id for item in favorites]
    assert len(ids) == 1


def test_queue_endpoints_404_for_unknown_targets(client, db_session):
    assert client.get("/content/queue/channel/9999").status_code == 404
    assert client.get("/content/queue/playlist/bogus").status_code == 404


def test_channel_queue_404s_for_another_users_channel(client, db_session):
    other_user = User(username="other2", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)

    other_feed = Artist(user_id=other_user.id, channel_id="https://example.com/queue-other")
    db_session.add(other_feed)
    db_session.commit()
    db_session.refresh(other_feed)

    assert client.get(f"/content/queue/channel/{other_feed.id}").status_code == 404


def test_queue_is_capped(client, db_session, monkeypatch):
    from app import content_query

    monkeypatch.setattr(content_query, "QUEUE_MAX_ITEMS", 5)
    _seed(db_session, count=25, is_favorite=True)

    assert len(client.get("/content/queue/playlist/favorites").json()["ids"]) == 5


def _seed_one(db_session, **overrides):
    artist = Artist(user_id=USER_ID, channel_id="https://example.com/dl-artist", name="Download Channel")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    fields = {"status": "not_downloaded", **overrides}
    item = Content(
        artist_id=artist.id, user_id=USER_ID, video_id="downloadvi1", title="Download Me", **fields
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)
    return item


def test_start_download_dispatches_and_settles_to_ready(client, db_session, monkeypatch, tmp_path):
    from app.routers import content as content_router

    item = _seed_one(db_session)
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: fake_file)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    # TestClient runs BackgroundTasks after building the response, so the body is the pre-task state.
    assert res.json()["status"] == "downloading"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(fake_file)


def test_start_download_swaps_the_music_video_before_it_fetches_anything(
    client, db_session, monkeypatch, tmp_path
):
    """The download fetches the row's video_id, so the song swap has to land server-side first."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL, duration_seconds=235)
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song())

    fetched = []
    def record(video_id, *a, **k):
        fetched.append(video_id)
        path = tmp_path / f"{video_id}.m4a"
        path.write_bytes(b"audio")
        return path

    monkeypatch.setattr(content_router, "download_audio", record)

    res = client.post(f"/content/{item.id}/download")

    assert res.status_code == 200
    # The song's id, not the music video's the row was seeded with.
    assert fetched == ["songvideo11"]

    # The answer carries the swapped row: the caller's title/cover still describe the video.
    body = res.json()
    assert body["content"]["title"] == "Download Me"
    assert body["content"]["thumbnail_url"] == _SQUARE
    assert body["content"]["duration_seconds"] == 200

    db_session.refresh(item)
    assert item.video_id == "songvideo11"


def test_the_status_poll_carries_no_row_of_its_own(client, db_session):
    """Only the download answers with `content`; repeating it on every 200ms status tick is waste."""
    item = _seed_one(db_session)

    body = client.get(f"/content/{item.id}/status").json()

    assert body["status"] == "not_downloaded"
    assert body["content"] is None


def test_start_download_409s_while_already_downloading(client, db_session):
    item = _seed_one(db_session, status="downloading")
    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 409


def test_start_download_leaves_a_track_thats_already_on_disk_alone(client, db_session, monkeypatch, tmp_path):
    """The queue's prefetch fires without knowing the next track's status."""
    from app.routers import content as content_router

    on_disk = tmp_path / "already-here.m4a"
    on_disk.write_bytes(b"audio")
    item = _seed_one(db_session, status="ready", file_path=str(on_disk))

    def fail(*args, **kwargs):
        raise AssertionError("re-downloaded a track that was already on disk")

    monkeypatch.setattr(content_router, "download_audio", fail)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    assert res.json()["status"] == "ready"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(on_disk)


def test_start_download_refetches_when_the_file_is_gone(client, db_session, monkeypatch, tmp_path):
    from app.routers import content as content_router

    item = _seed_one(db_session, status="ready", file_path=str(tmp_path / "vanished.m4a"))
    replacement = tmp_path / f"{item.video_id}.m4a"
    replacement.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: replacement)

    assert client.post(f"/content/{item.id}/download").json()["status"] == "downloading"

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(replacement)


def test_a_video_youtube_wont_serve_is_recorded_as_settled(client, db_session, monkeypatch):
    """So the app stops re-attempting it forever (see Content.is_unavailable)."""
    from app.routers import content as content_router

    item = _seed_one(db_session)

    def unavailable(*args, **kwargs):
        raise content_router.VideoUnavailableError("ERROR: [youtube] x: Video unavailable")

    monkeypatch.setattr(content_router, "download_audio", unavailable)
    client.post(f"/content/{item.id}/download")

    db_session.refresh(item)
    assert item.status == "error"
    assert item.is_unavailable is True
    assert client.get(f"/content/{item.id}/status").json()["is_unavailable"] is True


def test_an_ordinary_failure_stays_worth_retrying(client, db_session, monkeypatch):
    from app.routers import content as content_router

    item = _seed_one(db_session)

    def refused(*args, **kwargs):
        raise content_router.DownloadError("ERROR: HTTP Error 403: Forbidden")

    monkeypatch.setattr(content_router, "download_audio", refused)
    client.post(f"/content/{item.id}/download")

    db_session.refresh(item)
    assert item.status == "error"
    assert item.is_unavailable is False


def test_an_unavailable_track_is_never_attempted_again(client, db_session, monkeypatch):
    """The prefetch fires blindly, so a known-broken track must not cost another extraction."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="error")
    item.is_unavailable = True
    db_session.commit()

    def fail(*args, **kwargs):
        raise AssertionError("re-attempted a track YouTube has already refused")

    monkeypatch.setattr(content_router, "download_audio", fail)

    res = client.post(f"/content/{item.id}/download")
    assert res.status_code == 200
    assert res.json()["is_unavailable"] is True

    db_session.refresh(item)
    assert item.status == "error"


def test_removing_a_download_reopens_an_unavailable_track(client, db_session):
    """Licensing changes, so removing the download is the one way to reopen a track."""
    item = _seed_one(db_session, status="error")
    item.is_unavailable = True
    db_session.commit()

    client.delete(f"/content/{item.id}")

    db_session.refresh(item)
    assert item.is_unavailable is False
    assert item.status == "not_downloaded"


def test_there_is_no_restart_endpoint(client, db_session):
    """Retrying is downloader.py's job; two concurrent yt-dlp runs on one .part file broke plays."""
    item = _seed_one(db_session, status="downloading")
    res = client.post(f"/content/{item.id}/download/restart")
    assert res.status_code == 404


def test_the_extracting_phase_reaches_the_status_endpoint(client, db_session, monkeypatch, tmp_path):
    """Resolving moves no bytes, so without the phase the UI sits on "Preparing audio…" throughout."""
    from app.routers import content as content_router

    item = _seed_one(db_session, status="downloading")
    fake_file = tmp_path / f"{item.video_id}.m4a"
    fake_file.write_bytes(b"audio")

    def fake_download(video_id, quality="high", on_progress=None, user_id=None):
        on_progress("extracting", None)
        assert client.get(f"/content/{item.id}/status").json()["phase"] == "extracting"
        return fake_file

    monkeypatch.setattr(content_router, "download_audio", fake_download)
    content_router._run_download(item.id, item.video_id, "high", USER_ID)

    # ...and cleared once the download settles.
    assert client.get(f"/content/{item.id}/status").json()["phase"] is None


def test_download_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.post("/content/1/download", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/login"


def _seed_ready(db_session, tmp_path):
    audio = tmp_path / "track.m4a"
    audio.write_bytes(b"audio")
    return _seed_one(db_session, status="ready", file_path=str(audio))


def test_streaming_a_track_does_not_record_it_as_played(client, db_session, tmp_path):
    """The player prefetches the next track's audio, so a stream request is not a play."""
    item = _seed_ready(db_session, tmp_path)

    assert client.get(f"/content/{item.id}/stream").status_code == 200

    db_session.refresh(item)
    assert item.last_played_at is None


def test_marking_a_track_played_records_it(client, db_session, tmp_path):
    item = _seed_ready(db_session, tmp_path)
    assert item.last_played_at is None

    assert client.post(f"/content/{item.id}/played").status_code == 204

    db_session.refresh(item)
    assert item.last_played_at is not None


def test_marking_an_unknown_track_played_is_a_404(client):
    assert client.post("/content/999999/played").status_code == 404


def test_only_the_export_link_serves_the_file_as_an_attachment(client, db_session, tmp_path):
    """?download=1 is the export link; a player stream must not get Content-Disposition: attachment."""
    item = _seed_ready(db_session, tmp_path)

    played = client.get(f"/content/{item.id}/stream")
    assert played.status_code == 200
    assert "content-disposition" not in played.headers

    exported = client.get(f"/content/{item.id}/stream?download=1")
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith("attachment")

    db_session.refresh(item)
    assert item.last_played_at is None


_STILL = "/image-proxy?u=https%3A//i.ytimg.com/vi/downloadvi1/hqdefault.jpg%3Fsqp%3Dabc"
_SQUARE = "/image-proxy?u=https%3A//yt3.ggpht.com/abc%3Dw544-h544-l90-rj"


def _song(video_id="songvideo11", title="Download Me", duration=200):
    from app.youtube.models import VideoSearchResult

    return VideoSearchResult(
        video_id=video_id,
        title=title,
        thumbnail_url=_SQUARE,
        duration_seconds=duration,
        channel_title="Download Channel",
        channel_id="UCsomethingsomething",
    )


def test_a_music_video_row_is_rewritten_in_place(client, db_session, monkeypatch):
    """In place: the queue holds this row's id, and a new row would drop the playing track from it."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL, duration_seconds=235)
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song())

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    body = res.json()
    assert body["id"] == item.id
    assert body["video_id"] == "songvideo11"
    assert body["thumbnail_url"] == _SQUARE
    assert body["duration_seconds"] == 200
    assert body["is_music_video"] is False

    db_session.refresh(item)
    assert item.video_id == "songvideo11"


def test_the_swap_takes_the_songs_credit_and_drops_the_videos(client, db_session, monkeypatch):
    """A music-video row is credited to its uploader, so the song's credit replaces it."""
    from app.routers import content as content_router
    from app.youtube.models import VideoSearchResult

    def _credited(credit):
        song = _song()
        return VideoSearchResult(**{**song.__dict__, "artist_credit": credit})

    item = _seed_one(db_session, thumbnail_url=_STILL)
    monkeypatch.setattr(
        content_router, "find_song_version", lambda *a: _credited("Baby Keem, Kendrick Lamar")
    )

    res = client.post(f"/content/{item.id}/song-version")

    assert res.json()["channel_title"] == "Baby Keem, Kendrick Lamar"
    db_session.refresh(item)
    assert item.artist_credit == "Baby Keem, Kendrick Lamar"


def test_the_swap_clears_a_credit_the_song_does_not_have(client, db_session, monkeypatch):
    from app.routers import content as content_router
    from app.youtube.models import VideoSearchResult

    item = _seed_one(db_session, thumbnail_url=_STILL, artist_credit="Wrong, Names")
    song = _song()
    monkeypatch.setattr(
        content_router,
        "find_song_version",
        lambda *a: VideoSearchResult(**{**song.__dict__, "artist_credit": None}),
    )

    res = client.post(f"/content/{item.id}/song-version")

    db_session.refresh(item)
    assert item.artist_credit is None, "the video's credit survived a swap onto a solo song"
    assert res.json()["channel_title"] == item.artist.name


def test_a_row_that_is_already_the_song_is_left_alone(client, db_session, monkeypatch):
    """Square art means YouTube Music already calls it a song."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_SQUARE)

    def fail(*a):
        raise AssertionError("must not search for a row that is already a song")

    monkeypatch.setattr(content_router, "find_song_version", fail)

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    assert res.json()["video_id"] == item.video_id


def test_a_downloaded_row_is_never_rewritten(client, db_session, monkeypatch):
    """Changing video_id under a fetched file would orphan the audio."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL, status="ready", file_path="data/x.m4a")

    def fail(*a):
        raise AssertionError("must not search for a row that is already downloaded")

    monkeypatch.setattr(content_router, "find_song_version", fail)

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    assert res.json()["video_id"] == "downloadvi1"


def test_no_song_version_leaves_the_video_playable(client, db_session, monkeypatch):
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: None)

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    assert res.json()["video_id"] == "downloadvi1"
    assert res.json()["is_music_video"] is True


def test_a_song_already_in_the_library_is_not_swapped_into(client, db_session, monkeypatch):
    """(user_id, video_id) is unique; returning the other row's id would drop the track from its queue."""
    from app.models import Content
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    db_session.add(
        Content(
            artist_id=item.artist_id,
            user_id=USER_ID,
            video_id="songvideo11",
            title="Download Me",
            status="not_downloaded",
        )
    )
    db_session.commit()
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song())

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    assert res.json()["video_id"] == "downloadvi1"


def test_the_stored_channel_id_is_offered_to_the_matcher(client, db_session, monkeypatch):
    """Display names differ between responses ("Marie Ulven" vs "girl in red"); the channel id doesn't."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    seen = {}

    def capture(title, artist_name, artist_channel_id=None):
        seen.update(title=title, name=artist_name, channel_id=artist_channel_id)
        return None

    monkeypatch.setattr(content_router, "find_song_version", capture)

    client.post(f"/content/{item.id}/song-version")

    assert seen["channel_id"] == "https://example.com/dl-artist"


def test_a_placeholder_attribution_moves_to_the_credited_artist(client, db_session, monkeypatch):
    """Label-uploaded videos are attributed to the label; the song version names the real artist."""
    from app.models import Artist
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    label = db_session.get(Artist, item.artist_id)
    label.followed = False
    db_session.commit()
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song())

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    db_session.refresh(item)
    assert item.artist_id != label.id
    moved_to = db_session.get(Artist, item.artist_id)
    assert moved_to.channel_id == "UCsomethingsomething"
    # Still a placeholder: knowing who recorded a track isn't a decision to follow them.
    assert moved_to.followed is False


def test_a_followed_artist_keeps_the_track(client, db_session, monkeypatch):
    """Once followed, re-pointing the row would take it off that artist's Library page."""
    from app.models import Artist
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    followed = db_session.get(Artist, item.artist_id)
    assert followed.followed is True
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song())

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    db_session.refresh(item)
    assert item.artist_id == followed.id


def test_the_row_takes_the_song_s_own_title(client, db_session, monkeypatch):
    """A chart entry is named for the video file ("... Official MV"), not the song."""
    from app.routers import content as content_router

    item = _seed_one(db_session, thumbnail_url=_STILL)
    item.title = "ARTIST 'Song' Official MV"
    db_session.commit()
    monkeypatch.setattr(content_router, "find_song_version", lambda *a: _song(title="Song"))

    res = client.post(f"/content/{item.id}/song-version")

    assert res.status_code == 200
    assert res.json()["title"] == "Song"
    db_session.refresh(item)
    assert item.title == "Song"
