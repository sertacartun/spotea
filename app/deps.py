from collections.abc import Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import SESSION_KEY
from app.database import SessionLocal
from app.models import User


class NotAuthenticated(Exception):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_login(request: Request) -> None:
    if not request.session.get(SESSION_KEY):
        raise NotAuthenticated()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    # Never fall back to another user: a missing, stale or forged id is simply unauthenticated.
    user_id = request.session.get(SESSION_KEY)
    user = db.get(User, user_id) if user_id else None
    if user is None:
        raise NotAuthenticated()
    return user
