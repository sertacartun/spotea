from fastapi.testclient import TestClient

import app.routers.auth as auth_router
from app.auth import DUMMY_PASSWORD_HASH
from app.main import app

# Must match conftest.py's own DEFAULT_USERNAME/DEFAULT_USER_PASSWORD
# (the bootstrap account _init_schema seeds) — duplicated rather than
# imported since tests/ isn't a real package (no __init__.py) and importing
# conftest as `tests.conftest` re-executes its module-level setup as a
# second, different module instance.
DEFAULT_ACCOUNT_USERNAME = "test-user"
DEFAULT_ACCOUNT_PASSWORD = "test-password"


def _client_at(ip: str) -> TestClient:
    """A TestClient with its own fake source IP, distinct from the default
    ("testclient", 50000) every plain `TestClient(app)` shares — the failed-
    login counter in routers/auth.py is keyed by client IP and lives in a
    module-level registry for the whole test session, so a rate-limit test
    reusing the default address would trip (or reset) the counter every other
    login test in this file relies on. Each test below picks its own address
    so none of them can see each other's attempts."""
    return TestClient(app, client=(ip, 12345))


def test_protected_route_redirects_to_login_when_unauthenticated():
    with TestClient(app) as anon:
        res = anon.get("/", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_login_with_wrong_password_is_rejected():
    with _client_at("203.0.113.1") as anon:
        res = anon.post(
            "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": "definitely-not-it"}
        )

    assert res.status_code == 401


def test_login_with_unknown_username_is_rejected():
    with _client_at("203.0.113.2") as anon:
        res = anon.post("/login", data={"username": "nobody", "password": "whatever123"})

    assert res.status_code == 401


def test_an_unknown_username_still_runs_a_real_bcrypt_check(monkeypatch):
    """The actual timing bug: login_submit used to short-circuit on `account
    is None` and skip verify_password entirely, which measured 6.0ms against
    419.8ms for a real mismatch — a 70x gap that told an attacker whether a
    name was registered even though the error message doesn't."""
    calls = []
    real_verify = auth_router.verify_password

    def recording_verify(password, password_hash):
        calls.append(password_hash)
        return real_verify(password, password_hash)

    monkeypatch.setattr(auth_router, "verify_password", recording_verify)

    with _client_at("203.0.113.3") as anon:
        res = anon.post("/login", data={"username": "nobody", "password": "whatever123"})

    assert res.status_code == 401
    assert calls == [DUMMY_PASSWORD_HASH]


def test_repeated_failed_logins_from_one_client_are_rate_limited(monkeypatch):
    """Past the cap, the response comes back without a bcrypt check at all —
    that's the point, not just a different status code. A saturated 40-slot
    threadpool from concurrent 420ms bcrypt calls is what an unthrottled sync
    login_submit allowed."""
    calls = []
    monkeypatch.setattr(
        auth_router, "verify_password", lambda password, password_hash: calls.append(1) or False
    )

    with _client_at("203.0.113.4") as anon:
        for _ in range(auth_router.MAX_FAILED_LOGIN_ATTEMPTS):
            res = anon.post(
                "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": "wrong"}
            )
            assert res.status_code == 401

        assert len(calls) == auth_router.MAX_FAILED_LOGIN_ATTEMPTS

        # One more, this time with the *correct* password — still blocked,
        # because the lockout is per client IP, not per outcome.
        res = anon.post(
            "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": DEFAULT_ACCOUNT_PASSWORD}
        )

    assert res.status_code == 429
    assert len(calls) == auth_router.MAX_FAILED_LOGIN_ATTEMPTS  # no bcrypt call for the 11th


def test_a_different_client_is_not_caught_by_another_clients_lockout():
    with _client_at("203.0.113.5") as attacker:
        for _ in range(auth_router.MAX_FAILED_LOGIN_ATTEMPTS):
            attacker.post("/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": "wrong"})
        assert (
            attacker.post(
                "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": DEFAULT_ACCOUNT_PASSWORD}
            ).status_code
            == 429
        )

    with _client_at("203.0.113.6") as someone_else:
        res = someone_else.post(
            "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": DEFAULT_ACCOUNT_PASSWORD}
        )

    assert res.status_code == 200


def test_a_successful_login_resets_the_failure_count():
    with _client_at("203.0.113.7") as c:
        for _ in range(auth_router.MAX_FAILED_LOGIN_ATTEMPTS - 1):
            c.post("/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": "wrong"})

        assert (
            c.post(
                "/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": DEFAULT_ACCOUNT_PASSWORD}
            ).status_code
            == 200
        )

        # Two more failed attempts, both still under the cap — if the
        # successful login hadn't cleared the count left over from the
        # MAX_FAILED_LOGIN_ATTEMPTS - 1 failures above, the second of these
        # would already be blocked with 429 instead of answering 401.
        statuses = [
            c.post("/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": "wrong"}).status_code
            for _ in range(2)
        ]

    assert statuses == [401, 401]


def test_login_with_correct_password_grants_access(client):
    # `client` (from conftest) has already logged in for real — confirm a
    # protected route is actually reachable now, not just that login itself
    # returned 200.
    res = client.get("/")
    assert res.status_code == 200


def test_logout_revokes_access():
    with TestClient(app) as c:
        c.post("/login", data={"username": DEFAULT_ACCOUNT_USERNAME, "password": DEFAULT_ACCOUNT_PASSWORD})
        assert c.get("/").status_code == 200

        c.post("/logout")
        res = c.get("/", follow_redirects=False)

    assert res.status_code == 303
    assert res.headers["location"] == "/login"


def test_register_creates_account_and_logs_in():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "username": "newuser",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
            follow_redirects=False,
        )
        assert res.status_code == 303
        assert res.headers["location"] == "/#home"
        assert anon.get("/").status_code == 200


def test_register_rejects_duplicate_username():
    # DEFAULT_ACCOUNT_USERNAME is the bootstrap account seeded in conftest,
    # always present.
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "username": DEFAULT_ACCOUNT_USERNAME,
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )

    assert res.status_code == 400
    assert "already taken" in res.text.lower()


def test_register_rejects_mismatched_passwords():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "username": "another",
                "password": "supersecret",
                "confirm_password": "different123",
            },
        )

    assert res.status_code == 400


def test_register_rejects_short_password():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={"username": "shortpw", "password": "short1", "confirm_password": "short1"},
        )

    assert res.status_code == 400


def test_register_rejects_a_username_with_an_at_sign():
    """The field used to be an email address, so this is the shape someone
    upgrading will type first. It has to fail loudly rather than quietly
    creating an account whose name nobody would guess again."""
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={
                "username": "someone@example.com",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )

    assert res.status_code == 400
    assert "username must be" in res.text.lower()


def test_register_rejects_a_too_short_username():
    with TestClient(app) as anon:
        res = anon.post(
            "/register",
            data={"username": "ab", "password": "supersecret", "confirm_password": "supersecret"},
        )

    assert res.status_code == 400


def test_a_username_is_stored_and_matched_lowercased(db_session):
    """Registered in mixed case, logged in in another — the column carries a
    plain unique constraint rather than a case-insensitive collation, so the
    normalization at the router is the only thing making that safe."""
    from app.models import User

    with TestClient(app) as anon:
        anon.post(
            "/register",
            data={
                "username": "MixedCase",
                "password": "supersecret",
                "confirm_password": "supersecret",
            },
        )

    assert db_session.query(User).filter(User.username == "mixedcase").first() is not None

    with _client_at("203.0.113.8") as anon:
        res = anon.post("/login", data={"username": "MIXEDCASE", "password": "supersecret"})

    assert res.status_code == 200
