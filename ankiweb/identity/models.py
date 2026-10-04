"""Typed identity/control-plane values.

This package deliberately contains no FastAPI or Anki imports.  It owns metadata only;
card and note content remains in each user's Anki collection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping


class GlobalRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    USER = "user"


class AccountState(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    PURGE_PENDING = "purge_pending"
    PURGED = "purged"


@dataclass(frozen=True)
class User:
    id: str
    username: str
    username_norm: str
    display_name: str
    global_role: GlobalRole
    state: AccountState
    created_at: datetime
    suspended_at: datetime | None
    purge_after: datetime | None
    auth_epoch: int


@dataclass(frozen=True)
class Credential:
    user_id: str
    password_hash: str
    changed_at: datetime


@dataclass(frozen=True)
class Session:
    id: str
    user_id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    auth_epoch: int
    user_agent_hash: str | None
    ip_prefix: str | None


@dataclass(frozen=True)
class SessionGrant:
    """A newly created session. Plaintext secrets are returned exactly once."""

    session: Session
    token: str
    csrf_token: str


@dataclass(frozen=True)
class AccountInvite:
    id: str
    created_by: str
    intended_username: str
    intended_username_norm: str
    global_role: GlobalRole
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class InviteGrant:
    """An invitation plus its one-time plaintext bearer token."""

    invite: AccountInvite
    token: str


@dataclass(frozen=True)
class UserQuota:
    user_id: str
    storage_bytes: int = 5 * 1024**3
    import_bytes: int = 2 * 1024**3
    active_jobs: int = 2
    active_sessions: int = 10
    review_sockets: int = 4


@dataclass(frozen=True)
class AuditEvent:
    id: int
    occurred_at: datetime
    actor_user_id: str | None
    target_user_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    request_id: str | None
    outcome: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
