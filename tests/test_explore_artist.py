"""The artist detail panel: the two things it exists to get right, and the
fallback that lets every channel result go through it.

Getting right: following the artist's *official* channel rather than the
auto-generated "<Artist> - Topic" one every music result is attributed to,
and channel search no longer offering those Topic channels to follow at
all.

The fallback: this panel is what a channel card opens now, so an id that
isn't an artist — or is one with nothing playable — has to come back as the
plain channel listing rather than a 404. Both sources are monkeypatched
out.
"""

import pytest

from app.models import Artist
from app.services import remote_detail
from app.timeutil import utcnow
from app.youtube.models import VideoSearchResult
from app.youtube.music import ArtistProfile, ArtistRelease, ReleaseDetail

USER_ID = 1
BROWSE_ID = "UCNaGLJRPE3ohleIDM7RFtlQ"
OFFICIAL_ID = "UC6OI7Crv96jgra5pwJNDFRQ"
TOPIC_ID = "UCDdTH-sn8qG64wK5ChFDQ4Q"


def _track(video_id="_efHZg9D9iE"):
    return VideoSearchResult(
        video_id=video_id,
        title="Biliyorsun",
        thumbnail_url="https://yt3.ggpht.com/abc=w544-h544-l90-rj",
        duration_seconds=317,
        channel_title="Sezen Aksu",
        channel_id=BROWSE_ID,
    )


def _release(title="Schlau aber blond", year="2025", kind="Album", browse_id="MPREb_HIQTwIoDtEM"):
    return ArtistRelease(
        browse_id=browse_id,
        title=title,
        year=year,
        kind=kind,
        cover_url="https://lh3.googleusercontent.com/c=w544-h544-l90-rj",
    )


def _profile(**overrides):
    fields = {
        "browse_id": BROWSE_ID,
        "channel_id": OFFICIAL_ID,
        "topic_channel_id": TOPIC_ID,
        "name": "Sezen Aksu",
        "description": "Turkish singer, songwriter and producer.",
        "subscriber_count": 3_190_000,
        "monthly_listeners": "28.9M",
        "avatar_url": "https://lh3.googleusercontent.com/x=w544-h544-p-l90-rj",
        "tracks": [_track()],
        "track_count": 1,
        "albums": [_release()],
        "singles": [_release("Gut Genug", "2026", "Single", "MPREb_5Y3mCZ5XtG3")],
        "related": [],
    }
    fields.update(overrides)
    return ArtistProfile(**fields)


@pytest.fixture
def fake_artist(monkeypatch):
    def install(profile):
        monkeypatch.setattr(remote_detail, "fetch_artist", lambda browse_id: profile)

    return install


def test_the_panel_lists_the_artists_tracks(client, fake_artist):
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.status_code == 200
    assert "Sezen Aksu" in res.text
    assert "Biliyorsun" in res.text
    assert "28.9M monthly listeners" in res.text


def test_a_capped_track_list_says_so(client, fake_artist):
    """An artist with 150 songs whose list reads "100 tracks" is claiming to
    be their whole catalogue. Same wording a truncated remote playlist
    uses."""
    fake_artist(_profile(tracks=[_track(f"_efHZg9D{n:03d}") for n in range(100)], track_count=157))

    res = client.get(f"/partials/detail/yt-artist-songs/{BROWSE_ID}")

    assert "First 100 of 157 tracks" in res.text


def test_an_uncapped_track_list_just_counts(client, fake_artist):
    fake_artist(_profile(tracks=[_track()], track_count=1))

    res = client.get(f"/partials/detail/yt-artist-songs/{BROWSE_ID}")

    assert "1 track" in res.text
    assert "First 1" not in res.text


def test_follow_targets_the_topic_channel(client, fake_artist):
    """What "follow an artist" means in a music app: the channel that
    carries their releases and nothing else, so a new single reaches Home
    while their vlogs don't. Their official channel is the wrong artist for
    this even though it's the right page to link to."""
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{TOPIC_ID}" in res.text
    assert f"https://www.youtube.com/channel/{OFFICIAL_ID}" not in res.text


def test_an_artist_with_no_topic_channel_falls_back(client, fake_artist):
    """A handful of artists have none. Following their official channel is a
    worse answer than the right one, and a better answer than a button that
    does nothing."""
    fake_artist(_profile(topic_channel_id=None))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{OFFICIAL_ID}" in res.text


def test_an_artist_with_no_channel_at_all_falls_back_to_the_browse_id(client, fake_artist):
    fake_artist(_profile(topic_channel_id=None, channel_id=None))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{BROWSE_ID}" in res.text


def test_an_already_followed_artist_points_at_the_library_copy(client, db_session, fake_artist):
    """Followed-ness is checked against the same channel the button follows
    — anything else would leave it saying "Follow" forever."""
    fake_artist(_profile())
    artist = Artist(
        user_id=USER_ID,
        channel_id=TOPIC_ID,
        name="Sezen Aksu",
        followed=True,
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert "Following" in res.text
    assert "unfollow-artist-btn" in res.text


def test_an_id_that_is_not_an_artist_is_a_404(client, fake_artist):
    """There is no channel listing to fall back to any more. Every id that
    reaches this route came out of an artist search, a chart entry or an
    artist's own page, so one YouTube Music can't answer for is genuinely
    nothing this app can show."""
    fake_artist(None)

    assert client.get(f"/partials/detail/yt-artist/{BROWSE_ID}").status_code == 404


def test_an_artist_with_nothing_playable_is_a_404_too(client, fake_artist):
    """YouTube Music knowing the name is not the same as it having tracks —
    a VEVO container is exactly that, and music._redirected_artist is what
    walks off it to the page that does. If that already ran and there is
    still nothing, there is nothing to show."""
    fake_artist(_profile(tracks=[]))

    assert client.get(f"/partials/detail/yt-artist/{BROWSE_ID}").status_code == 404


@pytest.mark.parametrize("browse_id", ["not-an-id", "PLcQNVKi2yvHREvYwLPBMWEAyuq4AERnrm", "UC"])
def test_a_non_channel_id_is_rejected_without_being_fetched(client, monkeypatch, browse_id):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not fetch an id that failed validation")

    monkeypatch.setattr(remote_detail, "fetch_artist", fail_if_called)

    assert client.get(f"/partials/detail/yt-artist/{browse_id}").status_code == 404


# --------------------------------------------------------------------------
# The profile itself: shelves rather than a track list, and the two views it
# hands off to.
# --------------------------------------------------------------------------


def test_the_profile_shows_the_releases_not_just_songs(client, fake_artist):
    """The whole point of the profile. A ranked list of 150 songs can't
    answer "what did they just put out" — it ranks by popularity, so a new
    single sits wherever it charts."""
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.status_code == 200
    assert "Albums" in res.text
    assert "Schlau aber blond" in res.text
    assert "Singles" in res.text
    assert "Gut Genug" in res.text
    # Opening a release goes by its browse id, which works for both kinds.
    assert 'data-release-id="MPREb_HIQTwIoDtEM"' in res.text


def test_an_empty_shelf_renders_nothing_at_all(client, fake_artist):
    """Not an empty heading — an artist with no albums shouldn't have a
    section telling them so."""
    fake_artist(_profile(albums=[], singles=[]))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert "Albums" not in res.text
    assert "Singles" not in res.text


def test_this_years_releases_are_badged(client, fake_artist):
    """The year is the only date YouTube Music reports here, so "new" can
    mean nothing finer — and a release from a past year must not claim it."""
    this_year = str(utcnow().year)
    fake_artist(_profile(albums=[_release(year=this_year)], singles=[_release("Old", "2019")]))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert res.text.count(">New<") == 1


def test_the_profile_offers_the_full_song_list(client, fake_artist):
    fake_artist(_profile(tracks=[_track(f"_efHZg9D{n:03d}") for n in range(10)], track_count=56))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert f"/#yt-artist-songs/{BROWSE_ID}" in res.text
    assert "artist-see-all" in res.text


def test_a_preview_that_is_the_whole_catalogue_offers_nothing_more(client, fake_artist):
    """"See all" pointing at the same ten rows is a control that does
    nothing."""
    fake_artist(_profile(tracks=[_track()], track_count=1))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert "yt-artist-songs" not in res.text


def test_the_profile_never_offers_play_all(client, fake_artist):
    """Play all reads the rendered rows on a remote list (home/remote.js),
    and the rows here are a preview — it would quietly play ten of 150."""
    fake_artist(_profile(tracks=[_track(f"_efHZg9D{n:03d}") for n in range(10)], track_count=56))

    res = client.get(f"/partials/detail/yt-artist/{BROWSE_ID}")

    assert "detail-play-all" not in res.text


def test_the_full_song_list_keeps_the_artists_follow_button(client, fake_artist):
    """Going one level in shouldn't drop the action the profile offered."""
    fake_artist(_profile())

    res = client.get(f"/partials/detail/yt-artist-songs/{BROWSE_ID}")

    assert f"https://www.youtube.com/channel/{TOPIC_ID}" in res.text
    assert "detail-play-all" in res.text


def test_a_release_opens_as_a_track_list(client, monkeypatch):
    """An album and a single are the same thing once opened, so they share
    a route — see music.fetch_release.

    Two tracks rather than one, because one is now the case that doesn't
    open at all (see test_explore_remote.py): it plays instead.
    """
    monkeypatch.setattr(
        remote_detail,
        "fetch_release",
        lambda browse_id: ReleaseDetail(
            title="Schlau aber blond",
            year="2025",
            kind="Album",
            cover_url="https://lh3.googleusercontent.com/c=w544-h544-l90-rj",
            artist_names="Shirin David",
            tracks=[_track(), _track(video_id="bbbbbbbbbbb")],
        ),
    )

    res = client.get("/partials/detail/yt-release/MPREb_HIQTwIoDtEM")

    assert res.status_code == 200
    assert "Album · 2025 · Shirin David · 2 tracks" in res.text
    assert "Biliyorsun" in res.text


@pytest.mark.parametrize("browse_id", ["UCNaGLJRPE3ohleIDM7RFtlQ", "MPREb_", "../etc/passwd"])
def test_a_non_release_id_is_rejected_without_being_fetched(client, monkeypatch, browse_id):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("must not fetch an id that failed validation")

    monkeypatch.setattr(remote_detail, "fetch_release", fail_if_called)

    assert client.get(f"/partials/detail/yt-release/{browse_id}").status_code == 404


def test_a_redirected_artist_is_followed_and_reopened_by_the_page_that_has_the_music(
    client, fake_artist
):
    """A VEVO channel opens the artist page through a redirect (see
    music._redirected_artist), and everything the panel offers has to belong
    to the page it landed on — not the songless one that was asked for."""
    fake_artist(_profile(browse_id="UCtxdfwb9wfkoGocVUAJ-Bmg"))

    res = client.get("/partials/detail/yt-artist/UClRx3MMyYUyqOxyEqA5F2nQ")

    assert f"https://www.youtube.com/channel/{TOPIC_ID}" in res.text
    assert "UClRx3MMyYUyqOxyEqA5F2nQ" not in res.text
