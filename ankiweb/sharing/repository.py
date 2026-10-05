from __future__ import annotations

import json
import re
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
    ShareRelease, ShareRole, ShareState, ShareSubscription, SubscriptionEntity,
    UpdateConflict, WorkspaceComment, WorkspaceRevision,
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


def _release(row: sqlite3.Row) -> ShareRelease:
    return ShareRelease(
        id=row["id"], share_id=row["share_id"], version=row["version"],
        manifest_path=row["manifest_path"], bundle_path=row["bundle_path"],
        manifest_sha256=row["manifest_sha256"], bundle_sha256=row["bundle_sha256"],
        created_by=row["created_by"],
        created_at=_datetime(row["created_at"]),
    )


def _subscription(row: sqlite3.Row) -> ShareSubscription:
    return ShareSubscription(
        id=row["id"], share_id=row["share_id"], user_id=row["user_id"], mode=row["mode"],
        installed_release=row["installed_release"], target_deck_id=row["target_deck_id"],
        conflict_policy=row["conflict_policy"], created_at=_datetime(row["created_at"]),
    )


def _entity(row: sqlite3.Row) -> SubscriptionEntity:
    return SubscriptionEntity(
        subscription_id=row["subscription_id"], entity_type=row["entity_type"],
        source_id=row["source_id"], recipient_id=row["recipient_id"],
        base_hash=row["base_hash"], media_name=row["media_name"],
        updated_at=_datetime(row["updated_at"]),
    )


def _conflict(row: sqlite3.Row) -> UpdateConflict:
    return UpdateConflict(
        id=row["id"], job_id=row["job_id"], subscription_id=row["subscription_id"],
        entity_type=row["entity_type"], source_id=row["source_id"],
        field_name=row["field_name"], base_hash=row["base_hash"],
        local_hash=row["local_hash"], upstream_hash=row["upstream_hash"],
        resolution=row["resolution"], resolved_by=row["resolved_by"],
        resolved_at=_datetime(row["resolved_at"]),
    )


def _revision(row: sqlite3.Row) -> WorkspaceRevision:
    return WorkspaceRevision(
        share_id=row["share_id"], entity_type=row["entity_type"],
        entity_id=row["entity_id"], revision=row["revision"],
        changed_by=row["changed_by"], changed_at=_datetime(row["changed_at"]),
    )


def _comment(row: sqlite3.Row) -> WorkspaceComment:
    return WorkspaceComment(
        id=row["id"], share_id=row["share_id"], entity_type=row["entity_type"],
        entity_id=row["entity_id"], author_user_id=row["author_user_id"],
        body=row["body"], resolved_at=_datetime(row["resolved_at"]),
        created_at=_datetime(row["created_at"]), updated_at=_datetime(row["updated_at"]),
    )


def _comment_body(body: str) -> str:
    cleaned = body.strip()
    if not cleaned or len(cleaned) > 10_000 or "<" in cleaned or ">" in cleaned:
        raise ValueError("comment must be plain Markdown of 1 to 10000 characters")
    links = re.findall(r"\[[^\]]*\]\(([^)]+)\)", cleaned)
    if any(
        not (target.startswith("#") or target.startswith("/") and not target.startswith("//"))
        for target in links
    ) or "://" in cleaned:
        raise ValueError("comment links must be local")
    return cleaned


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

    def require_owner(self, *, actor_user_id: str, share_id: str) -> DeckShare:
        with self.database.read() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            row = conn.execute("SELECT * FROM deck_shares WHERE id=?", (share_id,)).fetchone()
            if row is None:
                raise ShareNotFoundError("share not found")
            return _share(row)

    def require_member(self, *, actor_user_id: str, share_id: str) -> ShareMembership:
        with self.database.read() as conn:
            return _membership(self._require_member(conn, share_id, actor_user_id))

    def require_editor(self, *, actor_user_id: str, share_id: str) -> ShareMembership:
        with self.database.read() as conn:
            row = self._require_member(conn, share_id, actor_user_id)
            if row["role"] not in (ShareRole.OWNER.value, ShareRole.EDITOR.value):
                raise AuthorizationError("share editor permission required")
            return _membership(row)

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

    def next_release_version(self, *, actor_user_id: str, share_id: str) -> int:
        with self.database.read() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            row = conn.execute(
                "SELECT state,current_release FROM deck_shares WHERE id=?", (share_id,),
            ).fetchone()
            if row is None:
                raise ShareNotFoundError("share not found")
            if row["state"] == ShareState.ARCHIVED.value:
                raise ConflictError("archived share cannot publish")
            return int(row["current_release"] or 0) + 1

    def commit_release(
        self, *, actor_user_id: str, share_id: str, version: int,
        manifest_path: str, bundle_path: str, manifest_sha256: str,
        bundle_sha256: str, now: datetime,
    ) -> ShareRelease:
        with self.database.transaction() as conn:
            self._require_owner(conn, share_id, actor_user_id)
            share = conn.execute(
                "SELECT state,current_release FROM deck_shares WHERE id=?", (share_id,),
            ).fetchone()
            expected = int(share["current_release"] or 0) + 1
            if share["state"] == ShareState.ARCHIVED.value or version != expected:
                raise ConflictError("release version changed concurrently")
            release_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO share_releases(id,share_id,version,manifest_path,bundle_path,
                   manifest_sha256,bundle_sha256,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (release_id, share_id, version, manifest_path, bundle_path,
                 manifest_sha256, bundle_sha256, actor_user_id, _epoch(now)),
            )
            conn.execute(
                "UPDATE deck_shares SET current_release=?,state='active' WHERE id=?",
                (version, share_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.release.published",
                share_id=share_id, metadata={
                    "version": version,
                    "bundle_sha256": bundle_sha256,
                    "manifest_sha256": manifest_sha256,
                },
            )
            return _release(conn.execute(
                "SELECT * FROM share_releases WHERE id=?", (release_id,)
            ).fetchone())

    def get_release(
        self, *, actor_user_id: str, share_id: str, version: int,
    ) -> ShareRelease:
        with self.database.read() as conn:
            self._require_member(conn, share_id, actor_user_id)
            row = conn.execute(
                "SELECT * FROM share_releases WHERE share_id=? AND version=?",
                (share_id, version),
            ).fetchone()
            if row is None:
                raise NotFoundError("release not found")
            return _release(row)

    def get_release_for_owner(
        self, *, actor_user_id: str, share_id: str, version: int,
    ) -> ShareRelease:
        self.require_owner(actor_user_id=actor_user_id, share_id=share_id)
        return self.get_release(
            actor_user_id=actor_user_id, share_id=share_id, version=version,
        )

    def commit_follow_install(
        self, *, actor_user_id: str, share_id: str, version: int,
        target_deck_id: int | None, entities: list[dict], now: datetime,
    ) -> ShareSubscription:
        with self.database.transaction() as conn:
            self._require_member(conn, share_id, actor_user_id)
            if not conn.execute(
                "SELECT 1 FROM share_releases WHERE share_id=? AND version=?",
                (share_id, version),
            ).fetchone():
                raise NotFoundError("release not found")
            if conn.execute(
                "SELECT 1 FROM share_subscriptions WHERE share_id=? AND user_id=?",
                (share_id, actor_user_id),
            ).fetchone():
                raise ConflictError("follow subscription already exists")
            subscription_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO share_subscriptions(id,share_id,user_id,mode,installed_release,
                   target_deck_id,conflict_policy,created_at)
                   VALUES(?,?,?,'follow',?,?,'retire',?)""",
                (subscription_id, share_id, actor_user_id, version, target_deck_id, _epoch(now)),
            )
            for item in entities:
                conn.execute(
                    """INSERT INTO subscription_entities(subscription_id,entity_type,source_id,
                       recipient_id,base_hash,media_name,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (subscription_id, item["entity_type"], item["source_id"],
                     item["recipient_id"], item["base_hash"], item.get("media_name"),
                     _epoch(now)),
                )
            self._audit(
                conn, now=now, actor=actor_user_id, target=actor_user_id,
                action="share.subscription.created", share_id=share_id,
                metadata={"version": version},
            )
            return _subscription(conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=?", (subscription_id,)
            ).fetchone())

    def list_subscription_entities(
        self, *, actor_user_id: str, subscription_id: str,
    ) -> list[SubscriptionEntity]:
        with self.database.read() as conn:
            subscription = conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=? AND user_id=?",
                (subscription_id, actor_user_id),
            ).fetchone()
            if subscription is None:
                raise NotFoundError("subscription not found")
            rows = conn.execute(
                """SELECT * FROM subscription_entities WHERE subscription_id=?
                   ORDER BY entity_type,source_id""",
                (subscription_id,),
            ).fetchall()
            return [_entity(row) for row in rows]

    def get_subscription(
        self, *, actor_user_id: str, subscription_id: str,
    ) -> ShareSubscription:
        with self.database.read() as conn:
            row = conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=? AND user_id=?",
                (subscription_id, actor_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("subscription not found")
            self._require_member(conn, row["share_id"], actor_user_id)
            return _subscription(row)

    def list_subscriptions(self, *, actor_user_id: str) -> list[ShareSubscription]:
        with self.database.read() as conn:
            rows = conn.execute(
                """SELECT s.* FROM share_subscriptions s
                   JOIN share_members m ON m.share_id=s.share_id
                   WHERE s.user_id=? AND m.user_id=? AND m.state='active'
                   ORDER BY s.created_at,s.id""",
                (actor_user_id, actor_user_id),
            ).fetchall()
            return [_subscription(row) for row in rows]

    def set_subscription_policy(
        self, *, actor_user_id: str, subscription_id: str, policy: str, now: datetime,
    ) -> ShareSubscription:
        if policy not in {"retire", "mirror"}:
            raise ValueError("subscription policy must be retire or mirror")
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=? AND user_id=?",
                (subscription_id, actor_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("subscription not found")
            self._require_member(conn, row["share_id"], actor_user_id)
            conn.execute(
                "UPDATE share_subscriptions SET conflict_policy=? WHERE id=?",
                (policy, subscription_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.subscription.policy",
                share_id=row["share_id"], metadata={"policy": policy},
            )
            return _subscription(conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=?", (subscription_id,),
            ).fetchone())

    def store_update_conflicts(
        self, *, actor_user_id: str, job_id: str, subscription_id: str,
        conflicts: list[dict], now: datetime,
    ) -> list[UpdateConflict]:
        with self.database.transaction() as conn:
            subscription = conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=? AND user_id=?",
                (subscription_id, actor_user_id),
            ).fetchone()
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND actor_user_id=?",
                (job_id, actor_user_id),
            ).fetchone()
            if subscription is None or job is None:
                raise NotFoundError("update job not found")
            self._require_member(conn, subscription["share_id"], actor_user_id)
            for item in conflicts:
                existing = conn.execute(
                    """SELECT id FROM update_conflicts WHERE job_id=? AND entity_type=?
                       AND source_id=? AND field_name=?""",
                    (job_id, item["entity_type"], item["source_id"], item["field_name"]),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """INSERT INTO update_conflicts(
                           id,job_id,subscription_id,entity_type,source_id,field_name,
                           base_hash,local_hash,upstream_hash)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), job_id, subscription_id, item["entity_type"],
                         item["source_id"], item["field_name"], item["base_hash"],
                         item["local_hash"], item["upstream_hash"]),
                    )
            rows = conn.execute(
                "SELECT * FROM update_conflicts WHERE job_id=? ORDER BY entity_type,source_id,field_name",
                (job_id,),
            ).fetchall()
            return [_conflict(row) for row in rows]

    def list_job_conflicts(
        self, *, actor_user_id: str, job_id: str,
    ) -> list[UpdateConflict]:
        with self.database.read() as conn:
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND actor_user_id=?", (job_id, actor_user_id),
            ).fetchone()
            if job is None:
                raise NotFoundError("update job not found")
            subscription = conn.execute(
                "SELECT share_id FROM share_subscriptions WHERE id=? AND user_id=?",
                (job["resource_id"], actor_user_id),
            ).fetchone()
            if subscription is None:
                raise NotFoundError("update job not found")
            self._require_member(conn, subscription["share_id"], actor_user_id)
            return [_conflict(row) for row in conn.execute(
                "SELECT * FROM update_conflicts WHERE job_id=? ORDER BY entity_type,source_id,field_name",
                (job_id,),
            )]

    def resolve_conflict(
        self, *, actor_user_id: str, job_id: str, conflict_id: str,
        resolution: str, now: datetime,
    ) -> UpdateConflict:
        if resolution not in {"mine", "upstream", "manual"}:
            raise ValueError("invalid conflict resolution")
        with self.database.transaction() as conn:
            row = conn.execute(
                """SELECT c.*,s.share_id,s.user_id FROM update_conflicts c
                   JOIN share_subscriptions s ON s.id=c.subscription_id
                   JOIN jobs j ON j.id=c.job_id
                   WHERE c.id=? AND c.job_id=? AND j.actor_user_id=?""",
                (conflict_id, job_id, actor_user_id),
            ).fetchone()
            if row is None or row["user_id"] != actor_user_id:
                raise NotFoundError("update conflict not found")
            self._require_member(conn, row["share_id"], actor_user_id)
            if row["entity_type"] == "media" and resolution == "manual":
                raise ValueError("binary media conflicts allow only mine or upstream")
            conn.execute(
                """UPDATE update_conflicts SET resolution=?,resolved_by=?,resolved_at=?
                   WHERE id=?""",
                (resolution, actor_user_id, _epoch(now), conflict_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.conflict.resolved",
                share_id=row["share_id"], metadata={
                    "conflict_id": conflict_id, "resolution": resolution,
                    "base_hash": row["base_hash"], "local_hash": row["local_hash"],
                    "upstream_hash": row["upstream_hash"],
                },
            )
            return _conflict(conn.execute(
                "SELECT * FROM update_conflicts WHERE id=?", (conflict_id,),
            ).fetchone())

    def commit_subscription_update(
        self, *, actor_user_id: str, subscription_id: str,
        expected_version: int, target_version: int, entities: list[dict],
        now: datetime,
    ) -> ShareSubscription:
        with self.database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=? AND user_id=?",
                (subscription_id, actor_user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("subscription not found")
            self._require_member(conn, row["share_id"], actor_user_id)
            if row["installed_release"] != expected_version:
                raise ConflictError("subscription changed concurrently")
            if not conn.execute(
                "SELECT 1 FROM share_releases WHERE share_id=? AND version=?",
                (row["share_id"], target_version),
            ).fetchone():
                raise NotFoundError("release not found")
            conn.execute("DELETE FROM subscription_entities WHERE subscription_id=?", (subscription_id,))
            for item in entities:
                conn.execute(
                    """INSERT INTO subscription_entities(subscription_id,entity_type,source_id,
                       recipient_id,base_hash,media_name,updated_at) VALUES(?,?,?,?,?,?,?)""",
                    (subscription_id, item["entity_type"], item["source_id"],
                     item["recipient_id"], item["base_hash"], item.get("media_name"),
                     _epoch(now)),
                )
            conn.execute(
                "UPDATE share_subscriptions SET installed_release=? WHERE id=?",
                (target_version, subscription_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.subscription.updated",
                share_id=row["share_id"], metadata={
                    "from_version": expected_version, "to_version": target_version,
                },
            )
            return _subscription(conn.execute(
                "SELECT * FROM share_subscriptions WHERE id=?", (subscription_id,),
            ).fetchone())

    def get_workspace_revision(
        self, *, actor_user_id: str, share_id: str, entity_type: str, entity_id: str,
    ) -> WorkspaceRevision | None:
        with self.database.read() as conn:
            self._require_member(conn, share_id, actor_user_id)
            row = conn.execute(
                """SELECT * FROM workspace_revisions WHERE share_id=?
                   AND entity_type=? AND entity_id=?""",
                (share_id, entity_type, entity_id),
            ).fetchone()
            return _revision(row) if row else None

    def commit_workspace_revision(
        self, *, actor_user_id: str, share_id: str, entity_type: str,
        entity_id: str, expected_revision: int, now: datetime,
    ) -> WorkspaceRevision:
        with self.database.transaction() as conn:
            member = self._require_member(conn, share_id, actor_user_id)
            if member["role"] not in (ShareRole.OWNER.value, ShareRole.EDITOR.value):
                raise AuthorizationError("share editor permission required")
            row = conn.execute(
                """SELECT * FROM workspace_revisions WHERE share_id=?
                   AND entity_type=? AND entity_id=?""",
                (share_id, entity_type, entity_id),
            ).fetchone()
            current = int(row["revision"]) if row else 0
            if current != expected_revision:
                raise ConflictError("workspace entity revision changed")
            revision = current + 1
            conn.execute(
                """INSERT INTO workspace_revisions(
                   share_id,entity_type,entity_id,revision,changed_by,changed_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(share_id,entity_type,entity_id) DO UPDATE SET
                   revision=excluded.revision,changed_by=excluded.changed_by,
                   changed_at=excluded.changed_at""",
                (share_id, entity_type, entity_id, revision, actor_user_id, _epoch(now)),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.workspace.changed",
                share_id=share_id, metadata={
                    "entity_type": entity_type, "entity_id": entity_id,
                    "revision": revision,
                },
            )
            return _revision(conn.execute(
                """SELECT * FROM workspace_revisions WHERE share_id=?
                   AND entity_type=? AND entity_id=?""",
                (share_id, entity_type, entity_id),
            ).fetchone())

    def add_workspace_comment(
        self, *, actor_user_id: str, share_id: str, entity_type: str,
        entity_id: str, body: str, now: datetime,
    ) -> WorkspaceComment:
        cleaned = _comment_body(body)
        if entity_type not in {"note", "template", "deck"}:
            raise ValueError("invalid comment entity type")
        with self.database.transaction() as conn:
            self._require_member(conn, share_id, actor_user_id)
            comment_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO workspace_comments(
                   id,share_id,entity_type,entity_id,author_user_id,body,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (comment_id, share_id, entity_type, entity_id, actor_user_id,
                 cleaned, _epoch(now), _epoch(now)),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.comment.created",
                share_id=share_id, metadata={
                    "comment_id": comment_id, "entity_type": entity_type,
                    "entity_id": entity_id,
                },
            )
            return _comment(conn.execute(
                "SELECT * FROM workspace_comments WHERE id=?", (comment_id,),
            ).fetchone())

    def list_workspace_comments(
        self, *, actor_user_id: str, share_id: str,
        entity_type: str, entity_id: str,
    ) -> list[WorkspaceComment]:
        with self.database.read() as conn:
            self._require_member(conn, share_id, actor_user_id)
            return [_comment(row) for row in conn.execute(
                """SELECT * FROM workspace_comments WHERE share_id=?
                   AND entity_type=? AND entity_id=? ORDER BY created_at,id""",
                (share_id, entity_type, entity_id),
            )]

    def edit_workspace_comment(
        self, *, actor_user_id: str, share_id: str, comment_id: str,
        body: str, now: datetime,
    ) -> WorkspaceComment:
        cleaned = _comment_body(body)
        with self.database.transaction() as conn:
            self._require_member(conn, share_id, actor_user_id)
            row = conn.execute(
                "SELECT * FROM workspace_comments WHERE id=? AND share_id=?",
                (comment_id, share_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("comment not found")
            if row["author_user_id"] != actor_user_id:
                raise AuthorizationError("only the comment author may edit")
            if _epoch(now) > row["created_at"] + 15 * 60:
                raise ConflictError("comment edit window has closed")
            conn.execute(
                "UPDATE workspace_comments SET body=?,updated_at=? WHERE id=?",
                (cleaned, _epoch(now), comment_id),
            )
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.comment.edited",
                share_id=share_id, metadata={"comment_id": comment_id},
            )
            return _comment(conn.execute(
                "SELECT * FROM workspace_comments WHERE id=?", (comment_id,),
            ).fetchone())

    def resolve_workspace_comment(
        self, *, actor_user_id: str, share_id: str, comment_id: str, now: datetime,
    ) -> WorkspaceComment:
        with self.database.transaction() as conn:
            self._require_member(conn, share_id, actor_user_id)
            changed = conn.execute(
                """UPDATE workspace_comments SET resolved_at=?,updated_at=?
                   WHERE id=? AND share_id=? AND resolved_at IS NULL""",
                (_epoch(now), _epoch(now), comment_id, share_id),
            ).rowcount
            if changed != 1:
                raise NotFoundError("active comment not found")
            self._audit(
                conn, now=now, actor=actor_user_id, action="share.comment.resolved",
                share_id=share_id, metadata={"comment_id": comment_id},
            )
            return _comment(conn.execute(
                "SELECT * FROM workspace_comments WHERE id=?", (comment_id,),
            ).fetchone())
