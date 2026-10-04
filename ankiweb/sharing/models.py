from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ShareState(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ShareRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class MembershipState(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class DeckShare:
    id: str
    owner_user_id: str
    name: str
    state: ShareState
    current_release: int | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ShareMembership:
    share_id: str
    user_id: str
    role: ShareRole
    state: MembershipState
    joined_at: datetime
    removed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ShareInvite:
    id: str
    share_id: str
    role: ShareRole
    created_by: str
    intended_user_id: str | None
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by: str | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class ShareInviteGrant:
    invite: ShareInvite
    token: str


@dataclass(frozen=True, slots=True)
class ShareDetail:
    share: DeckShare
    membership: ShareMembership
    members: tuple[ShareMembership, ...]


@dataclass(frozen=True, slots=True)
class ShareRelease:
    id: str
    share_id: str
    version: int
    manifest_path: str
    bundle_path: str
    bundle_sha256: str
    created_by: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ShareSubscription:
    id: str
    share_id: str
    user_id: str
    mode: str
    installed_release: int
    target_deck_id: int | None
    conflict_policy: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionEntity:
    subscription_id: str
    entity_type: str
    source_id: str
    recipient_id: str
    base_hash: str
    media_name: str | None
    updated_at: datetime
