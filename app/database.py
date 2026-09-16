from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")

# Sessions are used from worker threads. NullPool: a background refresh's thread pool would
# exhaust QueuePool's 15 connections and stall requests; SQLite connections are cheap to open.
connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    poolclass=NullPool if _is_sqlite else None,
)


if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        # WAL so background writes don't block reads; SQLite turns foreign_keys off per connection.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass
