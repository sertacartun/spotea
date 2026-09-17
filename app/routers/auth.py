import re

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import DUMMY_PASSWORD_HASH, SESSION_KEY, hash_password, verify_password
from app.deps import get_db
from app.models import User
from app.progress import ProgressRegistry
from app.templating import templates

router = APIRouter()

# Applied after lowercasing; excluding whitespace is what lets usernames compare with plain ==.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 30
MIN_PASSWORD_LENGTH = 8
# bcrypt ignores bytes past 72 — two passwords sharing that prefix would verify as equal.
MAX_PASSWORD_LENGTH = 72

# Each login costs a real bcrypt check in the shared threadpool; without a per-IP
# limit, failed logins alone could starve every other sync route.
MAX_FAILED_LOGIN_ATTEMPTS = 10
LOGIN_LOCKOUT_WINDOW_SECONDS = 60

_failed_login_attempts: ProgressRegistry[str, int] = ProgressRegistry(
    ttl_seconds=LOGIN_LOCKOUT_WINDOW_SECONDS
)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get(SESSION_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None, "username": ""})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    key = _client_key(request)
    if (_failed_login_attempts.get(key) or 0) >= MAX_FAILED_LOGIN_ATTEMPTS:
        # No DB query, no bcrypt: the point is to stop spending CPU on this client.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Try again in a minute.", "username": username},
            status_code=429,
        )

    normalized_username = username.strip().lower()
    user = db.query(User).filter(User.username == normalized_username).first()
    # Always a real bcrypt check so an unknown username takes as long as a wrong password.
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, password_hash)
    # One generic message: don't reveal whether the username exists.
    if user is None or not password_ok:
        _failed_login_attempts.set(key, (_failed_login_attempts.get(key) or 0) + 1)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password", "username": username},
            status_code=401,
        )

    _failed_login_attempts.discard(key)
    request.session[SESSION_KEY] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    if request.session.get(SESSION_KEY):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "register.html", {"error": None, "username": ""})


def _validate_registration(
    username: str, password: str, confirm_password: str, db: Session
) -> str | None:
    if not (MIN_USERNAME_LENGTH <= len(username) <= MAX_USERNAME_LENGTH) or not USERNAME_RE.match(
        username
    ):
        return (
            f"Username must be {MIN_USERNAME_LENGTH}-{MAX_USERNAME_LENGTH} characters, "
            "using letters, numbers, dots, dashes or underscores"
        )
    if not (MIN_PASSWORD_LENGTH <= len(password.encode("utf-8")) <= MAX_PASSWORD_LENGTH):
        return f"Password must be {MIN_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} characters"
    if password != confirm_password:
        return "Passwords do not match"
    if db.query(User).filter(User.username == username).first() is not None:
        return "Username already taken"
    return None


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    normalized_username = username.strip().lower()
    error = _validate_registration(normalized_username, password, confirm_password, db)
    if error:
        return templates.TemplateResponse(
            request, "register.html", {"error": error, "username": username}, status_code=400
        )

    user = User(username=normalized_username, password_hash=hash_password(password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent registrations can both pass the pre-check; the unique constraint decides.
        db.rollback()
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": "Username already taken", "username": username},
            status_code=400,
        )
    db.refresh(user)

    request.session[SESSION_KEY] = user.id
    return RedirectResponse(url="/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
