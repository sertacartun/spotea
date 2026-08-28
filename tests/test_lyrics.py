"""Timed lyrics for the player panel's Lyrics tab.

The fetch is monkeypatched out; what's under test is the caching contract,
because that is where the cost is. A miss is two live YouTube requests and,
measured over 21 tracks before this was built, about two thirds of tracks
have no lyrics at all — so "we asked and there are none" has to be as
durable an answer as "here they are".
"""

import json

import pytest

from app.models import Artist, Content, TrackLyrics, User
from app.services import lyrics as lyrics_service
from app.youtube.music import LyricLine, TimedLyrics

USER_ID = 1
VIDEO_ID = "abcdefghijk"


def _content(db_session, video_id=VIDEO_ID, user_id=USER_ID):
    artist = Artist(
        user_id=user_id,
        channel_id=f"UClyrics{user_id}00000000000",
        name="Lyric Artist",
        followed=True,
    )
    db_session.add(artist)
    db_session.commit()
    db_session.refresh(artist)

    content = Content(
        artist_id=artist.id, user_id=user_id, video_id=video_id, title="A Song"
    )
    db_session.add(content)
    db_session.commit()
    db_session.refresh(content)
    return content


@pytest.fixture
def fake_lyrics(monkeypatch):
    """Installs an answer for the fetcher and counts how often it was asked."""
    holder = {
        "result": TimedLyrics(
            lines=[
                LyricLine(text="First line", start_ms=1000, end_ms=2500),
                LyricLine(text="Second line", start_ms=2500, end_ms=4000),
            ],
            source="Source: LyricFind",
        )
    }
    calls = []

    def fetch(video_id):
        calls.append(video_id)
        return holder["result"]

    monkeypatch.setattr(lyrics_service, "fetch_timed_lyrics", fetch)
    return holder, calls


def test_lyrics_come_back_as_timed_lines(client, db_session, fake_lyrics):
    content = _content(db_session)

    res = client.get(f"/content/{content.id}/lyrics")

    assert res.status_code == 200
    assert res.json() == {
        "lines": [
            {"text": "First line", "start_ms": 1000, "end_ms": 2500},
            {"text": "Second line", "start_ms": 2500, "end_ms": 4000},
        ],
        "source": "Source: LyricFind",
    }


def test_a_second_request_is_served_from_the_cache(client, db_session, fake_lyrics):
    _, calls = fake_lyrics
    content = _content(db_session)

    first = client.get(f"/content/{content.id}/lyrics").json()
    second = client.get(f"/content/{content.id}/lyrics").json()

    assert first == second
    assert calls == [VIDEO_ID], "the second request re-asked YouTube"


def test_having_no_lyrics_is_cached_just_as_hard(client, db_session, fake_lyrics):
    """The majority answer. Without storing it, the two-request miss would
    repeat every single time the tab is opened on such a track."""
    holder, calls = fake_lyrics
    holder["result"] = None
    content = _content(db_session)

    first = client.get(f"/content/{content.id}/lyrics")
    second = client.get(f"/content/{content.id}/lyrics")

    assert first.json() == {"lines": None, "source": None}
    assert second.json() == {"lines": None, "source": None}
    assert calls == [VIDEO_ID]
    # A row that exists with lines NULL — "asked, there are none" — rather
    # than no row, which would mean "never asked".
    row = db_session.get(TrackLyrics, VIDEO_ID)
    assert row is not None
    assert row.lines is None


def test_the_cache_is_keyed_by_recording_not_by_content_row(client, db_session, fake_lyrics):
    """The same track is several Content rows — a preview from Explore, the
    same song picked up by a sync, one row per user — and the lyrics are a
    property of the recording, not of anyone's library."""
    _, calls = fake_lyrics
    mine = _content(db_session)
    other_user = User(username="lyrics-other", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)
    theirs = _content(db_session, user_id=other_user.id)

    client.get(f"/content/{mine.id}/lyrics")
    row = db_session.get(TrackLyrics, VIDEO_ID)

    assert row is not None
    assert theirs.video_id == mine.video_id
    assert calls == [VIDEO_ID]


def test_another_users_track_is_not_readable(client, db_session, fake_lyrics):
    _, calls = fake_lyrics
    other_user = User(username="lyrics-stranger", password_hash="x")
    db_session.add(other_user)
    db_session.commit()
    db_session.refresh(other_user)
    theirs = _content(db_session, user_id=other_user.id)

    res = client.get(f"/content/{theirs.id}/lyrics")

    assert res.status_code == 404
    assert calls == [], "a 404 must not cost a YouTube request"


def test_stored_lines_round_trip_as_json(client, db_session, fake_lyrics):
    content = _content(db_session)

    client.get(f"/content/{content.id}/lyrics")

    stored = json.loads(db_session.get(TrackLyrics, VIDEO_ID).lines)
    assert stored[0] == {"text": "First line", "start_ms": 1000, "end_ms": 2500}


def test_lyrics_require_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.get("/content/1/lyrics", follow_redirects=False)
        assert res.status_code == 303
