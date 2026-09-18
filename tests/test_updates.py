"""Knowing an update exists: version comparison, who is told, and how often GitHub is asked."""

from datetime import timedelta

import pytest

from app.auth import hash_password
from app.models import UpdateCheck, User
from app.services import update_check
from app.services.update_check import (
    RECORD_ID,
    available_update,
    check_if_due,
    is_enabled,
    is_newer,
    set_enabled,
)
from app.timeutil import utcnow
from app.version import APP_VERSION, RELEASES_URL

DEFAULT_USER_ID = 1


@pytest.fixture
def newer_release(db_session):
    """A stored answer naming a release well past whatever APP_VERSION currently is."""
    major = int(APP_VERSION.split(".")[0]) + 1
    version = f"{major}.0.0"
    db_session.add(
        UpdateCheck(
            id=RECORD_ID,
            checked_at=utcnow(),
            latest_version=f"v{version}",
            release_url=f"{RELEASES_URL}/tag/v{version}",
        )
    )
    db_session.commit()
    return version


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("1.1.0", "1.0.0", True),
        ("v1.1.0", "1.0.0", True),
        ("1.0.0", "1.0.0", False),
        ("0.9.9", "1.0.0", False),
        # Padded, not compared as tuples of different length: 1.2 and 1.2.0 are one release.
        ("1.2", "1.2.0", False),
        ("1.10.0", "1.9.0", True),
        # Nothing this can't read is ever presented as an update.
        ("nightly", "1.0.0", False),
        ("2026-09-18", "1.0.0", False),
        ("", "1.0.0", False),
    ],
)
def test_version_comparison(candidate, current, expected):
    assert is_newer(candidate, current) is expected


def test_no_stored_answer_means_no_update(db_session):
    assert available_update(db_session) is None


def test_an_older_release_upstream_is_not_an_update(db_session):
    db_session.add(UpdateCheck(id=RECORD_ID, checked_at=utcnow(), latest_version="0.0.1"))
    db_session.commit()

    assert available_update(db_session) is None


def test_a_newer_release_is_reported_with_its_page(db_session, newer_release):
    found = available_update(db_session)

    assert found is not None
    # Shown without the tag's "v", so it reads as the same kind of number as APP_VERSION.
    version, url = found
    assert version == newer_release
    assert url.endswith(f"/tag/v{newer_release}")


def test_the_check_is_off_when_the_setting_is(db_session, newer_release, monkeypatch):
    """UPDATE_CHECK=0 has to silence the notice too, not just the outgoing request."""
    monkeypatch.setattr(update_check.settings, "update_check", False)

    assert available_update(db_session) is None


def test_a_disabled_check_never_asks_github(monkeypatch):
    monkeypatch.setattr(update_check.settings, "update_check", False)
    monkeypatch.setattr(update_check, "_fetch_latest", _must_not_run)

    check_if_due()


def _must_not_run():
    raise AssertionError("GitHub was asked when it should not have been")


def test_the_first_check_stores_what_github_answered(db_session, monkeypatch):
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: ("v9.9.9", "https://example.test/r"))

    check_if_due()

    record = db_session.get(UpdateCheck, RECORD_ID)
    assert record.latest_version == "v9.9.9"
    assert record.release_url == "https://example.test/r"


def test_a_check_within_the_window_does_not_ask_again(db_session, monkeypatch):
    db_session.add(UpdateCheck(id=RECORD_ID, checked_at=utcnow(), latest_version="v1.0.0"))
    db_session.commit()
    monkeypatch.setattr(update_check, "_fetch_latest", _must_not_run)

    check_if_due()


def test_a_check_asks_again_once_a_boundary_has_passed(db_session, monkeypatch):
    db_session.add(
        UpdateCheck(id=RECORD_ID, checked_at=utcnow() - timedelta(days=2), latest_version="v1.0.0")
    )
    db_session.commit()
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: ("v9.9.9", RELEASES_URL))

    check_if_due()

    db_session.expire_all()
    assert db_session.get(UpdateCheck, RECORD_ID).latest_version == "v9.9.9"


def test_a_failed_attempt_still_counts_as_an_attempt(db_session, monkeypatch):
    """Otherwise a GitHub outage turns every foreground return into another request."""
    monkeypatch.setattr(update_check, "_fetch_latest", lambda: None)

    check_if_due()

    record = db_session.get(UpdateCheck, RECORD_ID)
    assert record is not None
    assert record.checked_at is not None
    assert record.latest_version is None


def test_a_raising_check_does_not_escape(monkeypatch):
    """It runs as a background task; FastAPI abandons the rest of the queue if one raises."""

    def explode():
        raise RuntimeError("github went sideways")

    monkeypatch.setattr(update_check, "_fetch_latest", explode)

    check_if_due()


def test_a_check_is_skipped_while_the_owner_has_turned_it_off(db_session, monkeypatch):
    """The toggle, not just UPDATE_CHECK, has to stop the automatic check from asking at all."""
    db_session.add(
        UpdateCheck(id=RECORD_ID, enabled=False, checked_at=utcnow() - timedelta(days=2))
    )
    db_session.commit()
    monkeypatch.setattr(update_check, "_fetch_latest", _must_not_run)

    check_if_due()


def test_no_row_yet_means_enabled(db_session):
    """The column defaults to True: a fresh install is "on" without anyone touching a setting."""
    assert is_enabled(db_session) is True


def test_the_toggle_is_off_when_the_setting_is(db_session, monkeypatch):
    """UPDATE_CHECK=0 overrides the toggle even if the stored row says enabled."""
    monkeypatch.setattr(update_check.settings, "update_check", False)

    assert is_enabled(db_session) is False


def test_set_enabled_creates_the_row_on_a_first_change(db_session):
    set_enabled(db_session, False)

    assert is_enabled(db_session) is False
    record = db_session.get(UpdateCheck, RECORD_ID)
    assert record is not None
    assert record.enabled is False


def test_set_enabled_flips_an_existing_row(db_session, newer_release):
    set_enabled(db_session, False)
    db_session.expire_all()
    assert is_enabled(db_session) is False

    set_enabled(db_session, True)
    db_session.expire_all()
    assert is_enabled(db_session) is True


def test_turning_it_off_hides_an_update_already_found(db_session, newer_release):
    """Off means off — not "keep showing what was already found", which isn't opting out."""
    assert available_update(db_session) is not None

    set_enabled(db_session, False)
    db_session.expire_all()

    assert available_update(db_session) is None


def test_turning_it_back_on_reveals_the_stored_answer_immediately(db_session, newer_release):
    """No need to wait out the window again — set_enabled never touches the stored result."""
    set_enabled(db_session, False)
    db_session.expire_all()
    set_enabled(db_session, True)
    db_session.expire_all()

    found = available_update(db_session)
    assert found is not None
    assert found[0] == newer_release


def test_the_endpoint_reports_the_running_version(client):
    body = client.get("/updates").json()

    assert body["version"] == APP_VERSION
    assert body["latest"] is None
    assert body["release_url"] == RELEASES_URL


def test_the_first_account_is_told_about_an_update(client, newer_release):
    body = client.get("/updates").json()

    assert body["latest"] == newer_release


def test_a_later_account_is_not(client, db_session, newer_release):
    """On a server shared with family, only whoever can run `docker compose pull` is nagged."""
    db_session.add(User(id=DEFAULT_USER_ID + 1, username="listener", password_hash=hash_password("x")))
    db_session.commit()

    with_owner = client.get("/updates").json()
    assert with_owner["latest"] == newer_release

    client.post("/logout")
    client.post("/login", data={"username": "listener", "password": "x"})
    assert client.get("/updates").json()["latest"] is None


def test_the_endpoint_needs_a_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        assert anonymous.get("/updates", follow_redirects=False).status_code == 303


def test_settings_shows_the_version(client):
    page = client.get("/").text

    assert f"This server runs Spotea {APP_VERSION}" in page
    assert f'data-app-version="{APP_VERSION}"' in page


def test_settings_names_the_new_version_when_there_is_one(client, newer_release):
    page = client.get("/").text

    assert f"Spotea {newer_release} is out" in page
    assert "docker compose pull" in page
    assert 'class="has-update"' in page


def test_the_about_fragment_re_renders_the_row(client, newer_release):
    fragment = client.get("/partials/about").text

    assert 'data-target="settings-about"' in fragment
    assert f"Spotea {newer_release} is out" in fragment


def test_settings_shows_the_toggle_on_by_default(client):
    """update_check defaults to on, so the owner sees it enabled with no setup."""
    page = client.get("/").text

    assert 'id="update-check-toggle"' in page
    assert 'id="update-check-toggle" checked' in page


def test_settings_hides_the_toggle_when_disabled(client, monkeypatch):
    monkeypatch.setattr("app.page_context.settings.update_check", False)

    page = client.get("/").text

    assert 'id="update-check-toggle"' not in page


def test_the_toggle_reflects_a_stored_off_state(client, db_session):
    set_enabled(db_session, False)

    page = client.get("/").text

    assert 'id="update-check-toggle" checked' not in page


def test_a_later_account_does_not_get_the_toggle(client, db_session):
    """Only the owner's preference means anything; nobody else's flip would change what they see."""
    db_session.add(User(id=DEFAULT_USER_ID + 1, username="listener", password_hash=hash_password("x")))
    db_session.commit()

    client.post("/logout")
    client.post("/login", data={"username": "listener", "password": "x"})
    page = client.get("/").text

    assert 'id="update-check-toggle"' not in page


def test_the_toggle_endpoint_turns_it_off(client, db_session):
    res = client.put("/updates/settings", json={"enabled": False})

    assert res.status_code == 200
    assert is_enabled(db_session) is False


def test_the_toggle_endpoint_turning_it_off_clears_the_notice(client, newer_release):
    body = client.put("/updates/settings", json={"enabled": False}).json()

    assert body["latest"] is None


def test_the_toggle_endpoint_is_gone_when_disabled(client, monkeypatch):
    monkeypatch.setattr("app.routers.updates.settings.update_check", False)

    res = client.put("/updates/settings", json={"enabled": False})

    assert res.status_code == 404


def test_the_toggle_endpoint_refuses_a_non_owner(client, db_session):
    db_session.add(User(id=DEFAULT_USER_ID + 1, username="listener", password_hash=hash_password("x")))
    db_session.commit()
    client.post("/logout")
    client.post("/login", data={"username": "listener", "password": "x"})

    res = client.put("/updates/settings", json={"enabled": False})

    assert res.status_code == 403


def test_the_toggle_endpoint_needs_a_login():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as anonymous:
        res = anonymous.put("/updates/settings", json={"enabled": False}, follow_redirects=False)
        assert res.status_code == 303
