import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import settings
from app.youtube.urls import cover_url_at_size, is_video_still, video_still_url

FETCH_TIMEOUT_SECONDS = 10

# Far above any real image; read() is otherwise unbounded and this writes to disk.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Distinct from storage.py's EXPORT_TEMP_SUFFIX so a sweeper can tell the two apart.
_TEMP_SUFFIX = ".download.tmp"


def _download_image(directory: Path, filename: str, image_url: str, url_prefix: str) -> str | None:
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / filename
    if dest.is_file():
        return f"{url_prefix}/{dest.name}"

    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    if not body or len(body) > MAX_IMAGE_BYTES:
        return None

    # Write-then-rename: is_file() is the only cache check, so a truncated file would stick forever.
    temp = dest.with_name(dest.name + _TEMP_SUFFIX)
    try:
        temp.write_bytes(body)
        os.replace(temp, dest)
    except OSError:
        temp.unlink(missing_ok=True)
        return None

    return f"{url_prefix}/{dest.name}"


def fetch_image_bytes(image_url: str) -> tuple[bytes, str] | None:
    """Fetch an image into memory only, for proxying without a permanent local copy."""
    try:
        req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            body = resp.read(MAX_IMAGE_BYTES + 1)
            content_type = resp.headers.get_content_type()
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    if not body or len(body) > MAX_IMAGE_BYTES or not content_type.startswith("image/"):
        return None
    return body, content_type


def download_avatar(channel_id: str, avatar_url: str) -> str | None:
    """Cache an avatar for same-origin serving; Chrome's ORB intermittently blocks Google's CDN."""
    return _download_image(settings.avatars_dir, f"{channel_id}.jpg", avatar_url, "/avatars")


def download_thumbnail(video_id: str, thumbnail_url: str) -> str | None:
    return _download_image(settings.thumbnails_dir, f"{video_id}.jpg", thumbnail_url, "/thumbnails")


def needs_thumbnail_caching(thumbnail_url: str | None) -> bool:
    """Absolute http(s) only: a relative /image-proxy URL would make urllib raise ValueError."""
    return bool(thumbnail_url) and thumbnail_url.startswith(("http://", "https://"))


def cached_avatar_path(channel_id: str) -> str | None:
    if (settings.avatars_dir / f"{channel_id}.jpg").is_file():
        return f"/avatars/{channel_id}.jpg"
    return None


def proxied_image_url(remote_url: str) -> str:
    return f"/image-proxy?u={urllib.parse.quote(remote_url, safe='')}"


def track_cover(item) -> str | None:
    """The track's cover, else the video still — derived, not stored, so real covers stay distinguishable."""
    if item.thumbnail_url:
        return item.thumbnail_url
    still = video_still_url(item.video_id)
    return proxied_image_url(still) if still else None


def is_music_video(item) -> bool:
    """Inferred from the cover (an i.ytimg.com still): videoType isn't stored and the schema is never migrated."""
    stored = getattr(item, "thumbnail_url", None)
    if not stored:
        return False
    return is_video_still(unproxied_image_url(stored))


def unproxied_image_url(url: str) -> str:
    """The remote URL behind an /image-proxy URL; any other URL comes back untouched."""
    if not url.startswith("/image-proxy"):
        return url
    _, _, query = url.partition("?")
    return urllib.parse.unquote(urllib.parse.parse_qs(query).get("u", [""])[0])


# iOS won't downscale large art for the compact player (Dynamic Island) and draws a grey box.
NOW_PLAYING_ARTWORK_SIZES = (96, 192, 512)


def track_artwork(item) -> list[dict[str, str]]:
    """MediaSession artwork per size; an unresizable cover (video still) gets one unsized entry."""
    cover = track_cover(item)
    if not cover:
        return []

    remote = unproxied_image_url(cover)
    sized = {size: proxied_image_url(cover_url_at_size(remote, size)) for size in NOW_PLAYING_ARTWORK_SIZES}
    if len(set(sized.values())) == 1:
        return [{"src": cover}]
    return [{"src": url, "sizes": f"{size}x{size}"} for size, url in sized.items()]


def cached_avatar_or_hotlink(channel_id: str, remote_url: str | None) -> str | None:
    """Reuse a cached avatar, else proxy it — only followed artists earn a local copy."""
    if not remote_url:
        return None
    return cached_avatar_path(channel_id) or proxied_image_url(remote_url)
