"""Durable multi-user identity/control-plane foundation (SPEC section 11)."""
from .database import IdentityDatabase, MigrationError, SCHEMA_VERSION
from .models import (
    AccountInvite, AccountState, AuditEvent, Credential, GlobalRole, InviteGrant,
    Session, SessionGrant, User, UserQuota,
)
from .repository import (
    AuthorizationError, ConflictError, ExpiredTokenError, IdentityError,
    IdentityRepository, InvalidTokenError, LastOwnerError, NotFoundError,
    normalize_username,
)
from .service import IdentityService, token_digest
from .jobs import Job, JobRepository, JobState, request_digest

__all__ = [
    "AccountInvite", "AccountState", "AuditEvent", "AuthorizationError", "ConflictError",
    "Credential", "ExpiredTokenError", "GlobalRole", "IdentityDatabase", "IdentityError",
    "IdentityRepository", "IdentityService", "InvalidTokenError", "InviteGrant",
    "LastOwnerError", "MigrationError", "NotFoundError", "SCHEMA_VERSION", "Session",
    "SessionGrant", "User", "UserQuota", "normalize_username", "token_digest",
    "Job", "JobRepository", "JobState", "request_digest",
]
