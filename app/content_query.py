"""Query building blocks shared so lists, counts and queues agree on what they select."""


from sqlalchemy import or_
from sqlalchemy.orm import Query, Session, joinedload

from app.models import Artist, Content

DEFAULT_PAGE_SIZE = 50

def followed_artists(db: Session, user_id: int | None = None) -> Query[Artist]:
    """Followed artists, newest first; followed=False rows are placeholders and must stay excluded."""
    query = db.query(Artist).filter(Artist.followed.is_(True))
    if user_id is not None:
        query = query.filter(Artist.user_id == user_id)
    return query.order_by(Artist.added_at.desc())


def _content_query(
    db: Session, user_id: int, filter: str = "", artist_id: int | None = None
) -> Query[Content]:
    """The single definition of a filter/artist selection, ordered but unpaginated."""
    # Unfavorited Explore previews are hidden everywhere except Recently Played.
    query = db.query(Content).filter(Content.user_id == user_id)
    if filter != "__played__":
        query = query.filter(Content.is_preview.is_(False))

    if artist_id is not None:
        query = query.filter(Content.artist_id == artist_id)

    needs_feed_join = filter not in ("", "__favorites__", "__played__")
    if needs_feed_join:
        query = query.join(Artist)

    if filter == "__favorites__":
        query = query.filter(Content.is_favorite.is_(True))
    elif filter == "__played__":
        query = query.filter(Content.last_played_at.isnot(None))
    elif filter:
        pattern = f"%{filter}%"
        query = query.filter(or_(Artist.name.ilike(pattern), Content.title.ilike(pattern)))

    order_column = {
        "__played__": Content.last_played_at,
    }.get(filter, Content.published_at)
    return query.order_by(order_column.desc())


def query_content_page(
    db: Session,
    user_id: int,
    page: int = 1,
    filter: str = "",
    page_size: int = DEFAULT_PAGE_SIZE,
    artist_id: int | None = None,
) -> tuple[list[Content], int, int]:
    # joinedload stays out of _content_query: query_content_ids never loads rows.
    query = _content_query(db, user_id, filter=filter, artist_id=artist_id).options(
        joinedload(Content.artist)
    )

    total_items = query.count()
    total_pages = max(1, -(-total_items // page_size))
    page = min(max(1, page), total_pages)
    items = query.offset((page - 1) * page_size).limit(page_size).all()

    return items, page, total_pages


def count_content(db: Session, user_id: int, filter: str = "", artist_id: int | None = None) -> int:
    """Count via _content_query, never a hand-rolled func.count, so it matches the list."""
    return _content_query(db, user_id, filter=filter, artist_id=artist_id).count()


# Cap on a "Play all" queue, which the client keeps in sessionStorage.
QUEUE_MAX_ITEMS = 1000


def query_content_ids(
    db: Session, user_id: int, filter: str = "", artist_id: int | None = None
) -> list[int]:
    """Ids for "Play all", in the same order as the track list."""
    query = _content_query(db, user_id, filter=filter, artist_id=artist_id)
    return [row[0] for row in query.with_entities(Content.id).limit(QUEUE_MAX_ITEMS).all()]


def query_content_by_ids(db: Session, user_id: int, ids: list[int]) -> list[Content]:
    """Rows in the client's order (a shuffle the server can't reproduce); foreign ids are dropped."""
    if not ids:
        return []
    rows = (
        db.query(Content)
        .options(joinedload(Content.artist))
        .filter(Content.user_id == user_id, Content.id.in_(ids[:QUEUE_MAX_ITEMS]))
        .all()
    )
    by_id = {row.id: row for row in rows}
    return [by_id[content_id] for content_id in ids[:QUEUE_MAX_ITEMS] if content_id in by_id]
