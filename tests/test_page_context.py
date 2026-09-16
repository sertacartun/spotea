"""Counts must agree with the list they describe: previews never inflate them."""

import json

from app.content_query import count_content
from app.models import Artist, Content, User
from app.page_context import home_context, library_context, playlist_detail_context
from app.timeutil import utcnow

USER_ID = 1


def _artist(db_session, channel_id: str, **kwargs) -> Artist:
    artist = Artist(user_id=USER_ID, channel_id=channel_id, name="Preview Test Artist", **kwargs)
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist


def _content(artist: Artist, video_id: str, **kwargs) -> Content:
    return Content(artist_id=artist.id, user_id=USER_ID, video_id=video_id, title=video_id, **kwargs)


def test_count_content_excludes_previews_like_the_page_it_describes(db_session):
    artist = _artist(db_session, "https://example.com/count-previews")
    db_session.add_all(
        [
            _content(artist, "realvideo01", is_preview=False),
            _content(artist, "realvideo02", is_preview=False),
            _content(artist, "previewvid1", is_preview=True),
        ]
    )
    db_session.commit()

    assert count_content(db_session, USER_ID, artist_id=artist.id) == 2


def test_count_content_played_filter_keeps_previews_like_the_page_it_describes(db_session):
    """A preview that has been played still belongs on Recently Played, so its count keeps it."""
    artist = _artist(db_session, "https://example.com/count-played-preview")
    db_session.add(_content(artist, "playedpreview01", is_preview=True, last_played_at=utcnow()))
    db_session.commit()

    assert count_content(db_session, USER_ID, filter="__played__") == 1


def test_the_library_tile_count_excludes_previews(db_session):
    artist = _artist(db_session, "https://example.com/library-tile-count")
    db_session.add_all(
        [
            _content(artist, "librarytile01", is_preview=False),
            _content(artist, "librarypreview1", is_preview=True),
        ]
    )
    db_session.commit()

    assert library_context(db_session, USER_ID)["artist_track_counts"][artist.id] == 1


def test_the_library_tile_falls_back_to_the_release_count(db_session):
    """Following doesn't import the back catalogue, so the tile falls back to the release snapshot."""
    artist = _artist(
        db_session,
        "https://example.com/release-count-fallback",
        release_snapshot='["MPREb_1", "MPREb_2", "MPREb_3"]',
    )

    counts = library_context(db_session, USER_ID)

    assert counts["artist_track_counts"].get(artist.id, 0) == 0
    assert counts["artist_release_counts"][artist.id] == 3


def test_the_library_tile_release_count_survives_no_snapshot_yet(db_session):
    artist = _artist(db_session, "https://example.com/no-snapshot-yet")

    assert library_context(db_session, USER_ID)["artist_release_counts"][artist.id] == 0


def test_the_library_tile_release_count_survives_a_malformed_snapshot(db_session):
    artist = _artist(
        db_session, "https://example.com/bad-snapshot", release_snapshot="not json"
    )

    assert library_context(db_session, USER_ID)["artist_release_counts"][artist.id] == 0


def test_favorites_playlist_count_excludes_previews(db_session):
    artist = _artist(db_session, "https://example.com/favorites-preview-count")
    db_session.add_all(
        [
            _content(artist, "favreal0001", is_favorite=True, is_preview=False),
            _content(artist, "favpreview01", is_favorite=True, is_preview=True),
        ]
    )
    db_session.commit()

    context = playlist_detail_context(db_session, USER_ID, "favorites", page=1)

    assert context["video_count"] == 1
    assert context["video_count"] == len(context["content"])


# Home's "New releases" shelf, read off Artist.release_snapshot.


THIS_YEAR = str(utcnow().year)


def _release_entry(browse_id, title, year=THIS_YEAR):
    """Defaults to this year, because that is the only year the shelf shows."""
    return {
        "browse_id": browse_id,
        "title": title,
        "year": year,
        "kind": "Single",
        "cover_url": None,
    }


def _followed_with_releases(db_session, name, *entries, user_id=USER_ID):
    artist = Artist(
        user_id=user_id,
        channel_id=f"UC{name}".ljust(24, "0"),
        name=name,
        followed=True,
        release_snapshot=json.dumps(list(entries)),
    )
    db_session.add(artist)
    db_session.commit()
    return artist


def test_the_shelf_reads_releases_off_the_snapshot(db_session):
    _followed_with_releases(db_session, "Alpha", _release_entry("MPREb_a1", "Alpha Single"))

    shelf = home_context(db_session, USER_ID)["home_new_releases"]

    assert [r["title"] for r in shelf] == ["Alpha Single"]
    assert shelf[0]["artist_name"] == "Alpha"


def test_only_this_years_releases_are_shown(db_session):
    """YouTube Music only publishes a release year, so "new" means this year."""
    _followed_with_releases(
        db_session,
        "Alpha",
        _release_entry("MPREb_old", "Old One", year="2019"),
        _release_entry("MPREb_new", "New One"),
    )

    shelf = home_context(db_session, USER_ID)["home_new_releases"]

    assert [r["title"] for r in shelf] == ["New One"]


def test_a_release_with_no_year_is_not_guessed_at(db_session):
    _followed_with_releases(db_session, "Alpha", _release_entry("MPREb_a1", "Undated", year=None))

    assert home_context(db_session, USER_ID)["home_new_releases"] == []


def test_one_prolific_artist_does_not_fill_the_shelf(db_session):
    _followed_with_releases(
        db_session, "Alpha", *[_release_entry(f"MPREb_a{i}", f"Alpha {i}") for i in range(20)]
    )
    _followed_with_releases(
        db_session, "Beta", *[_release_entry(f"MPREb_b{i}", f"Beta {i}") for i in range(20)]
    )

    shelf = home_context(db_session, USER_ID)["home_new_releases"]

    assert any(r["title"].startswith("Beta") for r in shelf), "the second artist got no slot"


def test_an_unfollowed_artists_releases_are_not_shown(db_session):
    artist = _followed_with_releases(db_session, "Gone", _release_entry("MPREb_g1", "Gone Single"))
    artist.followed = False
    db_session.commit()

    assert home_context(db_session, USER_ID)["home_new_releases"] == []


def test_an_old_bare_id_snapshot_renders_nothing_rather_than_crashing(db_session):
    """An old bare-id snapshot has no title to show."""
    artist = _followed_with_releases(db_session, "Alpha")
    artist.release_snapshot = '["MPREb_a1", "MPREb_a2"]'
    db_session.commit()

    assert home_context(db_session, USER_ID)["home_new_releases"] == []


def test_another_users_releases_stay_out(db_session):
    other = User(username="releases-other", password_hash="x")
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)
    _followed_with_releases(db_session, "Theirs", _release_entry("MPREb_t1", "Theirs"), user_id=other.id)

    assert home_context(db_session, USER_ID)["home_new_releases"] == []
