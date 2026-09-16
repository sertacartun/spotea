"""Timed lyrics for the Lyrics tab, fetched once per recording.

A miss costs two YouTube requests and most tracks have none, so absence is cached too (`lines` NULL).
"""

import json
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TrackLyrics
from app.youtube.music import fetch_timed_lyrics

logger = logging.getLogger(__name__)


def _to_payload(row: TrackLyrics) -> dict:
    if row.lines is None:
        return {"lines": None, "source": None}
    return {"lines": json.loads(row.lines), "source": row.source}


def lyrics_for(db: Session, video_id: str) -> dict:
    cached = db.get(TrackLyrics, video_id)
    if cached is not None:
        return _to_payload(cached)

    fetched = fetch_timed_lyrics(video_id)
    row = TrackLyrics(
        video_id=video_id,
        lines=(
            json.dumps(
                [
                    {"text": line.text, "start_ms": line.start_ms, "end_ms": line.end_ms}
                    for line in fetched.lines
                ]
            )
            if fetched
            else None
        ),
        source=fetched.source if fetched else None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent request for the same track already stored the answer; use theirs.
        db.rollback()
        existing = db.get(TrackLyrics, video_id)
        if existing is not None:
            return _to_payload(existing)
        raise

    return _to_payload(row)
