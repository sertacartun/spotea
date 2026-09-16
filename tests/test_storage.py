"""collect_usage and the /storage endpoints; sizes come from Content.file_size_bytes."""

import io
import zipfile

from app.models import Artist, Content
from app.storage import clear_all, collect_usage, usage_summary

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


def test_usage_reports_the_stored_size(db_session, tmp_path):
    _ready_content(db_session, tmp_path, video_id="stored00001", size_bytes=10, stored_size=4096)

    usage = collect_usage(db_session, USER_ID)

    assert usage.count == 1
    # The stored value wins over what's on disk: the file isn't stat'ed on every call.
    assert usage.total_bytes == 4096


def test_unmeasured_rows_are_backfilled_from_disk_once(db_session, tmp_path):
    """Rows downloaded before file_size_bytes existed are NULL until the first collect_usage."""
    content, _audio = _ready_content(
        db_session, tmp_path, video_id="legacy00001", size_bytes=2048, stored_size=None
    )

    usage = collect_usage(db_session, USER_ID)

    assert usage.total_bytes == 2048
    db_session.refresh(content)
    assert content.file_size_bytes == 2048


def test_a_missing_file_backfills_as_zero_rather_than_failing(db_session, tmp_path):
    content, audio = _ready_content(
        db_session, tmp_path, video_id="vanished001", size_bytes=512, stored_size=None
    )
    audio.unlink()

    usage = collect_usage(db_session, USER_ID)

    assert usage.total_bytes == 0
    db_session.refresh(content)
    assert content.file_size_bytes == 0


def test_totals_add_up_across_rows(db_session, tmp_path):
    _ready_content(db_session, tmp_path, video_id="multi000001", size_bytes=1, stored_size=1000)
    _ready_content(db_session, tmp_path, video_id="multi000002", size_bytes=1, stored_size=2000)

    usage = collect_usage(db_session, USER_ID)

    assert usage.count == 2
    assert usage.total_bytes == 3000


def test_usage_summary_matches_collect_usage_for_an_empty_library(db_session):
    """usage_summary must add up to what collect_usage's full list reports."""
    summary = usage_summary(db_session, USER_ID)
    full = collect_usage(db_session, USER_ID)

    assert summary.count == full.count == 0
    assert summary.total_bytes == full.total_bytes == 0


def test_usage_summary_matches_collect_usage_across_several_rows(db_session, tmp_path):
    _ready_content(db_session, tmp_path, video_id="sum0000001", size_bytes=1, stored_size=1000)
    _ready_content(db_session, tmp_path, video_id="sum0000002", size_bytes=1, stored_size=2000)
    # A not-downloaded row must not be counted by either path.
    artist = db_session.query(Artist).filter(Artist.user_id == USER_ID).first()
    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="notdownld1",
            title="Not downloaded", status="not_downloaded",
        )
    )
    db_session.commit()

    summary = usage_summary(db_session, USER_ID)
    full = collect_usage(db_session, USER_ID)

    assert summary.count == full.count == 2
    assert summary.total_bytes == full.total_bytes == 3000


def test_usage_summary_reads_a_backfilled_size_after_collect_usage_has_run(db_session, tmp_path):
    """usage_summary never writes, so a legacy NULL row counts only after collect_usage has backfilled it."""
    _ready_content(db_session, tmp_path, video_id="legacysum01", size_bytes=4096, stored_size=None)

    before_backfill = usage_summary(db_session, USER_ID)
    assert before_backfill.total_bytes == 0

    collect_usage(db_session, USER_ID)

    after_backfill = usage_summary(db_session, USER_ID)
    assert after_backfill.total_bytes == 4096


def test_clear_all_resets_the_stored_size_too(db_session, tmp_path):
    """A stale size would make the next collect_usage skip its backfill."""
    content, audio = _ready_content(
        db_session, tmp_path, video_id="cleared0001", size_bytes=64, stored_size=64
    )

    cleared = clear_all(db_session, USER_ID)

    assert cleared == 1
    assert not audio.exists()
    db_session.refresh(content)
    assert content.status == "not_downloaded"
    assert content.file_path is None
    assert content.file_size_bytes is None
    assert collect_usage(db_session, USER_ID).total_bytes == 0


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


def test_items_endpoint_carries_what_a_device_copy_needs(client, db_session, tmp_path):
    """A device copy must render with no server: title, artist, cover and duration."""
    content, _audio = _ready_content(
        db_session, tmp_path, video_id="items000001", size_bytes=7, stored_size=7
    )
    content.thumbnail_url = "/image-proxy?u=https%3A%2F%2Fexample.com%2Fc.jpg"
    content.duration_seconds = 212
    db_session.commit()

    res = client.get("/storage/items")

    assert res.status_code == 200
    (item,) = res.json()
    assert item["id"] == content.id
    assert item["title"] == "Track items000001"
    assert item["channel_title"] == "Storage Channel"
    assert item["size_bytes"] == 7
    assert item["thumbnail_url"] == "/image-proxy?u=https%3A%2F%2Fexample.com%2Fc.jpg"
    assert item["duration_seconds"] == 212


def test_items_endpoint_is_empty_with_nothing_downloaded(client, db_session):
    res = client.get("/storage/items")

    assert res.status_code == 200
    assert res.json() == []
