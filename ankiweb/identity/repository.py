"""SQL repositories for identity and control-plane metadata."""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .database import IdentityDatabase
from .models import (
    AccountInvite, AccountState, AuditEvent, Credential, GlobalRole, Session, User, UserQuota,
)


class IdentityError(RuntimeError):
    """Base class for safe-to-handle identity errors."""


class ConflictError(IdentityError):
    pass


class NotFoundError(IdentityError):
    pass


class AuthorizationError(IdentityError):
    pass


class InvalidTokenError(IdentityError):
    pass


class ExpiredTokenError(IdentityError):
    pass


class LastOwnerError(ConflictError):
    pass


def normalize_username(username: str) -> str:
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not normalized or len(normalized) > 64:
        raise ValueError("username must contain 1 to 64 characters")
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in normalized):
        raise ValueError("username may not contain whitespace or control characters")
    if "/" in normalized or "\\" in normalized:
        raise ValueError("username may not contain path separators")
    return normalized


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.timestamp())


def _datetime(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, tz=timezone.utc) if value is not None else None


def _user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"], username=row["username"], username_norm=row["username_norm"],
        display_name=row["display_name"], global_role=GlobalRole(row["global_role"]),
        state=AccountState(row["state"]), created_at=_datetime(row["created_at"]),
        suspended_at=_datetime(row["suspended_at"]), purge_after=_datetime(row["purge_after"]),
        auth_epoch=row["auth_epoch"],
    )


def _session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"], user_id=row["user_id"], created_at=_datetime(row["created_at"]),
        last_seen_at=_datetime(row["last_seen_at"]), expires_at=_datetime(row["expires_at"]),
        auth_epoch=row["auth_epoch"], user_agent_hash=row["user_agent_hash"],
        ip_prefix=row["ip_prefix"],
    )


def _invite(row: sqlite3.Row) -> AccountInvite:
    return AccountInvite(
        id=row["id"], created_by=row["created_by"],
        intended_username=row["intended_username"],
        intended_username_norm=row["intended_username_norm"],
        global_role=GlobalRole(row["global_role"]), expires_at=_datetime(row["expires_at"]),
        created_at=_datetime(row["created_at"]), consumed_at=_datetime(row["consumed_at"]),
        revoked_at=_datetime(row["revoked_at"]),
    )


class IdentityRepository:
    def __init__(self, database: IdentityDatabase) -> None:
        self.database = database

    def initialize(self) -> int:
        return self.database.migrate()

    @staticmethod
    def _insert_user(
        conn: sqlite3.Connection, *, username: str, display_name: str, role: GlobalRole,
        state: AccountState, now: datetime,
    ) -> User:
        user_id = str(uuid.uuid4())
        username_norm = normalize_username(username)
        try:
            conn.execute(
                """INSERT INTO users(id, username, username_norm, display_name, global_role,
                   state, created_at) VALUES(?,?,?,?,?,?,?)""",
                (user_id, username.strip(), username_norm, display_name.strip(), role.value,
                 state.value, _epoch(now)),
            )
            quota = UserQuota(user_id=user_id)
            conn.execute(
                """INSERT INTO user_quotas(user_id, storage_bytes, import_bytes, active_jobs,
                   active_sessions, review_sockets) VALUES(?,?,?,?,?,?)""",
                (user_id, quota.storage_bytes, quota.import_bytes, quota.active_jobs,
                 quota.active_sessions, quota.review_sockets),
            )
        except sqlite3.IntegrityError as exc:
            if "username_norm" in str(exc):
                raise ConflictError("username is already in use") from exc
            raise
        return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def create_user(
        self, *, username: str, display_name: str = "", role: GlobalRole = GlobalRole.USER,
        state: AccountState = AccountState.ACTIVE, now: datetime,
    ) -> User:
        with self.database.transaction() as conn:
            return self._insert_user(
                conn, username=username, display_name=display_name or username, role=role,
                state=state, now=now,
            )

    def bootstrap_owner(
        self, *, username: str, display_name: str, password_hash: str, now: datetime,
        provision: Callable[[User], None] | None = None,
        rollback_provision: Callable[[User], None] | None = None,
    ) -> User:
        """Atomically create the sole bootstrap identity and its credential."""
        provisioned: User | None = None
        try:
            with self.database.transaction() as conn:
                if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    raise ConflictError("identity database is already provisioned")
                user = self._insert_user(
                    conn, username=username, display_name=display_name, role=GlobalRole.OWNER,
                    state=AccountState.ACTIVE, now=now,
                )
                conn.execute(
                    "INSERT INTO credentials(user_id, password_hash, changed_at) VALUES(?,?,?)",
                    (user.id, password_hash, _epoch(now)),
                )
                if provision is not None:
                    provision(user)
                    provisioned = user
                return user
        except BaseException:
            if provisioned is not None and rollback_provision is not None:
                rollback_provision(provisioned)
            raise

    def validate_invite_token(self, token_hash: bytes, *, now: datetime) -> AccountInvite:
        """Cheap preflight before password hashing; acceptance still rechecks atomically."""
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM account_invites WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if not row or row["revoked_at"] is not None or row["consumed_at"] is not None:
            raise InvalidTokenError("invitation is invalid or already used")
        if row["expires_at"] <= _epoch(now):
            raise ExpiredTokenError("invitation has expired")
        return _invite(row)

    def get_user(self, user_id: str) -> User | None:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return _user(row) if row else None

    def has_active_owner(self) -> bool:
        with self.database.read() as conn:
            return conn.execute(
                "SELECT 1 FROM users WHERE global_role = 'owner' AND state = 'active' LIMIT 1"
            ).fetchone() is not None

    def get_user_by_username(self, username: str) -> User | None:
        username_norm = normalize_username(username)
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username_norm = ?", (username_norm,)
            ).fetchone()
            return _user(row) if row else None

    @staticmethod
    def _credential(conn: sqlite3.Connection, user_id: str) -> Credential | None:
        row = conn.execute("SELECT * FROM credentials WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        return Credential(row["user_id"], row["password_hash"], _datetime(row["changed_at"]))

    def get_credential(self, user_id: str) -> Credential | None:
        with self.database.read() as conn:
            return self._credential(conn, user_id)

    def set_password_hash(self, user_id: str, password_hash: str, *, now: datetime) -> None:
        with self.database.transaction() as conn:
            if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                raise NotFoundError("user not found")
            conn.execute(
                """INSERT INTO credentials(user_id, password_hash, changed_at) VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash,
                   changed_at=excluded.changed_at""",
                (user_id, password_hash, _epoch(now)),
            )

    def replace_password_and_revoke(
        self, user_id: str, password_hash: str, *, now: datetime,
    ) -> None:
        with self.database.transaction() as conn:
            row = conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise NotFoundError("user not found")
            conn.execute(
                """INSERT INTO credentials(user_id, password_hash, changed_at) VALUES(?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash,
                   changed_at=excluded.changed_at""",
                (user_id, password_hash, _epoch(now)),
            )
            conn.execute("UPDATE users SET auth_epoch = auth_epoch + 1 WHERE id = ?", (user_id,))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    def create_invite(
        self, *, token_hash: bytes, created_by: str, intended_username: str,
        role: GlobalRole, now: datetime, expires_at: datetime,
    ) -> AccountInvite:
        if role is GlobalRole.OWNER:
            raise AuthorizationError("owner accounts cannot be created by invitation")
        with self.database.transaction() as conn:
            actor = conn.execute("SELECT * FROM users WHERE id = ?", (created_by,)).fetchone()
            if not actor or actor["state"] != AccountState.ACTIVE.value:
                raise AuthorizationError("active owner or admin required")
            actor_role = GlobalRole(actor["global_role"])
            if actor_role not in (GlobalRole.OWNER, GlobalRole.ADMIN):
                raise AuthorizationError("active owner or admin required")
            if role is GlobalRole.ADMIN and actor_role is not GlobalRole.OWNER:
                raise AuthorizationError("only owners may invite administrators")
            invite_id = str(uuid.uuid4())
            norm = normalize_username(intended_username)
            conn.execute(
                """INSERT INTO account_invites(id, token_hash, created_by, intended_username,
                   intended_username_norm, global_role, created_at, expires_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (invite_id, token_hash, created_by, intended_username.strip(), norm, role.value,
                 _epoch(now), _epoch(expires_at)),
            )
            return _invite(conn.execute(
                "SELECT * FROM account_invites WHERE id = ?", (invite_id,)
            ).fetchone())

    def accept_invite(
        self, *, token_hash: bytes, password_hash: str, display_name: str,
        now: datetime, provision: Callable[[User], None] | None = None,
        rollback_provision: Callable[[User], None] | None = None,
    ) -> User:
        """Create the account and consume the bearer token in one write transaction."""
        provisioned: User | None = None
        try:
            with self.database.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM account_invites WHERE token_hash = ?", (token_hash,)
                ).fetchone()
                if not row or row["revoked_at"] is not None or row["consumed_at"] is not None:
                    raise InvalidTokenError("invitation is invalid or already used")
                if row["expires_at"] <= _epoch(now):
                    raise ExpiredTokenError("invitation has expired")
                user = self._insert_user(
                    conn, username=row["intended_username"],
                    display_name=display_name or row["intended_username"],
                    role=GlobalRole(row["global_role"]), state=AccountState.INVITED, now=now,
                )
                conn.execute(
                    "INSERT INTO credentials(user_id, password_hash, changed_at) VALUES(?,?,?)",
                    (user.id, password_hash, _epoch(now)),
                )
                if provision is not None:
                    provision(user)
                    provisioned = user
                conn.execute(
                    "UPDATE users SET state = ? WHERE id = ?",
                    (AccountState.ACTIVE.value, user.id),
                )
                changed = conn.execute(
                    """UPDATE account_invites SET consumed_at = ?, consumed_by = ?
                       WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL""",
                    (_epoch(now), user.id, row["id"]),
                ).rowcount
                if changed != 1:  # defensive; BEGIN IMMEDIATE should make this unreachable
                    raise InvalidTokenError("invitation was consumed concurrently")
                return _user(conn.execute(
                    "SELECT * FROM users WHERE id = ?", (user.id,)
                ).fetchone())
        except BaseException:
            if provisioned is not None and rollback_provision is not None:
                rollback_provision(provisioned)
            raise

    def revoke_invite(self, invite_id: str, *, actor_user_id: str, now: datetime) -> None:
        with self.database.transaction() as conn:
            actor = conn.execute("SELECT global_role, state FROM users WHERE id = ?",
                                 (actor_user_id,)).fetchone()
            if not actor or actor["state"] != "active" or actor["global_role"] not in {
                "owner", "admin"
            }:
                raise AuthorizationError("active owner or admin required")
            changed = conn.execute(
                """UPDATE account_invites SET revoked_at = ?
                   WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL""",
                (_epoch(now), invite_id),
            ).rowcount
            if not changed:
                raise NotFoundError("active invitation not found")

    def create_session(
        self, *, token_hash: bytes, csrf_hash: bytes, user_id: str, now: datetime,
        expires_at: datetime, user_agent_hash: str | None = None,
        ip_prefix: str | None = None,
    ) -> Session:
        with self.database.transaction() as conn:
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not user or user["state"] != AccountState.ACTIVE.value:
                raise AuthorizationError("active account required")
            quota = conn.execute(
                "SELECT active_sessions FROM user_quotas WHERE user_id = ?", (user_id,)
            ).fetchone()
            conn.execute(
                "DELETE FROM sessions WHERE user_id = ? AND (expires_at <= ? OR auth_epoch != ?)",
                (user_id, _epoch(now), user["auth_epoch"]),
            )
            limit = quota["active_sessions"]
            count = conn.execute(
                "SELECT count(*) AS n FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchone()["n"]
            if count >= limit:
                # Evict enough oldest-idle sessions to make room for this login.
                conn.execute(
                    """DELETE FROM sessions WHERE id IN (
                       SELECT id FROM sessions WHERE user_id = ?
                       ORDER BY last_seen_at ASC, created_at ASC LIMIT ?)""",
                    (user_id, count - limit + 1),
                )
            session_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO sessions(id, user_id, token_hash, csrf_hash, created_at,
                   last_seen_at, expires_at, auth_epoch, user_agent_hash, ip_prefix)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (session_id, user_id, token_hash, csrf_hash, _epoch(now), _epoch(now),
                 _epoch(expires_at), user["auth_epoch"], user_agent_hash, ip_prefix),
            )
            return _session(conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone())

    def validate_session(
        self, token_hash: bytes, *, now: datetime, refreshed_expires_at: datetime | None = None,
    ) -> Session | None:
        with self.database.read() as conn:
            row = conn.execute(
                """SELECT s.* FROM sessions s JOIN users u ON u.id = s.user_id
                   WHERE s.token_hash = ? AND s.expires_at > ? AND s.auth_epoch = u.auth_epoch
                   AND u.state = 'active'""",
                (token_hash, _epoch(now)),
            ).fetchone()
            if not row:
                return None

        created = _datetime(row["created_at"])
        absolute = created.timestamp() + 90 * 86400
        if now.timestamp() >= absolute:
            with self.database.transaction() as conn:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
            return None
        expiry = row["expires_at"]
        if refreshed_expires_at is not None:
            expiry = min(_epoch(refreshed_expires_at), int(absolute))

        # Avoid a global SQLite write lock on every asset/request. Five-minute metadata
        # granularity preserves a 30-day idle window while bounding write amplification.
        refresh_due = _epoch(now) - row["last_seen_at"] >= 300
        if refresh_due:
            with self.database.transaction() as conn:
                changed = conn.execute(
                    """UPDATE sessions SET last_seen_at = ?, expires_at = ?
                       WHERE id = ? AND token_hash = ? AND auth_epoch = (
                         SELECT auth_epoch FROM users WHERE id = sessions.user_id
                         AND state = 'active'
                       )""",
                    (_epoch(now), expiry, row["id"], token_hash),
                ).rowcount
                if not changed:
                    return None
            updated = dict(row)
            updated["last_seen_at"] = _epoch(now)
            updated["expires_at"] = expiry
            return _session(updated)  # type: ignore[arg-type]
        return _session(row)

    def revoke_session(self, session_id: str, *, user_id: str) -> bool:
        with self.database.transaction() as conn:
            return bool(conn.execute(
                "DELETE FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
            ).rowcount)

    def active_session_by_id(self, session_id: str, *, now: datetime) -> Session | None:
        with self.database.read() as conn:
            row = conn.execute(
                """SELECT s.* FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.id=? AND s.expires_at>? AND s.auth_epoch=u.auth_epoch
                   AND u.state='active'""",
                (session_id, _epoch(now)),
            ).fetchone()
        if row is None:
            return None
        created = _datetime(row["created_at"])
        if now.timestamp() >= created.timestamp() + 90 * 86400:
            return None
        return _session(row)

    def list_sessions(self, user_id: str, *, now: datetime) -> list[Session]:
        with self.database.read() as conn:
            rows = conn.execute(
                """SELECT s.* FROM sessions s JOIN users u ON u.id=s.user_id
                   WHERE s.user_id=? AND s.expires_at>? AND s.auth_epoch=u.auth_epoch
                   AND u.state='active' ORDER BY s.last_seen_at DESC, s.created_at DESC""",
                (user_id, _epoch(now)),
            ).fetchall()
            return [_session(row) for row in rows]

    def csrf_hash(self, session_id: str, *, user_id: str) -> bytes | None:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT csrf_hash FROM sessions WHERE id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
            return bytes(row["csrf_hash"]) if row else None

    def rotate_csrf(self, session_id: str, *, user_id: str, csrf_hash: bytes) -> bool:
        with self.database.transaction() as conn:
            return bool(conn.execute(
                "UPDATE sessions SET csrf_hash=? WHERE id=? AND user_id=?",
                (csrf_hash, session_id, user_id),
            ).rowcount)

    def revoke_all_sessions(self, user_id: str, *, now: datetime) -> int:
        """Epoch bump provides immediate revocation even to cached session readers."""
        with self.database.transaction() as conn:
            changed = conn.execute(
                "UPDATE users SET auth_epoch = auth_epoch + 1 WHERE id = ?", (user_id,)
            ).rowcount
            if not changed:
                raise NotFoundError("user not found")
            deleted = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
            return deleted

    def set_role(self, user_id: str, role: GlobalRole) -> User:
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise NotFoundError("user not found")
            if row["global_role"] == "owner" and role is not GlobalRole.OWNER:
                active_owners = conn.execute(
                    "SELECT count(*) AS n FROM users WHERE global_role='owner' AND state='active'"
                ).fetchone()["n"]
                if row["state"] == "active" and active_owners <= 1:
                    raise LastOwnerError("cannot demote the last active owner")
            conn.execute("UPDATE users SET global_role = ? WHERE id = ?", (role.value, user_id))
            return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def set_state(
        self, user_id: str, state: AccountState, *, now: datetime,
        purge_after: datetime | None = None,
    ) -> User:
        with self.database.transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                raise NotFoundError("user not found")
            if row["global_role"] == "owner" and row["state"] == "active" and state != AccountState.ACTIVE:
                active_owners = conn.execute(
                    "SELECT count(*) AS n FROM users WHERE global_role='owner' AND state='active'"
                ).fetchone()["n"]
                if active_owners <= 1:
                    raise LastOwnerError("cannot deactivate the last active owner")
            suspended = _epoch(now) if state is AccountState.SUSPENDED else None
            epoch_delta = 0 if state is AccountState.ACTIVE else 1
            conn.execute(
                """UPDATE users SET state=?, suspended_at=?, purge_after=?,
                   auth_epoch=auth_epoch+? WHERE id=?""",
                (state.value, suspended, _epoch(purge_after) if purge_after else None,
                 epoch_delta, user_id),
            )
            if state is not AccountState.ACTIVE:
                conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            return _user(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    def get_quota(self, user_id: str) -> UserQuota:
        with self.database.read() as conn:
            row = conn.execute("SELECT * FROM user_quotas WHERE user_id = ?", (user_id,)).fetchone()
            if not row:
                raise NotFoundError("quota not found")
            return UserQuota(**dict(row))

    def set_quota(self, quota: UserQuota) -> None:
        with self.database.transaction() as conn:
            changed = conn.execute(
                """UPDATE user_quotas SET storage_bytes=?, import_bytes=?, active_jobs=?,
                   active_sessions=?, review_sockets=? WHERE user_id=?""",
                (quota.storage_bytes, quota.import_bytes, quota.active_jobs,
                 quota.active_sessions, quota.review_sockets, quota.user_id),
            ).rowcount
            if not changed:
                raise NotFoundError("quota not found")

    def append_audit(
        self, *, now: datetime, action: str, resource_type: str, outcome: str,
        actor_user_id: str | None = None, target_user_id: str | None = None,
        resource_id: str | None = None, request_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        # Deliberately reject bytes: content/media does not belong in metadata audit rows.
        if any(isinstance(value, (bytes, bytearray, memoryview))
               for value in (metadata or {}).values()):
            raise ValueError("binary content is not audit metadata")
        payload = json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)
        if len(payload.encode("utf-8")) > 16_384:
            raise ValueError("audit metadata exceeds 16 KiB")
        with self.database.transaction() as conn:
            cursor = conn.execute(
                """INSERT INTO audit_events(occurred_at, actor_user_id, target_user_id,
                   action, resource_type, resource_id, request_id, outcome, metadata_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (_epoch(now), actor_user_id, target_user_id, action, resource_type,
                 resource_id, request_id, outcome, payload),
            )
            return int(cursor.lastrowid)

    def list_audit(self, *, limit: int = 100) -> list[AuditEvent]:
        if not 1 <= limit <= 1_000:
            raise ValueError("limit must be between 1 and 1000")
        with self.database.read() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_events ORDER BY occurred_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [AuditEvent(
            id=row["id"], occurred_at=_datetime(row["occurred_at"]),
            actor_user_id=row["actor_user_id"], target_user_id=row["target_user_id"],
            action=row["action"], resource_type=row["resource_type"],
            resource_id=row["resource_id"], request_id=row["request_id"], outcome=row["outcome"],
            metadata=json.loads(row["metadata_json"]),
        ) for row in rows]
