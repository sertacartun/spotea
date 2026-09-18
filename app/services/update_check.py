"""Asking GitHub whether a newer Spotea has been released, at most once per 12-hour UTC window.

Spotea is self-hosted: an instance knows the version it is running, but nothing on the machine
knows what the project has published since. Only upstream can say, so once per window the
server asks GitHub's releases API and remembers the answer.

From the server, never the browser. A browser check would multiply one request per window into
one per user per foreground return, hand every listener's IP to GitHub, and need `connect-src`
widened past 'self' in app/middleware.py — which would also be the only reason this app ever
talks to a third-party host from the page.

Two on/off switches, not one:
- UPDATE_CHECK=0, a deploy-time env var, is the hard kill switch — nothing below ever runs, and
  nobody sees a toggle for it, because turning it on again would need editing .env and restarting.
- UpdateCheck.enabled is the owner's Settings toggle, on by default, for the same instance to opt
  out at runtime without touching the deployment — the point for someone privacy-sensitive who
  never reads .env.example. Both gates must pass before a request leaves the machine.
"""

import json
import logging
import threading
import urllib.error
import urllib.request

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import UpdateCheck, User
from app.timeutil import is_stale, utcnow
from app.version import APP_VERSION, LATEST_RELEASE_API, RELEASES_URL

logger = logging.getLogger(__name__)

# Short: this runs in a background task, and a hung GitHub must not hold one open.
TIMEOUT_SECONDS = 5

# One row, so one id.
RECORD_ID = 1

# Process-local, like services/refresh.py: the app runs single-worker by design.
_lock = threading.Lock()


def _parts(value: str) -> tuple[int, ...] | None:
    """"v1.2.3" -> (1, 2, 3). None for anything that isn't plain dotted numbers."""
    cleaned = value.strip().removeprefix("v").removeprefix("V")
    parts = cleaned.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    """Whether `candidate` is a later release than `current`.

    False unless both parse: a tag this doesn't understand ("nightly", "2026-09-18-hotfix")
    must never nag someone to update to something it can't compare.
    """
    left, right = _parts(candidate), _parts(current)
    if left is None or right is None:
        return False
    # "1.2" and "1.2.0" are the same release; compare them padded or the shorter one loses.
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) > right + (0,) * (size - len(right))


def _fetch_latest() -> tuple[str, str] | None:
    """The newest release's tag and page, or None if GitHub didn't answer with one."""
    request = urllib.request.Request(
        LATEST_RELEASE_API,
        headers={
            "Accept": "application/vnd.github+json",
            # GitHub rejects API requests without one.
            "User-Agent": f"spotea/{APP_VERSION}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # A repo with no releases yet answers 404, a rate limit 403, a down VPN a timeout.
        # None of them is a fault of this install, and none is worth a traceback in its logs.
        logger.info("Update check did not complete: %s", exc)
        return None

    tag = payload.get("tag_name") if isinstance(payload, dict) else None
    if not isinstance(tag, str) or not tag:
        return None
    url = payload.get("html_url")
    return tag, url if isinstance(url, str) and url else RELEASES_URL


def check_if_due() -> None:
    """Ask upstream if a window has passed since the last attempt, and the owner allows it.

    Run as a FastAPI background task, which executes tasks in sequence and abandons the rest
    of the queue if one raises — so everything here is swallowed and logged.
    """
    if not settings.update_check:
        return
    # Several tabs and devices return to the foreground at once; one asks, the rest skip.
    if not _lock.acquire(blocking=False):
        return
    try:
        with SessionLocal() as db:
            record = db.get(UpdateCheck, RECORD_ID)
            if record is not None and not record.enabled:
                return
            if record is not None and not is_stale(record.checked_at):
                return

            latest = _fetch_latest()
            if record is None:
                record = UpdateCheck(id=RECORD_ID)
                db.add(record)
            # Stamped whether or not the request worked: a failed attempt is still an attempt,
            # and retrying it on every request would hammer an API that already said no.
            record.checked_at = utcnow()
            if latest is not None:
                record.latest_version, record.release_url = latest
            db.commit()
    except Exception:
        logger.exception("Update check failed")
    finally:
        _lock.release()


def is_enabled(db: Session) -> bool:
    """Whether the owner's Settings toggle is on. No row yet means never turned off — the
    column defaults to True, so a first-ever load is "on" without anyone having to ask for it."""
    if not settings.update_check:
        return False
    record = db.get(UpdateCheck, RECORD_ID)
    return record is None or record.enabled


def set_enabled(db: Session, enabled: bool) -> None:
    """Settings' toggle. Doesn't touch a stored `latest_version` on either flip — turning it back
    on shouldn't have to wait out a stale answer, and available_update already hides it while off."""
    record = db.get(UpdateCheck, RECORD_ID)
    if record is None:
        db.add(UpdateCheck(id=RECORD_ID, enabled=enabled))
    else:
        record.enabled = enabled
    db.commit()


def available_update(db: Session) -> tuple[str, str] | None:
    """The newer release this instance last heard about, or None. One row, no network."""
    if not settings.update_check:
        return None
    record = db.get(UpdateCheck, RECORD_ID)
    if record is None or not record.enabled or not record.latest_version:
        return None
    if not is_newer(record.latest_version):
        return None
    # The tag as published ("v1.1.0"), shown next to APP_VERSION ("1.0.0"), would read as two
    # different kinds of number. is_newer already accepted it, so the prefix is all there is to drop.
    version = record.latest_version.strip().removeprefix("v").removeprefix("V")
    return version, record.release_url or RELEASES_URL


def is_instance_owner(db: Session, user_id: int) -> bool:
    """Whether this account is the first one registered — in practice whoever set the instance up.

    Only they are told an update exists: on a server shared with family, everyone else would be
    reading a notice about a command they can't run.

    Derived rather than stored: a column would need a migration (`create_all` never adds columns),
    and taking the lowest id still names someone if that first account was since deleted.
    """
    owner_id = db.query(func.min(User.id)).scalar()
    return owner_id is not None and owner_id == user_id
