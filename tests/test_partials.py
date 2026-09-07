"""The fragment endpoints behind the in-place refresh (routers/partials.py).

The property that matters is that a fragment renders the *same* markup the
full page does for that region — a refresh that quietly disagreed with the
first render would reintroduce exactly the staleness fragments exist to
remove. These tests pin that down by rendering both and comparing.
"""

import re
from datetime import datetime, timedelta

from app.models import Artist, Content, User
from app.timeutil import utcnow

USER_ID = 1

# Must match conftest.py's own DEFAULT_USER_ID — duplicated rather than
# imported, same as test_content_api.py and test_profiles_api.py do: importing
# conftest as a module re-runs the env setup at its top, which points the
# settings singleton at a second temp directory and trips its own isolation
# guard.
DEFAULT_USER_ID = 1


def _other_user_feed(db_session) -> Artist:
    """A second profile with one channel, for the scoping tests below.

    A real `User` row rather than a made-up user_id: SQLite only enforces
    foreign keys when a connection asks it to, and the app now does (see
    app/database.py), so a artist pointing at a profile that doesn't exist is
    rejected rather than silently accepted.
    """
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
    ("/partials/downloads", ["downloads-body"]),
    ("/partials/storage-summary", ["settings-storage-desc"]),
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
            ),
        ]
    )
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
    """The whole point: swapping a fragment in must leave the page in the
    state a fresh load would have produced."""
    _seed(db_session)
    page = _normalize(client.get("/").text)

    for url, targets in FRAGMENTS:
        fragment = client.get(url).text
        for target in targets:
            body = _fragment_body(fragment, target)
            assert body, (url, target)
            assert body in page, f"{url} -> {target} is not what index.html renders"


def _shelf_row(html, row_id):
    """One shelf row's markup, from its id up to the next row's. Matched on
    ids rather than visible text because "Saved for later" is both a shelf
    title and every card's save-button label."""
    start = html.index(f'id="{row_id}"')
    rest = html[start:]
    following = re.search(r'id="home-[a-z-]+-row"', rest[1:])
    return rest[: following.start() + 1] if following else rest


def test_home_fragment_reflects_a_change_without_a_page_reload(client, db_session):
    """Favoriting has to show up in the next fragment render — this is the
    behaviour that replaced hand-patching the shelf."""
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
    assert "1 song</span>" in before  # one favorite

    client.post(f"/content/{content.id}/favorite")

    after = _fragment_body(client.get("/partials/library").text, "library-grid")
    assert "2 songs</span>" in after


def test_new_releases_is_the_first_pinned_tile(client, db_session):
    """Of the three pinned tiles it is the only one whose contents change
    without the user doing anything — Favorites and Recently Played are both
    records of something they just did. So it goes first: it is the tile
    that answers the question the Library is opened with.

    Asserted on positions rather than eyeballed, because the three are
    interchangeable-looking blocks of markup and a later edit that adds a
    fourth tile has no other reason to notice which one leads.
    """
    _seed(db_session)

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert (
        body.index('data-detail-kind="new-uploads"')
        < body.index('data-detail-kind="favorites"')
        < body.index('data-detail-kind="recently-played"')
    )


def test_an_artists_library_card_opens_their_profile(client, db_session):
    """The card stands for the artist, so it opens the artist — albums,
    singles, what they just released — rather than the track list of
    whatever has synced so far. Nothing in home/library.js decides this: the
    card already carries which kind it opens."""
    _seed(db_session)
    artist = db_session.query(Artist).first()
    artist.browse_id = "UC5ZkRnYd3__WBBGnAnWO9Cg"
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert 'data-detail-kind="yt-artist"' in body
    assert 'data-detail-id="UC5ZkRnYd3__WBBGnAnWO9Cg"' in body
    assert 'href="/#yt-artist/UC5ZkRnYd3__WBBGnAnWO9Cg"' in body


def test_an_artists_card_prefers_its_own_synced_track_count(client, db_session):
    """_seed's artist has three real (non-preview) Content rows, so the card
    shows those rather than falling back to a release count it has none of
    — see page_context.library_context's artist_track_counts/
    artist_release_counts split."""
    _seed(db_session)

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "3 tracks</span>" in body


def test_a_followed_artists_card_falls_back_to_its_release_count(client, db_session):
    """The common case: following only starts recording releases from here
    on (see services/artist_sync.py), so most cards have no synced track of
    their own to show — nothing here reads as "0 songs" any more, since
    the release snapshot every followed artist already carries is worth
    more than a count that's almost always zero."""
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
    # Scoped to the artist's own card, not the four pinned playlist tiles
    # above it (Favorites/Saved/New releases/Recently played), which say
    # "N songs" and are unrelated to this artist.
    card = body[body.index('data-detail-id="UCreleasefallback0000000"') :]
    assert "0 song" not in card[: card.index("</a>")]


def test_a_followed_artist_with_neither_count_just_says_following(client, db_session):
    """Nothing synced, and no release snapshot yet either (still between
    following and its first sync landing) — "Following" beats a card that
    claims to know a count of anything."""
    artist = Artist(
        user_id=USER_ID, channel_id="UCnocounteither00000000", name="Freshly Followed"
    )
    db_session.add(artist)
    db_session.commit()

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "Following</span>" in body


def test_a_followed_artists_card_prefers_monthly_listeners(client, db_session):
    """The top of the fallback chain: YouTube Music's own count outranks
    both the synced-track count and the release-count fallback, since it's
    the one figure that's almost always meaningful and never reads as 0."""
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


def test_downloads_fragment_reports_stored_sizes(client, db_session):
    """The modal's own list (full collect_usage) and the Settings summary
    line (the cheaper usage_summary) are two separate fragments now — see
    page_context.storage_summary_context — but they still have to agree,
    same as before the split."""
    _seed(db_session)

    downloads_fragment = client.get("/partials/downloads").text
    summary_fragment = client.get("/partials/storage-summary").text

    assert "3.0 MB" in _fragment_body(downloads_fragment, "downloads-body")
    assert _fragment_body(summary_fragment, "settings-storage-desc") == "3.0 MB across 1 item"


def test_fragments_are_empty_but_valid_for_a_fresh_profile(client):
    """No seeded content at all — the empty branches still have to render,
    since that's what a refresh after clearing everything produces."""
    for url, targets in FRAGMENTS:
        res = client.get(url)
        assert res.status_code == 200, url
        for target in targets:
            assert f'data-target="{target}"' in res.text

    # A fresh profile's first run is the interests overlay, which lives in
    # the page rather than in this fragment — deliberately, so that a refresh
    # landing mid-onboarding can't replace the thing being filled in. What
    # this fragment has to say at that point is the empty branch behind it.
    assert "Nothing played yet" in client.get("/partials/home").text
    assert "Nothing downloaded yet" in client.get("/partials/downloads").text


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

    # new-uploads is deliberately absent: it is the one pinned kind that
    # isn't a track list any more — it holds releases off
    # Artist.release_snapshot, and has its own tests below.
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
    """Each pinned playlist explains itself when it has nothing in it: what
    belongs there, how to put something there, and a way to go do that. The
    strings come from PLAYLIST_KINDS, so this checks all three parts arrive
    rather than re-spelling every sentence."""
    for kind, title in [
        ("favorites", "Songs you like live here"),
        ("new-uploads", "No new releases yet"),
        ("recently-played", "Nothing played yet"),
    ]:
        # All three still explain themselves when empty, including the
        # releases grid — see _releases_panel.html's else branch.
        res = client.get(f"/partials/detail/playlist/{kind}")
        assert res.status_code == 200, kind
        assert title in res.text, kind
        assert 'class="empty-state-help"' in res.text, kind
        assert 'href="/#explore"' in res.text, kind
        # Nothing to play — a Play button on an empty list is a control that
        # can only ever do nothing.
        assert 'id="detail-play-all"' not in res.text, kind


def test_queue_fragment_renders_the_ids_in_the_order_it_was_given(client, db_session):
    """The queue's order is the client's, not the database's — a shuffled
    queue is a permutation the server never computed — so the rows come back
    in exactly the order asked for rather than in any query order."""
    _seed(db_session)
    rows = {c.video_id: c.id for c in db_session.query(Content).all()}
    wanted = [rows["partplay001"], rows["partnew0001"], rows["partfav0001"]]

    body = _fragment_body(
        client.get(f"/partials/queue?ids={','.join(str(i) for i in wanted)}").text,
        "queue-panel-body",
    )

    positions = [body.index(f'data-content-id="{content_id}"') for content_id in wanted]
    assert positions == sorted(positions)
    # Every id asked for, including the one being played. The panel used to be
    # handed only the tracks ahead of the pointer, which meant picking a row
    # rebuilt the list without the ones above it; which row is current is
    # marked in the browser now, so nothing here says so.
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
    """The ids come from the client's own sessionStorage. A stale or
    half-written record should cost the panel a row, not fail the request and
    leave it blank."""
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
    """A playlist with a few thousand tracks runs past a thousand
    pages at DEFAULT_PAGE_SIZE — showing every page number would be its own
    scroll-forever problem, so only current ± 2 plus the first/last page (with
    an ellipsis for the gap) actually render as links. _seed() itself adds
    one favorite ("partfav0001"), so 499 more makes an even 500 — exactly 10
    pages at DEFAULT_PAGE_SIZE=50."""
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
    # First (1) and last (10) page always render, each with its own ellipsis
    # since there's a real gap on both sides of the 3-7 window.
    assert 'page=1">1</a>' in res.text
    assert 'page=10">10</a>' in res.text
    assert res.text.count('class="pagination-ellipsis"') == 2
    # Page 2 and page 8 are inside neither the window nor the first/last
    # pins, so they must not appear as their own link.
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
    """Another profile's content must never leak into a fragment — the
    fragment endpoints resolve the profile the same way the page does."""
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
    """It used to be a track list of is_new_upload rows. Those only ever held
    releases that appeared *after* the follow and expired after fourteen
    days; releases read off the snapshot have neither limit."""
    _followed_with_releases(db_session, "Alpha", ("MPREb_a1", "Alpha Album", "Album", None))

    body = _fragment_body(client.get("/partials/detail/playlist/new-uploads").text, "detail-panel")

    assert 'class="mood-grid"' in body
    assert 'data-release-id="MPREb_a1"' in body
    assert "Alpha Album" in body
    assert 'class="track-list"' not in body


def test_the_releases_panel_has_no_play_all(client, db_session):
    """Deliberate, not missing: a release carries no video ids until it is
    opened, so "play all of these" would be one live request per release.
    Each release has its own Play all once opened."""
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
    """A count that disagrees with the page it opens onto is worse than no
    count — the same rule library_context's docstring already states."""
    _followed_with_releases(
        db_session,
        "Alpha",
        ("MPREb_a1", "One", "Album", None),
        ("MPREb_a2", "Two", "Single", None),
    )

    body = _fragment_body(client.get("/partials/library").text, "library-grid")

    assert "2 releases</span>" in body
