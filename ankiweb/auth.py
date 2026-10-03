"""Single-user authentication with opaque, server-side sessions."""
from __future__ import annotations
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

COOKIE = "ankiweb_auth"
SESSION_AGE_SECONDS = 30 * 86400


def password_ok(submitted: str, password: str = "", password_hash: str = "") -> bool:
    """Verify either an Argon2id hash or the legacy environment password."""
    if password_hash:
        try:
            return PasswordHasher().verify(password_hash, submitted or "")
        except (VerificationError, InvalidHashError):
            return False
    return hmac.compare_digest(submitted or "", password or "")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class SessionStore:
    """In-memory opaque sessions; only token digests are retained server-side."""

    max_age: int = SESSION_AGE_SECONDS
    _expires: dict[str, float] = field(default_factory=dict)

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._expires[_digest(token)] = time.time() + self.max_age
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        key = _digest(token)
        expires = self._expires.get(key, 0)
        if expires <= time.time():
            self._expires.pop(key, None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._expires.pop(_digest(token), None)


@dataclass
class LoginLimiter:
    """Small per-client sliding-window limiter for the password endpoint."""

    attempts: int = 8
    window_seconds: int = 60
    _events: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def allow(self, client: str) -> bool:
        now = time.monotonic()
        events = self._events[client]
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        if len(events) >= self.attempts:
            return False
        events.append(now)
        return True
