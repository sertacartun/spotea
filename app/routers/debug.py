"""Write-only log sink for client-side playback breadcrumbs, which server logs can't otherwise see."""

import json
import logging
import re

from fastapi import APIRouter, Depends, Request

from app.deps import require_login

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/debug", tags=["debug"], dependencies=[Depends(require_login)])

# sendBeacon may flush a backlog at once; bounded so a client bug can't flood the log.
MAX_EVENTS_PER_REQUEST = 20

# request.json() would read any body size regardless of Content-Length.
MAX_BODY_BYTES = 64 * 1024

# Newlines/ANSI escapes would let a client forge log lines; stripped, not rejected, to keep the breadcrumb.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_EVENT_LOG_LENGTH = 500


def _sanitize_for_log(value: object) -> str:
    text = _CONTROL_CHARS_RE.sub(" ", str(value))
    if len(text) > MAX_EVENT_LOG_LENGTH:
        text = text[:MAX_EVENT_LOG_LENGTH] + "…"
    return text


@router.post("/playback", status_code=204)
async def record_playback_events(request: Request) -> None:
    """Log a batch of player breadcrumbs.

    Raw body, not a schema, and bad input is dropped silently: sendBeacon can't see errors anyway.
    """
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return
    try:
        events = json.loads(body)
    except ValueError:
        return
    if not isinstance(events, list):
        events = [events]
    for event in events[:MAX_EVENTS_PER_REQUEST]:
        logger.info("playback: %s", _sanitize_for_log(event))
