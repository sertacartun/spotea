import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

# Already-compressed bodies. Matched on path because the decision precedes the response.
_BINARY_PATH_PREFIXES = ("/thumbnails/", "/avatars/", "/static/img/")
_BINARY_PATH_SUFFIXES = ("/stream", "/export")


def _skip_compression(scope: Scope) -> bool:
    path: str = scope.get("path", "")
    if path.startswith(_BINARY_PATH_PREFIXES) or path.endswith(_BINARY_PATH_SUFFIXES):
        return True
    # Never gzip a range request: a compressed 206 no longer matches the file offsets <audio> asked for.
    return any(name == b"range" for name, _value in scope.get("headers", []))


class SelectiveGZipMiddleware(GZipMiddleware):
    """GZip, except for binary and range responses (Starlette only excludes event-stream)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _skip_compression(scope):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# script-src uses a per-request nonce for index.html's single pre-paint inline script.
# media-src/img-src blob: is required: without it offline/prefetched audio and covers fail silently.
_CSP_TEMPLATE = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'nonce-{nonce}'",
        "style-src 'self' 'unsafe-inline'",
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
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Templates read request.state.csp_nonce directly.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce

        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", _CSP_TEMPLATE.format(nonce=nonce))
        for header, value in _STATIC_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def install(app: ASGIApp) -> None:
    """Security headers are added last so they run outermost, covering compressed and error responses."""
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)
