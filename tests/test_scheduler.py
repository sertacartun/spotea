"""What still runs on a clock: the disk and row sweeps. Release checks are asked for by the client."""

import asyncio
from pathlib import Path

from app.auth import hash_password
from app.content_query import followed_artists
from app.models import Artist, User

DEFAULT_USER_ID = 1


def _second_user(db_session, **user_kwargs) -> User:
    """A second login with one followed artist, for the scoping test below."""
    defaults = {"username": "second", "password_hash": hash_password("x")}
    defaults.update(user_kwargs)
    user = User(**defaults)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    artist = Artist(user_id=user.id, channel_id="https://example.com/second-user", name="Second")
    db_session.add(artist)
    db_session.commit()

    return user


def test_followed_feeds_scoped_to_a_user_excludes_everyone_elses(db_session):
    artist = Artist(user_id=DEFAULT_USER_ID, channel_id="https://example.com/mine", name="Mine")
    db_session.add(artist)
    db_session.commit()

    other = _second_user(db_session)

    feed_ids = {f.id for f in followed_artists(db_session, user_id=DEFAULT_USER_ID).all()}
    other_ids = {f.id for f in followed_artists(db_session, user_id=other.id).all()}

    assert artist.id in feed_ids
    assert feed_ids.isdisjoint(other_ids)


def test_run_scheduler_sweeps_disk_every_tick(monkeypatch):
    import app.scheduler as scheduler_module

    calls = []
    monkeypatch.setattr(scheduler_module, "_sweep_disk", lambda: calls.append(1))

    async def drive():
        scheduler_module.start()
        try:
            for _ in range(200):
                await asyncio.sleep(0.005)
                if calls:
                    break
        finally:
            await scheduler_module.stop()

    asyncio.run(drive())

    assert calls, "the scheduler tick never called _sweep_disk"


def test_the_scheduler_no_longer_refreshes_anyone():
    """The artist-refresh loop must stay gone; the client asks for release checks."""
    import app.scheduler as scheduler_module

    assert not hasattr(scheduler_module, "_refresh_due_users")
    assert not hasattr(scheduler_module, "_due_users")
    source = Path(scheduler_module.__file__).read_text()
    assert "sync_artists" not in source
    assert "sync_if_due" not in source
