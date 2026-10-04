from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from ankiweb.identity import IdentityDatabase
from ankiweb.identity.repository import (
    AuthorizationError, ConflictError, ExpiredTokenError, InvalidTokenError,
    NotFoundError,
)

from .models import (
    DeckShare, MembershipState, ShareDetail, ShareInvite, ShareMembership,
    ShareRole, ShareState,
)


class ShareNotFoundError(AuthorizationError):
    """Deliberately indistinguishable from a nonexistent private share."""


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.timestamp())


def _datetime(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


def _share(row: sqlite3.Row) -> DeckShare:
    return DeckShare(
        id=row["id"], owner_user_id=row["owner_user_id"], name=row["name"],
        state=ShareState(row["state"]), current_release=row["current_release"],
        created_at=_datetime(row["created_at"]),
    )


def _membership(row: sqlite3.Row) -> ShareMembership:
    return ShareMembership(
        share_id=row["share_id"], user_id=row["user_id"], role=ShareRole(row["role"]),
        state=MembershipState(row["state"]), joined_at=_datetime(row["joined_at"]),
        removed_at=_datetime(row["removed_at"]),
    )


def _invite(row: sqlite3.Row) -> ShareInvite:
    return ShareInvite(
        id=row["id"], share_id=row["share_id"], role=ShareRole(row["role"]),
        created_by=row["created_by"], intended_user_id=row["intended_user_id"],
        created_at=_datetime(row["created_at"]), expires_at=_datetime(row["expires_at"]),
        consumed_at=_datetime(row["consumed_at"]), consumed_by=row["consumed_by"],
        revoked_at=_datetime(row["revoked_at"]),
    )


class SharingRepository:
    def __init__(self, database: IdentityDatabase) -> None:
        self.database = database

    @staticmethod
    def _active_user(conn: sqlite3.Connection, user_id: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM users WHERE id=? AND state='active'", (user_id,),
        ).fetchone() is not None

    @staticmethod
    def _membership_row(
        conn: sqlite3.Connection, share_id: str, user_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM share_members WHERE share_id=? AND user_id=?
               AND state='active'""",
            (share_id, user_id),
        ).fetchone()

    @classmethod
    def _require_member(
        cls, conn: sqlite3.Connection, share_id: str, user_id: str,
    ) -> sqlite3.Row:
        row = cls._membership_row(conn, share_id, user_id)
        if row is None:
            raise ShareNotFoundError("share not found")
        return row

    @classmethod
    def _require_owner(
        cls, conn: sqlite3.Connection, share_id: str, user_id: str,
    ) -> sqlite3.Row:
        row = cls._require_member(conn, share_id, user_id)
        if row["role"] != ShareRole.OWNER.value:
            raise AuthorizationError("share owner permission required")
        return row

    @staticmethod
    def _audit(
        conn: sqlite3.Connection, *, now: datetime, actor: str, action: str,
        share_id: str, target: str | None = None, metadata: dict | None = None,
    ) -> None:
        conn.execute(
            """INSERT INTO audit_events(occurred_at,actor_user_id,target_user_id,action,
               resource_type,resource_id,outcome,metadata_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (_epoch(now), actor, target, action, "share", share_id, "success",
             json.dumps(metadata or {}, sort_keys=True, separators=(",", ":"))),
        )

    def create_share(self, *, actor_user_id: str, name: str, now: datetime) -> DeckShare:
        cleaned = " ".join(name.split())
        if not cleaned or len(cleaned) > 128:
            raise ValueError("share name must contain 1 to 128 characters")
        with self.database.transaction() as conn:
            if not self._active_user(conn, actor_user_id):
                raise AuthorizationError("active account required")
            share_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO deck_shares(id,owner_user_id,name,state,created_at)
                   VALUES(?,?,?,'draft',?)""",
                (share_id, actor_user_id, cleaned, _epoch(now)),
            )
            conn.execute(
                """INSERT INTO share_members(share_id,user_id,role,state,joined_at)
                   VALUES(?,?,'owner','active',?)""",
                (share_id, actor_user_id, _epoch(now)),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.created",
                share_id=share_id,
            )
            return _share(conn.execute(
                "SELECT * FROM deck_shares WHERE id=?", (share_id,)
            ).fetchone())

    def list_shares(self, actor_user_id: str) -> list[DeckShare]:
        with self.database.read() as conn:
            rows = conn.execute(
                """SELECT s.* FROM deck_shares s JOIN share_members m ON m.share_id=s.id
                   WHERE m.user_id=? AND m.state='active'
                   ORDER BY s.created_at DESC,s.id""",
                (actor_user_id,),
            ).fetchall()
            return [_share(row) for row in rows]

    def get_detail(self, *, actor_user_id: str, share_id: str) -> ShareDetail:
        with self.database.read() as conn:
            mine = self._require_member(conn, share_id, actor_user_id)
            row = conn.execute("SELECT * FROM deck_shares WHERE id=?", (share_id,)).fetchone()
            if row is None:
                raise ShareNotFoundError("share not found")
            members = conn.execute(
                """SELECT * FROM share_members WHERE share_id=? AND state='active'
                   ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'editor' THEN 1 ELSE 2 END,
                   joined_at,user_id""",
                (share_id,),
            ).fetchall()
            return ShareDetail(
                share=_share(row), membership=_membership(mine),
                members=tuple(_membership(item) for item in members),
            )

    def create_invite(
        self, *, actor_user_id: str, share_id: str, token_hash: bytes,
        role: ShareRole, intended_user_id: str | None, now: datetime,
        expires_at: datetime,
    ) -> ShareInvite:
        if role is ShareRole.OWNER:
            raise AuthorizationError("owner role cannot be invited")
        with self.database.transaction() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            share = conn.execute("SELECT state FROM deck_shares WHERE id=?", (share_id,)).fetchone()
            if share is None:
                raise ShareNotFoundError("share not found")
            if share["state"] == ShareState.ARCHIVED.value:
                raise ConflictError("archived share does not accept invitations")
            if intended_user_id is not None and not self._active_user(conn, intended_user_id):
                raise NotFoundError("intended user not found")
            invite_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO share_invites(id,share_id,token_hash,role,created_by,
                   intended_user_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)""",
                (invite_id, share_id, token_hash, role.value, actor_user_id,
                 intended_user_id, _epoch(now), _epoch(expires_at)),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, target=intended_user_id,
                action="share.invite.created", share_id=share_id,
                metadata={"role": role.value},
            )
            return _invite(conn.execute(
                "SELECT * FROM share_invites WHERE id=?", (invite_id,)
            ).fetchone())

    def accept_invite(
        self, *, actor_user_id: str, token_hash: bytes, now: datetime,
    ) -> ShareMembership:
        with self.database.transaction() as conn:
            if not self._active_user(conn, actor_user_id):
                raise AuthorizationError("active account required")
            row = conn.execute(
                "SELECT * FROM share_invites WHERE token_hash=?", (token_hash,),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                raise InvalidTokenError("share invitation is invalid")
            if row["consumed_at"] is not None:
                if row["consumed_by"] == actor_user_id:
                    existing = self._membership_row(conn, row["share_id"], actor_user_id)
                    if existing is not None:
                        return _membership(existing)
                raise InvalidTokenError("share invitation was already used")
            if row["expires_at"] <= _epoch(now):
                raise ExpiredTokenError("share invitation has expired")
            if row["intended_user_id"] is not None and row["intended_user_id"] != actor_user_id:
                raise AuthorizationError("share invitation is intended for another user")
            share = conn.execute(
                "SELECT state FROM deck_shares WHERE id=?", (row["share_id"],)
            ).fetchone()
            if share is None or share["state"] == ShareState.ARCHIVED.value:
                raise InvalidTokenError("share invitation is no longer active")
            conn.execute(
                """INSERT INTO share_members(share_id,user_id,role,state,joined_at,removed_at)
                   VALUES(?,?,?,'active',?,NULL)
                   ON CONFLICT(share_id,user_id) DO UPDATE SET role=excluded.role,
                   state='active',joined_at=excluded.joined_at,removed_at=NULL""",
                (row["share_id"], actor_user_id, row["role"], _epoch(now)),
            )
            changed = conn.execute(
                """UPDATE share_invites SET consumed_at=?,consumed_by=?
                   WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL""",
                (_epoch(now), actor_user_id, row["id"]),
            ).rowcount
            if changed != 1:
                raise InvalidTokenError("share invitation was consumed concurrently")
            self._audit(
                conn, now=now, actor=actor_user_id, target=actor_user_id,
                action="share.member.joined", share_id=row["share_id"],
                metadata={"role": row["role"]},
            )
            return _membership(conn.execute(
                "SELECT * FROM share_members WHERE share_id=? AND user_id=?",
                (row["share_id"], actor_user_id),
            ).fetchone())

    def revoke_invite(
        self, *, actor_user_id: str, share_id: str, invite_id: str, now: datetime,
    ) -> None:
        with self.database.transaction() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            changed = conn.execute(
                """UPDATE share_invites SET revoked_at=? WHERE id=? AND share_id=?
                   AND consumed_at IS NULL AND revoked_at IS NULL""",
                (_epoch(now), invite_id, share_id),
            ).rowcount
            if changed != 1:
                raise NotFoundError("active share invitation not found")
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.invite.revoked",
                share_id=share_id,
            )

    def remove_member(
        self, *, actor_user_id: str, share_id: str, target_user_id: str, now: datetime,
    ) -> None:
        with self.database.transaction() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            target = self._membership_row(conn, share_id, target_user_id)
            if target is None:
                raise NotFoundError("active member not found")
            if target["role"] == ShareRole.OWNER.value:
                raise AuthorizationError("owner must be transferred, not removed")
            conn.execute(
                """UPDATE share_members SET state='removed',removed_at=?
                   WHERE share_id=? AND user_id=?""",
                (_epoch(now), share_id, target_user_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, target=target_user_id,
                action="share.member.removed", share_id=share_id,
            )

