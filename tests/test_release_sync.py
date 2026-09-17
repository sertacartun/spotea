"""The twice-a-day release check (app/services/refresh.py) and the boundary it runs on (app/timeutil.py)."""

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Artist, User
from app.services import refresh as refresh_module
from app.services.refresh import is_due, sync_if_due
from app.timeutil import is_stale, last_refresh_boundary, utcnow

DEFAULT_USER_ID = 1


@pytest.fixture(autouse=True)
def _no_stale_locks():
    """The per-user locks are process-global; a lock left held would hang the next test."""
    yield
    refresh_module._user_locks.clear()


def _stamp(db_session, refreshed_at):
    user = db_session.get(User, DEFAULT_USER_ID)
    user.refreshed_at = refreshed_at
    db_session.commit()


def _refreshed_at(db_session):
    db_session.expire_all()
    return db_session.get(User, DEFAULT_USER_ID).refreshed_at


@pytest.mark.parametrize(
    ("now", "boundary"),
    [
        (datetime(2026, 9, 18, 0, 0), datetime(2026, 9, 18, 0, 0)),
        (datetime(2026, 9, 18, 11, 59, 59), datetime(2026, 9, 18, 0, 0)),
        (datetime(2026, 9, 18, 12, 0), datetime(2026, 9, 18, 12, 0)),
        (datetime(2026, 9, 18, 23, 59, 59), datetime(2026, 9, 18, 12, 0)),
    ],
)
def test_the_boundary_is_the_last_midnight_or_noon_utc(now, boundary):
    assert last_refresh_boundary(now) == boundary


def test_never_checked_is_stale():
    assert is_stale(None)


def test_a_check_in_the_current_window_is_fresh():
    assert not is_stale(datetime(2026, 9, 18, 0, 1), now=datetime(2026, 9, 18, 11, 59))


def test_a_check_from_before_the_boundary_is_stale_even_minutes_later():
    assert is_stale(datetime(2026, 9, 18, 11, 59), now=datetime(2026, 9, 18, 12, 1))


def test_an_evening_check_does_not_hide_a_release_that_went_live_at_midnight():
    """A rolling 12-hour window would still call Thursday 20:00 fresh at Friday 07:00."""
    assert is_stale(datetime(2026, 9, 17, 20, 0), now=datetime(2026, 9, 18, 7, 0))


def test_due_follows_the_stamp(db_session):
    user = db_session.get(User, DEFAULT_USER_ID)

    user.refreshed_at = None
    assert is_due(user)

    user.refreshed_at = last_refresh_boundary() - timedelta(seconds=1)
    assert is_due(user)

    user.refreshed_at = utcnow()
    assert not is_due(user)


def test_a_due_library_is_synced_and_stamped(db_session, monkeypatch):
    artist = Artist(user_id=DEFAULT_USER_ID, channel_id="UCfollowed", name="Followed", browse_id="UCfollowed")
    db_session.add(artist)
    db_session.commit()
    _stamp(db_session, None)
    synced = []
    monkeypatch.setattr(refresh_module, "sync_artists", lambda db, artists: synced.append([a.id for a in artists]))

    assert sync_if_due(DEFAULT_USER_ID) is True

    assert synced == [[artist.id]]
    assert _refreshed_at(db_session) is not None


def test_a_library_that_is_not_due_is_left_alone(db_session, monkeypatch):
    stamp = utcnow()
    _stamp(db_session, stamp)
    monkeypatch.setattr(
        refresh_module, "sync_artists", lambda db, artists: pytest.fail("synced a library that was not due")
    )

    assert sync_if_due(DEFAULT_USER_ID) is False

    assert _refreshed_at(db_session) == stamp


def test_an_empty_library_is_still_stamped(db_session, monkeypatch):
    """Otherwise it is due on every open."""
    _stamp(db_session, None)
    monkeypatch.setattr(refresh_module, "sync_artists", lambda db, artists: None)

    sync_if_due(DEFAULT_USER_ID)

    assert _refreshed_at(db_session) is not None


def test_a_failed_check_is_logged_and_stamped_so_the_window_costs_one_attempt(db_session, monkeypatch, caplog):
    _stamp(db_session, None)

    def explode(db, artists):
        raise RuntimeError("YouTube said no")

    monkeypatch.setattr(refresh_module, "sync_artists", explode)

    assert sync_if_due(DEFAULT_USER_ID) is True

    assert "Release check failed" in caplog.text
    assert _refreshed_at(db_session) is not None
    # And the lock is released, or every later check for this user would hang.
    assert not refresh_module._lock_for(DEFAULT_USER_ID).locked()


def test_a_second_caller_waits_for_the_running_check_instead_of_starting_its_own(db_session, monkeypatch):
    _stamp(db_session, None)
    started = threading.Event()
    finish = threading.Event()
    calls = []

    def slow_sync(db, artists):
        calls.append(1)
        started.set()
        assert finish.wait(5)

    monkeypatch.setattr(refresh_module, "sync_artists", slow_sync)

    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", sync_if_due(DEFAULT_USER_ID)))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.setdefault("second", sync_if_due(DEFAULT_USER_ID)))
    second.start()
    second.join(0.2)
    assert second.is_alive(), "the second caller answered before the running check finished"

    finish.set()
    first.join(5)
    second.join(5)

    assert calls == [1]
    # The waiter reports the check it waited on, so its tab re-renders too.
    assert results == {"first": True, "second": True}


def test_the_sync_route_reports_whether_it_checked(client, db_session, monkeypatch):
    monkeypatch.setattr(refresh_module, "sync_artists", lambda db, artists: None)
    _stamp(db_session, None)

    assert client.post("/artists/sync").json() == {"checked": True}
    assert client.post("/artists/sync").json() == {"checked": False}


def test_the_sync_route_requires_login():
    with TestClient(app) as anonymous:
        assert anonymous.post("/artists/sync", follow_redirects=False).status_code == 303


def test_opening_the_app_does_not_check_by_itself(client, db_session, monkeypatch):
    """The client asks after load; the page render must not sync or queue a sync."""
    _stamp(db_session, None)
    monkeypatch.setattr(
        refresh_module, "sync_artists", lambda db, artists: pytest.fail("the page render started a sync")
    )

    assert client.get("/").status_code == 200

    assert _refreshed_at(db_session) is None


def test_the_old_refresh_routes_are_gone(client):
    assert client.post("/artists/refresh").status_code in (404, 405)
    assert client.post("/recommendations/refresh").status_code in (404, 405)
