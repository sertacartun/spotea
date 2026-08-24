"""Response-level middleware: compression and security headers.

Both are cross-cutting and neither belongs to a router, so they live here
rather than growing main.py.
"""

import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

# Paths whose bodies are already compressed, so gzipping them costs CPU and
# saves nothing: the audio itself, cached JPEGs, the export archive, and the
# static images. Matched on the path rather than the response's content type
# because the decision has to be made before the response starts.
_BINARY_PATH_PREFIXES = ("/thumbnails/", "/avatars/", "/static/img/")
_BINARY_PATH_SUFFIXES = ("/stream", "/export")


def _skip_compression(scope: Scope) -> bool:
    path: str = scope.get("path", "")
    if path.startswith(_BINARY_PATH_PREFIXES) or path.endswith(_BINARY_PATH_SUFFIXES):
        return True
    # A range request must never be compressed. The <audio> element issues
    # these constantly while seeking, and the byte offsets it asks for are
    # offsets into the *file* — a gzipped 206 answers with a different number
    # of bytes than the range it claims to be, which the element cannot
    # reconcile. Checked structurally rather than relying on the path list
    # above staying complete.
    return any(name == b"range" for name, _value in scope.get("headers", []))


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZip, minus the responses where it is useless or actively wrong.

    Starlette's own middleware excludes only `text/event-stream`, so on its
    own it would compress `/content/{id}/stream` — including its 206 Partial
    Content responses — and re-compress every cached JPEG. This bypasses it
    for those and leaves the ordinary text responses (HTML, JSON, CSS, JS),
    which is where the whole benefit is: the app's own pages compress by
    83-94%.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _skip_compression(scope):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# One inline <script> exists in the whole app — index.html's pre-paint tab
# resolver, which has to run before first paint and so cannot be moved to an
# external file without putting a blocking request in front of every render.
# It gets a per-request nonce instead, which is what lets script-src stay free
# of 'unsafe-inline'.
#
# style-src does keep 'unsafe-inline': _base.html sets a background colour in a
# style attribute on <html> precisely so the page isn't white before the
# stylesheet arrives, and _icons.html hides the SVG sprite the same way.
# Neither can move into the stylesheet without reintroducing what they prevent,
# and an injected style is a far smaller problem than an injected script.
#
# Remote images are the one external origin the app has: uncached thumbnails
# come straight from i*.ytimg.com and channel avatars from yt3.ggpht.com (see
# youtube/urls.py's absolute_thumbnail_url, which rewrites to that host
# deliberately). media-src allows data: for the one-sample silent clip
# player.js uses to unlock WebKit's autoplay gate, and blob: for the next
# track's audio, which home/overlay.js pulls down during the current one so
# the handoff touches no network (see cacheUpcomingAudio). Neither widens
# anything: a blob: URL can only ever be minted by this page, out of bytes it
# already fetched under 'self'. It is also not optional — with media-src
# unchanged the element refuses the URL outright ("violates the following
# Content Security Policy directive"), lands on readyState 0 with
# MEDIA_ERR_SRC_NOT_SUPPORTED, and the track never plays at all. Every
# source-level test still passed while that was true.
_CSP_TEMPLATE = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
        # blob: is for cover art read back out of offline storage — a track
        # kept on the device carries its own copy of the cover, and it is
        # shown from a blob: URL because the /image-proxy URL it was fetched
        # from is a request, which is the one thing an offline track has to
        # avoid (see static/js/offline.js). Without this the audio would play
        # and the artwork alone would silently fail to render.
        "img-src 'self' blob: https://*.ytimg.com https://*.ggpht.com",
        "media-src 'self' data: blob:",
        "connect-src 'self'",
        "worker-src 'self'",
        "manifest-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

_STATIC_HEADERS = {
    # The app never needs to be framed, and the Downloads modal's "Clear all"
    # is exactly the kind of control clickjacking targets.
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Nothing here uses any of these; saying so keeps a compromised page from
    # asking.
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds the headers the app shipped without any of.

    Without a CSP, an injected attribute in client-built markup is a full
    session takeover rather than a nuisance — which is the state escapeHtml's
    quote bug left the app in. This is the structural half of that fix: it
    limits what an injection can do even when one gets through.
    """

    async def dispatch(self, request: Request, call_next):
        # Read by index.html via `request.state`, which Jinja2Templates puts
        # in every template's context — so no router has to pass it along.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP_TEMPLATE.format(nonce=nonce))
        for header, value in _STATIC_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def install(app: ASGIApp) -> None:
    """Wire both up. Order matters: security headers are added last and so run
    outermost, which is what puts them on compressed and error responses too.
    """
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)
