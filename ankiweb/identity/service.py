"""Credential/token policy over the identity repositories."""
from __future__ import annotations

import hashlib
import secrets
import hmac
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .models import (
    AccountState, GlobalRole, InviteGrant, Session, SessionGrant, User,
)
from .repository import AuthorizationError, IdentityRepository


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class IdentityService:
    def __init__(self, repository: IdentityRepository, *, password_hasher: PasswordHasher | None = None,
                 clock=None, provision: Callable[[User], None] | None = None,
                 rollback_provision: Callable[[User], None] | None = None) -> None:
        self.repository = repository
        self.password_hasher = password_hasher or PasswordHasher()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._dummy_password_hash = self.password_hasher.hash(secrets.token_urlsafe(32))
        self._provision = provision
        self._rollback_provision = rollback_provision

    def _burn_password_check(self, password: str) -> None:
        try:
            self.password_hasher.verify(self._dummy_password_hash, password)
        except VerificationError:
            pass

    def initialize(self) -> int:
        return self.repository.initialize()

    def bootstrap_owner(self, *, username: str = "local", password: str,
                        display_name: str = "Local owner", provision=None) -> User:
        """Seed the first owner. The caller must expose this through local CLI only."""
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
        now = self._clock()
        provisioner = provision if provision is not None else self._provision
        return self.repository.bootstrap_owner(
            username=username, display_name=display_name,
            password_hash=self.password_hasher.hash(password), now=now,
            provision=provisioner,
            rollback_provision=self._rollback_provision if provisioner is not None else None,
        )

    def set_password(self, user_id: str, password: str) -> None:
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
        now = self._clock()
        self.repository.replace_password_and_revoke(
            user_id, self.password_hasher.hash(password), now=now,
        )

    def verify_password(self, username: str, password: str) -> User | None:
        try:
            user = self.repository.get_user_by_username(username)
        except ValueError:
            self._burn_password_check(password)
            return None
        if not user or user.state is not AccountState.ACTIVE:
            self._burn_password_check(password)
            return None
        credential = self.repository.get_credential(user.id)
        if not credential:
            self._burn_password_check(password)
            return None
        try:
            valid = self.password_hasher.verify(credential.password_hash, password)
        except InvalidHashError:
            self._burn_password_check(password)
            return None
        except VerificationError:
            return None
        return user if valid else None

    def login(
        self, *, username: str, password: str, user_agent_hash: str | None = None,
        ip_prefix: str | None = None,
    ) -> SessionGrant | None:
        user = self.verify_password(username, password)
        if not user:
            return None
        now = self._clock()
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        session = self.repository.create_session(
            token_hash=token_digest(token), csrf_hash=token_digest(csrf), user_id=user.id,
            now=now, expires_at=now + timedelta(days=30), user_agent_hash=user_agent_hash,
            ip_prefix=ip_prefix,
        )
        return SessionGrant(session=session, token=token, csrf_token=csrf)

    def authenticate(self, token: str | None, *, refresh: bool = True) -> Session | None:
        if not token:
            return None
        now = self._clock()
        return self.repository.validate_session(
            token_digest(token), now=now,
            refreshed_expires_at=now + timedelta(days=30) if refresh else None,
        )

    def validate_csrf(self, session: Session, submitted: str | None) -> bool:
        if not submitted:
            return False
        stored = self.repository.csrf_hash(session.id, user_id=session.user_id)
        return stored is not None and hmac.compare_digest(stored, token_digest(submitted))

    def rotate_csrf(self, session: Session) -> str:
        token = secrets.token_urlsafe(32)
        if not self.repository.rotate_csrf(
            session.id, user_id=session.user_id, csrf_hash=token_digest(token)
        ):
            raise AuthorizationError("session is no longer active")
        return token

    def list_sessions(self, user_id: str) -> list[Session]:
        return self.repository.list_sessions(user_id, now=self._clock())

    def session_active(self, session_id: str) -> bool:
        return self.repository.active_session_by_id(
            session_id, now=self._clock()
        ) is not None

    def revoke_session(self, *, actor_user_id: str, session_id: str) -> bool:
        return self.repository.revoke_session(session_id, user_id=actor_user_id)

    def revoke_invite(self, *, actor_user_id: str, invite_id: str) -> None:
        self.repository.revoke_invite(
            invite_id, actor_user_id=actor_user_id, now=self._clock(),
        )

    def create_invite(
        self, *, actor_user_id: str, intended_username: str,
        role: GlobalRole = GlobalRole.USER, lifetime: timedelta = timedelta(hours=24),
    ) -> InviteGrant:
        if lifetime <= timedelta(0):
            raise ValueError("invitation lifetime must be positive")
        now = self._clock()
        token = secrets.token_urlsafe(32)
        invite = self.repository.create_invite(
            token_hash=token_digest(token), created_by=actor_user_id,
            intended_username=intended_username, role=role, now=now,
            expires_at=now + lifetime,
        )
        return InviteGrant(invite=invite, token=token)

    def accept_invite(self, *, token: str, password: str, display_name: str = "") -> User:
        if len(password) < 10:
            raise ValueError("password must contain at least 10 characters")
        digest = token_digest(token)
        self.repository.validate_invite_token(digest, now=self._clock())
        return self.repository.accept_invite(
            token_hash=digest, password_hash=self.password_hasher.hash(password),
            display_name=display_name, now=self._clock(), provision=self._provision,
            rollback_provision=self._rollback_provision,
        )
