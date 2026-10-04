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

