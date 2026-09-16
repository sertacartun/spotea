from datetime import datetime, timedelta

from app.content_query import query_content_page
from app.models import Artist, Content

USER_ID = 1


def _seed(db_session, *, channels=("Alpha Channel", "Beta Channel"), count=25):
    """`count` items alternating across `channels`, newest (index 0) first."""
    artists = [
        Artist(user_id=USER_ID, channel_id=f"https://example.com/artist{i}", name=title)
        for i, title in enumerate(channels)
    ]
    db_session.add_all(artists)
    db_session.commit()
    for f in artists:
        db_session.refresh(f)

    now = datetime(2026, 1, 1)
    items = []
    for i in range(count):
        item = Content(
            artist_id=artists[i % len(artists)].id,
            user_id=USER_ID,
            video_id=f"vid{i:04d}"[:11],
            title=f"Title {count - i:03d}",
            published_at=now - timedelta(days=i),
            is_favorite=(i % 5 == 0),
            status="ready" if i < 3 else "not_downloaded",
        )
        items.append(item)
    db_session.add_all(items)
    db_session.commit()
    return artists, items


def test_default_pagination_is_newest_first_50_per_page(db_session):
    _seed(db_session, count=55)

    items, page, total_pages = query_content_page(db_session, USER_ID)

    assert page == 1
    assert total_pages == 2
    assert len(items) == 50
    assert items[0].title == "Title 055"
    assert items[-1].title == "Title 006"


def test_second_page_has_the_remainder(db_session):
    _seed(db_session, count=55)

    items, page, total_pages = query_content_page(db_session, USER_ID, page=2)

    assert page == 2
    assert total_pages == 2
    assert len(items) == 5
    assert items[0].title == "Title 005"


def test_page_zero_and_negative_clamp_to_first_page(db_session):
    _seed(db_session, count=55)

    for requested in (0, -5, -1):
        items, page, total_pages = query_content_page(db_session, USER_ID, page=requested)
        assert page == 1
        assert len(items) == 50


def test_page_past_the_end_clamps_to_last_page(db_session):
    _seed(db_session, count=55)

    items, page, total_pages = query_content_page(db_session, USER_ID, page=9999)

    assert page == total_pages == 2
    assert len(items) == 5


def test_empty_library_is_one_empty_page(db_session):
    items, page, total_pages = query_content_page(db_session, USER_ID)

    assert items == []
    assert page == 1
    assert total_pages == 1


def test_filter_favorites(db_session):
    _seed(db_session, count=25)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="__favorites__", page_size=100)

    assert total_pages == 1
    assert items  # at least one favorite (i % 5 == 0 in the seed)
    assert all(i.is_favorite for i in items)


def test_filter_by_channel_title(db_session):
    _seed(db_session, channels=("Alpha Channel", "Beta Channel"), count=10)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="Beta Channel", page_size=100)

    assert items
    assert all(i.artist.name == "Beta Channel" for i in items)


def test_filter_with_no_matches_is_a_valid_empty_page(db_session):
    _seed(db_session, count=5)

    items, page, total_pages = query_content_page(db_session, USER_ID, filter="Nonexistent Channel")

    assert items == []
    assert page == 1
    assert total_pages == 1


def test_only_returns_the_requesting_users_content(db_session):
    _seed(db_session, count=5)

    items, page, total_pages = query_content_page(db_session, USER_ID + 999)

    assert items == []


def test_filter_played_orders_by_last_played_at_not_published_at(db_session):
    artist = Artist(user_id=USER_ID, channel_id="https://example.com/played-artist", name="C")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    db_session.add_all(
        [
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id="oldpub0001", title="Published old, played last",
                published_at=datetime(2020, 1, 1), last_played_at=datetime(2026, 1, 2),
            ),
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id="newpub0001", title="Published new, played first",
                published_at=datetime(2026, 1, 1), last_played_at=datetime(2026, 1, 1),
            ),
        ]
    )
    db_session.commit()

    items, _, _ = query_content_page(db_session, USER_ID, filter="__played__", page_size=100)

    assert [i.title for i in items] == ["Published old, played last", "Published new, played first"]


def test_filter_played_includes_preview_content(db_session):
    """Same exception as the home shelf; every other filter still excludes previews."""
    artist = Artist(user_id=USER_ID, channel_id="https://example.com/preview-artist", name="C")
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    db_session.add(
        Content(
            artist_id=artist.id, user_id=USER_ID, video_id="previewvid1", title="Played preview",
            published_at=datetime(2026, 1, 1), last_played_at=datetime(2026, 1, 1), is_preview=True,
        )
    )
    db_session.commit()

    played_items, _, _ = query_content_page(db_session, USER_ID, filter="__played__", page_size=100)
    assert [i.title for i in played_items] == ["Played preview"]

    default_items, _, _ = query_content_page(db_session, USER_ID, page_size=100)
    assert default_items == []
