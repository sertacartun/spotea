"""A fragment must render the same markup the full page renders for that region."""

import re
from datetime import datetime, timedelta

from app.models import Artist, Content, OfflinePin, User
from app.timeutil import utcnow

USER_ID = 1

# Duplicated rather than imported: importing conftest re-runs its env setup.
DEFAULT_USER_ID = 1


def _other_user_feed(db_session) -> Artist:
    """A second profile with one channel, for the scoping tests below."""
    other_user = User(username="other3", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)

    artist = Artist(
        user_id=other_user.id,
        channel_id="https://example.com/other",
        name="Someone Else",
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)
    return artist

FRAGMENTS = [
    ("/partials/home", ["home-shelves"]),
    ("/partials/library", ["library-grid"]),
    (
        "/partials/storage-summary",
        ["settings-downloads-desc", "settings-downloads-actions", "settings-cache-desc", "settings-cache-actions"],
    ),
]


def _seed(db_session):
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCpartials000000000000000",
        name="Partial Channel",
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    now = utcnow()
    db_session.add_all(
        [
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id="partnew0001", title="Fresh Upload",
                published_at=now - timedelta(days=1), duration_seconds=300,
            ),
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id="partfav0001", title="A Favorite",
                published_at=datetime(2026, 1, 1), is_favorite=True,
                status="ready", file_path="/nonexistent.m4a", file_size_bytes=3 * 1024 * 1024,
            ),
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id="partplay001", title="Played Recently",
                published_at=datetime(2025, 12, 1), last_played_at=now,
                status="ready", file_path="/nonexistent2.m4a", file_size_bytes=1024 * 1024,
            ),
        ]
    )
    db_session.commit()
    # The favorite is a download (a downloaded list pins it); the played track is cache.
    favorite = db_session.query(Content).filter(Content.video_id == "partfav0001").one()
    db_session.add(OfflinePin(user_id=USER_ID, list_key="favorites", content_id=favorite.id))
    db_session.commit()
    return artist


def _normalize(html):
    return re.sub(r"\s+", " ", html).strip()


def _fragment_body(html, target):
    match = re.search(rf'<template data-target="{target}">(.*?)</template>', html, re.S)
    assert match, f"no <template data-target={target!r}> in fragment"
    return _normalize(match.group(1))


def test_every_fragment_declares_its_targets(client, db_session):
    _seed(db_session)
    for url, targets in FRAGMENTS:
        res = client.get(url)
        assert res.status_code == 200, url
        for target in targets:
            assert f'data-target="{target}"' in res.text, (url, target)


def test_fragments_match_what_the_full_page_renders(client, db_session):
    _seed(db_session)
    page = _normalize(client.get("/").text)

    for url, targets in FRAGMENTS:
        fragment = client.get(url).text
        for target in targets:
            body = _fragment_body(fragment, target)
            assert body, (url, target)
            assert body in page, f"{url} -> {target} is not what index.html renders"


def _shelf_row(html, row_id):
    """Matched on ids: "Saved for later" is both a shelf title and a button label."""
    start = html.index(f'id="{row_id}"')
    rest = html[start:]
    following = re.search(r'id="home-[a-z-]+-row"', rest[1:])
    return rest[: following.start() + 1] if following else rest


def test_home_fragment_reflects_a_change_without_a_page_reload(client, db_session):
    _seed(db_session)
    content = db_session.query(Content).filter(Content.video_id == "partnew0001").first()
    marker = f'data-content-id="{content.id}"'

    before = _fragment_body(client.get("/partials/home").text, "home-shelves")
    assert marker not in _shelf_row(before, "home-favorites-row")

    res = client.post(f"/content/{content.id}/favorite")
    assert res.status_code == 200

    after = _fragment_body(client.get("/partials/home").text, "home-shelves")
    assert marker in _shelf_row(after, "home-favorites-row")


def test_library_fragment_counts_follow_the_data(client, db_session):
    _seed(db_session)
    content = db_session.query(Content).filter(Content.video_id == "partnew0001").first()

    before = _fragment_body(client.get("/partials/library").text, "library-grid")
    assert "1 song</span>" in before

    client.post(f"/content/{content.id}/favorite")

    after = _fragment_body(client.get("/partials/library").text, "library-grid")
    assert "2 songs</span>" in after


def test_new_releases_is_the_first_pinned_tile(client, db_session):
    """New releases leads: it is the only pinned tile whose contents change on their own."""
    _seed(db_session)

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert (
        body.index('data-detail-kind="new-uploads"')
        < body.index('data-detail-kind="favorites"')
        < body.index('data-detail-kind="recently-played"')
    )


def test_an_artists_library_card_opens_their_profile(client, db_session):
    _seed(db_session)
    artist = db_session.query(Artist).first()
    artist.browse_id = "UC5ZkRnYd3__WBBGnAnWO9Cg"
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert 'data-detail-kind="yt-artist"' in body
    assert 'data-detail-id="UC5ZkRnYd3__WBBGnAnWO9Cg"' in body
    assert 'href="/#yt-artist/UC5ZkRnYd3__WBBGnAnWO9Cg"' in body


def test_an_artists_card_prefers_its_own_synced_track_count(client, db_session):
    """_seed's artist has three non-preview Content rows."""
    _seed(db_session)

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "3 tracks</span>" in body


def test_a_followed_artists_card_falls_back_to_its_release_count(client, db_session):
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCreleasefallback0000000",
        browse_id="UCreleasefallback0000000",
        name="No Synced Tracks Yet",
        release_snapshot='["MPREb_a", "MPREb_b"]',
    )
    db_session.add(artist)
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "2 releases</span>" in body
    # Scoped to the artist's card; the pinned playlist tiles also say "N songs".
    card = body[body.index('data-detail-id="UCreleasefallback0000000"') :]
    assert "0 song" not in card[: card.index("</a>")]


def test_a_followed_artist_with_neither_count_just_says_following(client, db_session):
    artist = Artist(
        user_id=USER_ID, channel_id="UCnocounteither00000000", name="Freshly Followed"
    )
    db_session.add(artist)
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "Following</span>" in body


def test_a_followed_artists_card_prefers_monthly_listeners(client, db_session):
    artist = Artist(
        user_id=USER_ID,
        channel_id="UCmonthlylisteners00000",
        name="Has A Real Following",
        monthly_listeners="1.91M",
        release_snapshot='["MPREb_a", "MPREb_b"]',
    )
    db_session.add(artist)
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "1.91M monthly listeners</span>" in body
    assert "2 releases</span>" not in body


def test_storage_fragment_splits_downloads_from_cache(client, db_session):
    _seed(db_session)

    fragment = client.get("/partials/storage-summary").text

    assert _fragment_body(fragment, "settings-downloads-desc") == "3.0 MB across 1 song"
    assert 'href="/storage/export"' in _fragment_body(fragment, "settings-downloads-actions")
    assert _fragment_body(fragment, "settings-cache-desc").startswith("1.0 MB across 1 song · ")
    assert 'id="clear-cache"' in _fragment_body(fragment, "settings-cache-actions")


def test_fragments_are_empty_but_valid_for_a_fresh_profile(client):
    for url, targets in FRAGMENTS:
        res = client.get(url)
        assert res.status_code == 200, url
        for target in targets:
            assert f'data-target="{target}"' in res.text

    # The interests overlay lives in the page, not this fragment, so this renders the empty branch.
    assert "Nothing played yet" in client.get("/partials/home").text
    # Nothing to export or clear.
    storage = client.get("/partials/storage-summary").text
    assert "/storage/export" not in storage
    assert "clear-cache" not in storage


def test_fragments_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for url, _targets in FRAGMENTS:
            res = anonymous.get(url, follow_redirects=False)
            assert res.status_code == 303, url
            assert res.headers["location"] == "/login", url


def test_playlist_detail_fragments(client, db_session):
    _seed(db_session)

    # new-uploads holds releases, not tracks, and has its own tests below.
    for kind, expected, unexpected in [
        ("favorites", "A Favorite", "Fresh Upload"),
        ("recently-played", "Played Recently", "Fresh Upload"),
    ]:
        res = client.get(f"/partials/detail/playlist/{kind}")
        assert res.status_code == 200, kind
        body = _fragment_body(res.text, "detail-panel")
        assert expected in body, kind
        assert unexpected not in body, kind


def test_detail_fragments_carry_the_play_all_controls(client, db_session):
    _seed(db_session)

    body = _fragment_body(client.get("/partials/detail/playlist/favorites").text, "detail-panel")

    assert 'id="detail-play-all"' in body
    assert 'id="detail-shuffle"' in body


def test_empty_playlist_detail_fragments_render_their_empty_state(client):
    for kind, title in [
        ("favorites", "Songs you like live here"),
        ("new-uploads", "No new releases yet"),
        ("recently-played", "Nothing played yet"),
    ]:
        res = client.get(f"/partials/detail/playlist/{kind}")
        assert res.status_code == 200, kind
        assert title in res.text, kind
        assert 'class="empty-state-help"' in res.text, kind
        assert 'href="/#explore"' in res.text, kind
        assert 'id="detail-play-all"' not in res.text, kind


def test_queue_fragment_renders_the_ids_in_the_order_it_was_given(client, db_session):
    """Rows come back in the client's order (e.g. shuffled), not query order."""
    _seed(db_session)
    rows = {c.video_id: c.id for c in db_session.query(Content).all()}
    wanted = [rows["partplay001"], rows["partnew0001"], rows["partfav0001"]]

    body = _fragment_body(
        client.get(f"/partials/queue?ids={','.join(str(i) for i in wanted)}").text,
        "queue-panel-body",
    )

    positions = [body.index(f'data-content-id="{content_id}"') for content_id in wanted]
    assert positions == sorted(positions)
    # Every id, including the current one; which row is current is marked client-side.
    assert body.count('class="track-row"') == len(wanted)
    assert "is-current" not in body


def test_queue_fragment_ignores_ids_that_arent_the_users(client, db_session):
    _seed(db_session)
    mine = db_session.query(Content).first()
    theirs = Content(
        artist_id=_other_user_feed(db_session).id,
        user_id=db_session.query(User).filter(User.username == "other3").first().id,
        video_id="notyours001",
        title="Not Yours",
    )
    db_session.add(theirs)
    db_session.commit()
    db_session.refresh(theirs)

    body = _fragment_body(
        client.get(f"/partials/queue?ids={mine.id},{theirs.id}").text, "queue-panel-body"
    )

    assert "Not Yours" not in body
    assert f'data-content-id="{mine.id}"' in body


def test_queue_fragment_survives_a_junk_id_list(client, db_session):
    """The ids come from sessionStorage; a stale one should cost a row, not the request."""
    _seed(db_session)
    real = db_session.query(Content).first().id

    for ids in ("", "abc", f"{real},abc,,999999"):
        res = client.get(f"/partials/queue?ids={ids}")
        assert res.status_code == 200, ids
        assert 'data-target="queue-panel-body"' in res.text, ids

    empty = _fragment_body(client.get("/partials/queue?ids=").text, "queue-panel-body")
    assert "Nothing queued" in empty


def test_playlist_detail_fragment_404s_for_an_unknown_kind(client):
    assert client.get("/partials/detail/playlist/bogus").status_code == 404


def test_detail_pagination(client, db_session):
    """DEFAULT_PAGE_SIZE=50 — same threshold as the fragment/page tests above."""
    artist = _seed(db_session)
    db_session.add_all(
        [
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id=f"bulkvid{i:04d}"[:11], title=f"Bulk {i}",
                published_at=datetime(2025, 6, 1) - timedelta(days=i), is_favorite=True,
            )
            for i in range(55)
        ]
    )
    db_session.commit()

    first_page = client.get("/partials/detail/playlist/favorites")
    assert 'aria-label="Pagination, page 1 of 2"' in first_page.text
    assert 'is-current" aria-current="page">1</span>' in first_page.text
    assert 'page=2">2</a>' in first_page.text

    second_page = client.get("/partials/detail/playlist/favorites?page=2")
    assert second_page.status_code == 200
    assert 'aria-label="Pagination, page 2 of 2"' in second_page.text
    assert 'is-current" aria-current="page">2</span>' in second_page.text


def test_pagination_numbered_links_are_windowed_around_the_current_page(client, db_session):
    """Only current ± 2 plus first/last render as links; _seed's favorite + 499 = 500 tracks = 10 pages."""
    artist = _seed(db_session)
    db_session.add_all(
        [
            Content(
                artist_id=artist.id, user_id=USER_ID, video_id=f"windowvi{i:03d}"[:11], title=f"Window {i}",
                published_at=datetime(2025, 6, 1) - timedelta(days=i), is_favorite=True,
            )
            for i in range(499)
        ]
    )
    db_session.commit()

    res = client.get("/partials/detail/playlist/favorites?page=5")
    assert res.status_code == 200
    assert 'aria-label="Pagination, page 5 of 10"' in res.text
    # Window is 3-7 (current ± 2): 3, 4, [5], 6, 7 all render as links/current.
    for page in (3, 4, 6, 7):
        assert f'page={page}">{page}</a>' in res.text
    assert 'is-current" aria-current="page">5</span>' in res.text
    # First and last always render, each with an ellipsis for the gap.
    assert 'page=1">1</a>' in res.text
    assert 'page=10">10</a>' in res.text
    assert res.text.count('class="pagination-ellipsis"') == 2
    assert 'page=2">2</a>' not in res.text
    assert 'page=8">8</a>' not in res.text


def test_detail_fragments_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        for url in ["/partials/detail/playlist/favorites"]:
            res = anonymous.get(url, follow_redirects=False)
            assert res.status_code == 303, url
            assert res.headers["location"] == "/login", url


def test_detail_fragments_are_scoped_to_the_current_profile(client, db_session):
    _seed(db_session)
    other = _other_user_feed(db_session)
    db_session.add(
        Content(
            artist_id=other.id,
            user_id=other.user_id,
            video_id="otherprof02",
            title="Not Yours",
            is_favorite=True,
        )
    )
    db_session.commit()

    assert "Not Yours" not in client.get("/partials/detail/playlist/favorites").text


def test_fragments_are_scoped_to_the_current_profile(client, db_session):
    _seed(db_session)
    other = _other_user_feed(db_session)
    db_session.add(
        Content(
            artist_id=other.id,
            user_id=other.user_id,
            video_id="otherprof01",
            title="Not Yours",
            is_favorite=True,
            last_played_at=utcnow(),
        )
    )
    db_session.commit()

    for url, _targets in FRAGMENTS:
        assert "Not Yours" not in client.get(url).text, url
        assert "Someone Else" not in client.get(url).text, url


# --- Library's "New releases" is a grid of releases, not a track list ------


def _followed_with_releases(db_session, name, *entries):
    import json as _json

    from app.timeutil import utcnow as _utcnow

    artist = Artist(
        user_id=USER_ID,
        channel_id=f"UC{name}".ljust(24, "0"),
        name=name,
        followed=True,
        release_snapshot=_json.dumps(
            [
                {
                    "browse_id": browse_id,
                    "title": title,
                    "year": year or str(_utcnow().year),
                    "kind": kind,
                    "cover_url": None,
                }
                for browse_id, title, kind, year in entries
            ]
        ),
    )
    db_session.add(artist)
    db_session.commit()
    return artist


def test_the_releases_panel_is_a_grid_of_release_cards(client, db_session):
    _followed_with_releases(db_session, "Alpha", ("MPREb_a1", "Alpha Album", "Album", None))

    body = _fragment_body(client.get("/partials/detail/playlist/new-uploads").text, "detail-panel")

    assert 'class="mood-grid"' in body
    assert 'data-release-id="MPREb_a1"' in body
    assert "Alpha Album" in body
    assert 'class="track-list"' not in body


def test_the_releases_panel_has_no_play_all(client, db_session):
    """A release has no video ids until opened, so play-all would cost a request per release."""
    _followed_with_releases(db_session, "Alpha", ("MPREb_a1", "Alpha Album", "Album", None))

    body = _fragment_body(client.get("/partials/detail/playlist/new-uploads").text, "detail-panel")

    assert 'id="detail-play-all"' not in body
    assert 'id="detail-shuffle"' not in body


def test_a_release_card_says_what_kind_it_is(client, db_session):
    _followed_with_releases(
        db_session,
        "Alpha",
        ("MPREb_a1", "An Album", "Album", None),
        ("MPREb_a2", "A Single", "Single", None),
    )

    body = _fragment_body(client.get("/partials/detail/playlist/new-uploads").text, "detail-panel")

    assert '<span class="badge-kind">Album</span>' in body
    assert '<span class="badge-kind">Single</span>' in body


def test_the_releases_panel_shows_this_year_only(client, db_session):
    _followed_with_releases(
        db_session,
        "Alpha",
        ("MPREb_now", "This Year", "Album", None),
        ("MPREb_old", "Years Ago", "Album", "2019"),
    )

    body = _fragment_body(client.get("/partials/detail/playlist/new-uploads").text, "detail-panel")

    assert "This Year" in body
    assert "Years Ago" not in body


def test_the_library_tile_counts_releases_not_songs(client, db_session):
    _followed_with_releases(
        db_session,
        "Alpha",
        ("MPREb_a1", "One", "Album", None),
        ("MPREb_a2", "Two", "Single", None),
    )

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "2 releases</span>" in body
