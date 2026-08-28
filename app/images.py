"""Fetching and caching remote images (channel avatars, video thumbnails).

Split out of downloader.py, which is otherwise entirely about yt-dlp and
audio extraction. The two had nothing in common but the word "download",
and keeping them together meant app.youtube — a metadata layer that has no
business knowing about audio downloads — had to import from the audio
downloader just to cache an avatar.
"""

import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import settings
from app.youtube.urls import cover_url_at_size, is_video_still, video_still_url

FETCH_TIMEOUT_SECONDS = 10

# Far above any real thumbnail (~30 KB) or avatar (~50 KB). It exists because
# read() is otherwise unbounded and this writes straight to disk.
MAX_IMAGE_BYTES = 8 * 1024 * 1024

# Renamed into place from this, rather than written to the destination directly.
# The suffix is distinct from storage.py's EXPORT_TEMP_SUFFIX so a sweeper can
# tell the two apart.
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

    # dest.is_file() above is the *only* cache check there is, and nothing ever
    # re-downloads an image that exists — so a write cut short (a container
    # killed by `docker compose up --build` mid-refresh, a dropped connection)
    # used to leave a truncated JPEG that every later call accepted as a
    # complete cached image, permanently. os.replace is atomic within a
    # directory, so the destination only ever appears whole.
    temp = dest.with_name(dest.name + _TEMP_SUFFIX)
    try:
        temp.write_bytes(body)
        os.replace(temp, dest)
    except OSError:
        temp.unlink(missing_ok=True)
        return None

    return f"{url_prefix}/{dest.name}"


def fetch_image_bytes(image_url: str) -> tuple[bytes, str] | None:
    """A remote image's bytes and content-type, fetched once and handed back
    in memory rather than written to disk — see routers using this for why
    (proxying an avatar that's never been followed/searched-for before, so it
    doesn't earn a permanent local copy the way download_avatar's callers do,
    but still needs a same-origin response to dodge Chrome's ORB — see
    download_avatar's docstring)."""
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
    """Fetch a channel avatar's bytes once and save them locally, returning a
    same-origin path to re-serve it from. Hotlinking Google's image CDN
    directly from the browser turned out to be unreliable — Chrome's Opaque
    Response Blocking (ORB) intermittently rejects it even for a URL that
    loaded fine moments earlier from the same page — so the app fetches once
    server-side (where that doesn't apply) instead of trusting the browser to
    load Google's URL every time."""
    return _download_image(settings.avatars_dir, f"{channel_id}.jpg", avatar_url, "/avatars")


def download_thumbnail(video_id: str, thumbnail_url: str) -> str | None:
    """Same deal as download_avatar, for a video's thumbnail — re-served from
    our own origin instead of every Home/Library/Explore render hotlinking
    YouTube Music's cover CDN (yt3.ggpht.com, lh3.googleusercontent.com)
    directly for every card on screen. Safe to call for videos already known
    (e.g. every entry in a freshly-fetched artist sync, not just new ones) —
    _download_image's on-disk check makes repeat calls a no-op file stat
    rather than a redundant fetch."""
    return _download_image(settings.thumbnails_dir, f"{video_id}.jpg", thumbnail_url, "/thumbnails")


def needs_thumbnail_caching(thumbnail_url: str | None) -> bool:
    """True for a thumbnail still pointing at a remote URL rather than our
    own /thumbnails/{video_id}.jpg — the condition every caller of
    artist_sync.cache_thumbnail queues a background download on.

    Used to key off "ytimg.com in thumbnail_url", back when every thumbnail
    came from yt-dlp/RSS reading i*.ytimg.com stills. YouTube Music's cover
    art never uses that host (yt3.ggpht.com and lh3.googleusercontent.com
    instead — see youtube/urls.py's absolute_thumbnail_url), so that check
    stopped matching anything the day discovery moved to YouTube Music: every
    thumbnail silently stayed an uncached, ORB-flaky hotlink forever. Keying
    off "already local" instead of "old CDN" can't go stale the same way if
    the CDN changes again.

    Absolute http(s) only, which is the honest form of the question: "is
    this something we could actually go and download?". "Not already local"
    was too loose — a cover stored as `/image-proxy?u=…` (which is how
    nearly every track arrives, see music._proxied_cover_url) passed the
    check, and download_thumbnail then handed that relative path to
    urllib.request.Request, which raises ValueError("unknown url type").
    That is not one of the exceptions _download_image catches, so the
    background task died on it every time — measured live, two tracebacks in
    a 400-line log window, and no thumbnail ever cached for any of the 906
    rows in that shape.
    """
    return bool(thumbnail_url) and thumbnail_url.startswith(("http://", "https://"))


def cached_avatar_path(channel_id: str) -> str | None:
    """Same-origin path for an avatar already on disk, or None. Lets a caller
    skip a download for a channel that's already been followed or searched
    for before (see youtube/search.py)."""
    if (settings.avatars_dir / f"{channel_id}.jpg").is_file():
        return f"/avatars/{channel_id}.jpg"
    return None


def proxied_image_url(remote_url: str) -> str:
    """A remote image URL — an avatar, or a song/album/playlist cover —
    wrapped so the browser fetches it through this app's own /image-proxy
    (app/main.py) instead of hotlinking Google's CDN directly. See
    cached_avatar_or_hotlink below, and youtube/music.py's
    _proxied_cover_url, for why."""
    return f"/image-proxy?u={urllib.parse.quote(remote_url, safe='')}"


def track_cover(item) -> str | None:
    """What to draw for a track: its own cover, or the video's still.

    A track is normally stored with the square art YouTube Music serves for
    it. Some are not, and nothing ever revisits a row once written — so a
    track that arrived without a cover kept a blank square forever. Measured
    on the live library: seventeen rows, one whole album, materialized eight
    hours before the release-cover fallback in music.fetch_release landed
    (see its comment — an album's track entries carry `thumbnails: None`,
    since the release has one cover rather than each track having its own).
    That hole is closed at the source now; these rows are simply older than
    the fix and cannot heal themselves.

    Derived rather than backfilled into the column. The fallback needs no
    network call, so storing it would buy nothing and cost the ability to
    tell a real cover from a stand-in — and any row that slips through in
    future is covered without a second repair.

    Lives here, rather than beside the Jinja filter it is also registered as,
    because it is not a rendering concern: the player overlay and the mini
    player set their artwork from ContentOut in JavaScript, never from a
    template. Fixing only the templates left a track's row and card showing a
    cover while the player it opened showed a blank square — which is exactly
    how this was reported the second time.

    Accepts an ORM Content row or one of the remote dataclasses; both carry
    `thumbnail_url` and `video_id`, which is all this reads.
    """
    if item.thumbnail_url:
        return item.thumbnail_url
    still = video_still_url(item.video_id)
    # Proxied like every other remote cover: i.ytimg.com is allowed by the
    # img-src CSP, but going through /image-proxy keeps this on the same
    # path as the rest and dodges the ORB refusals that path exists for.
    return proxied_image_url(still) if still else None


def is_music_video(item) -> bool:
    """Whether this row is a music video rather than the song itself.

    Read off the cover, because that is the only place the distinction
    survives: YouTube Music reports it as `videoType`, but nothing here
    stores that, and a column can't be added to an existing install (the
    schema is created, never migrated). It doesn't need to be stored —
    the two are exactly correlated. A song's cover is square album art on
    Google's image CDN; a video's is a 16:9 still on i.ytimg.com, which is
    the same signature is_video_still matches on. Measured across three
    playlists, the count of i.ytimg covers equalled the count of
    videoType == OMV entries every time (197/197, 93/93, 198/198).

    A row with no cover at all is not a video: it falls back to a still (see
    track_cover above) but that is this app drawing something rather than
    YouTube saying what the recording is.
    """
    stored = getattr(item, "thumbnail_url", None)
    if not stored:
        return False
    return is_video_still(unproxied_image_url(stored))


def unproxied_image_url(url: str) -> str:
    """The remote URL behind one of ours.

    Covers are stored and served proxied — /image-proxy?u=<escaped remote
    url> — and anything that needs to reason about the *remote* URL (what
    host it is on, what size it names) has to get it back out first. Was
    inline in is_music_video until a second caller needed the same three
    lines.

    A URL that isn't one of ours comes back untouched, so this is safe to
    call on a value that may already be remote.
    """
    if not url.startswith("/image-proxy"):
        return url
    _, _, query = url.partition("?")
    return urllib.parse.unquote(urllib.parse.parse_qs(query).get("u", [""])[0])


# What to offer the OS's Now Playing surface, smallest first.
#
# Not one image, and not one big one. iOS picks an entry per surface and it
# will not resize a big one down for a small surface: given only art larger
# than 128 pixels it draws a grey translucent box on the compact player —
# which on a modern iPhone is the Dynamic Island, the exact surface this is
# for. Every cover this app stores is asked for at music.COVER_SIZE (544),
# so the single entry that used to be published was always in that failing
# range.
#
# 512 for the expanded player and the lock screen, 96 for the compact one,
# and 192 between them, all declared with `sizes` so the OS can choose
# rather than guess. They cost one URL each and no request: Google's image
# CDN resizes from the size named in the URL (see youtube/urls.
# cover_url_at_size), so this is the same picture three ways.
NOW_PLAYING_ARTWORK_SIZES = (96, 192, 512)


def track_artwork(item) -> list[dict[str, str]]:
    """The cover a track should hand the OS, at every size it might want.

    Separate from track_cover, which answers what to *draw* in the page —
    one image at one size, into an element whose size the CSS already knows.
    This answers what to publish to MediaSession, where the sizes are the
    whole point (see NOW_PLAYING_ARTWORK_SIZES).

    A cover that cannot be resized comes back as a single entry with no
    declared size rather than as three identical URLs claiming three
    different ones: video stills carry a signed query and ignore the size
    segment entirely (see youtube/urls.video_still_url), and a lie about
    what an image is would be worse than saying nothing.
    """
    cover = track_cover(item)
    if not cover:
        return []

    remote = unproxied_image_url(cover)
    sized = {size: proxied_image_url(cover_url_at_size(remote, size)) for size in NOW_PLAYING_ARTWORK_SIZES}
    if len(set(sized.values())) == 1:
        return [{"src": cover}]
    return [{"src": url, "sizes": f"{size}x{size}"} for size, url in sized.items()]


def cached_avatar_or_hotlink(channel_id: str, remote_url: str | None) -> str | None:
    """A search result's avatar — reused from disk if this artist already
    has one (already followed, or found in an earlier search), proxied
    through this app's own /image-proxy otherwise. This used to download a
    fresh copy for every result instead: measured live, 977 of 1060 avatar
    files on disk (92%, 16.4 MB) were exactly that — orphans nothing ever
    pointed at, because nothing anywhere deletes an avatar. Per the locked
    decision, only an artist someone actually follows earns a local copy now;
    search reuses what exists without creating more.

    Used to hand back `remote_url` for the browser to hotlink directly, but
    Chrome's Opaque Response Blocking rejected a meaningful share of those
    even after the yt3.ggpht.com rewrite — the same problem a followed
    artist's local-copy fetch dodges by fetching server-side instead of
    trusting the browser to load Google's URL. /image-proxy is that same fix
    without a permanent local copy, which an artist nobody's followed yet
    doesn't earn.
    """
    if not remote_url:
        return None
    return cached_avatar_path(channel_id) or proxied_image_url(remote_url)
