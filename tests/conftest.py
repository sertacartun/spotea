"""Pytest fixtures.

Isolation from the real database is the whole point of this file: the app
was already hit by an accidental full data loss once (empty `content`/
`artists` tables, no backup) — a test run must never be able to touch
`data/spotea.db` or `data/storage`/`data/avatars`, even indirectly.

`app/config.py`'s `Settings()` is a module-level singleton instantiated the
first time `app.config` is imported, so the env vars below MUST be set
before any `from app...` import anywhere in the test session — hence they
run at import time, at the very top of this file, before the `from app...`
imports later on.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spotea-test-"))
# mkdtemp doesn't clean up after itself, and this runs at import time (before
# any pytest fixture could), so a fixture teardown can't own this either —
# atexit is the only hook that fires regardless of how the run ends. 17 of
# these had piled up in $TMPDIR/spotea-test-* before this existed.
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DIR / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TEST_DIR / "storage")
os.environ["AVATARS_DIR"] = str(_TEST_DIR / "avatars")
# Was missing while every other data path was covered, so settings.thumbnails_dir
# kept its production default through the whole suite. Nothing wrote there yet,
# which is the only reason it never showed — exactly the "even indirectly" case
# this file exists to rule out.
os.environ["THUMBNAILS_DIR"] = str(_TEST_DIR / "thumbnails")
os.environ["APP_PASSWORD"] = "test-password"
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production-use"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth import hash_password
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import User
from app.services import artist_follow


def _assert_paths_isolated() -> None:
    """A guard that runs once, at collection time (conftest.py itself is
    never collected as a test module by pytest) — a misconfigured
    environment fails loudly here instead of silently touching real data."""
    assert str(_TEST_DIR) in settings.database_url
    assert str(_TEST_DIR) in str(settings.storage_dir)
    assert str(_TEST_DIR) in str(settings.avatars_dir)
    assert str(_TEST_DIR) in str(settings.thumbnails_dir)


_assert_paths_isolated()


@pytest.fixture
def db_session() -> Session:
    """A fresh session per test for seeding data directly. Function-scoped
    (not shared across tests) so a session's SQLAlchemy identity map never
    goes stale from another test's cleanup deleting rows out from under it
    on a different connection."""
    with SessionLocal() as session:
        yield session


DEFAULT_USER_ID = 1
DEFAULT_USERNAME = "test-user"
DEFAULT_USER_PASSWORD = "test-password"


@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    """Runs the app's real startup path once (Base.metadata.create_all)
    against the isolated test DB, the same way `client` would, so
    `db_session`-only tests still get a real schema without needing to spin
    up a TestClient themselves. Registration is not automatic on startup, so
    the bootstrap user every other test implicitly relies on (id 1) is
    seeded here directly instead."""
    with TestClient(app):
        pass
    with SessionLocal() as db:
        if db.get(User, DEFAULT_USER_ID) is None:
            db.add(
                User(
                    id=DEFAULT_USER_ID,
                    username=DEFAULT_USERNAME,
                    password_hash=hash_password(DEFAULT_USER_PASSWORD),
                )
            )
            db.commit()
    yield


@pytest.fixture(autouse=True)
def _clean_tables(_init_schema):
    """Runs after every test — deletes all rows except the bootstrap default
    user (every test implicitly relies on users.id==1 existing) so tests
    don't leak state into each other without paying to recreate the DB file
    each time. Any extra user a test created is cleaned up here too, not
    just the other tables — otherwise it'd silently carry over into later
    tests' counts.

    The preserved row's own mutable columns are reset explicitly, not just
    its child rows deleted: several tests PUT a non-default interests /
    audio_quality through Settings, and without this reset that value would
    silently carry into whatever test runs next —
    the same leak the per-table deletes exist to prevent everywhere else.
    Caught by a test that left interests set, which broke
    test_no_interests_means_an_empty_batch_and_no_searches
    (test_recommendations.py) further into the run.
    """
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            if table.name == "users":
                conn.execute(
                    table.update()
                    .where(table.c.id == DEFAULT_USER_ID)
                    .values(
                        interests=None,
                        audio_quality="low",
                        refreshed_at=None,
                    )
                )
                conn.execute(table.delete().where(table.c.id != DEFAULT_USER_ID))
            else:
                conn.execute(table.delete())


@pytest.fixture(autouse=True)
def _no_artist_lookup(monkeypatch):
    """Following anything asks YouTube Music whether the channel belongs to
    a musician (see services/artist_follow._as_artist_follow). That is a live
    request on a code path dozens of tests take incidentally — every "add a
    artist and check X" test in the suite — so the default answer here is
    "not an artist", which is also what leaves those tests asserting the
    behaviour they were written for.

    Same reasoning as test_recommendations.py's _no_browse_shelves: a
    network call that fires without any test asking for it has to be shut
    off centrally, or the suite quietly goes online. The tests that are
    *about* artist detection install their own answer over this one.
    """
    monkeypatch.setattr(artist_follow, "fetch_artist", lambda browse_id, all_songs=True: None)


@pytest.fixture
def client() -> TestClient:
    """An authenticated TestClient — logs in for real via POST /login, so
    the returned client's cookie jar carries a genuine session and most
    tests never need to think about auth. See test_auth.py for the
    unauthenticated case."""
    with TestClient(app) as c:
        res = c.post("/login", data={"username": DEFAULT_USERNAME, "password": DEFAULT_USER_PASSWORD})
        assert res.status_code == 200
        yield c
