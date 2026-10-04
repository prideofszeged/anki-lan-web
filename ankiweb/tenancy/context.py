from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class ResourceKind(StrEnum):
    USER = "user"
    SHARE = "share"


class TenantRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


def canonical_uuid(value: UUID | str) -> UUID:
    """Parse a UUID without ever treating the input as a filesystem fragment."""
    if isinstance(value, UUID):
        return value
    return UUID(value)


@dataclass(frozen=True, slots=True, order=True)
class ResourceKey:
    kind: ResourceKind
    resource_id: UUID

    @classmethod
    def user(cls, user_id: UUID | str) -> ResourceKey:
        return cls(ResourceKind.USER, canonical_uuid(user_id))

    @classmethod
    def share(cls, share_id: UUID | str) -> ResourceKey:
        return cls(ResourceKind.SHARE, canonical_uuid(share_id))

    @property
    def stable_name(self) -> str:
        return f"{self.kind.value}:{self.resource_id}"


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Authorization result carried through HTTP, websocket, and background work."""

    actor_user_id: UUID
    resource_owner_id: UUID
    resource_key: ResourceKey
    role: TenantRole
    session_id: UUID | None = None

    @classmethod
    def private_collection(
        cls,
        user_id: UUID | str,
        *,
        session_id: UUID | str | None = None,
    ) -> TenantContext:
        user_uuid = canonical_uuid(user_id)
        return cls(
            actor_user_id=user_uuid,
            resource_owner_id=user_uuid,
            resource_key=ResourceKey.user(user_uuid),
            role=TenantRole.OWNER,
            session_id=canonical_uuid(session_id) if session_id is not None else None,
        )

    @property
    def can_write(self) -> bool:
        return self.role in (TenantRole.OWNER, TenantRole.EDITOR)
