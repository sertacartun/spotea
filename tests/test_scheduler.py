"""What still runs on a clock, and what moved off one.

Artist refreshing used to be a background loop here, ticking every five
minutes whether or not anyone was using the app. It now happens when someone
opens it (app/services/refresh.py, covered in test_refresh_on_open.py). All
that is left on the clock is the disk and row sweeps — the
loop-survives-a-failure regression test lives in test_health.py, which
already covers the try/except shape.
"""

import asyncio
from pathlib import Path

from app.auth import hash_password
from app.content_query import followed_artists
from app.models import Artist, User

DEFAULT_USER_ID = 1


def _second_user(db_session, **user_kwargs) -> User:
    """A whole second login, with one followed artist — for the scoping test
    below. A real User row rather than a bare user_id: foreign keys are
    enforced now (see app/database.py)."""
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
    """The disk/DB sweeps (storage.sweep_orphans, sweep_stale_previews — see
    scheduler._sweep_disk) are the whole of a tick now. They are not
    user-scoped at all, which is exactly why they stayed behind when
    refreshing left."""
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
    """Not a style point: leaving the loop in beside the on-open check would
    mean a library of 150 artists is still being fetched around the clock,
    which is the entire thing this change removes. Asserted on the module's
    own surface, because the loop failing silently is precisely how it went
    unnoticed before (see run_scheduler's docstring)."""
    import app.scheduler as scheduler_module

    assert not hasattr(scheduler_module, "_refresh_due_users")
    assert not hasattr(scheduler_module, "_due_users")
    assert "refresh_feeds" not in Path(scheduler_module.__file__).read_text()
