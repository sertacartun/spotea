from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC — the app-wide convention; mixing aware and naive datetimes raises on comparison."""
    return datetime.now(UTC).replace(tzinfo=None)
