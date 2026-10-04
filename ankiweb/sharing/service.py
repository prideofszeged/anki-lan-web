from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .models import DeckShare, ShareDetail, ShareInviteGrant, ShareMembership, ShareRole
from .repository import SharingRepository


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class SharingService:
    def __init__(self, repository: SharingRepository, *, clock=None) -> None:
        self.repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def create_share(self, *, actor_user_id: str, name: str) -> DeckShare:
        return self.repository.create_share(
            actor_user_id=actor_user_id, name=name, now=self._clock(),
        )

    def list_shares(self, actor_user_id: str) -> list[DeckShare]:
        return self.repository.list_shares(actor_user_id)

    def get_share(self, *, actor_user_id: str, share_id: str) -> ShareDetail:
        return self.repository.get_detail(actor_user_id=actor_user_id, share_id=share_id)

    def create_invite(
        self, *, actor_user_id: str, share_id: str, role: ShareRole,
        intended_user_id: str | None = None, lifetime: timedelta = timedelta(days=7),
    ) -> ShareInviteGrant:
        if lifetime <= timedelta(0):
            raise ValueError("share invitation lifetime must be positive")
        token = secrets.token_urlsafe(32)
        now = self._clock()
        invite = self.repository.create_invite(
            actor_user_id=actor_user_id, share_id=share_id, token_hash=_digest(token),
            role=role, intended_user_id=intended_user_id, now=now,
            expires_at=now + lifetime,
        )
        return ShareInviteGrant(invite=invite, token=token)

    def accept_invite(self, *, actor_user_id: str, token: str) -> ShareMembership:
        return self.repository.accept_invite(
            actor_user_id=actor_user_id, token_hash=_digest(token), now=self._clock(),
        )

    def revoke_invite(
        self, *, actor_user_id: str, share_id: str, invite_id: str,
    ) -> None:
        self.repository.revoke_invite(
            actor_user_id=actor_user_id, share_id=share_id,
            invite_id=invite_id, now=self._clock(),
        )

    def remove_member(
        self, *, actor_user_id: str, share_id: str, target_user_id: str,
    ) -> None:
        self.repository.remove_member(
            actor_user_id=actor_user_id, share_id=share_id,
            target_user_id=target_user_id, now=self._clock(),
        )
