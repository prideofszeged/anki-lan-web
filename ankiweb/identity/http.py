"""Mountable FastAPI boundary for durable multi-user identity.

The existing single-user routes remain untouched until the application composes this
router.  Downstream routers can reuse :meth:`IdentityHttp.require_principal` as a FastAPI
dependency, so authentication policy stays out of deck/card handlers.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ankiweb.auth import LoginLimiter, login_client

from .models import AccountState, GlobalRole, Session, User
from .repository import (
    AuthorizationError, ConflictError, ExpiredTokenError, IdentityError,
    InvalidTokenError, LastOwnerError, NotFoundError,
)
from .service import IdentityService

IDENTITY_COOKIE = "ankiweb_session"
CSRF_HEADER = "x-csrf-token"
COOKIE_MAX_AGE = 90 * 86400


@dataclass(frozen=True)
class IdentityPrincipal:
    user: User
    session: Session


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=64)
    password: SecretStr = Field(min_length=1, max_length=4096)


class UserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    global_role: GlobalRole

    @classmethod
    def from_user(cls, user: User) -> "UserResponse":
        return cls(id=user.id, username=user.username, display_name=user.display_name,
                   global_role=user.global_role)


class LoginResponse(BaseModel):
    user: UserResponse
    csrf_token: str


class SessionResponse(BaseModel):
    id: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent_hash: str | None
    ip_prefix: str | None
    current: bool

    @classmethod
    def from_session(cls, session: Session, *, current: bool) -> "SessionResponse":
        return cls(
            id=session.id, created_at=session.created_at, last_seen_at=session.last_seen_at,
            expires_at=session.expires_at, user_agent_hash=session.user_agent_hash,
            ip_prefix=session.ip_prefix, current=current,
        )


class SessionListResponse(BaseModel):
    sessions: list[SessionResponse]


class CsrfResponse(BaseModel):
    csrf_token: str


class InviteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intended_username: str = Field(min_length=1, max_length=64)
    global_role: GlobalRole = GlobalRole.USER
    expires_in_seconds: int = Field(default=86400, ge=60, le=7 * 86400)


class InviteResponse(BaseModel):
    id: str
    intended_username: str
    global_role: GlobalRole
    expires_at: datetime
    token: str


class InviteAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=32, max_length=512)
    password: SecretStr = Field(min_length=10, max_length=4096)
    display_name: str = Field(default="", max_length=128)


class AdminQuotaResponse(BaseModel):
    storage_bytes: int
    import_bytes: int
    active_jobs: int
    active_sessions: int
    review_sockets: int


class AdminUserResponse(BaseModel):
    id: str
    username: str
    display_name: str
    global_role: GlobalRole
    state: AccountState
    created_at: datetime
    last_login_at: datetime | None
    usage_bytes: int | None
    backup_age_seconds: int | None
    quota: AdminQuotaResponse


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]


class AdminUserPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: AccountState | None = None
    storage_bytes: int | None = Field(default=None, ge=1)
    import_bytes: int | None = Field(default=None, ge=1)


class AdminAuditResponse(BaseModel):
    events: list[dict]


AttemptGate = Callable[[Request], bool | Awaitable[bool]]
SuccessCallback = Callable[[Request], None | Awaitable[None]]


def _error(status: int, code: str, message: str, *, authenticate: bool = False) -> HTTPException:
    headers = {"WWW-Authenticate": "Session"} if authenticate else None
    return HTTPException(status_code=status, detail={"code": code, "message": message},
                         headers=headers)


def _translate(exc: IdentityError) -> HTTPException:
    if isinstance(exc, ExpiredTokenError):
        return _error(410, "invite_expired", str(exc))
    if isinstance(exc, InvalidTokenError):
        return _error(400, "invalid_invite", str(exc))
    if isinstance(exc, (ConflictError, LastOwnerError)):
        return _error(409, "identity_conflict", str(exc))
    if isinstance(exc, NotFoundError):
        return _error(404, "not_found", str(exc))
    if isinstance(exc, AuthorizationError):
        return _error(403, "forbidden", str(exc))
    return _error(400, "identity_error", str(exc))


async def _invoke(callable_, /, *args, **kwargs):
    try:
        return await asyncio.to_thread(callable_, *args, **kwargs)
    except IdentityError as exc:
        raise _translate(exc) from exc
    except ValueError as exc:
        raise _error(422, "invalid_input", str(exc)) from exc


async def _callback(callback, *args):
    result = callback(*args)
    if isinstance(result, Awaitable):
        return await result
    return result


class IdentityHttp:
    """Router plus dependencies needed to mount the identity API into an application."""

    def __init__(
        self, service: IdentityService, *, prefix: str = "/api/v1",
        cookie_name: str = IDENTITY_COOKIE, secure_cookie: bool = True,
        attempt_gate: AttemptGate | None = None,
        login_succeeded: SuccessCallback | None = None,
        invite_attempt_gate: AttemptGate | None = None,
        trusted_proxy_cidrs: tuple[str, ...] = (),
        usage_provider: Callable[[str], int] | None = None,
        backup_age_provider: Callable[[str], int | None] | None = None,
    ) -> None:
        self.service = service
        self.cookie_name = cookie_name
        self.csrf_cookie_name = f"{cookie_name}_csrf"
        self.secure_cookie = secure_cookie
        self.usage_provider = usage_provider
        self.backup_age_provider = backup_age_provider
        if attempt_gate is None:
            login_limiter = LoginLimiter()
            self.attempt_gate = lambda request: login_limiter.allow(
                login_client(request, trusted_proxy_cidrs)
            )
            self.login_succeeded = (
                lambda request: login_limiter.reset(
                    login_client(request, trusted_proxy_cidrs)
                )
            )
        else:
            self.attempt_gate = attempt_gate
            self.login_succeeded = login_succeeded
        if invite_attempt_gate is None:
            invite_limiter = LoginLimiter()
            self.invite_attempt_gate = (
                lambda request: invite_limiter.allow(
                    login_client(request, trusted_proxy_cidrs)
                )
            )
        else:
            self.invite_attempt_gate = invite_attempt_gate
        self.router = APIRouter(prefix=prefix)
        self._add_routes()

    async def optional_principal(self, request: Request) -> IdentityPrincipal | None:
        token = request.cookies.get(self.cookie_name)
        session = await asyncio.to_thread(self.service.authenticate, token)
        if not session:
            return None
        user = await asyncio.to_thread(self.service.repository.get_user, session.user_id)
        if not user:
            return None
        return IdentityPrincipal(user=user, session=session)

    async def require_principal(self, request: Request) -> IdentityPrincipal:
        principal = await self.optional_principal(request)
        if not principal:
            raise _error(401, "authentication_required", "authentication required",
                         authenticate=True)
        return principal

    def require_roles(self, *roles: GlobalRole):
        allowed = frozenset(roles)

        async def dependency(
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> IdentityPrincipal:
            if principal.user.global_role not in allowed:
                raise _error(403, "forbidden", "insufficient role")
            return principal

        return dependency

    async def require_csrf(self, request: Request, principal: IdentityPrincipal) -> None:
        valid = await asyncio.to_thread(
            self.service.validate_csrf, principal.session, request.headers.get(CSRF_HEADER),
        )
        if not valid:
            raise _error(403, "invalid_csrf", "missing or invalid CSRF token")

    def set_login_cookies(self, response: Response, token: str, csrf_token: str) -> None:
        response.set_cookie(
            self.cookie_name, token, httponly=True, secure=self.secure_cookie,
            samesite="strict", path="/", max_age=COOKIE_MAX_AGE,
        )
        # The frontend copies this value into CSRF_HEADER. SameSite prevents cross-site
        # attachment; the server-side digest prevents cookie/header forgery.
        response.set_cookie(
            self.csrf_cookie_name, csrf_token, httponly=False, secure=self.secure_cookie,
            samesite="strict", path="/", max_age=COOKIE_MAX_AGE,
        )

    def _clear_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name, path="/", secure=self.secure_cookie,
                               httponly=True, samesite="strict")
        response.delete_cookie(self.csrf_cookie_name, path="/", secure=self.secure_cookie,
                               httponly=False, samesite="strict")

    def _add_routes(self) -> None:
        router = self.router

        async def admin_user_response(record) -> AdminUserResponse:
            usage = (
                await _callback(self.usage_provider, record.user.id)
                if self.usage_provider is not None else None
            )
            backup_age = (
                await _callback(self.backup_age_provider, record.user.id)
                if self.backup_age_provider is not None else None
            )
            quota = record.quota
            return AdminUserResponse(
                id=record.user.id, username=record.user.username,
                display_name=record.user.display_name,
                global_role=record.user.global_role, state=record.user.state,
                created_at=record.user.created_at, last_login_at=record.last_login_at,
                usage_bytes=usage, backup_age_seconds=backup_age,
                quota=AdminQuotaResponse(
                    storage_bytes=quota.storage_bytes, import_bytes=quota.import_bytes,
                    active_jobs=quota.active_jobs, active_sessions=quota.active_sessions,
                    review_sockets=quota.review_sockets,
                ),
            )

        @router.post(
            "/auth/login", response_model=LoginResponse, tags=["identity"],
            responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
        )
        async def login(body: LoginRequest, request: Request) -> Response:
            if self.attempt_gate is not None and not await _callback(self.attempt_gate, request):
                raise _error(429, "rate_limited", "too many login attempts")
            grant = await asyncio.to_thread(
                self.service.login,
                username=body.username,
                password=body.password.get_secret_value(),
                user_agent_hash=_user_agent_hash(request),
                ip_prefix=_ip_prefix(request),
            )
            if grant is None:
                raise _error(401, "invalid_credentials", "invalid credentials", authenticate=True)
            if self.login_succeeded is not None:
                await _callback(self.login_succeeded, request)
            user = await asyncio.to_thread(
                self.service.repository.get_user, grant.session.user_id,
            )
            payload = LoginResponse(
                user=UserResponse.from_user(user), csrf_token=grant.csrf_token,
            )
            response = JSONResponse(payload.model_dump(mode="json"))
            self.set_login_cookies(response, grant.token, grant.csrf_token)
            return response

        @router.get("/auth/me", response_model=UserResponse, tags=["identity"])
        async def me(
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> UserResponse:
            return UserResponse.from_user(principal.user)

        @router.post("/auth/logout", status_code=204, tags=["identity"])
        async def logout(request: Request) -> Response:
            principal = await self.optional_principal(request)
            if principal is not None:
                await self.require_csrf(request, principal)
                await asyncio.to_thread(
                    self.service.revoke_session,
                    actor_user_id=principal.user.id, session_id=principal.session.id,
                )
            response = Response(status_code=204)
            self._clear_cookie(response)
            return response

        @router.get(
            "/auth/sessions", response_model=SessionListResponse, tags=["identity"],
        )
        async def sessions(
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> SessionListResponse:
            values = await asyncio.to_thread(self.service.list_sessions, principal.user.id)
            return SessionListResponse(sessions=[SessionResponse.from_session(
                item, current=item.id == principal.session.id,
            ) for item in values])

        @router.delete("/auth/sessions/{session_id}", status_code=204, tags=["identity"])
        async def revoke_session(
            session_id: str, request: Request,
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> Response:
            await self.require_csrf(request, principal)
            found = await asyncio.to_thread(
                self.service.revoke_session,
                actor_user_id=principal.user.id, session_id=session_id,
            )
            if not found:
                raise _error(404, "session_not_found", "session not found")
            response = Response(status_code=204)
            if session_id == principal.session.id:
                self._clear_cookie(response)
            return response

        @router.post("/auth/csrf", response_model=CsrfResponse, tags=["identity"])
        async def rotate_csrf(
            request: Request, response: Response,
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> CsrfResponse:
            await self.require_csrf(request, principal)
            token = await _invoke(self.service.rotate_csrf, principal.session)
            response.set_cookie(
                self.csrf_cookie_name, token, httponly=False, secure=self.secure_cookie,
                samesite="strict", path="/", max_age=COOKIE_MAX_AGE,
            )
            return CsrfResponse(csrf_token=token)

        @router.get("/auth/csrf", response_model=CsrfResponse, tags=["identity"])
        async def recover_csrf(
            response: Response,
            principal: IdentityPrincipal = Depends(self.require_principal),
        ) -> CsrfResponse:
            """Rotate after restart/cookie loss without requiring the missing raw token."""
            token = await _invoke(self.service.rotate_csrf, principal.session)
            response.set_cookie(
                self.csrf_cookie_name, token, httponly=False, secure=self.secure_cookie,
                samesite="strict", path="/", max_age=COOKIE_MAX_AGE,
            )
            return CsrfResponse(csrf_token=token)

        @router.post(
            "/account-invites/accept", response_model=UserResponse,
            status_code=201, tags=["identity"],
        )
        async def accept_invite_request(
            body: InviteAcceptRequest, request: Request,
        ) -> UserResponse:
            if self.invite_attempt_gate is not None and not await _callback(
                self.invite_attempt_gate, request
            ):
                raise _error(429, "rate_limited", "too many invitation attempts")
            user = await _invoke(
                self.service.accept_invite, token=body.token.get_secret_value(),
                password=body.password.get_secret_value(),
                display_name=body.display_name,
            )
            return UserResponse.from_user(user)

        @router.post(
            "/admin/account-invites", response_model=InviteResponse,
            status_code=201, tags=["identity-admin"],
        )
        async def create_invite(
            body: InviteCreateRequest, request: Request,
            principal: IdentityPrincipal = Depends(
                self.require_roles(GlobalRole.OWNER, GlobalRole.ADMIN)
            ),
        ) -> InviteResponse:
            await self.require_csrf(request, principal)
            grant = await _invoke(
                self.service.create_invite, actor_user_id=principal.user.id,
                intended_username=body.intended_username, role=body.global_role,
                lifetime=timedelta(seconds=body.expires_in_seconds),
            )
            return InviteResponse(
                id=grant.invite.id, intended_username=grant.invite.intended_username,
                global_role=grant.invite.global_role, expires_at=grant.invite.expires_at,
                token=grant.token,
            )

        @router.delete(
            "/admin/account-invites/{invite_id}", status_code=204,
            tags=["identity-admin"],
        )
        async def revoke_invite(
            invite_id: str, request: Request,
            principal: IdentityPrincipal = Depends(
                self.require_roles(GlobalRole.OWNER, GlobalRole.ADMIN)
            ),
        ) -> Response:
            await self.require_csrf(request, principal)
            await _invoke(
                self.service.revoke_invite,
                actor_user_id=principal.user.id, invite_id=invite_id,
            )
            return Response(status_code=204)

        @router.get(
            "/admin/users", response_model=AdminUserListResponse,
            tags=["identity-admin"],
        )
        async def list_admin_users(
            principal: IdentityPrincipal = Depends(
                self.require_roles(GlobalRole.OWNER, GlobalRole.ADMIN)
            ),
        ) -> AdminUserListResponse:
            records = await _invoke(
                self.service.repository.list_users_for_admin,
                actor_user_id=principal.user.id,
            )
            return AdminUserListResponse(users=[
                await admin_user_response(record) for record in records
            ])

        @router.patch(
            "/admin/users/{user_id}", response_model=AdminUserResponse,
            tags=["identity-admin"],
        )
        async def patch_admin_user(
            user_id: str, body: AdminUserPatch, request: Request,
            principal: IdentityPrincipal = Depends(
                self.require_roles(GlobalRole.OWNER, GlobalRole.ADMIN)
            ),
        ) -> AdminUserResponse:
            await self.require_csrf(request, principal)
            if (
                body.state is None and body.storage_bytes is None
                and body.import_bytes is None
            ):
                raise _error(422, "invalid_input", "at least one change is required")
            record = await _invoke(
                self.service.repository.update_user_for_admin,
                actor_user_id=principal.user.id, target_user_id=user_id,
                state=body.state, storage_bytes=body.storage_bytes,
                import_bytes=body.import_bytes, now=self.service._clock(),
            )
            return await admin_user_response(record)

        @router.get(
            "/admin/audit", response_model=AdminAuditResponse,
            tags=["identity-admin"],
        )
        async def list_admin_audit(
            limit: int = 100,
            principal: IdentityPrincipal = Depends(
                self.require_roles(GlobalRole.OWNER, GlobalRole.ADMIN)
            ),
        ) -> AdminAuditResponse:
            events = await _invoke(
                self.service.repository.list_audit_for_admin,
                actor_user_id=principal.user.id, limit=limit,
            )
            # Deliberately omit metadata: the admin API exposes operational identity,
            # not arbitrary strings that could have been supplied as content.
            return AdminAuditResponse(events=[{
                "id": item.id, "occurred_at": item.occurred_at,
                "actor_user_id": item.actor_user_id,
                "target_user_id": item.target_user_id,
                "action": item.action, "resource_type": item.resource_type,
                "resource_id": item.resource_id, "outcome": item.outcome,
            } for item in events])


def build_identity_http(
    service: IdentityService, **kwargs,
) -> IdentityHttp:
    """Construct the router/dependency bundle for composition in ``create_app``."""
    return IdentityHttp(service, **kwargs)


def _user_agent_hash(request: Request) -> str | None:
    value = request.headers.get("user-agent", "").strip()
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _ip_prefix(request: Request) -> str | None:
    """Record only an approximate direct-peer network, never a client-supplied header."""
    if request.client is None:
        return None
    try:
        address = ipaddress.ip_address(request.client.host)
    except ValueError:
        return None
    bits = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))
