"""Connection settings for the SQLite engine."""

from app.database import engine


def test_sqlite_runs_in_wal_mode_with_foreign_keys_on():
    """WAL so writers don't block readers; foreign_keys because SQLite leaves them off per connection."""
    with engine.connect() as conn:
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()

    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_a_missing_added_column_is_created_on_an_existing_database():
    """`create_all` never adds a missing column to an existing table, so startup has to."""
    import sqlite3

    import pytest

    from app.main import _ADDED_COLUMNS, _add_missing_columns

    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35+ to set the test up")

    column, _ddl = _ADDED_COLUMNS["content"][0]

    def columns():
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(content)")}

    assert column in columns(), "the model's column is missing from a fresh database"

    with engine.begin() as conn:
        conn.exec_driver_sql(f"ALTER TABLE content DROP COLUMN {column}")
    assert column not in columns()

    _add_missing_columns()
    assert column in columns()

    # Idempotent: runs on every start.
    _add_missing_columns()
    assert column in columns()


def test_a_missing_not_null_column_is_backfilled_on_an_existing_row():
    """The exact bug this regresses: update_checks shipped, then grew `enabled` in the same
    unreleased branch. A database already holding a row from before that column existed must
    not crash every read that touches it (app.services.update_check.available_update and
    friends all do `db.get(UpdateCheck, ...)`, which selects every mapped column)."""
    import sqlite3

    import pytest

    from app.main import _add_missing_columns

    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35+ to set the test up")

    with engine.begin() as conn:
        # A row from before `enabled` existed — the exact shape the real database was in.
        # Dropping the column afterward removes it from underneath the row it already has.
        conn.exec_driver_sql(
            "INSERT INTO update_checks (id, enabled, checked_at) VALUES (1, 1, '2026-01-01 00:00:00')"
        )
        conn.exec_driver_sql("ALTER TABLE update_checks DROP COLUMN enabled")

    def row():
        with engine.connect() as conn:
            return conn.exec_driver_sql("SELECT id, enabled FROM update_checks").fetchone()

    with pytest.raises(Exception, match="no such column"):
        row()

    _add_missing_columns()

    fetched = row()
    assert fetched is not None, "the bootstrap row must still be there, not recreated"
    assert fetched[1] == 1, "backfilled to enabled — the same default a fresh row gets"


def test_every_added_not_null_column_carries_a_default():
    """SQLite's ALTER TABLE ADD COLUMN refuses NOT NULL without a DEFAULT to backfill existing
    rows; a nullable one, like content.artist_credit, needs neither and backfills to NULL."""
    from app.main import _ADDED_COLUMNS

    for table, columns in _ADDED_COLUMNS.items():
        for column, ddl in columns:
            if "NOT NULL" in ddl.upper():
                assert "DEFAULT" in ddl.upper(), f"{table}.{column} is NOT NULL but carries no DEFAULT"


def test_an_obsolete_column_is_dropped_from_an_existing_database():
    """Added back nullable because SQLite refuses ADD COLUMN NOT NULL without a default."""
    import sqlite3

    import pytest

    from app.main import _drop_removed_columns

    if sqlite3.sqlite_version_info < (3, 35):
        pytest.skip("ALTER TABLE DROP COLUMN needs SQLite 3.35+")

    def columns():
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}

    assert "refresh_interval_minutes" not in columns()

    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE users ADD COLUMN refresh_interval_minutes INTEGER")
    assert "refresh_interval_minutes" in columns()

    _drop_removed_columns()
    assert "refresh_interval_minutes" not in columns()

    # Idempotent: runs on every start.
    _drop_removed_columns()
    assert "refresh_interval_minutes" not in columns()


def test_an_email_column_is_renamed_and_shortened_to_a_username():
    """Upgrade off email logins: the account keeps working under the part before the @."""
    from app.main import _rename_email_to_username

    def columns():
        with engine.connect() as conn:
            return {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(users)")}

    with engine.connect() as conn:
        original = conn.exec_driver_sql("SELECT username FROM users WHERE id = 1").scalar()

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE users RENAME COLUMN username TO email")
            conn.exec_driver_sql("UPDATE users SET email = 'someone@example.com' WHERE id = 1")
        assert "username" not in columns()

        _rename_email_to_username()

        assert "username" in columns()
        assert "email" not in columns()
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT username FROM users WHERE id = 1").scalar() == "someone"

        # Idempotent: no email column is left to find.
        _rename_email_to_username()
        assert "username" in columns()
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE users SET username = ? WHERE id = 1", (original,))


def test_two_addresses_sharing_a_local_part_both_keep_their_full_address():
    """The username column is unique, so colliding local parts keep their full address."""
    from app.auth import hash_password
    from app.main import _rename_email_to_username

    with engine.connect() as conn:
        original = conn.exec_driver_sql("SELECT username FROM users WHERE id = 1").scalar()

    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE users RENAME COLUMN username TO email")
            conn.exec_driver_sql("UPDATE users SET email = 'shared@one.com' WHERE id = 1")
            conn.exec_driver_sql(
                "INSERT INTO users (email, password_hash, created_at, audio_quality) "
                "VALUES (?, ?, datetime('now'), 'low')",
                ("shared@two.com", hash_password("x")),
            )

        _rename_email_to_username()

        with engine.connect() as conn:
            names = {row[0] for row in conn.exec_driver_sql("SELECT username FROM users")}
        assert names == {"shared@one.com", "shared@two.com"}
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM users WHERE id != 1")
            conn.exec_driver_sql("UPDATE users SET username = ? WHERE id = 1", (original,))
