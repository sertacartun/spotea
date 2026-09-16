"""Shallow smoke tests that the server-rendered pages render (routers/pages.py)."""

from datetime import datetime, timedelta

from app.interests import ONBOARDING_MIN_INTERESTS
from app.models import Artist, Content
from app.timeutil import utcnow

USER_ID = 1


def _seed(db_session, *, followed=True):
    """One followed channel with a new upload, a favorite, and a played+downloaded item."""
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCpagetest00000000000000",
        name="Page Test Channel",
        followed=followed,
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    now = utcnow()
    items = [
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id="newupload01",
            title="Fresh Upload",
            published_at=now - timedelta(days=1),
            duration_seconds=305,
        ),
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id="favorite001",
            title="A Favorite",
            published_at=datetime(2026, 1, 1),
            duration_seconds=61,
            is_favorite=True,
        ),
        Content(
            artist_id=artist.id,
            user_id=USER_ID,
            video_id="playedone1",
            title="Played And Downloaded",
            published_at=datetime(2025, 12, 1),
            last_played_at=now,
            status="ready",
            file_path="/nonexistent/playedone1.m4a",
        ),
    ]
    db_session.add_all(items)
    db_session.commit()
    return artist, items


def test_home_renders_every_shelf_and_the_library_grid(client, db_session):
    _seed(db_session)

    res = client.get("/")

    assert res.status_code == 200
    body = res.text
    # Not "Fresh Upload": Home's New releases shelf reads Artist.release_snapshot, not Content rows.
    assert "A Favorite" in body
    assert "Played And Downloaded" in body
    assert "Page Test Channel" in body


def test_a_brand_new_library_opens_on_the_interests_overlay(client):
    res = client.get("/")

    assert res.status_code == 200
    overlay = res.text[res.text.index('id="interests-overlay"') :][:300]
    assert "hidden" not in overlay
    assert 'data-required="true"' in overlay
    assert "What do you listen to?" in res.text
    assert 'data-genre="Rock"' in res.text


def test_the_first_run_cannot_be_skipped(client):
    """There is no Skip: the only way past the first run is to pick something."""
    body = client.get("/").text

    assert 'id="onboarding-skip"' not in body
    assert ">Skip<" not in body
    # The floor is handed to the client rather than duplicated (interests.ONBOARDING_MIN_INTERESTS).
    assert f'data-min-interests="{ONBOARDING_MIN_INTERESTS}"' in body


def test_a_library_that_has_been_started_is_not_asked_again(client, db_session):
    """The overlay stays in the page (Settings reuses it) but starts hidden and unlocked."""
    from app.models import User

    db_session.query(User).filter(User.id == 1).update({"interests": "rock"})
    db_session.commit()

    body = client.get("/").text

    overlay = body[body.index('id="interests-overlay"') :][:300]
    assert "hidden" in overlay
    assert "data-required" not in overlay
    assert "Nothing played yet" in body

def test_channel_avatars_are_lazy_loaded(client, db_session):
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCavatarlazy00000000000",
        browse_id="UCavatarlazy00000000000",
        name="Avatar Lazy Artist",
        avatar_url="/avatars/UCavatarlazy00000000000.jpg",
    )
    db_session.add(artist)
    db_session.commit()

    body = client.get("/").text

    assert '<img class="channel-chip-avatar" src="/avatars/UCavatarlazy00000000000.jpg" alt="" loading="lazy" />' in body
    assert '<img class="channel-card-avatar" src="/avatars/UCavatarlazy00000000000.jpg" alt="" loading="lazy" />' in body


def test_the_duration_and_filesize_template_filters_are_registered(client, db_session):
    _seed(db_session)

    assert "MB" in client.get("/").text  # filesize, from the storage summary

    # Favorites, not New releases: releases have no duration to render.
    rows = client.get("/partials/detail/playlist/favorites").text
    assert "1:01" in rows  # duration, from duration_seconds=61


def test_channel_and_playlist_pages_redirect_to_their_hash_route(client):
    """These routes only exist so old links and bookmarks still land somewhere."""
    for path, expected_hash in [
        ("/favorites", "/#favorites"),
        ("/new-uploads", "/#new-uploads"),
        ("/recently-played", "/#recently-played"),
        ("/player/1", "/#player/1"),
    ]:
        res = client.get(path, follow_redirects=False)
        assert res.status_code == 307, path
        assert res.headers["location"] == expected_hash, path


def test_page_routes_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for path in ["/", "/favorites", "/new-uploads", "/recently-played", "/player/1"]:
            res = anonymous.get(path, follow_redirects=False)
            assert res.status_code == 303, path
            assert res.headers["location"] == "/login", path


def test_the_player_overlay_renders_every_element_its_script_binds(client):
    """The player scripts bind these ids without null checks; a missing one blanks the app."""
    body = client.get("/").text

    for element_id in [
        "player-overlay",
        "player-root",
        "player-art-img",
        "queue-panel",
        "queue-panel-body",
        "lyrics-panel-body",
        "panel-tab-queue",
        "panel-tab-lyrics",
        "queue-toggle",
        "overlay-collapse-btn",
        "mini-player",
        "mini-player-progress",
        "mini-player-playpause",
    ]:
        assert f'id="{element_id}"' in body, element_id


def test_the_player_card_wraps_its_column_and_its_panel(client):
    """The desktop two-column layout needs .player-main and #queue-panel side by side in #player-root."""
    body = client.get("/").text

    card = body.index('id="player-root"')
    main = body.index('class="player-main"')
    panel = body.index('id="queue-panel"')
    assert card < main < panel
