from datetime import UTC, datetime, timedelta

# Fixed UTC boundaries rather than "12 hours since the last check", which drifts with when people open
# the app. 00:00 UTC also lands a few hours after local midnight in Europe, when releases go live.
REFRESH_INTERVAL = timedelta(hours=12)


def utcnow() -> datetime:
    """Naive UTC — the app-wide convention; mixing aware and naive datetimes raises on comparison."""
    return datetime.now(UTC).replace(tzinfo=None)


def last_refresh_boundary(now: datetime | None = None) -> datetime:
    """The most recent 00:00 or 12:00 UTC at or before `now`."""
    now = now or utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + REFRESH_INTERVAL * ((now - midnight) // REFRESH_INTERVAL)


def is_stale(checked_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether a boundary has passed since `checked_at`; never checked counts as stale."""
    return checked_at is None or checked_at < last_refresh_boundary(now)
