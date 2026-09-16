"""Pytest fixtures. Env vars are set before any `app` import so a test run never touches real data."""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_DIR = Path(tempfile.mkdtemp(prefix="spotea-test-"))
# atexit: mkdtemp never cleans up, and this runs at import time before any fixture could.
atexit.register(shutil.rmtree, _TEST_DIR, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DIR / 'test.db'}"
os.environ["STORAGE_DIR"] = str(_TEST_DIR / "storage")
os.environ["AVATARS_DIR"] = str(_TEST_DIR / "avatars")
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
    assert str(_TEST_DIR) in settings.database_url
    assert str(_TEST_DIR) in str(settings.storage_dir)
    assert str(_TEST_DIR) in str(settings.avatars_dir)
    assert str(_TEST_DIR) in str(settings.thumbnails_dir)


_assert_paths_isolated()


@pytest.fixture
def db_session() -> Session:
    with SessionLocal() as session:
        yield session


DEFAULT_USER_ID = 1
DEFAULT_USERNAME = "test-user"
DEFAULT_USER_PASSWORD = "test-password"


@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    """Run the real startup once and seed the bootstrap user (id 1) every test relies on."""
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
    """Delete all rows except the bootstrap user, whose mutable columns are reset so they don't leak."""
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
    """Following anything triggers a live YouTube Music artist lookup; default it to "not an artist"."""
    monkeypatch.setattr(artist_follow, "fetch_artist", lambda browse_id, all_songs=True: None)


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        res = c.post("/login", data={"username": DEFAULT_USERNAME, "password": DEFAULT_USER_PASSWORD})
        assert res.status_code == 200
        yield c
