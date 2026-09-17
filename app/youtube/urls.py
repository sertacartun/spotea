"""YouTube URL shapes and ID conventions — pure string work, no network."""

import re
import unicodedata
from urllib.parse import urlsplit

VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")

# Playlist ids vary in shape ("PL…", "RDCLAK5uy_…", "UULF…") but are all longer than a video id.
PLAYLIST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{12,64}$")

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{22}$")

# Untrusted path input that goes straight into an API call; length isn't fixed, so bound it.
RELEASE_ID_RE = re.compile(r"^MPREb_[\w-]{1,32}$")

# A mood's URL name, from mood_slug(); untrusted path input, so bounded.
MOOD_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MOOD_SLUG_MAX_LENGTH = 64

CHANNEL_ID_URL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]{22})")
CHANNEL_ID_PARAM_RE = re.compile(r"channel_id=([\w-]+)")

YOUTUBE_WATCH_URL = "https://www.youtube.com/watch?v={video_id}"


PLAYLIST_PAGE_URL_TEMPLATE = "https://www.youtube.com/playlist?list={playlist_id}"

CHANNEL_PAGE_URL_TEMPLATE = "https://www.youtube.com/channel/{channel_id}"


# Host allowlist: any URL handed to this package may be fetched, so without it
# POST /artists becomes an SSRF probe against localhost/LAN.
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)


def is_youtube_url(url: str) -> bool:
    """True only for an http(s) URL naming one of YouTube's own hosts.

    Uses `hostname`, not `netloc` or a substring test, so "https://youtube.com@evil.example/" fails.
    """
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    split = urlsplit(candidate)
    if split.scheme not in ("http", "https"):
        return False
    return (split.hostname or "").lower() in _YOUTUBE_HOSTS


def extract_channel_id(url: str) -> str | None:
    """The channel id from either "…?channel_id=UC…" or "youtube.com/channel/UC…"."""
    match = CHANNEL_ID_PARAM_RE.search(url) or CHANNEL_ID_URL_RE.search(url)
    return match.group(1) if match else None


def playlist_url(playlist_id: str) -> str:
    return PLAYLIST_PAGE_URL_TEMPLATE.format(playlist_id=playlist_id)


def absolute_thumbnail_url(raw: str | None) -> str | None:
    if not raw:
        return None
    url = f"https:{raw}" if raw.startswith("//") else raw
    # Hotlinking yt3.googleusercontent.com gets ERR_BLOCKED_BY_ORB in Chrome;
    # yt3.ggpht.com serves the same image with the headers ORB wants.
    return url.replace("//yt3.googleusercontent.com/", "//yt3.ggpht.com/")


# The CDN resizes via a trailing "=s<n>"; yt-dlp reports "=s0", the full-size original.
_AVATAR_SIZE_RE = re.compile(r"=s\d+(-[^=]*)?$")


def avatar_url_at_size(url: str | None, size: int) -> str | None:
    """The same avatar at `size` pixels; applied at render time so the stored URL stays as reported."""
    if not url:
        return None
    return _AVATAR_SIZE_RE.sub(f"=s{size}", url)


# YouTube Music's "=w60-h60-l90-rj[-…]" size dialect; unanchored so trailing segments are preserved.
_COVER_SIZE_RE = re.compile(r"=w\d+-h\d+")


def cover_url_at_size(url: str | None, size: int) -> str | None:
    """The same square artwork at `size` pixels, in either of the CDN's size dialects.

    URLs with neither (signed i.ytimg.com stills) pass through untouched.
    """
    if not url:
        return None
    if _COVER_SIZE_RE.search(url):
        return _COVER_SIZE_RE.sub(f"=w{size}-h{size}", url, count=1)
    return avatar_url_at_size(url, size)


def video_still_url(video_id: str | None) -> str | None:
    """A video's own thumbnail, built from its id — the last-resort cover.

    mqdefault, not hqdefault: hq pads 4:3 with black bars that an object-fit crop can't remove.
    """
    if not video_id:
        return None
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


_VIDEO_STILL_RE = re.compile(
    r"^https://i\.ytimg\.com/vi(?:_webp)?/[A-Za-z0-9_-]{11}/[a-z0-9]+\.(?:jpg|webp)(?:\?.*)?$"
)


def is_video_still(url: str | None) -> bool:
    """Whether this cover is a video frame rather than square album art (i.e. a music video)."""
    return bool(url) and _VIDEO_STILL_RE.match(url) is not None


# YouTube Music prefixes playlist browse ids with "VL". PLAYLIST_ID_RE still accepts the
# prefixed form, so the strip must happen before validation rather than be caught by it.
_BROWSE_PLAYLIST_PREFIX = "VL"


def playlist_id_from_browse_id(browse_id: str | None) -> str | None:
    """A YouTube Music browse id as a plain playlist id, or None if it isn't one."""
    if not browse_id:
        return None
    playlist_id = browse_id.removeprefix(_BROWSE_PLAYLIST_PREFIX)
    return playlist_id if PLAYLIST_ID_RE.match(playlist_id) else None


def mood_slug(title: str) -> str:
    """"Feel good" -> "feel-good": a readable, stable URL name, since a mood's params token is opaque."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_title.lower()).strip("-")
