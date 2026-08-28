"""Explore's interest-based "For you" shelves.

Two routes over one service call (services/recommendations.py), which owns
every decision worth arguing about — caching, sampling, request budget. The
only thing that separates them is `force`: GET takes whatever is cached,
POST /refresh insists on a rebuild.

A separate router rather than more of routers/explore.py: those routes all
live under /artists because they're about artists the user might add, and these
aren't about artists at all.
"""

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


def _recommendations_out(db: Session, user: User, *, force: bool) -> RecommendationsOut:
    # No expiry to pass: a batch is good until the interests behind it change
    # or someone presses Refresh — see services/recommendations.py.
    batch, generated_at = get_recommendations(db, user, force=force)
    return RecommendationsOut(
        interests=parse_interests(user.interests),
        generated_at=generated_at,
        **batch,
    )


@router.get("", response_model=RecommendationsOut)
def read_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RecommendationsOut:
    """Whatever the current batch is — served straight from the
    cache when there is a fresh one, which is the normal case for opening the
    Explore tab. Only builds (and only then goes near YouTube) when there
    isn't one, so this staying slow on first load is expected, not a bug."""
    return _recommendations_out(db, user, force=False)


@router.post("/refresh", response_model=RecommendationsOut)
def refresh_recommendations(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RecommendationsOut:
    """Always rebuilds, which also resamples which interests get searched — so
    a library with more interests than one run covers sees the rest of them
    this way.

    There is no dedicated refresh control in the UI: this is what the app-wide
    "Refresh artists" button calls alongside POST /artists/refresh, so the one
    button means "go and look at everything again"."""
    return _recommendations_out(db, user, force=True)
