"""Explore's interest-based "For you" shelves, served from a per-user cache that goes stale twice a day."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db, require_login
from app.interests import parse_interests
from app.models import User
from app.schemas import RecommendationsOut
from app.services.recommendations import get_recommendations

router = APIRouter(
    prefix="/recommendations", tags=["recommendations"], dependencies=[Depends(require_login)]
)


@router.get("", response_model=RecommendationsOut)
def read_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RecommendationsOut:
    """The current batch; only builds (and hits YouTube) when there is none or it is stale."""
    batch, generated_at = get_recommendations(db, user)
    return RecommendationsOut(
        interests=parse_interests(user.interests),
        generated_at=generated_at,
        **batch,
    )
