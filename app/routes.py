"""The app's page URLs, the paths the address bar shows. Every one serves the same index.html.

Mirrored in static/js/core.js (routePath/classifyPath) and index.html's head script; keep all three in step.
"""

# Library's pinned lists: a detail view with no id.
LIBRARY_LIST_KINDS = ("favorites", "new-uploads", "recently-played", "downloads")

TAB_PATHS = {"home": "/", "library": "/library", "explore": "/explore", "settings": "/settings"}

# Kind -> path prefix for detail views that take an id. yt-artist-songs is /artist/{id}/songs.
_ID_PREFIXES = {
    "user-playlist": "/library/playlists",
    "yt-artist": "/artist",
    "yt-release": "/album",
    "yt-playlist": "/playlist",
    "yt-mood": "/moods",
}


def detail_path(kind: str, detail_id: str | int | None = None) -> str:
    if kind in LIBRARY_LIST_KINDS:
        return f"/library/{kind}"
    if kind == "yt-artist-songs":
        return f"/artist/{detail_id}/songs"
    return f"{_ID_PREFIXES[kind]}/{detail_id}"


# Marks the shell response, so the service worker caches only that as the offline copy of every page.
SHELL_HEADER = "X-App-Shell"

# Route templates for pages.py. Ids aren't validated here: the panel's /partials fetch 404s on a bad one.
SHELL_PATHS = (
    *TAB_PATHS.values(),
    *(detail_path(kind) for kind in LIBRARY_LIST_KINDS),
    "/library/playlists/{playlist_id}",
    "/artist/{browse_id}",
    "/artist/{browse_id}/songs",
    "/album/{browse_id}",
    "/playlist/{playlist_id}",
    "/moods/{slug}",
    "/player/{content_id}",
)
