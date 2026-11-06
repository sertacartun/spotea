"""Single sign-on via a trusted reverse-proxy header (app/trusted_header.py)."""

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import SESSION_KEY
from app.config import Settings
from app.main import app as real_app
from app.models import User
from app.trusted_header import (
    TrustedHeaderAuthMiddleware,
    normalize_username,
    parse_trusted_proxies,
)

# Must match conftest.py's bootstrap user; conftest can't be imported as a module.
DEFAULT_USER_ID = 1
DEFAULT_USERNAME = "test-user"

HEADER = "X-Authentik-Username"
PROXY = ("172.20.0.1", 50000)
STRANGER = ("203.0.113.7", 50000)


def _whoami(request: Request) -> JSONResponse:
    return JSONResponse({"user_id": request.session.get(SESSION_KEY)})


def _make_client(client_addr=PROXY, **overrides) -> TestClient:
    config = {
        "trusted_auth_header": HEADER,
        "trusted_proxy_ips": "172.20.0.0/16",
        "trusted_auth_auto_create": False,
        **overrides,
    }
    settings = Settings(secret_key="test", _env_file=None, **config)
    app = Starlette(
        routes=[Route("/whoami", _whoami)],
        # Same order as app/main.py: the header middleware runs inside the session one.
        middleware=[
            Middleware(SessionMiddleware, secret_key="test", session_cookie="t"),
            Middleware(TrustedHeaderAuthMiddleware, settings=settings),
        ],
    )
    return TestClient(app, client=client_addr)


def test_known_user_is_signed_in_from_a_trusted_proxy():
    with _make_client() as c:
        res = c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
    assert res.json() == {"user_id": DEFAULT_USER_ID}


def test_header_is_case_insensitive_and_username_normalized():
    with _make_client() as c:
        res = c.get("/whoami", headers={HEADER.lower(): "  " + DEFAULT_USERNAME.upper() + " "})
    assert res.json() == {"user_id": DEFAULT_USER_ID}


def test_header_from_an_untrusted_peer_is_ignored():
    with _make_client(client_addr=STRANGER) as c:
        res = c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
    assert res.json() == {"user_id": None}


def test_feature_is_off_without_a_header_name():
    with _make_client(trusted_auth_header=None) as c:
        res = c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
    assert res.json() == {"user_id": None}


def test_header_is_ignored_when_no_proxy_is_trusted():
    with _make_client(trusted_proxy_ips="") as c:
        res = c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
    assert res.json() == {"user_id": None}


def test_unknown_user_is_not_created_by_default(db_session):
    with _make_client() as c:
        res = c.get("/whoami", headers={HEADER: "newcomer"})
    assert res.json() == {"user_id": None}
    assert db_session.query(User).filter(User.username == "newcomer").first() is None


def test_unknown_user_is_created_when_auto_create_is_on(db_session):
    with _make_client(trusted_auth_auto_create=True) as c:
        first = c.get("/whoami", headers={HEADER: "Newcomer"})
        second = c.get("/whoami", headers={HEADER: "newcomer"})
    user = db_session.query(User).filter(User.username == "newcomer").one()
    assert first.json() == {"user_id": user.id}
    assert second.json() == {"user_id": user.id}


def test_invalid_username_is_ignored_and_never_created(db_session):
    with _make_client(trusted_auth_auto_create=True) as c:
        res = c.get("/whoami", headers={HEADER: "../admin"})
    assert res.json() == {"user_id": None}
    assert db_session.query(User).count() == 1


def test_session_follows_the_proxy_when_the_user_changes(db_session):
    with _make_client(trusted_auth_auto_create=True) as c:
        c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
        res = c.get("/whoami", headers={HEADER: "someone-else"})
    other = db_session.query(User).filter(User.username == "someone-else").one()
    assert res.json() == {"user_id": other.id}


def test_ipv4_mapped_ipv6_peer_matches_an_ipv4_range():
    with _make_client(client_addr=("::ffff:172.20.0.1", 50000)) as c:
        res = c.get("/whoami", headers={HEADER: DEFAULT_USERNAME})
    assert res.json() == {"user_id": DEFAULT_USER_ID}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Alice", "alice"), (" bob.smith ", "bob.smith"), ("ab", None), ("a b", None), ("x" * 31, None)],
)
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


def test_parse_trusted_proxies_rejects_garbage():
    assert [str(n) for n in parse_trusted_proxies("127.0.0.1, 172.20.0.0/16")] == [
        "127.0.0.1/32",
        "172.20.0.0/16",
    ]
    with pytest.raises(ValueError):
        parse_trusted_proxies("not-an-ip")


def test_real_app_ignores_the_header_by_default():
    """Out of the box (no TRUSTED_AUTH_HEADER), sending the header must not sign anyone in."""
    with TestClient(real_app, client=PROXY) as c:
        res = c.get("/", headers={HEADER: DEFAULT_USERNAME}, follow_redirects=False)
    assert res.status_code in (302, 303, 307)
    assert "/login" in res.headers["location"]
