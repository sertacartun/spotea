"""Opening the app is what checks for new releases (app/services/refresh.py)."""

from datetime import timedelta

import pytest

from app.models import User
from app.services import refresh as refresh_module
from app.services.refresh import is_due, queue_due_refresh, refresh_if_due
from app.timeutil import utcnow

DEFAULT_USER_ID = 1


class _Recorder:
    def __init__(self):
        self.tasks = []

    def add_task(self, func, *args, **kwargs):
        self.tasks.append((func, args, kwargs))


@pytest.fixture(autouse=True)
def _no_stragglers():
    """The in-flight set is process-global, so a leftover id would no-op the next test."""
    yield
    refresh_module._in_flight.clear()


def test_a_never_refreshed_user_is_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None

    assert is_due(user)


def test_a_refreshed_user_is_never_due_again_however_long_it_has_been(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = utcnow() - timedelta(days=365)

    assert not is_due(user)


def test_a_user_refreshed_a_moment_ago_is_not_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = utcnow() - timedelta(minutes=1)

    assert not is_due(user)


def test_one_users_first_open_does_not_make_another_due(db_session):
    fresh = db_session.get(User, DEFAULT_USER_ID)
    fresh.refreshed_at = None

    already = User(username="already-checked", password_hash="x")
    already.refreshed_at = utcnow() - timedelta(minutes=20)

    assert is_due(fresh)
    assert not is_due(already)


def test_opening_the_app_queues_a_refresh_when_due(client, db_session):
    """Queued, not awaited: the page renders what was already stored."""
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None
    db_session.commit()

    tasks = _Recorder()
    queue_due_refresh(tasks, user)

    assert [func for func, _, _ in tasks.tasks] == [refresh_if_due]


def test_opening_the_app_queues_nothing_when_not_due(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = utcnow() - timedelta(minutes=5)

    tasks = _Recorder()
    queue_due_refresh(tasks, user)

    assert tasks.tasks == []


def test_a_due_refresh_runs_and_stamps_the_user(db_session, monkeypatch):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None
    db_session.commit()

    monkeypatch.setattr(refresh_module, "refresh_feeds", lambda db, artists: 0)

    refresh_if_due(DEFAULT_USER_ID)

    db_session.expire_all()
    assert db_session.get(User, DEFAULT_USER_ID).refreshed_at is not None


def test_a_refresh_that_is_no_longer_due_by_the_time_it_runs_does_nothing(db_session, monkeypatch):
    """Re-checked inside the task, since another tab may have refreshed in between."""
    user = db_session.get(User, DEFAULT_USER_ID)
    stamp = utcnow() - timedelta(minutes=5)
    user.refreshed_at = stamp
    db_session.commit()

    calls = []
    monkeypatch.setattr(
        refresh_module, "refresh_feeds", lambda db, artists: calls.append(1) or 0
    )

    refresh_if_due(DEFAULT_USER_ID)

    assert calls == []
    db_session.expire_all()
    assert db_session.get(User, DEFAULT_USER_ID).refreshed_at == stamp


def test_only_one_refresh_per_user_runs_at_a_time(db_session, monkeypatch):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None
    db_session.commit()

    calls = []

    def reentrant(db, artists):
        calls.append(1)
        # A second open arriving while this one is still going.
        refresh_if_due(DEFAULT_USER_ID)
        return 0

    monkeypatch.setattr(refresh_module, "refresh_feeds", reentrant)

    refresh_if_due(DEFAULT_USER_ID)

    assert calls == [1]


def test_a_failed_refresh_is_logged_rather_than_raised(db_session, monkeypatch, caplog):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None
    db_session.commit()

    def explode(db, artists):
        raise RuntimeError("YouTube said no")

    monkeypatch.setattr(refresh_module, "refresh_feeds", explode)

    refresh_if_due(DEFAULT_USER_ID)

    assert "Refresh on open failed" in caplog.text
    # And the id is released, or every later open for this user is a no-op.
    assert DEFAULT_USER_ID not in refresh_module._in_flight


def test_a_failing_thumbnail_task_cannot_cancel_the_refresh(db_session, monkeypatch):
    """FastAPI runs background tasks in sequence, so a raising thumbnail task would cancel the refresh."""
    from app.services.artist_sync import cache_thumbnail

    monkeypatch.setattr(
        "app.services.artist_sync.download_thumbnail",
        lambda video_id, url: (_ for _ in ()).throw(ValueError("unknown url type")),
    )

    assert cache_thumbnail("vid00000001", "/image-proxy?u=whatever") is None


def test_an_empty_library_is_still_stamped(db_session, monkeypatch):
    """Otherwise an empty library is due on every page load."""
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = None
    db_session.commit()
    monkeypatch.setattr(refresh_module, "refresh_feeds", lambda db, artists: 0)

    refresh_if_due(DEFAULT_USER_ID)

    db_session.expire_all()
    assert db_session.get(User, DEFAULT_USER_ID).refreshed_at is not None
