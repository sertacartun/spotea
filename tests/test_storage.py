"""The downloads/cache split, size backfill and the /storage endpoints; sizes come from Content.file_size_bytes."""

import io
import zipfile
from datetime import timedelta

from app.models import Artist, Content, OfflinePin
from app.storage import CACHE_RETENTION, backfill_file_sizes, clear_cache, storage_split, sweep_cache
from app.timeutil import utcnow

USER_ID = 1


def _ready_content(db_session, tmp_path, *, video_id, size_bytes, stored_size=None):
    artist = (
        db_session.query(Artist).filter(Artist.user_id == USER_ID).first()
        or Artist(user_id=USER_ID, channel_id="https://example.com/artist", name="Storage Channel")
    )
    if artist.id is None:
        db_session.add(artist)
        db_session.commit()
        db_session.refresh(artist)

    audio = tmp_path / f"{video_id}.m4a"
    audio.write_bytes(b"x" * size_bytes)

    content = Content(
        artist_id=artist.id,
        user_id=USER_ID,
        video_id=video_id,
        title=f"Track {video_id}",
        status="ready",
        file_path=str(audio),
        file_size_bytes=stored_size,
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return content, audio


def _pin(db_session, content, key="favorites"):
    db_session.add(OfflinePin(user_id=USER_ID, list_key=key, content_id=content.id))
    db_session.commit()


def test_the_stored_size_wins_over_the_file_on_disk(db_session, tmp_path):
    """The file isn't stat'ed on every render."""
    _ready_content(db_session, tmp_path, video_id="stored00001", size_bytes=10, stored_size=4096)

    backfill_file_sizes(db_session, USER_ID)

    assert storage_split(db_session, USER_ID).cache.total_bytes == 4096


def test_unmeasured_rows_are_backfilled_from_disk(db_session, tmp_path):
    """Rows downloaded before file_size_bytes existed are NULL, and count as 0 until measured."""
    content, _audio = _ready_content(
        db_session, tmp_path, video_id="legacy00001", size_bytes=2048, stored_size=None
    )
    assert storage_split(db_session, USER_ID).cache.total_bytes == 0

    backfill_file_sizes(db_session, USER_ID)

    assert storage_split(db_session, USER_ID).cache.total_bytes == 2048
    db_session.refresh(content)
    assert content.file_size_bytes == 2048


def test_a_missing_file_backfills_as_zero_rather_than_failing(db_session, tmp_path):
    content, audio = _ready_content(
        db_session, tmp_path, video_id="vanished001", size_bytes=512, stored_size=None
    )
    audio.unlink()

    backfill_file_sizes(db_session, USER_ID)

    db_session.refresh(content)
    assert content.file_size_bytes == 0


def test_the_split_separates_downloads_from_cache(db_session, tmp_path):
    downloaded, _ = _ready_content(db_session, tmp_path, video_id="sum0000001", size_bytes=1, stored_size=1000)
    _ready_content(db_session, tmp_path, video_id="sum0000002", size_bytes=1, stored_size=2000)
    _ready_content(db_session, tmp_path, video_id="sum0000003", size_bytes=1, stored_size=500)
    # Pinned by two lists: still one download, counted once.
    _pin(db_session, downloaded, "favorites")
    _pin(db_session, downloaded, "playlist:1")
    # A not-downloaded row is in neither.
    artist = db_session.query(Artist).filter(Artist.user_id == USER_ID).first()
    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="notdownld1",
            title="Not downloaded", status="not_downloaded",
        )
    )
    db_session.commit()

    split = storage_split(db_session, USER_ID)

    assert (split.downloads.count, split.downloads.total_bytes) == (1, 1000)
    assert (split.cache.count, split.cache.total_bytes) == (2, 2500)


def test_clear_cache_keeps_downloads_and_resets_the_stored_size(db_session, tmp_path):
    """A stale size would make the next backfill skip the row."""
    cached, cached_audio = _ready_content(db_session, tmp_path, video_id="cached00001", size_bytes=64, stored_size=64)
    kept, kept_audio = _ready_content(db_session, tmp_path, video_id="kept0000001", size_bytes=64, stored_size=64)
    _pin(db_session, kept)

    assert clear_cache(db_session, USER_ID) == 1

    assert not cached_audio.exists()
    assert kept_audio.exists()
    db_session.refresh(cached)
    db_session.refresh(kept)
    assert (cached.status, cached.file_path, cached.file_size_bytes) == ("not_downloaded", None, None)
    assert kept.status == "ready"


def test_the_cache_sweep_waits_a_week_after_the_last_play(db_session, tmp_path):
    old = utcnow() - CACHE_RETENTION - timedelta(hours=1)
    recent = utcnow() - timedelta(days=1)
    stale, stale_audio = _ready_content(db_session, tmp_path, video_id="stale000001", size_bytes=8)
    replayed, replayed_audio = _ready_content(db_session, tmp_path, video_id="replayed001", size_bytes=8)
    pinned, pinned_audio = _ready_content(db_session, tmp_path, video_id="pinned00001", size_bytes=8)
    stale.downloaded_at = old
    stale.last_played_at = old
    # Downloaded long ago but played yesterday: still in use.
    replayed.downloaded_at = old
    replayed.last_played_at = recent
    pinned.downloaded_at = old
    pinned.last_played_at = old
    db_session.commit()
    _pin(db_session, pinned)

    assert sweep_cache(db_session) == 1

    assert not stale_audio.exists()
    assert replayed_audio.exists()
    # A download has no expiry.
    assert pinned_audio.exists()


def test_clear_cache_endpoint(client, db_session, tmp_path):
    _content, audio = _ready_content(db_session, tmp_path, video_id="endpoint001", size_bytes=8)

    res = client.delete("/storage/cache")

    assert res.status_code == 200
    assert res.json() == {"cleared": 1}
    assert not audio.exists()


def test_delete_endpoint_resets_the_stored_size_too(client, db_session, tmp_path):
    content, audio = _ready_content(
        db_session, tmp_path, video_id="deleted0001", size_bytes=64, stored_size=64
    )

    res = client.delete(f"/content/{content.id}")

    assert res.status_code == 200
    assert not audio.exists()
    db_session.refresh(content)
    assert content.file_size_bytes is None


def test_export_streams_from_disk_and_leaves_nothing_behind(client, db_session, tmp_path):
    """The archive is built on disk, not in memory, and cleaned up afterwards."""
    from app.config import settings
    from app.routers.storage import EXPORT_TEMP_SUFFIX

    first, _ = _ready_content(db_session, tmp_path, video_id="export00001", size_bytes=32)
    second, _ = _ready_content(db_session, tmp_path, video_id="export00002", size_bytes=48)
    _pin(db_session, first)
    _pin(db_session, second)
    # Cache isn't a download, so it isn't exported.
    _ready_content(db_session, tmp_path, video_id="cacheonly01", size_bytes=16)

    res = client.get("/storage/export")

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"

    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        names = sorted(zf.namelist())
        assert names == [f"Track {first.video_id}.m4a", f"Track {second.video_id}.m4a"]
        # ZIP_STORED, so entries come back byte-identical to what's on disk.
        assert len(zf.read(names[0])) == 32

    leftovers = list(settings.storage_dir.glob(f"*{EXPORT_TEMP_SUFFIX}"))
    assert leftovers == [], f"export left temp files behind: {leftovers}"


def test_export_with_nothing_downloaded_is_a_conflict(client, db_session):
    res = client.get("/storage/export")

    assert res.status_code == 409


def test_purging_a_pinned_track_takes_its_pins_with_it(db_session, tmp_path):
    """offline_pins has a foreign key to content, so a pin left behind would fail the delete."""
    from app.storage import purge_content

    content, audio = _ready_content(db_session, tmp_path, video_id="purged00001", size_bytes=8)
    _pin(db_session, content)

    purge_content(db_session, content)
    db_session.commit()

    assert db_session.query(OfflinePin).count() == 0
    assert not audio.exists()
