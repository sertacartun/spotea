"""Keeping whole lists on a device: the paced download queue and the per-list endpoints."""

import threading

import pytest

from app.download_queue import DownloadQueue
from app.models import Artist, Content, OfflinePin, Playlist, PlaylistItem, User
from app.routers import content as content_router
from app.routers import offline as offline_router

USER_ID = 1


class _RecordingQueue:
    def __init__(self):
        self.enqueued: list[int] = []

    def enqueue(self, ids):
        self.enqueued.extend(ids)

    def is_queued(self, item):
        return item in self.enqueued


@pytest.fixture
def queue(monkeypatch):
    recorder = _RecordingQueue()
    monkeypatch.setattr(offline_router, "download_queue", recorder)
    return recorder


def _artist(db_session, user_id=USER_ID):
    artist = db_session.query(Artist).filter(Artist.user_id == user_id).first()
    if artist is None:
        artist = Artist(user_id=user_id, channel_id=f"https://example.com/a{user_id}", name="A")
        db_session.add(artist)
        db_session.commit()
    return artist


def _track(db_session, video_id, *, user_id=USER_ID, **fields):
    fields.setdefault("status", "not_downloaded")
    content = Content(
        artist_id=_artist(db_session, user_id).id,
        user_id=user_id,
        video_id=video_id,
        title=f"Track {video_id}",
        **fields,
    )
    db_session.add(content)
    db_session.commit()
    return content


def _playlist(db_session, tracks, *, user_id=USER_ID):
    playlist = Playlist(user_id=user_id, name=f"List {len(tracks)}")
    db_session.add(playlist)
    db_session.commit()
    for position, track in enumerate(tracks):
        db_session.add(PlaylistItem(playlist_id=playlist.id, content_id=track.id, position=position))
    db_session.commit()
    return playlist


def test_a_playlist_queues_only_what_the_server_does_not_have(client, db_session, queue):
    missing = _track(db_session, "missing0001")
    ready = _track(db_session, "ready000001", status="ready")
    failed = _track(db_session, "failed00001", status="error")
    gone = _track(db_session, "gone0000001", is_unavailable=True)
    playlist = _playlist(db_session, [missing, ready, failed, gone])

    res = client.post(f"/offline/playlists/{playlist.id}")

    assert res.status_code == 200
    # A failure isn't retried by a poll: that would hit YouTube for it on every sync.
    assert queue.enqueued == [missing.id]
    tracks = res.json()["tracks"]
    assert [t["id"] for t in tracks] == [missing.id, ready.id, failed.id, gone.id]
    assert [t["queued"] for t in tracks] == [True, False, False, False]
    assert tracks[3]["is_unavailable"] is True


def test_retry_requeues_failures_but_never_unavailable_tracks(client, db_session, queue):
    failed = _track(db_session, "failed00001", status="error")
    gone = _track(db_session, "gone0000001", status="error", is_unavailable=True)
    playlist = _playlist(db_session, [failed, gone])

    client.post(f"/offline/playlists/{playlist.id}?retry=true")

    assert queue.enqueued == [failed.id]


def test_favorites_are_a_list_too(client, db_session, queue):
    liked = _track(db_session, "liked000001", is_favorite=True)
    _track(db_session, "notliked001")

    res = client.post("/offline/favorites")

    assert res.status_code == 200
    assert [t["id"] for t in res.json()["tracks"]] == [liked.id]
    assert queue.enqueued == [liked.id]


def test_another_users_playlist_is_a_404(client, db_session, queue):
    db_session.add(User(id=2, username="other", password_hash="x"))
    db_session.commit()
    playlist = _playlist(db_session, [_track(db_session, "theirs00001", user_id=2)], user_id=2)

    assert client.post(f"/offline/playlists/{playlist.id}").status_code == 404
    assert queue.enqueued == []


def test_the_queue_caps_concurrency_and_survives_a_failure():
    running = 0
    peak = 0
    ran: list[int] = []
    lock = threading.Lock()
    # The first two steps only finish once both are in flight, which proves two run at once.
    both_started = threading.Barrier(2, timeout=5)

    def step(item):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            ran.append(item)
            first_two = len(ran) <= 2
        try:
            if first_two:
                both_started.wait()
            if item == 2:
                raise RuntimeError("YouTube said no")
        finally:
            with lock:
                running -= 1

    queue = DownloadQueue(step, workers=2, gap_seconds=0)
    queue.enqueue([1, 2, 3, 4, 5])
    # Already pending: not queued a second time.
    queue.enqueue([5])

    assert queue.wait_until_idle(timeout=5)
    assert sorted(ran) == [1, 2, 3, 4, 5]
    assert peak == 2


def test_a_queued_download_settles_the_row_like_a_played_one(db_session, monkeypatch, tmp_path):
    item = _track(db_session, "queued00001")
    audio = tmp_path / "queued.m4a"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(content_router, "download_audio", lambda *a, **k: audio)

    content_router.download_queued(item.id)

    db_session.refresh(item)
    assert item.status == "ready"
    assert item.file_path == str(audio)


def test_a_queued_download_leaves_a_track_already_on_disk_alone(db_session, monkeypatch, tmp_path):
    audio = tmp_path / "here.m4a"
    audio.write_bytes(b"audio")
    item = _track(db_session, "ondisk00001", status="ready", file_path=str(audio))

    def fail(*args, **kwargs):
        raise AssertionError("re-downloaded a track that was already on disk")

    monkeypatch.setattr(content_router, "download_audio", fail)

    content_router.download_queued(item.id)


def test_a_list_kept_by_ids_drops_what_is_not_this_users(client, db_session, queue):
    """An album or YouTube playlist is kept as ids; a stale or foreign id just drops out."""
    db_session.add(User(id=2, username="other", password_hash="x"))
    db_session.commit()
    mine = _track(db_session, "mine0000001")
    theirs = _track(db_session, "theirs00001", user_id=2)

    res = client.post("/offline/tracks", json={"key": "list:yt-release:MPREb_x", "ids": [mine.id, theirs.id, 999999]})

    assert res.status_code == 200
    assert [t["id"] for t in res.json()["tracks"]] == [mine.id]
    assert queue.enqueued == [mine.id]


def _pins(db_session, key=None):
    query = db_session.query(OfflinePin.list_key, OfflinePin.content_id)
    if key:
        query = query.filter(OfflinePin.list_key == key)
    return sorted(query.all())


def test_a_sync_pins_the_lists_current_tracks_and_drops_the_rest(client, db_session, queue):
    """A song taken off a downloaded list falls back to cache."""
    first = _track(db_session, "first000001", status="ready")
    second = _track(db_session, "second00001", status="ready")
    playlist = _playlist(db_session, [first, second])
    key = f"playlist:{playlist.id}"

    client.post(f"/offline/playlists/{playlist.id}")
    assert _pins(db_session) == [(key, first.id), (key, second.id)]

    client.delete(f"/playlists/{playlist.id}/tracks/{second.id}")
    client.post(f"/offline/playlists/{playlist.id}")
    assert _pins(db_session) == [(key, first.id)]


def test_unpinning_a_list_leaves_other_lists_pins_alone(client, db_session, queue):
    shared = _track(db_session, "shared00001", status="ready", is_favorite=True)
    playlist = _playlist(db_session, [shared])
    client.post("/offline/favorites")
    client.post(f"/offline/playlists/{playlist.id}")

    res = client.delete("/offline/lists", params={"key": f"playlist:{playlist.id}"})

    assert res.status_code == 204
    assert _pins(db_session) == [("favorites", shared.id)]


def test_deleting_a_playlist_unpins_it(client, db_session, queue):
    track = _track(db_session, "deleted0001", status="ready")
    playlist = _playlist(db_session, [track])
    client.post(f"/offline/playlists/{playlist.id}")

    client.delete(f"/playlists/{playlist.id}")

    assert _pins(db_session) == []


def test_a_list_key_must_name_a_kept_by_ids_list(client, queue):
    """Only "list:" keys come through /offline/tracks; favorites and playlists have their own routes."""
    res = client.post("/offline/tracks", json={"key": "favorites", "ids": []})

    assert res.status_code == 422
