from __future__ import annotations

import sqlite3
import os
from datetime import datetime, timezone

import pytest

from ankiweb.identity import IdentityDatabase, MigrationError, SCHEMA_VERSION


def test_migrations_are_ordered_idempotent_and_configure_connections(tmp_path):
    path = tmp_path / "app" / "app.db"
    first = IdentityDatabase(path)
    assert first.migrate() == SCHEMA_VERSION
    assert first.migrate() == SCHEMA_VERSION

    # A new process/object can safely run startup migrations again.
    restarted = IdentityDatabase(path)
    assert restarted.migrate() == SCHEMA_VERSION
    with restarted.connect() as conn:
        versions = [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == list(range(1, SCHEMA_VERSION + 1))
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5_000
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_journal'"
        ).fetchone() is not None


def test_newer_unknown_schema_blocks_startup(tmp_path):
    db = IdentityDatabase(tmp_path / "app.db")
    db.migrate()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
            (SCHEMA_VERSION + 1, "future", datetime.now(timezone.utc).isoformat()),
        )
    with pytest.raises(MigrationError, match="newer"):
        IdentityDatabase(db.path).migrate()


def test_existing_database_permissions_are_repaired(tmp_path):
    path = tmp_path / "app.db"
    path.touch(mode=0o644)
    os.chmod(path, 0o644)
    db = IdentityDatabase(path)
    db.migrate()
    assert path.stat().st_mode & 0o777 == 0o600


def test_database_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target.db"
    target.touch(mode=0o644)
    os.chmod(target, 0o644)
    link = tmp_path / "app.db"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        IdentityDatabase(link).connect()
    assert target.stat().st_mode & 0o777 == 0o644


def test_database_hardlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "target.db"
    target.touch(mode=0o644)
    os.chmod(target, 0o644)
    link = tmp_path / "app.db"
    os.link(target, link)
    with pytest.raises(ValueError, match="hard-linked"):
        IdentityDatabase(link).connect()
    assert target.stat().st_mode & 0o777 == 0o644


def test_migration_failure_rolls_back_version_and_ddl(tmp_path, monkeypatch):
    """A failed version cannot be recorded as applied or leave its DDL behind."""
    from ankiweb.identity import database as module

    def broken(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE must_rollback(value TEXT)")
        raise RuntimeError("boom")

    monkeypatch.setattr(module, "MIGRATIONS", ((1, "broken", broken),))
    db = IdentityDatabase(tmp_path / "app.db")
    with pytest.raises(RuntimeError, match="boom"):
        db.migrate()
    with db.connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='must_rollback'"
        ).fetchone() is None
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
