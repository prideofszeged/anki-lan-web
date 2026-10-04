"""SQLite application database and ordered, transactional schema migrations."""
from __future__ import annotations

import os
import errno
import sqlite3
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


class MigrationError(RuntimeError):
    pass


def _migration_1(conn: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE users (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            username_norm TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            global_role TEXT NOT NULL CHECK(global_role IN ('owner','admin','user')),
            state TEXT NOT NULL CHECK(state IN
                ('invited','active','suspended','purge_pending','purged')),
            created_at INTEGER NOT NULL,
            suspended_at INTEGER,
            purge_after INTEGER,
            auth_epoch INTEGER NOT NULL DEFAULT 0 CHECK(auth_epoch >= 0)
        )""",
        """CREATE TABLE credentials (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            password_hash TEXT NOT NULL,
            changed_at INTEGER NOT NULL
        )""",
        """CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash BLOB NOT NULL UNIQUE CHECK(length(token_hash) = 32),
            csrf_hash BLOB NOT NULL CHECK(length(csrf_hash) = 32),
            created_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            auth_epoch INTEGER NOT NULL,
            user_agent_hash TEXT,
            ip_prefix TEXT
        )""",
        """CREATE TABLE account_invites (
            id TEXT PRIMARY KEY,
            token_hash BLOB NOT NULL UNIQUE CHECK(length(token_hash) = 32),
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            intended_username TEXT NOT NULL,
            intended_username_norm TEXT NOT NULL,
            global_role TEXT NOT NULL CHECK(global_role IN ('admin','user')),
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            consumed_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            revoked_at INTEGER
        )""",
        """CREATE TABLE user_quotas (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            storage_bytes INTEGER NOT NULL CHECK(storage_bytes >= 0),
            import_bytes INTEGER NOT NULL CHECK(import_bytes >= 0),
            active_jobs INTEGER NOT NULL CHECK(active_jobs >= 0),
            active_sessions INTEGER NOT NULL CHECK(active_sessions >= 1),
            review_sockets INTEGER NOT NULL CHECK(review_sockets >= 0)
        )""",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            occurred_at INTEGER NOT NULL,
            actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            target_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            resource_type TEXT NOT NULL,
            resource_id TEXT,
            request_id TEXT,
            outcome TEXT NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}'
        )""",
    )
    for statement in statements:
        conn.execute(statement)


def _migration_2(conn: sqlite3.Connection) -> None:
    for statement in (
        "CREATE INDEX sessions_user_expiry ON sessions(user_id, expires_at)",
        "CREATE INDEX invites_expiry ON account_invites(expires_at)",
        "CREATE INDEX audit_occurred ON audit_events(occurred_at, id)",
        "CREATE INDEX audit_actor ON audit_events(actor_user_id, occurred_at)",
    ):
        conn.execute(statement)


def _migration_3(conn: sqlite3.Connection) -> None:
    for statement in (
        """CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            actor_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            resource_type TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            capability TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
                ('queued','running','succeeded','failed','cancelled')),
            idempotency_key TEXT NOT NULL,
            request_hash BLOB NOT NULL CHECK(length(request_hash) = 32),
            generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
            progress_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            finished_at INTEGER,
            UNIQUE(actor_user_id, idempotency_key)
        )""",
        """CREATE TABLE job_artifacts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            path_key TEXT NOT NULL,
            sha256 BLOB NOT NULL CHECK(length(sha256) = 32),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            expires_at INTEGER,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE job_journal (
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            sequence INTEGER NOT NULL CHECK(sequence >= 0),
            phase TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            committed_at INTEGER NOT NULL,
            PRIMARY KEY(job_id, sequence)
        )""",
        "CREATE INDEX jobs_resource_state ON jobs(resource_type, resource_id, state)",
        "CREATE INDEX job_artifacts_expiry ON job_artifacts(expires_at)",
    ):
        conn.execute(statement)


def _migration_4(conn: sqlite3.Connection) -> None:
    for statement in (
        """CREATE TABLE deck_shares (
            id TEXT PRIMARY KEY,
            owner_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('draft','active','archived')),
            current_release INTEGER,
            created_at INTEGER NOT NULL
        )""",
        """CREATE TABLE share_members (
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')),
            state TEXT NOT NULL CHECK(state IN ('active','removed')),
            joined_at INTEGER NOT NULL,
            removed_at INTEGER,
            PRIMARY KEY(share_id,user_id)
        )""",
        """CREATE TABLE share_invites (
            id TEXT PRIMARY KEY,
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE CASCADE,
            token_hash BLOB NOT NULL UNIQUE CHECK(length(token_hash)=32),
            role TEXT NOT NULL CHECK(role IN ('editor','viewer')),
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            intended_user_id TEXT REFERENCES users(id) ON DELETE RESTRICT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            consumed_at INTEGER,
            consumed_by TEXT REFERENCES users(id) ON DELETE RESTRICT,
            revoked_at INTEGER
        )""",
        "CREATE INDEX share_members_user ON share_members(user_id,state,share_id)",
        "CREATE INDEX share_invites_expiry ON share_invites(expires_at)",
    ):
        conn.execute(statement)


def _migration_5(conn: sqlite3.Connection) -> None:
    for statement in (
        """CREATE TABLE share_releases (
            id TEXT PRIMARY KEY,
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE CASCADE,
            version INTEGER NOT NULL CHECK(version >= 1),
            manifest_path TEXT NOT NULL,
            bundle_path TEXT NOT NULL,
            bundle_sha256 TEXT NOT NULL CHECK(length(bundle_sha256)=64),
            created_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            created_at INTEGER NOT NULL,
            UNIQUE(share_id,version)
        )""",
        """CREATE TABLE share_subscriptions (
            id TEXT PRIMARY KEY,
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE RESTRICT,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            mode TEXT NOT NULL CHECK(mode IN ('follow')),
            installed_release INTEGER NOT NULL CHECK(installed_release >= 1),
            target_deck_id INTEGER,
            conflict_policy TEXT NOT NULL CHECK(conflict_policy IN ('retire','mirror')),
            created_at INTEGER NOT NULL,
            UNIQUE(share_id,user_id)
        )""",
        """CREATE TABLE subscription_entities (
            subscription_id TEXT NOT NULL REFERENCES share_subscriptions(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            recipient_id TEXT NOT NULL,
            base_hash TEXT NOT NULL CHECK(length(base_hash)=64),
            media_name TEXT,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(subscription_id,entity_type,source_id)
        )""",
        "CREATE INDEX share_releases_share ON share_releases(share_id,version)",
        "CREATE INDEX subscriptions_user ON share_subscriptions(user_id,share_id)",
    ):
        conn.execute(statement)


def _migration_6(conn: sqlite3.Connection) -> None:
    # Existing v5 artifacts have no trustworthy manifest digest. Keep the column
    # nullable so the migration is non-destructive; release validation fails closed
    # for those rows and requires them to be republished.
    conn.execute(
        """ALTER TABLE share_releases ADD COLUMN manifest_sha256 TEXT
           CHECK(manifest_sha256 IS NULL OR length(manifest_sha256)=64)"""
    )


def _migration_7(conn: sqlite3.Connection) -> None:
    for statement in (
        """CREATE TABLE update_conflicts (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            subscription_id TEXT NOT NULL REFERENCES share_subscriptions(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            source_id TEXT NOT NULL,
            field_name TEXT,
            base_hash TEXT NOT NULL CHECK(length(base_hash)=64),
            local_hash TEXT NOT NULL CHECK(length(local_hash)=64),
            upstream_hash TEXT NOT NULL CHECK(length(upstream_hash)=64),
            resolution TEXT CHECK(resolution IN ('mine','upstream','manual')),
            resolved_by TEXT REFERENCES users(id) ON DELETE RESTRICT,
            resolved_at INTEGER,
            UNIQUE(job_id,entity_type,source_id,field_name)
        )""",
        """CREATE TABLE workspace_revisions (
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK(revision >= 0),
            changed_by TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            changed_at INTEGER NOT NULL,
            PRIMARY KEY(share_id,entity_type,entity_id)
        )""",
        """CREATE TABLE workspace_comments (
            id TEXT PRIMARY KEY,
            share_id TEXT NOT NULL REFERENCES deck_shares(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            author_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            body TEXT NOT NULL,
            resolved_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""",
        "CREATE INDEX update_conflicts_subscription ON update_conflicts(subscription_id,job_id)",
        "CREATE INDEX workspace_comments_entity ON workspace_comments(share_id,entity_type,entity_id)",
    ):
        conn.execute(statement)


MIGRATIONS: tuple[Migration, ...] = (
    (1, "identity control tables", _migration_1),
    (2, "identity lookup indexes", _migration_2),
    (3, "durable job journals", _migration_3),
    (4, "share membership and invitations", _migration_4),
    (5, "immutable releases and subscriptions", _migration_5),
    (6, "bind immutable release manifests", _migration_6),
    (7, "sharing updates and collaboration", _migration_7),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


class IdentityDatabase:
    """Connection factory for the durable application/control-plane database.

    A new connection is used per operation, making the object safe to share between
    request threads. SQLite serializes writes; ``BEGIN IMMEDIATE`` moves contention to
    the transaction boundary instead of failing halfway through an account mutation.
    """

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = busy_timeout_ms
        self._migration_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValueError(f"identity database may not be a symlink: {self.path}") from exc
            raise
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ValueError(f"identity database must be a regular file: {self.path}")
            if file_stat.st_nlink != 1:
                raise ValueError(
                    f"identity database may not be hard-linked: {self.path}"
                )
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {int(self.busy_timeout_ms)}")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def migrate(self) -> int:
        versions = [item[0] for item in MIGRATIONS]
        if versions != list(range(1, len(versions) + 1)):
            raise MigrationError("migrations must be contiguous and begin at version 1")
        with self._migration_lock, self.connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )"""
            )
            applied = {row["version"]: row["name"] for row in conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version")}
            unknown = sorted(set(applied) - set(versions))
            if unknown:
                raise MigrationError(f"database schema is newer than this application: {unknown}")
            for version, name, migration in MIGRATIONS:
                if version in applied:
                    if applied[version] != name:
                        raise MigrationError(
                            f"migration {version} name mismatch: {applied[version]!r} != {name!r}")
                    continue
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Another process may have migrated before our write lock was acquired.
                    row = conn.execute(
                        "SELECT name FROM schema_migrations WHERE version = ?", (version,)
                    ).fetchone()
                    if row is None:
                        migration(conn)
                        conn.execute(
                            "INSERT INTO schema_migrations(version, name, applied_at) VALUES(?,?,?)",
                            (version, name, datetime.now(timezone.utc).isoformat()),
                        )
                    elif row["name"] != name:
                        raise MigrationError(f"migration {version} name mismatch")
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
            return SCHEMA_VERSION

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
