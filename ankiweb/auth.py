"""Single-user authentication with opaque, server-side sessions."""
from __future__ import annotations
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from starlette.requests import Request

COOKIE = "ankiweb_auth"
SESSION_AGE_SECONDS = 30 * 86400


def password_ok(submitted: str, password: str = "", password_hash: str = "") -> bool:
    """Verify either an Argon2id hash or the legacy environment password."""
    if password_hash:
        try:
            return PasswordHasher().verify(password_hash, submitted or "")
        except (VerificationError, InvalidHashError):
            return False
    return hmac.compare_digest(
        (submitted or "").encode("utf-8"),
        (password or "").encode("utf-8"),
    )


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

    def _active(self, client: str) -> deque[float]:
        now = time.monotonic()
        events = self._events[client]
        while events and events[0] <= now - self.window_seconds:
            events.popleft()
        return events

    def allow(self, client: str) -> bool:
        """Atomically reserve one attempt before password verification can yield.

        FastAPI runs this synchronous method on the event-loop thread, so the check and
        append cannot be interleaved by parallel requests. A successful login resets it.
        """
        events = self._active(client)
        if len(events) >= self.attempts:
            return False
        events.append(time.monotonic())
        return True

    def reset(self, client: str) -> None:
        self._events.pop(client, None)


def login_client(request: Request, trusted_proxy_cidrs: tuple[str, ...] = ()) -> str:
    """Return a non-spoofable rate-limit key for the request's client.

    Forwarded headers are honored only when the direct peer belongs to an explicitly
    trusted proxy network. Direct LAN clients cannot mint new limiter buckets by
    changing ``X-Forwarded-For``.
    """
    direct = request.client.host if request.client else "unknown"
    try:
        peer = ipaddress.ip_address(direct)
        trusted = any(
            peer in ipaddress.ip_network(network, strict=False)
            for network in trusted_proxy_cidrs
        )
    except ValueError:
        trusted = False
    if trusted:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            candidate = forwarded.split(",")[0].strip()
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                pass
    return direct
