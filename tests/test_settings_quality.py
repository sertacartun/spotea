"""Download quality as per-user state (routers/settings.py).

This file used to be about the artist-refresh interval, which was the other
setting on this row. That interval is gone — a library is checked when its
owner first opens the app and whenever Refresh is pressed, and nothing in
between (see services/refresh.py) — so the properties it pinned moved onto
the preference that survived it.

Those properties are worth keeping whichever setting carries them. This was
a single AppSettings row shared by the *entire deployment* once, then
per-Account across the profiles under it; with one login owning one library
it lives on the user row, and these say it is genuinely per-user rather than
shared again under a new name.
"""

from app.models import User


def test_new_downloads_default_to_data_saver(client):
    """The default a fresh account gets. High quality is the opt-in: what a
    download costs is a phone's storage and someone's data allowance, and
    those are not things to spend by default on a library that syncs whole
    albums."""
    assert client.get("/settings").json()["audio_quality"] == "low"


def test_changing_the_quality_is_reflected_immediately(client):
    res = client.put("/settings", json={"audio_quality": "high"})
    assert res.json()["audio_quality"] == "high"
    assert client.get("/settings").json()["audio_quality"] == "high"


def test_an_invalid_quality_is_rejected(client):
    res = client.put("/settings", json={"audio_quality": "lossless"})
    assert res.status_code == 400


def test_the_quality_is_stored_on_the_user_row(client, db_session):
    client.put("/settings", json={"audio_quality": "high"})

    user = db_session.get(User, 1)
    db_session.refresh(user)
    assert user.audio_quality == "high"


def test_a_different_user_is_unaffected(client):
    """This must not have become a shared deployment-wide setting again
    under a different name."""
    client.put("/settings", json={"audio_quality": "high"})

    with client.__class__(client.app) as other:  # a second, unauthenticated TestClient
        other.post(
            "/register",
            data={
                "username": "second-user-settings",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )
        assert other.get("/settings").json()["audio_quality"] == "low"


def test_the_quality_resets_between_tests_1(client):
    """Paired with the test below: proves conftest.py's per-test reset of the
    preserved default user row actually works, not just that these two tests
    happen not to interfere by luck of file ordering."""
    assert client.get("/settings").json()["audio_quality"] == "low"
    client.put("/settings", json={"audio_quality": "high"})


def test_the_quality_resets_between_tests_2(client):
    assert client.get("/settings").json()["audio_quality"] == "low"


def test_the_settings_payload_no_longer_carries_a_refresh_interval(client):
    """The control is gone from Settings and the column is dropped from an
    upgrading database at startup (see main._REMOVED_COLUMNS). A client that
    still sends one gets it ignored rather than a 500, because SettingsUpdate
    simply has no such field."""
    assert "refresh_interval_minutes" not in client.get("/settings").json()

    res = client.put("/settings", json={"refresh_interval_minutes": 15})

    assert res.status_code == 200
    assert "refresh_interval_minutes" not in res.json()
