"""Disk cleanup: storage.py's sweep_orphans, sweep_startup_leftovers and sweep_stale_previews."""

import os
from datetime import timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.models import Artist, Content
from app.storage import (
    PREVIEW_RETENTION,
    STALE_EXPORT_AGE,
    reset_interrupted_downloads,
    sweep_orphans,
    sweep_stale_previews,
    sweep_startup_leftovers,
)
from app.timeutil import utcnow

USER_ID = 1


def _feed(db_session, channel_id, **kwargs):
    artist = Artist(user_id=USER_ID, channel_id=channel_id, name="Lifecycle Channel", **kwargs)
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist


def _feed_row_exists(db_session, artist_id) -> bool:
    """A fresh query: .get() raises ObjectDeletedError after a bulk delete in an expired session."""
    return db_session.query(Artist).filter(Artist.id == artist_id).first() is not None


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    """These tests count everything in each directory, so each test gets fresh ones."""
    storage_dir = tmp_path / "storage"
    thumbnails_dir = tmp_path / "thumbnails"
    avatars_dir = tmp_path / "avatars"
    for directory in (storage_dir, thumbnails_dir, avatars_dir):
        directory.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_dir", storage_dir)
    monkeypatch.setattr(settings, "thumbnails_dir", thumbnails_dir)
    monkeypatch.setattr(settings, "avatars_dir", avatars_dir)


def test_sweep_orphans_removes_audio_no_row_points_at(db_session):
    orphan = settings.storage_dir / "orphan0001.m4a"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_audio_a_row_still_references(db_session):
    artist = _feed(db_session, "https://example.com/orphan-audio-kept")
    referenced = settings.storage_dir / "kept00001.m4a"
    referenced.write_bytes(b"x")
    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="kept00001", title="Kept",
            status="ready", file_path=str(referenced),
        )
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert referenced.exists()


def test_sweep_orphans_keeps_audio_inside_a_user_directory(db_session):
    """Audio lives one level down; recursing without checking references would delete the library."""
    artist = _feed(db_session, "https://example.com/orphan-per-user-kept")
    user_dir = settings.storage_dir / str(USER_ID)
    user_dir.mkdir(parents=True, exist_ok=True)
    referenced = user_dir / "peruser001.m4a"
    referenced.write_bytes(b"x")
    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="peruser001", title="Kept",
            status="ready", file_path=str(referenced),
        )
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert referenced.exists()


def test_sweep_orphans_removes_a_stray_inside_a_user_directory(db_session):
    user_dir = settings.storage_dir / "42"
    user_dir.mkdir(parents=True, exist_ok=True)
    stray = user_dir / "strayfile1.m4a"
    stray.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not stray.exists()
    assert user_dir.exists(), "the directory itself is not a candidate"


def test_sweep_orphans_keeps_audio_a_row_spells_differently(db_session):
    """A relative file_path must still match an absolute storage_dir, or the sweep deletes the library."""
    artist = _feed(db_session, "https://example.com/orphan-audio-relative")
    referenced = settings.storage_dir / "spelled0001.m4a"
    referenced.write_bytes(b"x")
    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="spelled0001", title="Kept",
            status="ready", file_path=os.path.relpath(referenced, Path.cwd()),
        )
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert referenced.exists(), "deleted a file a row references under another spelling"


def test_sweep_orphans_removes_thumbnails_no_row_points_at(db_session):
    orphan = settings.thumbnails_dir / "orphanthumb1.jpg"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_a_thumbnail_any_row_still_references(db_session):
    """Keyed by video_id alone: a row just existing keeps its thumbnail."""
    artist = _feed(db_session, "https://example.com/orphan-thumb-kept")
    kept = settings.thumbnails_dir / "keptthumb01.jpg"
    kept.write_bytes(b"x")
    db_session.add(
        Content(artist_id=artist.id, user_id=USER_ID, video_id="keptthumb01", title="Kept")
    )
    db_session.commit()

    sweep_orphans(db_session)

    assert kept.exists()


def test_sweep_orphans_removes_avatars_no_feed_points_at(db_session):
    orphan = settings.avatars_dir / "UCorphanavatar00000000.jpg"
    orphan.write_bytes(b"x")

    sweep_orphans(db_session)

    assert not orphan.exists()


def test_sweep_orphans_keeps_an_avatar_a_followed_feed_references(db_session):
    channel_id = "UCkeptavatar000000000000"
    kept = settings.avatars_dir / f"{channel_id}.jpg"
    kept.write_bytes(b"x")
    _feed(
        db_session, channel_id,
        avatar_url=f"/avatars/{channel_id}.jpg",
    )

    sweep_orphans(db_session)

    assert kept.exists()


def test_sweep_orphans_leaves_a_fresh_export_temp_file_alone(db_session):
    """A live export could be mid-write (see STALE_EXPORT_AGE)."""
    fresh = settings.storage_dir / "abc123.export.tmp"
    fresh.write_bytes(b"x")

    sweep_orphans(db_session)

    assert fresh.exists()


def test_sweep_orphans_removes_an_abandoned_export_temp_file(db_session):
    stale = settings.storage_dir / "abandoned1.export.tmp"
    stale.write_bytes(b"x")
    old_time = (utcnow() - STALE_EXPORT_AGE - timedelta(minutes=5)).timestamp()
    os.utime(stale, (old_time, old_time))

    sweep_orphans(db_session)

    assert not stale.exists()


def test_sweep_orphans_never_touches_part_files(db_session):
    """A .part here may be an in-progress download; that cleanup runs at startup instead."""
    part = settings.storage_dir / "inprogress1.part"
    part.write_bytes(b"x")

    sweep_orphans(db_session)

    assert part.exists()


def test_sweep_startup_leftovers_removes_every_part_file(db_session):
    part_one = settings.storage_dir / "leftover001.part"
    part_two = settings.storage_dir / "leftover002.part"
    part_one.write_bytes(b"x")
    part_two.write_bytes(b"x")
    unrelated = settings.storage_dir / "real00001.m4a"
    unrelated.write_bytes(b"x")

    removed = sweep_startup_leftovers()

    assert removed == 2
    assert not part_one.exists()
    assert not part_two.exists()
    assert unrelated.exists()


def test_a_download_a_restart_cut_off_can_be_downloaded_again(db_session):
    """Left "downloading", every download path would skip the row for good."""
    artist = _feed(db_session, "UCrestart00000000000000")
    cut_off = Content(artist_id=artist.id, user_id=USER_ID, video_id="cutoff00001", title="t", status="downloading")
    done = Content(artist_id=artist.id, user_id=USER_ID, video_id="finished001", title="t", status="ready")
    db_session.add_all([cut_off, done])
    db_session.commit()

    assert reset_interrupted_downloads(db_session) == 1

    db_session.refresh(cut_off)
    db_session.refresh(done)
    assert cut_off.status == "not_downloaded"
    assert done.status == "ready"


def _preview(db_session, artist, video_id, *, age_days, **kwargs):
    defaults = {
        "is_preview": True,
        "added_at": utcnow() - timedelta(days=age_days),
    }
    defaults.update(kwargs)
    content = Content(artist_id=artist.id, user_id=USER_ID, video_id=video_id, title=video_id, **defaults)
    db_session.add(content)
    db_session.commit()
    return content


def test_an_old_untouched_preview_is_removed(db_session):
    artist = _feed(db_session, "https://example.com/stale-preview", followed=False)
    _preview(db_session, artist, "stalepreview1", age_days=PREVIEW_RETENTION.days + 1)

    removed = sweep_stale_previews(db_session)

    assert removed == 1
    assert db_session.query(Content).filter(Content.video_id == "stalepreview1").first() is None


def test_a_recent_preview_is_kept(db_session):
    artist = _feed(db_session, "https://example.com/fresh-preview", followed=False)
    _preview(db_session, artist, "freshpreview1", age_days=1)

    removed = sweep_stale_previews(db_session)

    assert removed == 0
    assert db_session.query(Content).filter(Content.video_id == "freshpreview1").first() is not None


def test_an_old_but_played_preview_is_kept(db_session):
    artist = _feed(db_session, "https://example.com/played-preview", followed=False)
    _preview(
        db_session, artist, "playedpreview1",
        age_days=PREVIEW_RETENTION.days + 1, last_played_at=utcnow(),
    )

    removed = sweep_stale_previews(db_session)

    assert removed == 0


def test_an_old_but_favorited_preview_is_kept(db_session):
    artist = _feed(db_session, "https://example.com/fav-preview", followed=False)
    _preview(db_session, artist, "favpreview001", age_days=PREVIEW_RETENTION.days + 1, is_favorite=True)

    removed = sweep_stale_previews(db_session)

    assert removed == 0


def test_an_old_but_downloaded_preview_is_kept(db_session, tmp_path):
    artist = _feed(db_session, "https://example.com/dl-preview", followed=False)
    audio = tmp_path / "dlpreview001.m4a"
    audio.write_bytes(b"x")
    _preview(
        db_session, artist, "dlpreview0001",
        age_days=PREVIEW_RETENTION.days + 1, status="ready", file_path=str(audio),
    )

    removed = sweep_stale_previews(db_session)

    assert removed == 0
    assert audio.exists()


def test_a_placeholder_feed_left_empty_by_the_sweep_is_also_removed(db_session):
    artist = _feed(db_session, "https://example.com/emptied-placeholder", followed=False)
    artist_id = artist.id  # captured before the sweep — see _feed_row_exists' docstring
    _preview(db_session, artist, "emptyplaceh1", age_days=PREVIEW_RETENTION.days + 1)

    sweep_stale_previews(db_session)

    assert not _feed_row_exists(db_session, artist_id)


def test_a_followed_feed_is_never_removed_even_if_emptied(db_session):
    """followed=True is a real subscription, not a placeholder."""
    artist = _feed(db_session, "https://example.com/followed-not-removed", followed=True)
    artist_id = artist.id
    _preview(db_session, artist, "followedpre1", age_days=PREVIEW_RETENTION.days + 1)

    sweep_stale_previews(db_session)

    assert _feed_row_exists(db_session, artist_id)


def test_a_placeholder_feed_with_other_content_left_is_not_removed(db_session):
    artist = _feed(db_session, "https://example.com/placeholder-not-empty", followed=False)
    artist_id = artist.id
    _preview(db_session, artist, "sweptaway001", age_days=PREVIEW_RETENTION.days + 1)
    db_session.add(
        Content(artist_id=artist.id, user_id=USER_ID, video_id="stayingsafe1", title="Stays", is_favorite=True)
    )
    db_session.commit()

    sweep_stale_previews(db_session)

    assert _feed_row_exists(db_session, artist_id)


def test_sweep_startup_leftovers_runs_during_app_startup(monkeypatch):
    """A fresh TestClient re-runs the lifespan, so no separate app instance is needed."""
    from fastapi.testclient import TestClient

    import app.main as main_module

    calls = []
    monkeypatch.setattr(main_module, "sweep_startup_leftovers", lambda: calls.append(1) or 0)

    with TestClient(main_module.app):
        pass

    assert calls == [1]


def test_audio_is_written_into_the_listeners_own_directory(monkeypatch, tmp_path):
    """A shared file let one account's delete remove the other's audio."""
    from app import downloader
    from app.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)

    assert downloader.user_storage_dir(7) == tmp_path / "7"
    # None is the flat layout older rows still name in their file_path.
    assert downloader.user_storage_dir(None) == tmp_path


def test_two_listeners_do_not_share_a_file(monkeypatch, tmp_path):
    from app import downloader
    from app.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)

    assert downloader.user_storage_dir(1) != downloader.user_storage_dir(2)


def test_the_startup_part_sweep_reaches_the_user_directories(monkeypatch, tmp_path):
    """.part files land beside the download, one level down."""
    from app import storage
    from app.config import settings

    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    (tmp_path / "3").mkdir()
    (tmp_path / "3" / "abc.part").write_bytes(b"x")
    (tmp_path / "flat.part").write_bytes(b"x")

    assert storage.sweep_startup_leftovers() == 2
    assert not list(tmp_path.rglob("*.part"))
