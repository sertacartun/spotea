"""Field limits on request bodies (app/schemas.py); SQLite doesn't enforce VARCHAR lengths."""

import pytest
from pydantic import ValidationError

from app.schemas import _CONTENT_TITLE_MAX_LENGTH as TITLE_MAX
from app.schemas import _URL_MAX_LENGTH as URL_MAX
from app.schemas import ArtistCreate, VideoAddCreate


@pytest.mark.parametrize("model, field, limit", [
    (ArtistCreate, "channel_url", URL_MAX),
])
def test_a_value_at_the_limit_is_accepted(model, field, limit):
    model(**{field: "x" * limit})


@pytest.mark.parametrize("model, field, limit", [
    (ArtistCreate, "channel_url", URL_MAX),
])
def test_one_character_past_the_limit_is_rejected(model, field, limit):
    with pytest.raises(ValidationError):
        model(**{field: "x" * (limit + 1)})


@pytest.mark.parametrize("model, field", [
    (ArtistCreate, "channel_url"),
])
def test_an_empty_value_is_rejected(model, field):
    with pytest.raises(ValidationError):
        model(**{field: ""})


def test_a_video_title_at_the_content_column_width_is_accepted():
    """Matches Content.title's String(500) column."""
    VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * TITLE_MAX, channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA")


def test_a_video_title_past_the_content_column_width_is_rejected():
    with pytest.raises(ValidationError):
        VideoAddCreate(video_id="dQw4w9WgXcQ", title="x" * (TITLE_MAX + 1), channel_id="UCX6OQ3DkcsbYNE6H8uQQuVA")
