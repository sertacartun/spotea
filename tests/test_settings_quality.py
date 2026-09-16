"""Download quality as per-user state (routers/settings.py)."""

from app.models import User


def test_new_downloads_default_to_data_saver(client):
    """High quality is opt-in: downloads cost storage and data."""
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
    client.put("/settings", json={"audio_quality": "high"})

    with client.__class__(client.app) as other:
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
    """Paired with the test below to prove conftest's per-test reset of the default user row."""
    assert client.get("/settings").json()["audio_quality"] == "low"
    client.put("/settings", json={"audio_quality": "high"})


def test_the_quality_resets_between_tests_2(client):
    assert client.get("/settings").json()["audio_quality"] == "low"


def test_the_settings_payload_no_longer_carries_a_refresh_interval(client):
    """A client still sending a refresh interval gets it ignored, not a 500."""
    assert "refresh_interval_minutes" not in client.get("/settings").json()

    res = client.put("/settings", json={"refresh_interval_minutes": 15})

    assert res.status_code == 200
    assert "refresh_interval_minutes" not in res.json()
