"""Connection settings for the SQLite engine."""

from app.database import engine


def test_sqlite_runs_in_wal_mode_with_foreign_keys_on():
    """WAL because a writer in rollback-journal mode blocks readers outright,
    and the background refresh commits once per channel across dozens of them.
    foreign_keys because SQLite leaves them off per connection, so the FKs the
    schema declares were never actually enforced."""
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_a_missing_added_column_is_created_on_an_existing_database():
    """`create_all` adds a missing table to an existing database but never a
    missing column, so a model that grows one leaves every SELECT against
    `content` failing with "no such column". For an app whose entire state is
    one SQLite file the owner cannot discard, that has to heal itself at
    startup rather than being an upgrade note.

    Exercised by actually removing the column and putting it back, because the
    only interesting case is the one where the file and the model disagree —
    which a freshly built test database never is.
    """
    import sqlite3

    import pytest

    from app.main import _ADDED_CONTENT_COLUMNS, _add_missing_columns

    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35+ to set the test up")

    column, _ddl = _ADDED_CONTENT_COLUMNS[0]

    def columns():
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(content)")}

    assert column in columns(), "the model's column is missing from a fresh database"

    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE content DROP COLUMN {column}")
    assert column not in columns()

    _add_missing_columns()
    assert column in columns()

    # Idempotent: this runs on every start, and every start after the first
    # finds nothing to do.
    _add_missing_columns()
    assert column in columns()


def test_every_added_column_is_nullable():
    """These are added to a table that already has rows, so there is nothing
    to backfill them with. A NOT NULL column here would fail outright on any
    database that isn't empty — which is every database this code path exists
    for."""
    from app.main import _ADDED_CONTENT_COLUMNS

    for column, ddl in _ADDED_CONTENT_COLUMNS:
        assert "NOT NULL" not in ddl.upper(), f"{column} is declared NOT NULL"
        assert "DEFAULT" not in ddl.upper(), f"{column} carries a DEFAULT, which needs a considered migration"
