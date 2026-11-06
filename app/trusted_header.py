"""Single sign-on through a reverse proxy that authenticates users and passes their username in a header.

Off unless TRUSTED_AUTH_HEADER is set. The header is only believed when the TCP peer is one of
TRUSTED_PROXY_IPS: anyone who can reach the app directly could otherwise send it themselves. The proxy
must also overwrite (not merely forward) the header, so a browser can't inject its own value.
"""

import ipaddress
import logging
import secrets

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import SESSION_KEY, hash_password
from app.config import Settings
from app.database import SessionLocal
from app.models import User
from app.routers.auth import MAX_USERNAME_LENGTH, MIN_USERNAME_LENGTH, USERNAME_RE

logger = logging.getLogger(__name__)

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Which proxy user the session was opened for, so a later request for the same user skips the lookup.
TRUSTED_USERNAME_KEY = "trusted_username"


def parse_trusted_proxies(raw: str) -> list[Network]:
    """Comma-separated IPs or CIDR ranges; a malformed entry is a startup error, not a silent hole."""
    networks: list[Network] = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            networks.append(ipaddress.ip_network(item, strict=False))
    return networks


def _peer_is_trusted(scope: Scope, networks: list[Network]) -> bool:
    client = scope.get("client")
    if not client:
        return False
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    # An IPv4 peer seen through an IPv6 socket arrives as ::ffff:a.b.c.d.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def _header_value(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def normalize_username(raw: str) -> str | None:
    """The same rules as registration, so a header can never produce a name the form would refuse."""
    username = raw.strip().lower()
    if not (MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH):
        return None
    if not USERNAME_RE.match(username):
        return None
    return username


def _find_or_create_user(username: str, auto_create: bool) -> int | None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == username).first()
        if user is not None:
            return user.id
        if not auto_create:
            return None
        # Random, never disclosed: such an account can only be entered through the proxy.
        db.add(User(username=username, password_hash=hash_password(secrets.token_urlsafe(32))))
        try:
            db.commit()
        except IntegrityError:
            # Two first requests raced; the other one created it.
            db.rollback()
        user = db.query(User).filter(User.username == username).first()
        if user is not None:
            logger.info("trusted header: created account %r", username)
        return user.id if user is not None else None


class TrustedHeaderAuthMiddleware:
    """Must run inside SessionMiddleware: it reads and writes scope["session"]."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.header = (settings.trusted_auth_header or "").strip().lower().encode("latin-1")
        self.networks = parse_trusted_proxies(settings.trusted_proxy_ips)
        self.auto_create = settings.trusted_auth_auto_create
        if self.header and not self.networks:
            logger.warning("TRUSTED_AUTH_HEADER is set but TRUSTED_PROXY_IPS is empty: header ignored")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and self.header
            and self.networks
            and "session" in scope
            and _peer_is_trusted(scope, self.networks)
        ):
            raw = _header_value(scope, self.header)
            username = normalize_username(raw) if raw else None
            session = scope["session"]
            # Already signed in as this proxy user: no DB round-trip on every request.
            if username is not None and not (
                session.get(SESSION_KEY) and session.get(TRUSTED_USERNAME_KEY) == username
            ):
                user_id = await run_in_threadpool(_find_or_create_user, username, self.auto_create)
                if user_id is not None:
                    # Also switches accounts when the proxy now says someone else is signed in.
                    session[SESSION_KEY] = user_id
                    session[TRUSTED_USERNAME_KEY] = username
        await self.app(scope, receive, send)
