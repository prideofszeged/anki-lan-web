from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ankiweb.identity.http import IdentityHttp, IdentityPrincipal
from ankiweb.identity.repository import (
    AuthorizationError, ConflictError, ExpiredTokenError, InvalidTokenError,
    NotFoundError,
)

from .models import DeckShare, ShareDetail, ShareMembership, ShareRole, ShareState
from .repository import ShareNotFoundError
from .service import SharingService


class ShareCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class ShareResponse(BaseModel):
    id: str
    owner_user_id: str
    name: str
    state: ShareState
    current_release: int | None
    created_at: datetime

    @classmethod
    def of(cls, share: DeckShare) -> "ShareResponse":
        return cls(**share.__dict__) if hasattr(share, "__dict__") else cls(
            id=share.id, owner_user_id=share.owner_user_id, name=share.name,
            state=share.state, current_release=share.current_release,
            created_at=share.created_at,
        )


class MembershipResponse(BaseModel):
    share_id: str
    user_id: str
    role: ShareRole
    joined_at: datetime

    @classmethod
    def of(cls, membership: ShareMembership) -> "MembershipResponse":
        return cls(
            share_id=membership.share_id, user_id=membership.user_id,
            role=membership.role, joined_at=membership.joined_at,
        )


class ShareListResponse(BaseModel):
    shares: list[ShareResponse]


class ShareDetailResponse(BaseModel):
    share: ShareResponse
    membership: MembershipResponse
    members: list[MembershipResponse]

    @classmethod
    def of(cls, detail: ShareDetail) -> "ShareDetailResponse":
        return cls(
            share=ShareResponse.of(detail.share),
            membership=MembershipResponse.of(detail.membership),
            members=[MembershipResponse.of(item) for item in detail.members],
        )


class ShareInviteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ShareRole
    intended_user_id: str | None = None
    expires_in_seconds: int = Field(default=7 * 86400, ge=60, le=30 * 86400)


class ShareInviteResponse(BaseModel):
    id: str
    role: ShareRole
    expires_at: datetime
    token: str
    accept_url: str


class ShareInviteAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr = Field(min_length=32, max_length=512)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ShareNotFoundError):
        return HTTPException(404, {"code": "share_not_found", "message": "share not found"})
    if isinstance(exc, ExpiredTokenError):
        return HTTPException(410, {"code": "invite_expired", "message": str(exc)})
    if isinstance(exc, InvalidTokenError):
        return HTTPException(400, {"code": "invalid_invite", "message": str(exc)})
    if isinstance(exc, NotFoundError):
        return HTTPException(404, {"code": "not_found", "message": str(exc)})
    if isinstance(exc, AuthorizationError):
        return HTTPException(403, {"code": "forbidden", "message": str(exc)})
    if isinstance(exc, ConflictError):
        return HTTPException(409, {"code": "share_conflict", "message": str(exc)})
    if isinstance(exc, ValueError):
        return HTTPException(422, {"code": "invalid_input", "message": str(exc)})
    raise exc


async def _call(function, /, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except (AuthorizationError, ConflictError, ExpiredTokenError, InvalidTokenError,
            NotFoundError, ValueError) as exc:
        raise _http_error(exc) from exc


def build_sharing_router(
    service: SharingService, identity_http: IdentityHttp, *, prefix: str = "/api/v1",
) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["sharing"])

    @router.get("/shares", response_model=ShareListResponse)
    async def list_shares(
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareListResponse:
        shares = await _call(service.list_shares, principal.user.id)
        return ShareListResponse(shares=[ShareResponse.of(item) for item in shares])

    @router.post("/shares", response_model=ShareResponse, status_code=201)
    async def create_share(
        body: ShareCreateRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareResponse:
        await identity_http.require_csrf(request, principal)
        share = await _call(
            service.create_share, actor_user_id=principal.user.id, name=body.name,
        )
        return ShareResponse.of(share)

    @router.get("/shares/{share_id}", response_model=ShareDetailResponse)
    async def get_share(
        share_id: str,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareDetailResponse:
        detail = await _call(
            service.get_share, actor_user_id=principal.user.id, share_id=share_id,
        )
        return ShareDetailResponse.of(detail)

    @router.post(
        "/shares/{share_id}/invitations", response_model=ShareInviteResponse,
        status_code=201,
    )
    async def create_invite(
        share_id: str, body: ShareInviteRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> ShareInviteResponse:
        await identity_http.require_csrf(request, principal)
        grant = await _call(
            service.create_invite, actor_user_id=principal.user.id, share_id=share_id,
            role=body.role, intended_user_id=body.intended_user_id,
            lifetime=timedelta(seconds=body.expires_in_seconds),
        )
        return ShareInviteResponse(
            id=grant.invite.id, role=grant.invite.role,
            expires_at=grant.invite.expires_at, token=grant.token,
            accept_url="/shares/invite#",
        )

    @router.post(
        "/share-invitations/accept", response_model=MembershipResponse,
    )
    async def accept_invite(
        body: ShareInviteAcceptRequest, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> MembershipResponse:
        await identity_http.require_csrf(request, principal)
        membership = await _call(
            service.accept_invite, actor_user_id=principal.user.id,
            token=body.token.get_secret_value(),
        )
        return MembershipResponse.of(membership)

    @router.delete("/shares/{share_id}/invitations/{invite_id}", status_code=204)
    async def revoke_invite(
        share_id: str, invite_id: str, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> Response:
        await identity_http.require_csrf(request, principal)
        await _call(
            service.revoke_invite, actor_user_id=principal.user.id,
            share_id=share_id, invite_id=invite_id,
        )
        return Response(status_code=204)

    @router.delete("/shares/{share_id}/members/{user_id}", status_code=204)
    async def remove_member(
        share_id: str, user_id: str, request: Request,
        principal: IdentityPrincipal = Depends(identity_http.require_principal),
    ) -> Response:
        await identity_http.require_csrf(request, principal)
        await _call(
            service.remove_member, actor_user_id=principal.user.id,
            share_id=share_id, target_user_id=user_id,
        )
        return Response(status_code=204)

    return router

