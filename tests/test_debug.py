"""POST /debug/playback: body size cap and log-injection defences."""

import json
import logging

from fastapi.testclient import TestClient

from app.main import app
from app.routers.debug import (
    MAX_BODY_BYTES,
    MAX_EVENT_LOG_LENGTH,
    MAX_EVENTS_PER_REQUEST,
    _sanitize_for_log,
)


def test_a_normal_batch_is_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post("/debug/playback", json=[{"event": "play-rejected", "trackId": 42}])

    assert res.status_code == 204
    assert "play-rejected" in caplog.text


def test_a_single_object_is_accepted_same_as_a_one_item_list(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post("/debug/playback", json={"event": "prepare-failed"})

    assert res.status_code == 204
    assert "prepare-failed" in caplog.text


def test_malformed_json_is_dropped_not_raised(client):
    """sendBeacon can't act on an error response, so a bad body fails silently."""
    res = client.post(
        "/debug/playback", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 204


def test_more_than_the_cap_is_truncated_not_rejected(client, caplog):
    events = [{"event": f"e{i}"} for i in range(MAX_EVENTS_PER_REQUEST + 5)]
    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post("/debug/playback", json=events)

    assert res.status_code == 204
    logged = caplog.text.count("playback: ")
    assert logged == MAX_EVENTS_PER_REQUEST


def test_an_oversized_body_is_dropped_before_it_is_parsed(client, caplog):
    """request.json() has no size ceiling of its own."""
    huge = json.dumps([{"event": "x" * (MAX_BODY_BYTES + 1024)}]).encode()
    assert len(huge) > MAX_BODY_BYTES

    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post(
            "/debug/playback", content=huge, headers={"Content-Type": "application/json"}
        )

    assert res.status_code == 204
    assert "playback:" not in caplog.text


def test_a_newline_in_an_event_cannot_forge_a_second_log_line(client, caplog):
    payload = [{"event": "play-rejected", "detail": "line one\nERROR: totally fake incident"}]
    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post("/debug/playback", json=payload)

    assert res.status_code == 204
    for line in caplog.text.splitlines():
        assert "ERROR: totally fake incident" not in line or "playback:" in line


def test_an_ansi_escape_sequence_is_stripped(client, caplog):
    payload = [{"event": "play-rejected", "detail": "\x1b[31mFAKE RED TEXT\x1b[0m"}]
    with caplog.at_level(logging.INFO, logger="app.routers.debug"):
        res = client.post("/debug/playback", json=payload)

    assert res.status_code == 204
    assert "\x1b" not in caplog.text


def test_a_very_long_event_is_truncated_in_the_log():
    text = _sanitize_for_log({"event": "x" * (MAX_EVENT_LOG_LENGTH * 2)})
    assert len(text) <= MAX_EVENT_LOG_LENGTH + len("…")


def test_debug_playback_requires_login():
    with TestClient(app) as anon:
        res = anon.post("/debug/playback", json=[{"event": "x"}], follow_redirects=False)

    assert res.status_code == 303
