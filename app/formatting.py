import re

_UNSAFE_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def safe_filename(name: str) -> str:
    cleaned = _UNSAFE_FILENAME_CHARS_RE.sub("", name)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip(" .")
    return cleaned[:150] or "download"


def format_size(num_bytes: int | None) -> str:
    if not num_bytes:
        return "0 MB"

    megabytes = num_bytes / (1024 * 1024)
    if megabytes >= 1024:
        return f"{megabytes / 1024:.2f} GB"
    return f"{megabytes:.1f} MB"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""

    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
