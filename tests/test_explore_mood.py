"""A mood's playlists (GET /partials/detail/yt-mood/{slug}); YouTube Music calls are faked."""

import pytest

from app.services import remote_detail
from app.youtube.models import PlaylistSearchResult
from app.youtube.music import MoodCategory
from app.youtube.urls import mood_slug

PARAMS = "ggMPOg1uX1JOQWZFeDByc2Jm"
OTHER_PARAMS = "zzzzzzzzzzzzzzzzzzzzzzzz"


def _playlist(playlist_id, title="A Playlist"):
    return PlaylistSearchResult(
        playlist_id=playlist_id, title=title, thumbnail_url=None, channel_title="YouTube Music"
    )


def _category(title, params):
    return MoodCategory(title=title, params=params, section="Moods & moments", slug=mood_slug(title))


@pytest.fixture(autouse=True)
def _forget_the_mood_menu(monkeypatch):
    """The menu is remembered module-wide; a test must not see the previous one's."""
    monkeypatch.setattr(remote_detail, "_mood_categories", None)


@pytest.fixture
def fake_mood(monkeypatch):
    """Records playlist fetches (by params) and category lookups."""
    calls = {"playlists": [], "categories": 0}

    def fetch_playlists(params):
        calls["playlists"].append(params)
        return [_playlist("aaaaaaaaaaaaaaaaaaaaaaa"), _playlist("bbbbbbbbbbbbbbbbbbbbbbb")]

    def fetch_categories():
        calls["categories"] += 1
        return [_category("Other", OTHER_PARAMS), _category("Feel good", PARAMS)]

    monkeypatch.setattr("app.services.remote_detail.fetch_mood_playlists", fetch_playlists)
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_categories", fetch_categories)
    return calls


@pytest.mark.parametrize(
    ("title", "slug"),
    [("Feel good", "feel-good"), ("Energy Booster", "energy-booster"), ("R&B & soul", "r-b-soul"), ("Café", "cafe")],
)
def test_a_mood_title_becomes_a_readable_url_name(title, slug):
    assert mood_slug(title) == slug


def test_a_mood_panel_renders_its_playlists(client, fake_mood):
    res = client.get("/partials/detail/yt-mood/feel-good")

    assert res.status_code == 200
    assert "Feel good" in res.text
    assert 'data-playlist-id="aaaaaaaaaaaaaaaaaaaaaaa"' in res.text
    assert fake_mood["playlists"] == [PARAMS]


def test_a_mood_card_does_not_name_the_curator(client, fake_mood):
    """Nearly every mood playlist is by "YouTube Music", so the line said nothing."""
    text = client.get("/partials/detail/yt-mood/feel-good").text

    assert "YouTube Music" not in text
    assert "card-channel" not in text


def test_the_mood_menu_is_remembered_between_opens(client, fake_mood):
    client.get("/partials/detail/yt-mood/feel-good")
    client.get("/partials/detail/yt-mood/other")

    assert fake_mood["categories"] == 1
    assert fake_mood["playlists"] == [PARAMS, OTHER_PARAMS]


def test_a_slug_missing_from_the_remembered_menu_refetches_it(client, fake_mood, monkeypatch):
    """A mood YouTube added since the menu was remembered must still open."""
    monkeypatch.setattr(
        remote_detail, "_mood_categories", (remote_detail.utcnow(), [_category("Other", OTHER_PARAMS)])
    )

    assert client.get("/partials/detail/yt-mood/feel-good").status_code == 200
    assert fake_mood["categories"] == 1


def test_a_mood_panel_has_no_hero_or_play_all(client, fake_mood):
    text = client.get("/partials/detail/yt-mood/feel-good").text

    assert "detail-play-all" not in text
    assert "channel-hero-avatar" not in text


@pytest.mark.parametrize("slug", ["Has-Caps", "under_score", "-leading", "a--b", "x" * 65])
def test_a_non_slug_is_rejected_without_being_fetched(client, fake_mood, slug):
    assert client.get(f"/partials/detail/yt-mood/{slug}").status_code == 404
    assert fake_mood == {"playlists": [], "categories": 0}


def test_an_empty_playlist_list_is_a_404(client, fake_mood, monkeypatch):
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_playlists", lambda params: [])

    assert client.get("/partials/detail/yt-mood/feel-good").status_code == 404


def test_a_slug_matching_no_known_category_is_a_404(client, fake_mood):
    assert client.get("/partials/detail/yt-mood/sad").status_code == 404
    assert fake_mood["playlists"] == []


def test_a_failed_menu_fetch_is_not_remembered(client, monkeypatch):
    monkeypatch.setattr("app.services.remote_detail.fetch_mood_categories", lambda: [])

    assert client.get("/partials/detail/yt-mood/feel-good").status_code == 404
    assert remote_detail._mood_categories is None


def test_yt_mood_route_requires_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.get("/partials/detail/yt-mood/feel-good", follow_redirects=False)

    assert res.status_code == 303


def test_a_moods_playlists_render_as_a_grid_not_a_slider(client, fake_mood):
    """.shelf-row is what drag-to-scroll binds to, so the grid needs a different class, not just style."""
    text = client.get("/partials/detail/yt-mood/feel-good").text

    assert 'class="mood-grid"' in text
    assert 'class="shelf-row"' not in text
